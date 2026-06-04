"""Resolve + verify the deploy target for the self-update path.

``admin_update`` (api.py) historically did ``git pull origin main`` and
deployed whatever was at the branch HEAD, using the resolved release tag
only for the status line — a drift from the documented intent that
"production hosts pull from the latest tag, not main HEAD"
(``.github/workflows/release.yml``). This module makes the deploy target
explicit and verified:

- **default** (``target=""``): the latest published GitHub Release tag.
- **a release tag** (e.g. ``"26.06.109"``): that release.
- **a commit SHA**: an explicit operator-pinned deploy.

A release-tag target is verified against the GitHub Releases API over
TLS before checkout: the tag must correspond to a *published*
(non-draft, non-prerelease) Release, and its commit must match the local
tag. ``api.github.com`` over TLS is the trust anchor — the repo is
public, so no token is needed. On any verification failure the caller
refuses and stays on the current version; ``force=True`` is the operator
override (e.g. when the GitHub API is unreachable).

A commit-SHA target carries no Release to verify against, so it is
treated as an explicit operator pin: the target must be a *full* commit
object id (40-hex sha1 / 64-hex sha256), which the daemon resolves to
its canonical commit and checks out by that resolved id. Ref names like
``main``, ``HEAD`` or ``origin/main`` are refused — accepting them would
silently resurrect the very branch-HEAD deploy this module replaces. The
``/admin/update`` call is itself HMAC-authenticated, so naming a commit
is an authenticated operator decision — logged, not blocked.

Trust boundary: ``git fetch`` runs over the daemon's configured
``origin`` remote (HTTPS → TLS-authenticated to github.com). An attacker
who could repoint ``origin`` or write the local repo already owns the
host — outside this module's threat model.
"""

from __future__ import annotations

import json
import re
import subprocess as sp
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

#: A release tag is CalVer ``YY.MM.NNN`` (see release.yml validate step).
_RELEASE_TAG_RE = re.compile(r"^[0-9]{2}\.[0-9]{2}\.[0-9]{2,3}$")

#: ``owner/repo`` slug — GitHub's own allowed character set for both parts.
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: A git object name as returned by GitHub — hex, abbreviated (7) to full.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

#: An operator commit pin must be a *full* object id (sha1 40 / sha256 64).
#: Anything shorter or non-hex (``main``, ``HEAD``, ``origin/main`` …) is
#: refused so a pin can never silently resurrect a branch-HEAD deploy.
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

# (status_code, parsed_json | None). Injectable so tests don't hit the network.
HttpGet = Callable[[str], "tuple[int, dict | None]"]


@dataclass
class DeployDecision:
    """Outcome of resolving + verifying a requested deploy target.

    ``error`` non-empty means: do NOT deploy. Otherwise check out ``ref``.
    """

    ref: str = ""  # tag name or commit SHA to check out
    kind: str = ""  # "release" | "commit"
    verified: bool = False  # True iff release-verified against GitHub
    error: str = ""  # non-empty → abort the deploy


# -- git helpers (daemon-local) ----------------------------------------------


def _git(repo_dir: str, *args: str, timeout: int = 15) -> str | None:
    """Run a git command, returning stripped stdout or None on failure."""
    try:
        out = sp.check_output(
            ["git", *args], cwd=repo_dir, stderr=sp.DEVNULL, timeout=timeout
        )
        return out.decode().strip()
    except Exception:
        return None


def latest_release_tag(repo_dir: str) -> str | None:
    """The newest CalVer release tag known locally (after a fetch)."""
    out = _git(repo_dir, "tag", "--sort=-version:refname", "-l", "[0-9][0-9].*")
    if not out:
        return None
    for line in out.splitlines():
        tag = line.strip()
        if _RELEASE_TAG_RE.match(tag):
            return tag
    return None


def tag_exists(repo_dir: str, tag: str) -> bool:
    return _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}") is not None


def commit_exists(repo_dir: str, ref: str) -> bool:
    """True if ``ref`` resolves to a commit object present locally."""
    try:
        sp.check_output(
            ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
            cwd=repo_dir, stderr=sp.DEVNULL, timeout=15,
        )
        return True
    except Exception:
        return False


def local_tag_commit(repo_dir: str, tag: str) -> str | None:
    """Commit SHA the (possibly annotated) local tag dereferences to."""
    return _git(repo_dir, "rev-parse", f"{tag}^{{commit}}")


def origin_owner_repo(repo_dir: str) -> str | None:
    """Parse ``owner/repo`` from the configured origin remote URL."""
    url = _git(repo_dir, "config", "--get", "remote.origin.url")
    if not url:
        return None
    # https://github.com/owner/repo(.git)  or  git@github.com:owner/repo(.git)
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


# -- GitHub API (TLS trust anchor) -------------------------------------------


def _http_get_json(url: str, timeout: int = 15) -> tuple[int, dict | None]:
    """GET ``url`` and parse JSON. Returns (status, data|None).

    A network/transport failure surfaces as status 0 so the caller treats
    it as "could not verify" (→ refuse unless forced).
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pinky-self-update",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def github_tag_commit(owner_repo: str, tag: str, *, http_get: HttpGet) -> str | None:
    """The commit SHA GitHub records for ``tag`` (deref annotated tags)."""
    # Validate every value that becomes part of the request URL before it is
    # interpolated: the domain is fixed (api.github.com) but the path parts
    # must not be able to redirect the request (CWE-918, partial SSRF).
    if not _OWNER_REPO_RE.match(owner_repo) or not _RELEASE_TAG_RE.match(tag):
        return None
    status, ref = http_get(f"https://api.github.com/repos/{owner_repo}/git/ref/tags/{tag}")
    if status != 200 or not ref:
        return None
    obj = ref.get("object") or {}
    if obj.get("type") == "commit":
        return obj.get("sha")
    if obj.get("type") == "tag" and obj.get("sha"):
        sha = obj["sha"]
        if not _SHA_RE.match(sha):  # path part comes from the API response
            return None
        status2, tagobj = http_get(
            f"https://api.github.com/repos/{owner_repo}/git/tags/{sha}"
        )
        if status2 == 200 and tagobj:
            return (tagobj.get("object") or {}).get("sha")
    return None


def verify_published_release(
    repo_dir: str, owner_repo: str, tag: str, *, http_get: HttpGet
) -> tuple[bool, str]:
    """Confirm ``tag`` is a published Release and binds to the local commit.

    Returns (ok, reason). On failure ``reason`` is a short human string.
    """
    # Fail closed before any network call if either value that lands in the
    # request URL is outside its strict allowlist (CWE-918, partial SSRF).
    if not _OWNER_REPO_RE.match(owner_repo):
        return False, "invalid origin owner/repo"
    if not _RELEASE_TAG_RE.match(tag):
        return False, "deploy tag is not a valid release tag (expected CalVer YY.MM.NNN)"
    status, rel = http_get(f"https://api.github.com/repos/{owner_repo}/releases/tags/{tag}")
    if status == 404:
        return False, "no published GitHub Release for this tag"
    if status == 0:
        return False, "GitHub API unreachable"
    if status != 200 or not rel:
        return False, f"GitHub API returned status {status}"
    if rel.get("draft"):
        return False, "release is a draft"
    if rel.get("prerelease"):
        return False, "release is a pre-release"
    # Bind the released tag to the commit we'd actually check out.
    remote_sha = github_tag_commit(owner_repo, tag, http_get=http_get)
    local_sha = local_tag_commit(repo_dir, tag)
    if not remote_sha:
        return False, "could not resolve the release commit from GitHub"
    if not local_sha:
        return False, "local tag does not resolve to a commit"
    if remote_sha != local_sha:
        return False, f"local tag commit {local_sha[:10]} != GitHub {remote_sha[:10]}"
    return True, ""


# -- orchestration -----------------------------------------------------------


def resolve_and_verify(
    repo_dir: str,
    target: str,
    *,
    force: bool = False,
    http_get: HttpGet = _http_get_json,
    log: Callable[[str], None] | None = None,
) -> DeployDecision:
    """Resolve ``target`` to a checkout ref and verify it.

    - ``target=""`` → latest published release tag (verified).
    - a known tag → that release (verified, unless ``force``).
    - otherwise a commit SHA → operator pin (existence-checked only).
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    target = (target or "").strip()

    if not target:
        tag = latest_release_tag(repo_dir)
        if not tag:
            return DeployDecision(error="no release tag found to deploy (pass target=<sha> to pin a commit)")
        target = tag

    if tag_exists(repo_dir, target):
        if force:
            _log(f"admin: force=True — skipping release verification for tag {target}")
            return DeployDecision(ref=target, kind="release", verified=False)
        owner_repo = origin_owner_repo(repo_dir)
        if not owner_repo:
            return DeployDecision(error="could not determine origin owner/repo for verification")
        ok, reason = verify_published_release(repo_dir, owner_repo, target, http_get=http_get)
        if not ok:
            return DeployDecision(
                kind="release",
                error=f"release {target!r} failed verification: {reason} "
                "(staying on current version; pass force=true to override)",
            )
        _log(f"admin: verified published release {target}")
        return DeployDecision(ref=target, kind="release", verified=True)

    # Not a tag → an explicit operator-pinned commit. Require a *full* object
    # id and pin to the canonical commit it names. Refs such as ``main``,
    # ``HEAD``, ``FETCH_HEAD`` or ``origin/main`` are rejected here: accepting
    # them would silently resurrect the branch-HEAD deploy this module exists
    # to prevent, with no release verification and no ``force``.
    if _FULL_SHA_RE.match(target):
        canonical = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}")
        # The resolved commit must be *exactly* the object the id names.
        # Equality (not startswith) guards two edge cases: a ref whose name
        # happens to be 40 hex chars (git resolves it to *its* target), and a
        # 40-hex prefix of a 64-hex commit in a sha256 repo (a prefix is not a
        # full object id). Both fail equality and are refused.
        if canonical and canonical.lower() == target.lower():
            _log(f"admin: operator-pinned commit deploy {canonical} (authenticated admin call)")
            return DeployDecision(ref=canonical, kind="commit", verified=False)
        return DeployDecision(
            error=f"commit {target!r} not found in the repository after fetch"
        )

    return DeployDecision(
        error=f"target {target!r} is neither a known release tag nor a full commit id "
        "(refs like 'main' or 'HEAD' are not accepted — pass a release tag or a 40-hex commit SHA)"
    )
