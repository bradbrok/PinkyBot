#!/usr/bin/env python3
"""
Affiliate URL Validator — redirect-chain + ref-marker validation.

Complements affiliate_link_watchdog.py (which scans local HTML for *missing*
affiliate links). This script validates that the configured affiliate URLs are
still *live and still carry their referral code*.

Why not judge by HTTP status code:
    Exchanges front their signup pages with anti-bot edges (Cloudflare
    challenges, empty 202s, HEAD-blocking 405s). Those status codes say nothing
    about whether the affiliate link works for a real user in a browser, and
    whitelisting them one by one means chasing a new whitelist entry for every
    exchange we add. Observed on 2026-08-03:
        Binance  -> 202 with a zero-byte body (edge anti-bot)
        Coinbase -> 302 -> 403 "Just a moment..." (Cloudflare challenge)
        MEXC     -> 405 on HEAD
    None of those are broken links.

What actually costs revenue is the referral code being dropped: the exchange
retires a code, changes the program, or redirects the link to a bare homepage.
That is always visible in the redirect chain, so that is what we assert:

    at least one URL in the chain (request URL or any hop) must still contain
    one of the exchange's ref_markers, and the chain must not end on a
    known dead end (DNS/connection failure, 404/410, or a bare homepage).

Usage:
    python3 affiliate_url_validator.py                # validate all configured
    python3 affiliate_url_validator.py --exchange Binance
    python3 affiliate_url_validator.py --json         # machine-readable output

Exit codes:
    0  all links valid (anti-bot responses included — they are not failures)
    1  at least one link genuinely broken
    2  configuration or runtime error
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    print("ERROR: requests required. Install with: pip install requests", file=sys.stderr)
    sys.exit(2)


DEFAULT_CONFIG_PATH = Path(__file__).parent / "affiliate_config.json"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "reports"

# Config entries with this ref_url are placeholders, not real affiliate links.
PLACEHOLDER_REF_URL = "MISSING_REF_LINK"

MAX_REDIRECTS = 10
TIMEOUT = 25

# A real browser UA. Not evasion — without it several exchanges serve a
# different edge path than a real user would ever hit, which is exactly the
# false-positive class this script exists to eliminate.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Body fingerprints of interstitial anti-bot challenges. Presence means "we were
# challenged", not "the link is broken".
ANTIBOT_FINGERPRINTS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
    "_cf_chl",
    "enable javascript and cookies to continue",
    "captcha-delivery",
    "px-captcha",
    "are you a human",
)

# Statuses that unambiguously mean the target resource is gone.
DEAD_STATUSES = frozenset({404, 410})


# --- Results ---

STATUS_OK = "OK"                      # our ref code is verifiably in the destination
STATUS_RESHAPED = "OK_RESHAPED"       # our code survived, in a different parameter shape
STATUS_UNPINNED = "UNPINNED"          # a referral param is present but not traceable to us
STATUS_BROKEN = "BROKEN"              # genuine failure — revenue impacting
STATUS_SKIPPED = "SKIPPED"            # no affiliate code configured yet

# Query parameters exchanges use to carry a referral. Presence of one with a
# non-empty value means we are still inside a referral flow, even when the code
# has been rewritten into a shape our config markers don't literally match.
REFERRAL_PARAMS = (
    "ref", "refcode", "ref_code", "referral", "referralcode", "referral_code",
    "invitecode", "invite_code", "invite", "rcode", "affiliate_id", "affiliateid",
    "shortlink", "clackcode", "claccode", "channelid", "pid", "utm_biz",
)


@dataclass
class ValidationResult:
    exchange: str
    ref_url: str
    status: str
    reason: str
    http_status: int | None = None
    final_url: str | None = None
    matched_marker: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    antibot_detected: bool = False

    referral_params: dict[str, str] = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.status == STATUS_BROKEN


# --- Core logic ---


def _is_bare_landing(url: str, ref_markers: list[str]) -> bool:
    """
    True if the URL looks like a bare homepage / generic landing with no path
    depth — i.e. the exchange dropped us out of the referral flow entirely.

    Only used when no ref_marker matched anywhere in the chain, so a legitimate
    short referral path (e.g. /r/CODE) is never judged by this.
    """
    path = urlparse(url).path.strip("/")
    if not path:
        return True
    # A single generic segment with no ref marker is also a dead end
    # (e.g. redirected to /join or /register with the code stripped).
    return "/" not in path and not any(m.lower() in url.lower() for m in ref_markers)


def _extract_referral_params(url: str) -> dict[str, str]:
    """Referral-carrying query parameters (with non-empty values) on a URL."""
    query = parse_qs(urlparse(url).query, keep_blank_values=False)
    found = {}
    for key, values in query.items():
        if key.lower() in REFERRAL_PARAMS and values and values[0].strip():
            found[key] = values[0]
    return found


def _identity_tokens(ref_url: str) -> set[str]:
    """
    The distinctive code tokens of our own affiliate URL — the path segments and
    query values that identify *our* account (e.g. '1i12t70k', '156434').

    Used to recognise our code after an exchange reshapes it from a path segment
    into a query parameter, which is a rewrite, not a broken link.
    """
    parsed = urlparse(ref_url)
    tokens = set(seg for seg in parsed.path.split("/") if seg)
    for values in parse_qs(parsed.query, keep_blank_values=False).values():
        tokens.update(values)
    # Drop generic path words that carry no account identity.
    generic = {
        "r", "af", "b", "join", "invite", "register", "signup", "sign-up",
        "referral", "referral-entry", "activity", "en", "cpa", "share", "gl",
    }
    return {t for t in tokens if len(t) >= 4 and t.lower() not in generic}


def _detect_antibot(response: requests.Response) -> bool:
    """Detect an interstitial challenge page rather than the real destination."""
    # An empty body on a 2xx is an edge anti-bot response, not real content.
    if 200 <= response.status_code < 300 and len(response.content) == 0:
        return True

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type:
        return False

    # Challenge pages are small; avoid scanning a full article page.
    snippet = response.text[:20000].lower()
    return any(fp in snippet for fp in ANTIBOT_FINGERPRINTS)


def validate_url(exchange: str, ref_url: str, ref_markers: list[str],
                 session: requests.Session) -> ValidationResult:
    """
    Follow the redirect chain for a single affiliate URL and assert the
    referral code survives it.
    """
    chain: list[str] = [ref_url]

    try:
        response = session.get(
            ref_url,
            allow_redirects=True,
            timeout=TIMEOUT,
            headers={"User-Agent": BROWSER_UA},
        )
    except requests.TooManyRedirects:
        return ValidationResult(
            exchange=exchange, ref_url=ref_url, status=STATUS_BROKEN,
            reason=f"Redirect loop (>{MAX_REDIRECTS} hops)", redirect_chain=chain,
        )
    except requests.RequestException as exc:
        # DNS failure, TLS failure, connection refused, timeout — the link is
        # unreachable, which is a genuine failure regardless of anti-bot rules.
        return ValidationResult(
            exchange=exchange, ref_url=ref_url, status=STATUS_BROKEN,
            reason=f"Unreachable: {type(exc).__name__}: {exc}", redirect_chain=chain,
        )

    chain = [r.url for r in response.history] + [response.url]
    antibot = _detect_antibot(response)

    # Check "resource gone" BEFORE the marker match. The request URL is derived
    # from the same config as the markers, so it always matches them — a 404 on
    # the affiliate URL itself would otherwise be scored as a pass.
    if response.status_code in DEAD_STATUSES:
        return ValidationResult(
            exchange=exchange, ref_url=ref_url, status=STATUS_BROKEN,
            reason=f"Destination gone (HTTP {response.status_code})",
            http_status=response.status_code, final_url=response.url,
            redirect_chain=chain, antibot_detected=antibot,
        )

    # For the same reason, only hops *after* the request prove the code was
    # honoured. Checking every later hop (not just the final URL) matters:
    # an exchange may rewrite /r/CODE into ?rcode=CODE mid-chain — the code
    # survived, in a different shape. With no redirects at all, the requested
    # URL is itself the live destination, so it is the thing to check.
    verifiable = chain[1:] if len(chain) > 1 else chain

    matched = next(
        (m for m in ref_markers
         for url in verifiable
         if m.lower() in url.lower()),
        None,
    )

    antibot_note = (f"; edge served an anti-bot response (HTTP {response.status_code})"
                    if antibot else "")
    referral_params = _extract_referral_params(response.url)

    def result(status: str, reason: str, marker: str | None = None) -> ValidationResult:
        return ValidationResult(
            exchange=exchange, ref_url=ref_url, status=status,
            reason=reason + antibot_note,
            http_status=response.status_code, final_url=response.url,
            matched_marker=marker, redirect_chain=chain,
            antibot_detected=antibot, referral_params=referral_params,
        )

    # Tier 1 — a configured marker survived the chain verbatim.
    if matched:
        return result(STATUS_OK, f"Referral code intact (matched {matched!r})", matched)

    # Tier 2 — our own account token survived, reshaped into a query parameter.
    # Exchanges routinely rewrite /r/<code> into ?rcode=<code> or ?shortlink=<code>.
    our_tokens = _identity_tokens(ref_url)
    reshaped = next(
        (tok for tok in our_tokens
         for val in referral_params.values()
         if tok.lower() == val.lower() or tok.lower() in val.lower()),
        None,
    )
    if reshaped:
        return result(
            STATUS_RESHAPED,
            f"Referral code intact but reshaped by the exchange "
            f"(our token {reshaped!r} found in {referral_params})",
            reshaped,
        )

    # Tier 3 — still inside a referral flow, but the code is not traceable to
    # our account. Usually a vanity short-link mapping to an internal invite id.
    # Not a failure, but it cannot be verified automatically: a human must
    # confirm once, then pin it via "resolved_markers" in the config.
    if referral_params:
        return result(
            STATUS_UNPINNED,
            f"Referral flow reached but code not traceable to our account "
            f"({referral_params}) — confirm once and pin via 'resolved_markers'",
        )

    # Tier 4 — genuine failure: no referral signal survived at all.
    if _is_bare_landing(response.url, ref_markers):
        reason = f"Redirected to a bare landing page with no referral code: {response.url}"
    else:
        reason = f"No referral code or referral parameter survived the chain (final: {response.url})"
    return result(STATUS_BROKEN, reason)


def validate_all(config: dict, only: str | None = None) -> list[ValidationResult]:
    results: list[ValidationResult] = []

    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS

    for name, data in config.get("exchanges", {}).items():
        if only and name.lower() != only.lower():
            continue

        ref_url = data.get("ref_url", "")
        # ref_markers describe our link as written in the site HTML;
        # resolved_markers (optional) pin what the exchange redirects it to,
        # once a human has confirmed the destination is our account.
        markers = list(data.get("ref_markers", [])) + list(data.get("resolved_markers", []))

        if not ref_url or ref_url == PLACEHOLDER_REF_URL:
            results.append(ValidationResult(
                exchange=name, ref_url=ref_url or "", status=STATUS_SKIPPED,
                reason="No affiliate code configured yet",
            ))
            continue

        if not markers:
            results.append(ValidationResult(
                exchange=name, ref_url=ref_url, status=STATUS_SKIPPED,
                reason="No ref_markers configured — cannot validate",
            ))
            continue

        results.append(validate_url(name, ref_url, markers, session))

    return results


def render_text(results: list[ValidationResult]) -> str:
    symbols = {
        STATUS_OK: "OK      ",
        STATUS_RESHAPED: "OK/RESHP",
        STATUS_UNPINNED: "UNPINNED",
        STATUS_BROKEN: "BROKEN  ",
        STATUS_SKIPPED: "SKIP    ",
    }
    lines = [
        "=" * 72,
        "AFFILIATE URL VALIDATOR — redirect chain + referral marker",
        f"Run: {datetime.now(timezone.utc).isoformat()}",
        "=" * 72,
        "",
    ]

    for r in results:
        lines.append(f"[{symbols.get(r.status, r.status)}] {r.exchange}")
        lines.append(f"           {r.reason}")
        if r.status == STATUS_BROKEN and len(r.redirect_chain) > 1:
            lines.append(f"           chain: {' -> '.join(r.redirect_chain)}")

    broken = [r for r in results if r.is_failure]
    antibot = [r for r in results if r.antibot_detected]
    unpinned = [r for r in results if r.status == STATUS_UNPINNED]
    skipped = [r for r in results if r.status == STATUS_SKIPPED]
    valid = [r for r in results if r.status in (STATUS_OK, STATUS_RESHAPED, STATUS_UNPINNED)]

    lines.extend([
        "",
        "-" * 72,
        f"valid: {len(valid)}  (anti-bot challenged: {len(antibot)}, "
        f"awaiting pin: {len(unpinned)})",
        f"broken: {len(broken)}   skipped (no code configured): {len(skipped)}",
    ])

    if unpinned:
        lines.append("")
        lines.append("CONFIRM ONCE — referral reached but code not traceable to our account:")
        for r in unpinned:
            lines.append(f"  - {r.exchange}: resolved to {r.referral_params}")

    if broken:
        lines.append("")
        lines.append("ACTION REQUIRED — genuinely broken affiliate links:")
        for r in broken:
            lines.append(f"  - {r.exchange}: {r.reason}")

    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate affiliate URLs by redirect chain and referral markers"
    )
    parser.add_argument("--config", "-c", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"Config JSON (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--exchange", "-e", default=None,
                        help="Validate a single exchange by name")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit JSON instead of the text report")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Write a JSON report into this directory")

    args = parser.parse_args()

    try:
        with open(args.config, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        print(f"ERROR: config not found: {args.config}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid config JSON: {exc}", file=sys.stderr)
        return 2

    results = validate_all(config, only=args.exchange)

    if not results:
        print(f"ERROR: no exchange matched {args.exchange!r}", file=sys.stderr)
        return 2

    report = {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "validator": "redirect_chain+ref_markers",
        "summary": {
            "total": len(results),
            "valid": len([r for r in results
                          if r.status in (STATUS_OK, STATUS_RESHAPED, STATUS_UNPINNED)]),
            "antibot_challenged": len([r for r in results if r.antibot_detected]),
            "awaiting_pin": len([r for r in results if r.status == STATUS_UNPINNED]),
            "broken": len([r for r in results if r.is_failure]),
            "skipped": len([r for r in results if r.status == STATUS_SKIPPED]),
        },
        "results": [asdict(r) for r in results],
    }

    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(results))

    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = args.output / f"affiliate_url_validation_{date_str}.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"\nJSON report written to: {out_path}", file=sys.stderr)

    return 1 if any(r.is_failure for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
