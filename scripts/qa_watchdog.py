#!/usr/bin/env python3
"""
QA Watchdog for bitcoinmarket.net
Scans HTML pages for SEO issues, broken links, and content quality problems.

Usage:
    python qa_watchdog.py [--config CONFIG_PATH] [--output OUTPUT_DIR] [--limit N]

Requirements:
    pip install beautifulsoup4 aiohttp
"""

import argparse
import asyncio
import fnmatch
import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 required. Install with: pip install beautifulsoup4")
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    print("WARNING: aiohttp not installed. Link checking disabled.")
    aiohttp = None


# --- Configuration ---

DEFAULT_CONFIG_PATH = Path(__file__).parent / "qa_config.json"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "reports"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# --- Data Classes ---

@dataclass
class Issue:
    """Represents a single QA issue."""
    severity: str  # "critical", "warning", "info"
    category: str  # "seo", "schema", "content", "link"
    message: str
    details: Optional[str] = None
    line_number: Optional[int] = None


@dataclass
class PageReport:
    """QA report for a single HTML page."""
    file_path: str
    relative_path: str
    page_type: str
    url: str
    issues: list[Issue] = field(default_factory=list)
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    h1_count: int = 0
    h1_texts: list[str] = field(default_factory=list)
    has_canonical: bool = False
    has_og_tags: bool = False
    has_schema: bool = False
    schema_types: list[str] = field(default_factory=list)
    images_without_alt: int = 0
    broken_links: list[str] = field(default_factory=list)
    scan_time: str = ""


# --- Core Logic ---

class QAWatchdog:
    """Scans HTML files for SEO and content quality issues."""

    def __init__(self, config_path: Path):
        self.config = self._load_config(config_path)
        self.site_root = Path(self.config["site_root"])
        self.output_dir = Path(self.config.get("output_dir", DEFAULT_OUTPUT_DIR))
        self.seo_limits = self.config.get("seo_limits", {})
        self.page_types = self.config.get("page_types", {})
        self.link_config = self.config.get("link_check", {})
        self.exclude_dirs = set(self.config.get("exclude_dirs", []))
        self.exclude_files = self.config.get("exclude_files", [])

    def _load_config(self, config_path: Path) -> dict:
        """Load configuration from JSON file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def find_html_files(self, limit: Optional[int] = None) -> list[Path]:
        """Recursively find all HTML files in site root."""
        if not self.site_root.exists():
            logger.error(f"Site root does not exist: {self.site_root}")
            return []

        html_files = []
        extensions = self.config.get("html_extensions", [".html", ".htm"])

        for ext in extensions:
            for path in self.site_root.rglob(f"*{ext}"):
                # Skip excluded directories
                if any(excluded in path.parts for excluded in self.exclude_dirs):
                    continue
                # Skip excluded file patterns
                if any(fnmatch.fnmatch(path.name, pattern) for pattern in self.exclude_files):
                    continue
                html_files.append(path)

        html_files = sorted(html_files)
        if limit:
            html_files = html_files[:limit]

        logger.info(f"Found {len(html_files)} HTML files to scan")
        return html_files

    def _detect_page_type(self, relative_path: str) -> str:
        """Detect page type based on path patterns."""
        for page_type, config in self.page_types.items():
            patterns = config.get("path_patterns", [])
            for pattern in patterns:
                if pattern in relative_path:
                    return page_type
        return "generic"

    def _get_page_type_config(self, page_type: str) -> dict:
        """Get configuration for a specific page type."""
        return self.page_types.get(page_type, {})

    def _check_meta_title(self, soup: BeautifulSoup, report: PageReport):
        """Check meta title for issues."""
        title_tag = soup.find("title")
        max_chars = self.seo_limits.get("title_max_chars", 60)

        if not title_tag or not title_tag.string:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message="Meta title missing",
                details="Page has no <title> tag or it's empty"
            ))
            return

        title_text = title_tag.string.strip()
        report.meta_title = title_text

        if len(title_text) > max_chars:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message=f"Meta title too long ({len(title_text)} chars)",
                details=f"Max {max_chars} chars. Current: '{title_text[:60]}...'"
            ))

        if len(title_text) < 10:
            report.issues.append(Issue(
                severity="warning",
                category="seo",
                message=f"Meta title too short ({len(title_text)} chars)",
                details=f"Title: '{title_text}'"
            ))

    def _check_meta_description(self, soup: BeautifulSoup, report: PageReport):
        """Check meta description for issues."""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        max_chars = self.seo_limits.get("description_max_chars", 160)
        min_chars = self.seo_limits.get("description_min_chars", 50)

        if not desc_tag or not desc_tag.get("content"):
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message="Meta description missing",
                details="Page has no meta description tag"
            ))
            return

        desc_text = desc_tag.get("content", "").strip()
        report.meta_description = desc_text

        if len(desc_text) > max_chars:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message=f"Meta description too long ({len(desc_text)} chars)",
                details=f"Max {max_chars} chars. Current: '{desc_text[:100]}...'"
            ))

        if len(desc_text) < min_chars:
            report.issues.append(Issue(
                severity="warning",
                category="seo",
                message=f"Meta description too short ({len(desc_text)} chars)",
                details=f"Min {min_chars} chars recommended"
            ))

    def _check_h1(self, soup: BeautifulSoup, report: PageReport):
        """Check H1 tags for issues."""
        h1_tags = soup.find_all("h1")
        report.h1_count = len(h1_tags)
        report.h1_texts = [h1.get_text(strip=True)[:100] for h1 in h1_tags]

        if len(h1_tags) == 0:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message="H1 tag missing",
                details="Page has no H1 heading"
            ))
        elif len(h1_tags) > 1:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message=f"Multiple H1 tags ({len(h1_tags)})",
                details=f"H1s: {report.h1_texts}"
            ))

    def _check_og_tags(self, soup: BeautifulSoup, report: PageReport):
        """Check Open Graph tags."""
        og_title = soup.find("meta", property="og:title")
        og_desc = soup.find("meta", property="og:description")
        og_image = soup.find("meta", property="og:image")

        missing = []
        if not og_title or not og_title.get("content"):
            missing.append("og:title")
        if not og_desc or not og_desc.get("content"):
            missing.append("og:description")
        if not og_image or not og_image.get("content"):
            missing.append("og:image")

        report.has_og_tags = len(missing) == 0

        if missing:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message=f"Missing Open Graph tags: {', '.join(missing)}",
                details="Required for social media sharing"
            ))

    def _check_canonical(self, soup: BeautifulSoup, report: PageReport):
        """Check for canonical tag."""
        canonical = soup.find("link", rel="canonical")
        report.has_canonical = canonical is not None and bool(canonical.get("href"))

        if not report.has_canonical:
            report.issues.append(Issue(
                severity="warning",
                category="seo",
                message="Canonical tag missing",
                details="Add <link rel='canonical' href='...'> for SEO"
            ))

    def _check_schema(self, soup: BeautifulSoup, report: PageReport, page_type_config: dict):
        """Check for JSON-LD schema markup."""
        schema_scripts = soup.find_all("script", type="application/ld+json")
        report.schema_types = []

        for script in schema_scripts:
            try:
                data = json.loads(script.string or "{}")
                if isinstance(data, dict):
                    # Check for @graph container (standard JSON-LD pattern)
                    if "@graph" in data and isinstance(data["@graph"], list):
                        for item in data["@graph"]:
                            if isinstance(item, dict):
                                schema_type = item.get("@type", "")
                                if schema_type:
                                    report.schema_types.append(schema_type)
                    # Regular @type at top level
                    schema_type = data.get("@type", "")
                    if schema_type:
                        report.schema_types.append(schema_type)
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            schema_type = item.get("@type", "")
                            if schema_type:
                                report.schema_types.append(schema_type)
            except json.JSONDecodeError:
                pass

        report.has_schema = len(report.schema_types) > 0

        # Check if schema is required for this page type
        if page_type_config.get("require_schema", False):
            if not report.has_schema:
                report.issues.append(Issue(
                    severity="critical",
                    category="schema",
                    message="JSON-LD schema markup missing",
                    details=f"Required schema types: {page_type_config.get('schema_types', [])}"
                ))
            else:
                # Check if correct schema types are present
                required_types = page_type_config.get("schema_types", [])
                found = any(st in report.schema_types for st in required_types)
                if not found and required_types:
                    report.issues.append(Issue(
                        severity="warning",
                        category="schema",
                        message=f"Schema type mismatch",
                        details=f"Found: {report.schema_types}, Expected one of: {required_types}"
                    ))

    def _check_images_alt(self, soup: BeautifulSoup, report: PageReport):
        """Check images for alt text."""
        images = soup.find_all("img")
        missing_alt = 0

        for img in images:
            alt = img.get("alt")
            src = img.get("src", "")
            # Skip decorative/icon images
            if "icon" in src.lower() or "logo" in src.lower():
                continue
            if alt is None or alt.strip() == "":
                missing_alt += 1

        report.images_without_alt = missing_alt

        if missing_alt > 0:
            report.issues.append(Issue(
                severity="warning",
                category="content",
                message=f"{missing_alt} images missing alt text",
                details="Alt text is important for accessibility and SEO"
            ))

    def _check_last_updated(self, soup: BeautifulSoup, html_content: str,
                            report: PageReport, page_type_config: dict):
        """Check for last updated date in content."""
        if not page_type_config.get("require_last_updated", False):
            return

        # Look for common patterns
        patterns = [
            r"ultimo aggiornamento[:\s]+",
            r"last updated[:\s]+",
            r"aggiornato[:\s]+\d",
            r"updated[:\s]+\d",
            r"aktualisiert[:\s]+",
            r"actualizado[:\s]+",
            r'class="[^"]*last-?update[^"]*"',
            r'class="[^"]*updated[^"]*"',
            r"dateModified",
        ]

        found = any(re.search(p, html_content, re.IGNORECASE) for p in patterns)

        if not found:
            report.issues.append(Issue(
                severity="warning",
                category="content",
                message="No 'last updated' date found",
                details="Add update date for freshness signals"
            ))

    def _check_cta(self, soup: BeautifulSoup, report: PageReport, page_type_config: dict):
        """Check for CTA buttons on pages that require them."""
        if not page_type_config.get("require_cta", False):
            return

        # Look for CTA patterns
        cta_selectors = [
            ("a", {"class": re.compile(r"btn|cta|button", re.I)}),
            ("button", {}),
            ("a", {"data-affiliate": True}),
        ]

        cta_found = False
        for tag, attrs in cta_selectors:
            elements = soup.find_all(tag, attrs)
            if elements:
                cta_found = True
                break

        if not cta_found:
            report.issues.append(Issue(
                severity="warning",
                category="content",
                message="No CTA/button found",
                details="Review pages should have affiliate CTA buttons"
            ))

    def _check_italian_bleed(self, soup: BeautifulSoup, html_content: str, report: PageReport):
        """Check EN/ES/DE pages for Italian-specific content (OAM, 730, Quadro RW, etc.)."""
        config = self.config.get("italian_bleed", {})
        suffixes = config.get("lang_suffixes", ["-en.html", "-es.html", "-de.html"])
        patterns = config.get("forbidden_patterns", [])
        whitelist = config.get("whitelist_pages", [])

        # Check if page is whitelisted (intentional Italian content allowed)
        if any(report.relative_path.endswith(page) for page in whitelist):
            return  # Skip whitelist pages

        if not any(report.relative_path.endswith(s) for s in suffixes):
            return  # Only check non-Italian pages

        # Get page text (strip scripts/styles for clean check)
        for tag in soup(["script", "style"]):
            tag.decompose()
        page_text = soup.get_text(" ", strip=True)

        hits = []
        for pattern in patterns:
            matches = list(re.finditer(pattern, page_text, re.IGNORECASE))
            if not matches:
                continue

            # Special case: OAM (Organismo Agenti e Mediatori) is OK if cited in Italian context
            if pattern == "\\bOAM\\b":
                italy_markers = r"\b(Italy|Italian|Italia|Italien|italiano|italienne|Italie)\b"
                valid_match = False
                for match in matches:
                    # Check ±200 chars around OAM mention for Italy-related words
                    context_start = max(0, match.start() - 200)
                    context_end = min(len(page_text), match.end() + 200)
                    context = page_text[context_start:context_end]
                    if re.search(italy_markers, context, re.IGNORECASE):
                        valid_match = True
                        break
                if valid_match:
                    continue  # Skip OAM if cited in Italian context

            hits.append(pattern)

        if hits:
            report.issues.append(Issue(
                severity="critical",
                category="content",
                message=f"Italian bleed detected ({len(hits)} pattern{'s' if len(hits) > 1 else ''})",
                details=f"Non-Italian page contains IT-specific content: {', '.join(hits[:3])}"
            ))

    def _check_hreflang_xdefault(self, soup: BeautifulSoup, report: PageReport):
        """Verify x-default hreflang points to an EN URL, not a non-English page."""
        hreflang_tags = soup.find_all("link", rel="alternate", hreflang=True)
        if not hreflang_tags:
            return

        xdefault = next((t for t in hreflang_tags if t.get("hreflang") == "x-default"), None)
        if not xdefault:
            return

        href = xdefault.get("href", "")
        # x-default should point to EN URL or root EN page
        is_en = (
            href.endswith("-en.html") or
            "best-exchange-europe" in href or
            "/en/" in href or
            href.rstrip("/").endswith(".net")  # root domain = EN homepage
        )
        # Flag if it points to an IT/ES/DE specific page
        is_non_en = any(href.endswith(s) for s in ["-it.html", "-es.html", "-de.html"])

        if is_non_en:
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message="hreflang x-default points to non-EN page",
                details=f"x-default: {href} — should point to EN equivalent"
            ))

    def _check_canonical_script_injection(self, html_content: str, report: PageReport):
        """Detect inline scripts that modify the canonical tag dynamically."""
        # Check if page is whitelisted (i18n mechanism is intentional)
        whitelist = self.config.get("canonical_whitelist", [])
        if any(report.relative_path.endswith(page) for page in whitelist):
            return  # Skip whitelist pages

        # Pattern: script blocks that set/change canonical href based on ?lang= param
        pattern = re.compile(
            r'<script[^>]*>.*?canonical.*?lang[=\s].*?</script>',
            re.IGNORECASE | re.DOTALL
        )
        if pattern.search(html_content):
            report.issues.append(Issue(
                severity="critical",
                category="seo",
                message="Canonical tag modified by inline script",
                details="Inline JS overrides canonical — Googlebot may ignore the tag. Use static hard-coded canonical instead."
            ))

    def _check_html_lang_en(self, soup: BeautifulSoup, report: PageReport):
        """
        Verify the <html lang="..."> attribute is valid and matches filename suffix if present.

        RULE: Language is determined by filename suffix.
          - "-en.html" suffix  → lang="en" (REQUIRED)
          - "-it.html" suffix  → lang="it" (REQUIRED)
          - "-de.html" suffix  → lang="de" (REQUIRED)
          - "-es.html" suffix  → lang="es" (REQUIRED)
          - NO language suffix → lang can be ANY of the 4 supported languages (en, it, de, es) — ACCEPT ALL

        This is a CRITICAL SEO rule — Googlebot uses lang attribute to determine indexing language.
        Pages without explicit language suffix (e.g., bitcoin-sparplan.html) may legitimately
        exist in any language variant and should not be flagged for false positives.
        """
        path = report.relative_path
        supported_langs = {"en", "it", "de", "es"}

        # Load lang convention from config
        lang_convention = self.config.get("lang_convention", {})
        suffix_to_lang = lang_convention.get("suffix_to_lang", {
            "-en.html": "en",
            "-it.html": "it",
            "-de.html": "de",
            "-es.html": "es"
        })

        # Determine expected lang based on filename suffix
        # If page has a language suffix (e.g., -en.html, -de.html), enforce that language
        # If page has NO language suffix, accept any valid language
        expected_lang = None
        has_lang_suffix = False

        for suffix, lang in suffix_to_lang.items():
            if path.endswith(suffix):
                expected_lang = lang
                has_lang_suffix = True
                break

        html_tag = soup.find('html')
        if not html_tag:
            return

        lang_attr = html_tag.get('lang', '')

        # Check 1: lang attribute is missing
        if not lang_attr:
            report.issues.append(Issue(
                severity="critical",
                category="lang",
                message="Missing lang attribute on <html>",
                details=f"Add a valid lang attribute (one of: {', '.join(sorted(supported_langs))}) to the <html> tag."
            ))
            return

        # Check 2: lang attribute is invalid (not one of the 4 supported languages)
        if lang_attr not in supported_langs:
            report.issues.append(Issue(
                severity="critical",
                category="lang",
                message=f"Invalid lang attribute: lang=\"{lang_attr}\"",
                details=(
                    f"Page has lang=\"{lang_attr}\" but only these are supported: "
                    f"{', '.join(sorted(supported_langs))}. "
                    f"Fix: change <html lang=\"{lang_attr}\"> to a valid lang value."
                )
            ))
            return

        # Check 3: page has language suffix but lang attribute doesn't match
        if has_lang_suffix and lang_attr != expected_lang:
            report.issues.append(Issue(
                severity="critical",
                category="lang",
                message=f"Lang mismatch: lang=\"{lang_attr}\" should be lang=\"{expected_lang}\"",
                details=(
                    f"Page filename {path} implies language \"{expected_lang}\" "
                    f"but <html> has lang=\"{lang_attr}\". "
                    f"Fix: change <html lang=\"{lang_attr}\"> to <html lang=\"{expected_lang}\">."
                )
            ))
            return

        # Check 4: page has NO language suffix — accept any valid language (no error)
        # This is correct behavior for multilingual content pages

    def _check_journalist_roster(self, soup: BeautifulSoup, report: PageReport):
        """For magazine pages, verify author is in the approved journalist roster."""
        if "magazine/" not in report.relative_path:
            return
        if report.relative_path.endswith("index.html"):
            return

        roster = self.config.get("journalist_roster", [])
        if not roster:
            return

        author_meta = soup.find("meta", attrs={"name": "magazine:author"})
        if not author_meta:
            return

        author = author_meta.get("content", "").strip()
        if not author or author in ("BitcoinMarket Editorial", "BitcoinMarket.net"):
            report.issues.append(Issue(
                severity="warning",
                category="content",
                message=f"Generic author '{author}' — use a named journalist from the roster",
                details=f"Approved roster: {', '.join(roster)}"
            ))
        elif author not in roster:
            report.issues.append(Issue(
                severity="critical",
                category="content",
                message=f"Unknown journalist '{author}' — not in approved roster",
                details=f"Approved roster: {', '.join(roster)}"
            ))

    def _check_dynamic_nosnippet(self, soup: BeautifulSoup, report: PageReport):
        """Detect dynamic/live sections that lack data-nosnippet (SERP snippet risk)."""
        patterns = self.config.get("dynamic_nosnippet", {}).get("section_patterns", [])
        if not patterns:
            return

        for pattern in patterns:
            # Find elements with matching id or class
            for tag in soup.find_all(id=re.compile(pattern, re.I)):
                if not tag.get("data-nosnippet"):
                    report.issues.append(Issue(
                        severity="warning",
                        category="seo",
                        message=f"Dynamic section '#{tag.get('id')}' missing data-nosnippet",
                        details="Google may use dynamic/error text as SERP snippet. Add data-nosnippet to this section."
                    ))

    def _detect_text_language(self, text: str) -> Optional[str]:
        """
        Heuristic language detection for short review texts.
        Returns 'en', 'it', 'de', 'es', or None if ambiguous.
        Only checks for 4 BMN-supported languages.
        """
        text_lower = text.lower()

        scores: dict[str, int] = {"en": 0, "it": 0, "de": 0, "es": 0}

        # English-EXCLUSIVE markers — phrases that do NOT appear in Italian/DE/ES financial text.
        # EXCLUDED (false-positive-prone): "trading", "exchange", "fees", "liquidity",
        # "futures", "outstanding" — these appear verbatim in Italian crypto content.
        en_markers = [
            r"\bthe most\b",          # IT: "il più"
            r"\bfor beginners\b",     # IT: "per principianti"
            r"\bfounded in\b",        # IT: "fondato nel"
            r"\bamong the most\b",    # IT: "tra i più"
            r"\bideal for those\b",   # IT: "ideale per chi"
            r"\bwidest catalog\b",    # IT: "catalogo più ampio"
            r"\bsimplest exchange\b", # IT: "exchange più semplice"
            r"\bmost trusted\b",      # IT: "più affidabile"
            r"\bperfect for those\b", # IT: "perfetto per chi"
            r"\bpros:\b",             # IT uses "Pro:" (capital, no space before colon)
            r"\bcons:\b",             # IT uses "Contro:"
            r"\bbank-grade\b",        # IT: "sicurezza bancaria"
            r"\bimpeccable track record\b",
            r"\bdated interface\b",   # IT: "interfaccia datata"
        ]
        # Italian-EXCLUSIVE markers
        it_markers = [
            r"\bper i\b", r"\bil più\b", r"\bla più\b", r"\bdelle\b",
            r"\bdegli\b", r"\bquesto\b", r"\bquesta\b", r"\bideale per\b",
            r"\btra i\b", r"\bagli\b", r"\ble migliori\b", r"\ble più\b",
            r"\bcommissioni\b", r"\bcriptovalute\b", r"\bscambio\b",
            r"\bprincipanti\b", r"\bpiù completo\b", r"\bpiù affidabile\b",
            r"\bper chi\b", r"\bfino al\b", r"\bdatata\b",
        ]
        # German-EXCLUSIVE markers (umlauts are reliable signals)
        de_markers = [
            r"\bdie meisten\b", r"\bfür anfänger\b", r"\bgegründet\b",
            r"\bgebühren\b", r"\bideal für\b", r"\bam meisten\b",
            r"\bbörse\b", r"\bkryptowährungen\b",
            r"\bder einfachste\b", r"\bder breiteste\b",
            r"\bvor- und nachteile\b", r"\bnachteile:\b",
            r"\beinwandfreie\b", r"\bveraltete\b", r"\banleger\b",
        ]
        # Spanish-EXCLUSIVE markers
        es_markers = [
            r"\bel más\b", r"\bla más\b", r"\bpara principiantes\b",
            r"\bfundado\b", r"\bcomisiones\b", r"\bideal para\b",
            r"\bentre los\b", r"\blos más\b", r"\bcriptomonedas\b",
            r"\bintercambio\b", r"\bventajas:\b", r"\bdesventajas:\b",
            r"\bel más sencillo\b", r"\bel más amplio\b",
            r"\bquienes\b", r"\btrayectoria\b",
        ]

        for pattern in en_markers:
            if re.search(pattern, text_lower):
                scores["en"] += 1
        for pattern in it_markers:
            if re.search(pattern, text_lower):
                scores["it"] += 1
        for pattern in de_markers:
            if re.search(pattern, text_lower):
                scores["de"] += 1
        for pattern in es_markers:
            if re.search(pattern, text_lower):
                scores["es"] += 1

        # Require at least 3 markers to be confident (raised from 2 to reduce false positives
        # from financial texts that share terminology across languages)
        best_lang = max(scores, key=lambda k: scores[k])
        best_score = scores[best_lang]

        if best_score < 3:
            return None  # Not enough signal

        # Ensure clear winner (no tie with second language)
        second_score = sorted(scores.values(), reverse=True)[1]
        if best_score == second_score:
            return None  # Ambiguous

        return best_lang

    def _check_jsonld_lang_mismatch(self, soup: BeautifulSoup, report: PageReport):
        """
        Prevention #2: Detect JSON-LD reviewBody language mismatch.

        Compares the HTML <html lang="..."> attribute against the detected language
        of reviewBody fields inside <script type="application/ld+json"> blocks.

        Flags CRITICAL if JSON-LD text is in a different language than the page lang.
        This catches cases where static JSON-LD was copied without being translated
        (e.g., Italian reviewBody on an English page).
        """
        html_tag = soup.find("html")
        if not html_tag:
            return

        page_lang = html_tag.get("lang", "").strip().lower()
        if not page_lang or page_lang not in {"en", "it", "de", "es"}:
            return  # Can't compare if lang is missing or unknown

        schema_scripts = soup.find_all("script", type="application/ld+json")
        if not schema_scripts:
            return

        mismatches: list[str] = []

        for script in schema_scripts:
            try:
                raw = script.string or ""
                if not raw.strip():
                    continue
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Flatten to list of objects
            items: list[dict] = []
            if isinstance(data, dict):
                if "@graph" in data:
                    items.extend(i for i in data["@graph"] if isinstance(i, dict))
                else:
                    items.append(data)
            elif isinstance(data, list):
                items.extend(i for i in data if isinstance(i, dict))

            for item in items:
                review_body = item.get("reviewBody", "")
                if not review_body or not isinstance(review_body, str):
                    continue

                detected_lang = self._detect_text_language(review_body)
                if detected_lang is None:
                    continue  # Not enough signal to judge

                if detected_lang != page_lang:
                    name = item.get("name", item.get("itemReviewed", {}).get("name", "unknown"))
                    if isinstance(name, dict):
                        name = name.get("name", "unknown")
                    mismatches.append(
                        f"'{name}': reviewBody detected as '{detected_lang}', page lang='{page_lang}'"
                    )

        if mismatches:
            report.issues.append(Issue(
                severity="critical",
                category="schema",
                message=f"JSON-LD reviewBody language mismatch ({len(mismatches)} block{'s' if len(mismatches) > 1 else ''})",
                details="; ".join(mismatches[:3]) + (" ..." if len(mismatches) > 3 else "")
            ))

    def check_sitemap_lang_params(self) -> list[Issue]:
        """Site-level: detect ?lang= URLs in sitemap.xml."""
        sitemap_path = Path(self.config.get("sitemap_path", ""))
        issues = []
        if not sitemap_path.exists():
            return issues

        content = sitemap_path.read_text(encoding="utf-8", errors="ignore")
        matches = re.findall(r'<loc>(https?://[^<]*\?lang=[^<]+)</loc>', content)
        if matches:
            issues.append(Issue(
                severity="critical",
                category="seo",
                message=f"{len(matches)} ?lang= URL(s) found in sitemap.xml",
                details=f"Examples: {', '.join(matches[:3])}. Remove these — ?lang= pages should not be indexed."
            ))
        return issues

    def check_internal_link_orphans(self) -> list[Issue]:
        """Site-level: detect magazine/guide pages with 0 inbound internal links."""
        html_files = self.find_html_files()
        site_url = "https://bitcoinmarket.net/"
        issues = []

        # Build inbound link map
        inbound: dict[str, int] = {}
        all_relative: set[str] = set()

        for fp in html_files:
            rel = str(fp.relative_to(self.site_root))
            all_relative.add(rel)

            try:
                content = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Find all internal hrefs
            for href in re.findall(r'href="(/[^"#?]+\.html)"', content):
                # Normalize
                key = href.lstrip("/")
                inbound[key] = inbound.get(key, 0) + 1

        # Check magazine + guide pages
        for rel in sorted(all_relative):
            if not (rel.startswith("magazine/") or rel.startswith("guide/")):
                continue
            if rel.endswith("index.html"):
                continue

            count = inbound.get(rel, 0)
            if count == 0:
                issues.append(Issue(
                    severity="warning",
                    category="seo",
                    message=f"Orphan page: {rel} (0 inbound internal links)",
                    details="Add at least one contextual link from a related guide or review."
                ))

        return issues

    async def _check_external_links(self, soup: BeautifulSoup, report: PageReport):
        """Check external links for broken URLs."""
        if not self.link_config.get("enabled", True) or aiohttp is None:
            return

        links = soup.find_all("a", href=True)
        external_links = []
        skip_protocols = self.link_config.get("skip_protocols", [])
        skip_domains = self.link_config.get("skip_domains", [])
        timeout = self.link_config.get("timeout_seconds", 5)

        for link in links:
            href = link.get("href", "")
            # Skip non-http links
            if any(href.startswith(proto) for proto in skip_protocols):
                continue
            # Only check external links
            if href.startswith("http"):
                parsed = urlparse(href)
                if parsed.netloc and parsed.netloc not in skip_domains:
                    external_links.append(href)

        # Deduplicate
        external_links = list(set(external_links))

        # Check links concurrently
        max_concurrent = self.link_config.get("max_concurrent", 10)
        semaphore = asyncio.Semaphore(max_concurrent)

        async def check_link(url: str) -> Optional[str]:
            async with semaphore:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.head(
                            url,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                            allow_redirects=True
                        ) as response:
                            if response.status >= 400:
                                return f"{url} (HTTP {response.status})"
                except asyncio.TimeoutError:
                    return f"{url} (timeout)"
                except Exception as e:
                    return f"{url} (error: {str(e)[:50]})"
            return None

        # Limit to first 20 external links to avoid slowdown
        links_to_check = external_links[:20]
        if links_to_check:
            results = await asyncio.gather(*[check_link(url) for url in links_to_check])
            broken = [r for r in results if r is not None]
            report.broken_links = broken

            if broken:
                report.issues.append(Issue(
                    severity="critical",
                    category="link",
                    message=f"{len(broken)} broken external links",
                    details="; ".join(broken[:5])
                ))

    def scan_file(self, file_path: Path) -> PageReport:
        """Scan a single HTML file for QA issues."""
        relative_path = str(file_path.relative_to(self.site_root))
        page_type = self._detect_page_type(relative_path)
        page_type_config = self._get_page_type_config(page_type)

        # Construct URL
        url = f"https://bitcoinmarket.net/{relative_path}"

        report = PageReport(
            file_path=str(file_path),
            relative_path=relative_path,
            page_type=page_type,
            url=url,
            scan_time=datetime.now().isoformat()
        )

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                html_content = f.read()
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            report.issues.append(Issue(
                severity="critical",
                category="content",
                message=f"Could not read file: {e}"
            ))
            return report

        soup = BeautifulSoup(html_content, "html.parser")

        # Run all checks
        self._check_meta_title(soup, report)
        self._check_meta_description(soup, report)
        self._check_h1(soup, report)
        self._check_og_tags(soup, report)
        self._check_canonical(soup, report)
        self._check_schema(soup, report, page_type_config)
        self._check_images_alt(soup, report)
        self._check_last_updated(soup, html_content, report, page_type_config)
        self._check_cta(soup, report, page_type_config)
        # New checks
        self._check_italian_bleed(soup, html_content, report)
        self._check_hreflang_xdefault(soup, report)
        self._check_canonical_script_injection(html_content, report)
        self._check_journalist_roster(soup, report)
        self._check_dynamic_nosnippet(soup, report)
        self._check_html_lang_en(soup, report)
        self._check_jsonld_lang_mismatch(soup, report)

        return report

    async def scan_file_with_links(self, file_path: Path) -> PageReport:
        """Scan a file including async link checking."""
        report = self.scan_file(file_path)

        # Add link checking
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                html_content = f.read()
            soup = BeautifulSoup(html_content, "html.parser")
            await self._check_external_links(soup, report)
        except Exception as e:
            logger.warning(f"Link check failed for {file_path}: {e}")

        return report

    async def scan_all(self, check_links: bool = True, limit: Optional[int] = None) -> dict:
        """Scan all HTML files and generate full report."""
        html_files = self.find_html_files(limit=limit)

        all_results: list[dict] = []
        summary = {
            "critical": 0,
            "warning": 0,
            "info": 0,
            "by_category": {},
            "by_page_type": {}
        }

        for file_path in html_files:
            if check_links and aiohttp:
                report = await self.scan_file_with_links(file_path)
            else:
                report = self.scan_file(file_path)

            result_dict = asdict(report)
            all_results.append(result_dict)

            # Update summary
            for issue in report.issues:
                summary[issue.severity] = summary.get(issue.severity, 0) + 1

                if issue.category not in summary["by_category"]:
                    summary["by_category"][issue.category] = {"critical": 0, "warning": 0}
                summary["by_category"][issue.category][issue.severity] = (
                    summary["by_category"][issue.category].get(issue.severity, 0) + 1
                )

            # Track page type stats
            if report.page_type not in summary["by_page_type"]:
                summary["by_page_type"][report.page_type] = {"pages": 0, "issues": 0}
            summary["by_page_type"][report.page_type]["pages"] += 1
            summary["by_page_type"][report.page_type]["issues"] += len(report.issues)

        # Site-level checks
        site_issues = []
        site_issues.extend(self.check_sitemap_lang_params())
        site_issues.extend(self.check_internal_link_orphans())
        summary["site_level_issues"] = [asdict(i) for i in site_issues]
        for issue in site_issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1

        full_report = {
            "scan_date": datetime.now().isoformat(),
            "site_root": str(self.site_root),
            "files_scanned": len(html_files),
            "summary": summary,
            "results": all_results
        }

        return full_report


def generate_summary(report: dict) -> str:
    """Generate human-readable summary from report."""
    lines = [
        "=" * 70,
        "QA WATCHDOG REPORT - bitcoinmarket.net",
        f"Scan Date: {report['scan_date']}",
        f"Files Scanned: {report['files_scanned']}",
        "=" * 70,
        "",
        "SUMMARY",
        "-" * 50,
        f"  CRITICAL issues: {report['summary']['critical']}",
        f"  WARNING issues:  {report['summary']['warning']}",
        "",
        "BY CATEGORY:",
    ]

    for cat, stats in report['summary'].get('by_category', {}).items():
        lines.append(f"  {cat.upper()}: {stats.get('critical', 0)} critical, {stats.get('warning', 0)} warning")

    lines.extend([
        "",
        "BY PAGE TYPE:",
    ])

    for ptype, stats in report['summary'].get('by_page_type', {}).items():
        lines.append(f"  {ptype}: {stats['pages']} pages, {stats['issues']} issues")

    # Site-level issues
    site_issues = report['summary'].get('site_level_issues', [])
    if site_issues:
        lines.extend(["", "SITE-LEVEL ISSUES", "-" * 50])
        for issue in site_issues:
            prefix = "[!]" if issue['severity'] == 'critical' else "[~]"
            lines.append(f"  {prefix} {issue['message']}")
            if issue.get('details'):
                lines.append(f"      {issue['details'][:120]}")

    # Critical issues detail
    critical_pages = [r for r in report['results'] if any(i['severity'] == 'critical' for i in r['issues'])]

    if critical_pages:
        lines.extend([
            "",
            "CRITICAL ISSUES",
            "-" * 50,
        ])

        for result in critical_pages[:20]:
            critical_issues = [i for i in result['issues'] if i['severity'] == 'critical']
            lines.append(f"\n  {result['relative_path']} ({result['page_type']})")
            for issue in critical_issues:
                lines.append(f"    [!] {issue['message']}")
                if issue.get('details'):
                    lines.append(f"        {issue['details'][:80]}")

    lines.extend([
        "",
        "=" * 70,
        "END OF REPORT",
        "=" * 70,
    ])

    return "\n".join(lines)


async def main_async():
    parser = argparse.ArgumentParser(
        description="Scan HTML files for SEO and content quality issues"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config JSON file (default: {DEFAULT_CONFIG_PATH})"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for reports (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Limit number of files to scan (for testing)"
    )
    parser.add_argument(
        "--no-link-check",
        action="store_true",
        help="Skip external link checking (faster)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be scanned"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Initialize watchdog
    try:
        watchdog = QAWatchdog(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.dry_run:
        files = watchdog.find_html_files(limit=args.limit)
        print(f"Would scan {len(files)} HTML files:")
        for f in files[:20]:
            rel = f.relative_to(watchdog.site_root)
            ptype = watchdog._detect_page_type(str(rel))
            print(f"  [{ptype}] {rel}")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        return

    # Run scan
    logger.info("Starting QA scan...")
    check_links = not args.no_link_check
    report = await watchdog.scan_all(check_links=check_links, limit=args.limit)

    # Ensure output directory exists
    output_dir = args.output or watchdog.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filenames with date
    date_str = datetime.now().strftime("%Y-%m-%d")
    json_path = output_dir / f"qa_report_{date_str}.json"
    txt_path = output_dir / f"qa_report_{date_str}.txt"

    # Write JSON report
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"JSON report written to: {json_path}")

    # Write human-readable summary
    summary = generate_summary(report)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(summary)
    logger.info(f"Summary written to: {txt_path}")

    # Print summary to stdout
    print("\n" + summary)

    # Exit with error code if critical issues found
    if report['summary']['critical'] > 0:
        sys.exit(1)

    sys.exit(0)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
