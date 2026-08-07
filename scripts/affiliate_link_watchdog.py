#!/usr/bin/env python3
"""
Affiliate Link Watchdog
Scans HTML pages for exchange mentions missing referral links.

Usage:
    python affiliate_link_watchdog.py [--config CONFIG_PATH] [--output OUTPUT_DIR]

Requirements:
    pip install beautifulsoup4
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from bs4 import BeautifulSoup, NavigableString
except ImportError:
    print("ERROR: beautifulsoup4 required. Install with: pip install beautifulsoup4")
    sys.exit(1)

# Add lib and scripts to path for importing audit_config
sys.path.insert(0, "/home/pinky/lib")
sys.path.insert(0, "/home/pinky/scripts")

try:
    from bitcoinmarket_check_config import get_site_config
except ImportError:
    get_site_config = None
    print("WARNING: bitcoinmarket_check_config not available. Config-driven mode disabled.")


# --- Configuration ---

DEFAULT_CONFIG_PATH = Path(__file__).parent / "affiliate_config.json"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "reports"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# --- Data Classes ---

@dataclass
class MissingLink:
    """Represents an exchange mention without proper referral link."""
    file_path: str
    relative_path: str
    exchange: str
    mention_text: str
    context: str  # surrounding text
    line_number: int
    has_link: bool  # has <a> but wrong href
    current_href: Optional[str]
    suggested_href: str


@dataclass
class ScanResult:
    """Full scan result for a single HTML file."""
    file_path: str
    relative_path: str
    exchanges_found: list[str]
    missing_links: list[MissingLink]
    proper_links: int
    scan_time: str


# --- Core Logic ---

class AffiliateWatchdog:
    """Scans HTML files for exchange mentions missing affiliate links."""

    def __init__(self, config_path: Path):
        self.config = self._load_config(config_path)
        self.site_root = Path(self.config["site_root"])
        self.exchanges = self.config["exchanges"]
        self.exclude_dirs = set(self.config.get("exclude_dirs", []))
        self.html_extensions = self.config.get("html_extensions", [".html", ".htm"])

        # Pre-compile regex patterns for each exchange
        self._compile_patterns()

    def _load_config(self, config_path: Path) -> dict:
        """Load configuration from JSON file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _compile_patterns(self):
        """Pre-compile regex patterns for exchange name matching."""
        self.exchange_patterns = {}
        for name, data in self.exchanges.items():
            # Create case-insensitive pattern that matches whole words
            patterns = data.get("patterns", [name])
            # Escape special chars and join with OR
            escaped = [re.escape(p) for p in patterns]
            # Match as whole word (not part of another word)
            pattern = r'\b(' + '|'.join(escaped) + r')\b'
            self.exchange_patterns[name] = re.compile(pattern, re.IGNORECASE)

    def find_html_files(self) -> list[Path]:
        """Recursively find all HTML files in site root."""
        if not self.site_root.exists():
            logger.error(f"Site root does not exist: {self.site_root}")
            return []

        html_files = []
        for ext in self.html_extensions:
            for path in self.site_root.rglob(f"*{ext}"):
                # Skip excluded directories
                if any(excluded in path.parts for excluded in self.exclude_dirs):
                    continue
                html_files.append(path)

        logger.info(f"Found {len(html_files)} HTML files in {self.site_root}")
        return sorted(html_files)

    def _get_text_context(self, element, max_len: int = 150) -> str:
        """Extract surrounding text context for a mention."""
        # Get parent paragraph or div text
        parent = element.parent
        while parent and parent.name not in ['p', 'div', 'li', 'td', 'span', 'article', 'section']:
            parent = parent.parent

        if parent:
            text = parent.get_text(separator=' ', strip=True)
            if len(text) > max_len:
                text = text[:max_len] + "..."
            return text
        return str(element)[:max_len]

    def _find_line_number(self, html_content: str, search_text: str, start_pos: int = 0) -> int:
        """Find approximate line number for a text match."""
        # Find position of search_text
        pos = html_content.lower().find(search_text.lower(), start_pos)
        if pos == -1:
            return 0
        # Count newlines before this position
        return html_content[:pos].count('\n') + 1

    def _check_link_has_ref(self, href: str, exchange_name: str) -> bool:
        """Check if a link contains proper referral markers for an exchange."""
        if not href:
            return False

        href_lower = href.lower()
        exchange_data = self.exchanges.get(exchange_name, {})
        ref_markers = exchange_data.get("ref_markers", [])

        # Check if any ref marker is in the href
        for marker in ref_markers:
            if marker.lower() in href_lower:
                return True

        return False

    def _is_in_link_with_ref(self, element, exchange_name: str) -> tuple[bool, Optional[str]]:
        """
        Check if element is inside an <a> tag with proper referral link.
        Returns (has_proper_ref, current_href)
        """
        # Walk up the tree looking for <a> tags
        current = element.parent
        while current:
            if current.name == 'a':
                href = current.get('href', '')
                has_ref = self._check_link_has_ref(href, exchange_name)
                return (has_ref, href)
            current = current.parent

        return (False, None)

    def scan_file(self, file_path: Path) -> ScanResult:
        """Scan a single HTML file for missing affiliate links."""
        relative_path = str(file_path.relative_to(self.site_root))

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                html_content = f.read()
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return ScanResult(
                file_path=str(file_path),
                relative_path=relative_path,
                exchanges_found=[],
                missing_links=[],
                proper_links=0,
                scan_time=datetime.now().isoformat()
            )

        soup = BeautifulSoup(html_content, "html.parser")

        # Remove script and style tags from consideration
        for tag in soup.find_all(['script', 'style', 'noscript']):
            tag.decompose()

        missing_links: list[MissingLink] = []
        exchanges_found: set[str] = set()
        proper_links = 0

        # Track positions we've already reported to avoid duplicates
        reported_positions: set[tuple[str, int]] = set()

        # Get all text nodes
        for text_node in soup.find_all(string=True):
            if not isinstance(text_node, NavigableString):
                continue

            text = str(text_node)
            if not text.strip():
                continue

            # Check each exchange pattern
            for exchange_name, pattern in self.exchange_patterns.items():
                matches = list(pattern.finditer(text))

                for match in matches:
                    exchanges_found.add(exchange_name)
                    mention_text = match.group(0)

                    # Check if this mention is inside a proper affiliate link
                    has_proper_ref, current_href = self._is_in_link_with_ref(
                        text_node, exchange_name
                    )

                    if has_proper_ref:
                        proper_links += 1
                        continue

                    # Find line number
                    line_number = self._find_line_number(
                        html_content,
                        mention_text,
                        0
                    )

                    # Avoid duplicate reports for same exchange on same line
                    report_key = (exchange_name, line_number)
                    if report_key in reported_positions:
                        continue
                    reported_positions.add(report_key)

                    # Get context
                    context = self._get_text_context(text_node)

                    # Get suggested href
                    suggested_href = self.exchanges[exchange_name].get("ref_url", "")

                    missing_links.append(MissingLink(
                        file_path=str(file_path),
                        relative_path=relative_path,
                        exchange=exchange_name,
                        mention_text=mention_text,
                        context=context,
                        line_number=line_number,
                        has_link=current_href is not None,
                        current_href=current_href,
                        suggested_href=suggested_href
                    ))

        return ScanResult(
            file_path=str(file_path),
            relative_path=relative_path,
            exchanges_found=list(exchanges_found),
            missing_links=missing_links,
            proper_links=proper_links,
            scan_time=datetime.now().isoformat()
        )

    def scan_all(self) -> dict:
        """Scan all HTML files and generate full report."""
        html_files = self.find_html_files()

        all_results: list[dict] = []
        total_missing = 0
        total_proper = 0
        exchange_stats: dict[str, dict] = {}

        for file_path in html_files:
            result = self.scan_file(file_path)

            # Only include files with exchange mentions
            if result.exchanges_found:
                all_results.append(asdict(result))
                total_missing += len(result.missing_links)
                total_proper += result.proper_links

                # Track per-exchange stats
                for ml in result.missing_links:
                    if ml.exchange not in exchange_stats:
                        exchange_stats[ml.exchange] = {"missing": 0, "proper": 0}
                    exchange_stats[ml.exchange]["missing"] += 1

                for ex in result.exchanges_found:
                    if ex not in exchange_stats:
                        exchange_stats[ex] = {"missing": 0, "proper": 0}

        # Add proper link counts to exchange stats
        for result in all_results:
            for ex in result["exchanges_found"]:
                if ex in exchange_stats:
                    # Estimate proper links per exchange (simplified)
                    exchange_stats[ex]["proper"] += result["proper_links"] // max(len(result["exchanges_found"]), 1)

        report = {
            "scan_date": datetime.now().isoformat(),
            "site_root": str(self.site_root),
            "summary": {
                "files_scanned": len(html_files),
                "files_with_exchanges": len(all_results),
                "total_missing_links": total_missing,
                "total_proper_links": total_proper,
                "exchange_breakdown": exchange_stats
            },
            "results": all_results
        }

        return report


def generate_summary(report: dict) -> str:
    """Generate human-readable summary from report."""
    lines = [
        "=" * 60,
        "AFFILIATE LINK WATCHDOG REPORT",
        f"Scan Date: {report['scan_date']}",
        f"Site: {report['site_root']}",
        "=" * 60,
        "",
        "SUMMARY",
        "-" * 40,
        f"Files scanned: {report['summary']['files_scanned']}",
        f"Files with exchange mentions: {report['summary']['files_with_exchanges']}",
        f"Total missing affiliate links: {report['summary']['total_missing_links']}",
        f"Total proper affiliate links: {report['summary']['total_proper_links']}",
        "",
        "BREAKDOWN BY EXCHANGE",
        "-" * 40,
    ]

    for exchange, stats in sorted(report['summary']['exchange_breakdown'].items()):
        status = "OK" if stats['missing'] == 0 else f"MISSING: {stats['missing']}"
        lines.append(f"  {exchange}: {status} (proper: {stats['proper']})")

    # List files with missing links
    files_with_missing = [r for r in report['results'] if r['missing_links']]

    if files_with_missing:
        lines.extend([
            "",
            "FILES WITH MISSING LINKS",
            "-" * 40,
        ])

        for result in files_with_missing:
            lines.append(f"\n  {result['relative_path']}")
            for ml in result['missing_links']:
                link_status = f"has link to: {ml['current_href'][:50]}..." if ml['has_link'] and ml['current_href'] else "no link"
                lines.append(f"    - Line {ml['line_number']}: {ml['exchange']} ({link_status})")
                lines.append(f"      Context: \"{ml['context'][:80]}...\"")
    else:
        lines.extend([
            "",
            "All exchange mentions have proper affiliate links!",
        ])

    lines.extend([
        "",
        "=" * 60,
        "END OF REPORT",
        "=" * 60,
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Scan HTML files for exchange mentions missing affiliate links"
    )
    parser.add_argument(
        "--site", "-s",
        default="bitcoinmarket",
        help="Site name (bitcoinmarket, aiena, etc.) — skips affiliate checks if not required by site config"
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
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be scanned, don't generate report"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Initialize watchdog
    try:
        watchdog = AffiliateWatchdog(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    # Load site-specific audit config (to check if affiliate disclosure is required)
    site_config = None
    if get_site_config:
        try:
            site_config = get_site_config(args.site)
            logger.info(f"Loaded audit config for site: {args.site} ({site_config.url})")

            # Skip affiliate checks if not required by site config (e.g., aiena has no affiliate program)
            if not site_config.standards.require_affiliate_disclosure:
                logger.info(f"Affiliate disclosure not required for {args.site} — skipping scan")
                print(f"Affiliate checks skipped for {args.site} (no affiliate program)")
                sys.exit(0)

            # Store in watchdog for reference
            watchdog.site_config = site_config
            watchdog.site_name = args.site
        except Exception as e:
            logger.warning(f"Could not load site config: {e}. Continuing with defaults.")
            watchdog.site_config = None
            watchdog.site_name = args.site
    else:
        watchdog.site_config = None
        watchdog.site_name = args.site

    if args.dry_run:
        files = watchdog.find_html_files()
        print(f"Would scan {len(files)} HTML files:")
        for f in files[:20]:
            print(f"  - {f}")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        return

    # Run scan
    logger.info("Starting affiliate link scan...")
    report = watchdog.scan_all()

    # Ensure output directory exists
    args.output.mkdir(parents=True, exist_ok=True)

    # Generate filenames with date and site name
    date_str = datetime.now().strftime("%Y-%m-%d")
    site_label = f"_{args.site}" if args.site != "bitcoinmarket" else ""
    json_path = args.output / f"affiliate_report{site_label}_{date_str}.json"
    txt_path = args.output / f"affiliate_report{site_label}_{date_str}.txt"

    # Write JSON report
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"JSON report written to: {json_path}")

    # Write human-readable summary
    summary = generate_summary(report)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(summary)
    logger.info(f"Summary written to: {txt_path}")

    # Also print summary to stdout
    print("\n" + summary)

    # Exit with error code if missing links found
    if report['summary']['total_missing_links'] > 0:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
