#!/usr/bin/env python3
"""
AIena Source Monitor - Autonomous investigative lead detection for AIena.it

Monitors Italian public data sources for investigative journalism leads:
- ANAC (Anticorruzione) - Public contract anomalies
- Camera dei Deputati RSS - Parliamentary activity keywords
- Italian news RSS feeds - Keyword tracking

Design: Leads are routed directly to AIena as autonomous investigation tasks.
NO Telegram messages to Mirko. AIena decides independently whether to investigate
and publish. Mirko is notified only AFTER publication, by AIena itself.

Runs daily via crontab, maintains state to avoid duplicate tasks.

Usage:
    python aiena_source_monitor.py [--dry-run] [--verbose]

Author: AIena automated monitoring system
"""

import hashlib
import json
import logging
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

# --- Configuration ---

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = Path("/home/pinky/.pinkybot/data")
STATE_FILE = DATA_DIR / "aiena_monitor_state.json"
CONFIG_FILE = DATA_DIR / "aiena_monitor_config.json"
LOG_FILE = DATA_DIR / "aiena_monitor.log"

# API endpoints
TASKS_URL = "http://localhost:8888/tasks"
PENDING_LEADS_FILE = DATA_DIR / "aiena_pending_leads.json"

# AIena agent for task assignment
AIENA_AGENT = "aiena"

# Mirko's Telegram chat ID (used by AIena after publishing, NOT by this script)
# Reference for AIena: POST http://localhost:8888/broker/send
#   {"agent_name": "satoshi", "chat_id": "32405655", "platform": "telegram", "content": "..."}
MIRKO_CHAT_ID = "32405655"

# Request settings
REQUEST_TIMEOUT = 30
USER_AGENT = "AIena-Monitor/1.0 (investigative journalism bot)"

# --- Logging setup ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


# --- Data Classes ---

@dataclass
class Lead:
    """Represents an investigative lead."""
    source: str  # "anac", "camera", "rss_news"
    lead_type: str  # e.g., "affidamento_diretto", "keyword_match", "emendamento"
    title: str
    subject: Optional[str]  # company, person, topic
    amount: Optional[float]  # for contracts
    url: str
    summary: str
    found_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def unique_id(self) -> str:
        """Generate unique ID for deduplication."""
        content = f"{self.source}:{self.lead_type}:{self.url}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class Config:
    """Runtime configuration."""
    keywords: list[str]
    anac_threshold_eur: float
    enabled_sources: list[str]
    news_feeds: list[str]
    camera_feeds: list[str]
    camera_keywords: list[str]

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load config from JSON file, creating default if missing."""
        if not path.exists():
            default = cls.default()
            default.save(path)
            return default

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls(
            keywords=data.get("keywords", []),
            anac_threshold_eur=data.get("anac_threshold_eur", 500000),
            enabled_sources=data.get("enabled_sources", ["anac", "rss_news", "camera"]),
            news_feeds=data.get("news_feeds", [
                "https://www.repubblica.it/rss/homepage/rss2.0.xml",
                "https://www.ilfattoquotidiano.it/feed/"
            ]),
            camera_feeds=data.get("camera_feeds", [
                "https://comunicazione.camera.it/rss/notizie-prima-pagina",
                "http://documenti.camera.it/apps/rssFeeds/odg/getFeed.asp"
            ]),
            camera_keywords=data.get("camera_keywords", [
                "emendamento", "fiducia", "decreto urgente", "commissione inchiesta"
            ])
        )

    @classmethod
    def default(cls) -> "Config":
        """Return default configuration."""
        return cls(
            keywords=[
                "Philip Morris",
                "Casaleggio",
                "M5S finanziamenti",
                "appalto irregolare",
                "anticorruzione",
                "conflitto interessi",
                "Salvini Russia",
                "fondi europei",
                "corruzione",
                "tangente",
                "peculato",
                "riciclaggio",
                "evasione fiscale",
                "frode",
                "bancarotta",
                "abuso d'ufficio"
            ],
            anac_threshold_eur=500000,
            enabled_sources=["anac", "rss_news", "camera"],
            news_feeds=[
                "https://www.repubblica.it/rss/homepage/rss2.0.xml",
                "https://www.ilfattoquotidiano.it/feed/"
            ],
            camera_feeds=[
                "https://comunicazione.camera.it/rss/notizie-prima-pagina",
                "http://documenti.camera.it/apps/rssFeeds/odg/getFeed.asp"
            ],
            camera_keywords=[
                "emendamento",
                "fiducia",
                "decreto urgente",
                "commissione inchiesta",
                "conflitto di interessi",
                "immunita",
                "decadenza"
            ]
        )

    def save(self, path: Path) -> None:
        """Save config to JSON file."""
        data = {
            "keywords": self.keywords,
            "anac_threshold_eur": self.anac_threshold_eur,
            "enabled_sources": self.enabled_sources,
            "news_feeds": self.news_feeds,
            "camera_feeds": self.camera_feeds,
            "camera_keywords": self.camera_keywords
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


@dataclass
class State:
    """Persistent state for tracking processed items."""
    seen_ids: set[str] = field(default_factory=set)
    last_run: Optional[str] = None
    last_anac_check: Optional[str] = None
    last_camera_check: Optional[str] = None
    last_news_check: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "State":
        """Load state from JSON file."""
        if not path.exists():
            return cls()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return cls(
                seen_ids=set(data.get("seen_ids", [])),
                last_run=data.get("last_run"),
                last_anac_check=data.get("last_anac_check"),
                last_camera_check=data.get("last_camera_check"),
                last_news_check=data.get("last_news_check")
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load state, starting fresh: {e}")
            return cls()

    def save(self, path: Path) -> None:
        """Save state to JSON file."""
        data = {
            "seen_ids": list(self.seen_ids),
            "last_run": self.last_run,
            "last_anac_check": self.last_anac_check,
            "last_camera_check": self.last_camera_check,
            "last_news_check": self.last_news_check
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def mark_seen(self, lead: Lead) -> bool:
        """Mark lead as seen. Returns True if new, False if duplicate."""
        lead_id = lead.unique_id
        if lead_id in self.seen_ids:
            return False
        self.seen_ids.add(lead_id)
        return True


# --- HTTP Utilities ---

def create_ssl_context() -> ssl.SSLContext:
    """Create SSL context that accepts most certificates."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[str]:
    """Fetch URL content with proper error handling."""
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT}
        )

        # Handle both http and https
        if url.startswith("https"):
            context = create_ssl_context()
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.read().decode("utf-8", errors="replace")
        else:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")

    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP error fetching {url}: {e.code} {e.reason}")
        return None
    except urllib.error.URLError as e:
        logger.warning(f"URL error fetching {url}: {e.reason}")
        return None
    except TimeoutError:
        logger.warning(f"Timeout fetching {url}")
        return None
    except Exception as e:
        logger.warning(f"Error fetching {url}: {e}")
        return None


def save_pending_lead(lead: Lead) -> None:
    """Save lead to fallback JSON file when task API fails."""
    try:
        pending = []
        if PENDING_LEADS_FILE.exists():
            try:
                pending = json.loads(PENDING_LEADS_FILE.read_text())
            except json.JSONDecodeError:
                pending = []

        lead_dict = {
            "source": lead.source,
            "type": lead.lead_type,
            "title": lead.title,
            "subject": lead.subject,
            "amount": lead.amount,
            "url": lead.url,
            "summary": lead.summary,
            "found_at": lead.found_at,
            "_saved_at": datetime.now().isoformat()
        }
        pending.append(lead_dict)

        PENDING_LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PENDING_LEADS_FILE.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
        logger.info(f"Lead saved to fallback file: {PENDING_LEADS_FILE}")
    except Exception as e:
        logger.error(f"CRITICAL: Could not save lead to fallback file: {e}")


def create_investigation_task(lead: Lead, dry_run: bool = False) -> bool:
    """
    Create an autonomous investigation task for AIena.

    Design: NO Telegram to Mirko. AIena gets the task and decides autonomously.
    Returns True on success, False on failure.
    """
    source_name = {
        "anac": "ANAC (Anticorruzione)",
        "camera": "Camera dei Deputati",
        "rss_news": "News RSS"
    }.get(lead.source, lead.source)

    # Build task description with full context for AIena
    description_parts = [
        f"Lead investigativo da {source_name}.",
        "",
        f"Tipo: {lead.lead_type}",
    ]

    if lead.subject:
        description_parts.append(f"Soggetto/Keywords: {lead.subject}")

    if lead.amount:
        formatted_amount = f"{lead.amount:,.0f}".replace(",", ".")
        description_parts.append(f"Importo: {formatted_amount} EUR")

    description_parts.extend([
        "",
        f"Dettaglio: {lead.summary[:500]}",
        f"Link: {lead.url}",
        "",
        "Azioni attese:",
        "1. Ricerca approfondita sulla fonte e sul soggetto",
        "2. Valuta se merita un articolo (soglia: fatti verificabili, interesse pubblico)",
        "3. Se si: scrivi articolo completo e aggiungi a /home/pinky/.pinkybot/data/aiena_articles.json",
        f"4. Notifica Mirko (chat_id: {MIRKO_CHAT_ID}) solo dopo la pubblicazione con: 'Ho pubblicato X'",
        "5. Se non merita: ignora silenziosamente"
    ])

    task_description = "\n".join(description_parts)
    task_title = f"Indaga: {lead.title[:80]}"

    if dry_run:
        logger.info(f"[DRY RUN] Would create task: {task_title}")
        logger.info(f"[DRY RUN] Description preview:\n{task_description[:300]}...")
        return True

    payload = {
        "title": task_title,
        "description": task_description,
        "priority": "normal",
        "assigned_agent": AIENA_AGENT,
        "created_by": "aiena_source_monitor",
        "tags": ["investigazione", "lead", lead.source.lower().replace(" ", "_")]
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            TASKS_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT
            },
            method="POST"
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status == 200:
                result = json.loads(response.read())
                task_id = result.get("id", "?")
                logger.info(f"Task created: #{task_id} for '{lead.title[:50]}...'")
                return True
            else:
                body = response.read().decode()
                logger.warning(f"Task API returned HTTP {response.status}: {body[:200]}")
                save_pending_lead(lead)
                return False

    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        logger.error(f"Task API HTTP error {e.code}: {error_body[:200]}")
        save_pending_lead(lead)
        return False
    except urllib.error.URLError as e:
        logger.error(f"Task API connection error: {e.reason}")
        save_pending_lead(lead)
        return False
    except Exception as e:
        logger.error(f"Task API error: {type(e).__name__}: {e}")
        save_pending_lead(lead)
        return False


# --- RSS Parser ---

def parse_rss(xml_content: str) -> list[dict]:
    """Parse RSS/Atom feed and return items."""
    items = []

    try:
        root = ET.fromstring(xml_content)

        # Handle RSS 2.0
        for item in root.findall(".//item"):
            entry = {
                "title": _get_text(item, "title"),
                "link": _get_text(item, "link"),
                "description": _get_text(item, "description"),
                "pubDate": _get_text(item, "pubDate"),
                "guid": _get_text(item, "guid")
            }
            if entry["title"] or entry["link"]:
                items.append(entry)

        # Handle Atom
        for entry_elem in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            entry = {
                "title": _get_text(entry_elem, "{http://www.w3.org/2005/Atom}title"),
                "link": _get_attr(entry_elem, "{http://www.w3.org/2005/Atom}link", "href"),
                "description": _get_text(entry_elem, "{http://www.w3.org/2005/Atom}summary") or
                               _get_text(entry_elem, "{http://www.w3.org/2005/Atom}content"),
                "pubDate": _get_text(entry_elem, "{http://www.w3.org/2005/Atom}updated"),
                "guid": _get_text(entry_elem, "{http://www.w3.org/2005/Atom}id")
            }
            if entry["title"] or entry["link"]:
                items.append(entry)

    except ET.ParseError as e:
        logger.warning(f"Failed to parse RSS: {e}")

    return items


def _get_text(element: ET.Element, tag: str) -> str:
    """Get text content of child element."""
    child = element.find(tag)
    if child is not None and child.text:
        # Strip HTML tags from description
        text = re.sub(r"<[^>]+>", "", child.text)
        return text.strip()
    return ""


def _get_attr(element: ET.Element, tag: str, attr: str) -> str:
    """Get attribute value from child element."""
    child = element.find(tag)
    if child is not None:
        return child.get(attr, "")
    return ""


def clean_html(text: str) -> str:
    """Remove HTML tags and clean up whitespace."""
    # Remove CDATA markers
    text = re.sub(r"<!\[CDATA\[|\]\]>", "", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# --- Source Monitors ---

class ANACMonitor:
    """
    Monitor ANAC (Anticorruzione) data for suspicious contracts.

    Note: ANAC's API often blocks automated requests. We try multiple approaches:
    1. Direct CKAN API
    2. RSS feed for updates
    3. Fallback to news/press release scraping
    """

    ANAC_NEWS_URL = "https://www.anticorruzione.it/-/comunicati-stampa"
    ANAC_DATA_URL = "https://dati.anticorruzione.it"

    def __init__(self, config: Config, state: State):
        self.config = config
        self.state = state
        self.threshold = config.anac_threshold_eur

    def check(self) -> list[Lead]:
        """Check ANAC sources for leads."""
        leads = []

        # Try multiple approaches as ANAC often blocks requests

        # Approach 1: Check ANAC news/press releases
        news_leads = self._check_anac_news()
        leads.extend(news_leads)

        # Approach 2: Try CKAN API (usually blocked but worth trying)
        # api_leads = self._check_anac_api()
        # leads.extend(api_leads)

        self.state.last_anac_check = datetime.now().isoformat()
        return leads

    def _check_anac_news(self) -> list[Lead]:
        """Check ANAC news/press releases for relevant updates."""
        leads = []

        # ANAC doesn't have a public RSS, so we look for keywords in their
        # communications that might indicate interesting investigations

        # For now, we focus on keywords that indicate ANAC actions
        anac_keywords = [
            "sospensione",
            "esclusione",
            "sanzione",
            "vigilanza",
            "irregolarita",
            "affidamento diretto",
            "procedura negoziata"
        ]

        logger.info("ANAC check: API access restricted, monitoring via news sources")

        # Note: Full ANAC integration would require:
        # 1. Whitelisted IP or API key
        # 2. Bulk download of CIG dataset (available quarterly)
        # 3. Analysis of affidamento diretto patterns

        # For now, we catch ANAC news via the general news RSS monitors
        # when they mention ANAC actions

        return leads

    def _check_anac_api(self) -> list[Lead]:
        """Try to access ANAC CKAN API (usually blocked)."""
        leads = []

        # List of endpoints to try
        endpoints = [
            "https://dati.anticorruzione.it/api/3/action/package_list",
            "https://dati.anticorruzione.it/api/3/action/datastore_search?resource_id=CIG"
        ]

        for endpoint in endpoints:
            content = fetch_url(endpoint)
            if content and "rejected" not in content.lower():
                logger.info(f"ANAC API accessible at {endpoint}")
                # Parse and analyze data...
                # This is placeholder for when/if API becomes accessible
                break

        return leads


class CameraMonitor:
    """Monitor Camera dei Deputati RSS feeds for parliamentary activity."""

    def __init__(self, config: Config, state: State):
        self.config = config
        self.state = state
        self.keywords = [kw.lower() for kw in config.camera_keywords]

    def check(self) -> list[Lead]:
        """Check Camera feeds for interesting activity."""
        leads = []

        for feed_url in self.config.camera_feeds:
            try:
                feed_leads = self._check_feed(feed_url)
                leads.extend(feed_leads)
            except Exception as e:
                logger.warning(f"Error checking Camera feed {feed_url}: {e}")

        self.state.last_camera_check = datetime.now().isoformat()
        return leads

    def _check_feed(self, feed_url: str) -> list[Lead]:
        """Check a single Camera RSS feed."""
        leads = []

        content = fetch_url(feed_url)
        if not content:
            return leads

        items = parse_rss(content)
        logger.info(f"Camera feed {feed_url}: found {len(items)} items")

        for item in items:
            title = item.get("title", "")
            description = item.get("description", "")
            link = item.get("link", "")

            # Check against keywords
            combined_text = f"{title} {description}".lower()

            matched_keywords = []
            for keyword in self.keywords:
                if keyword in combined_text:
                    matched_keywords.append(keyword)

            if matched_keywords:
                lead = Lead(
                    source="camera",
                    lead_type="attivita_parlamentare",
                    title=clean_html(title)[:200],
                    subject=", ".join(matched_keywords),
                    amount=None,
                    url=link,
                    summary=clean_html(description)[:500]
                )
                leads.append(lead)

        return leads


class NewsMonitor:
    """Monitor Italian news RSS feeds for keyword matches."""

    def __init__(self, config: Config, state: State):
        self.config = config
        self.state = state
        # Build keyword patterns (case-insensitive)
        self.keyword_patterns = []
        for kw in config.keywords:
            pattern = re.compile(re.escape(kw), re.IGNORECASE)
            self.keyword_patterns.append((kw, pattern))

    def check(self) -> list[Lead]:
        """Check news feeds for keyword matches."""
        leads = []

        for feed_url in self.config.news_feeds:
            try:
                feed_leads = self._check_feed(feed_url)
                leads.extend(feed_leads)
            except Exception as e:
                logger.warning(f"Error checking news feed {feed_url}: {e}")

        self.state.last_news_check = datetime.now().isoformat()
        return leads

    def _check_feed(self, feed_url: str) -> list[Lead]:
        """Check a single news RSS feed."""
        leads = []

        content = fetch_url(feed_url)
        if not content:
            return leads

        items = parse_rss(content)
        logger.info(f"News feed {feed_url}: found {len(items)} items")

        for item in items:
            title = item.get("title", "")
            description = item.get("description", "")
            link = item.get("link", "")

            combined_text = f"{title} {description}"

            # Check against keywords
            matched_keywords = []
            for keyword, pattern in self.keyword_patterns:
                if pattern.search(combined_text):
                    matched_keywords.append(keyword)

            if matched_keywords:
                # Determine lead type based on keywords
                lead_type = self._classify_lead(matched_keywords, combined_text)

                lead = Lead(
                    source="rss_news",
                    lead_type=lead_type,
                    title=clean_html(title)[:200],
                    subject=", ".join(matched_keywords[:3]),  # Top 3 keywords
                    amount=self._extract_amount(combined_text),
                    url=link,
                    summary=clean_html(description)[:500]
                )
                leads.append(lead)

        return leads

    def _classify_lead(self, keywords: list[str], text: str) -> str:
        """Classify lead type based on keywords and content."""
        text_lower = text.lower()

        if any(kw in text_lower for kw in ["corruzione", "tangente", "mazzetta"]):
            return "possibile_corruzione"
        elif any(kw in text_lower for kw in ["appalto", "gara", "affidamento"]):
            return "appalto_sospetto"
        elif any(kw in text_lower for kw in ["evasione", "frode", "riciclaggio"]):
            return "reato_finanziario"
        elif any(kw in text_lower for kw in ["conflitto", "interessi"]):
            return "conflitto_interessi"
        elif any(kw in text_lower for kw in ["fondi", "finanziamenti"]):
            return "finanziamenti_sospetti"
        else:
            return "keyword_match"

    def _extract_amount(self, text: str) -> Optional[float]:
        """Try to extract monetary amount from text."""
        # Pattern for Euro amounts like "100.000 euro", "1,5 milioni", "500mila euro"
        patterns = [
            # 100.000 euro / 100,000 euro
            r"(\d{1,3}(?:[.,]\d{3})+)\s*(?:euro|€)",
            # 1,5 milioni / 1.5 milioni di euro
            r"(\d+(?:[.,]\d+)?)\s*milion[ei]",
            # 500mila / 500 mila euro
            r"(\d+)\s*mila",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    value_str = match.group(1)
                    # Normalize number format
                    value_str = value_str.replace(".", "").replace(",", ".")
                    value = float(value_str)

                    # Adjust for millions/thousands
                    if "milion" in pattern:
                        value *= 1_000_000
                    elif "mila" in pattern:
                        value *= 1_000

                    return value
                except (ValueError, IndexError):
                    continue

        return None


# --- Main Logic ---

class SourceMonitor:
    """Main coordinator for all source monitors."""

    def __init__(self, config: Config, state: State, dry_run: bool = False):
        self.config = config
        self.state = state
        self.dry_run = dry_run

        self.monitors = {
            "anac": ANACMonitor(config, state),
            "camera": CameraMonitor(config, state),
            "rss_news": NewsMonitor(config, state)
        }

    def run(self) -> int:
        """Run all enabled monitors. Returns count of new leads found."""
        all_leads = []

        logger.info(f"Starting source monitor run at {datetime.now().isoformat()}")
        logger.info(f"Enabled sources: {', '.join(self.config.enabled_sources)}")

        for source_name in self.config.enabled_sources:
            if source_name not in self.monitors:
                logger.warning(f"Unknown source: {source_name}")
                continue

            monitor = self.monitors[source_name]

            try:
                logger.info(f"Checking source: {source_name}")
                leads = monitor.check()
                logger.info(f"Source {source_name}: found {len(leads)} potential leads")
                all_leads.extend(leads)
            except Exception as e:
                logger.error(f"Error in {source_name} monitor: {e}")

        # Deduplicate and filter
        new_leads = []
        for lead in all_leads:
            if self.state.mark_seen(lead):
                new_leads.append(lead)
            else:
                logger.debug(f"Skipping duplicate lead: {lead.title[:50]}")

        logger.info(f"New leads (after deduplication): {len(new_leads)}")

        # Create investigation tasks for new leads (NOT Telegram messages)
        task_count = 0
        for lead in new_leads:
            if create_investigation_task(lead, dry_run=self.dry_run):
                task_count += 1

        # Update state
        self.state.last_run = datetime.now().isoformat()
        self.state.save(STATE_FILE)

        logger.info(f"Run complete. Created {task_count} investigation tasks.")
        return len(new_leads)


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="AIena Source Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't send notifications, just log what would be sent")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose logging")
    parser.add_argument("--reset-state", action="store_true",
                        help="Reset state file (will re-process all items)")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("AIena Source Monitor starting")
    logger.info(f"Data dir: {DATA_DIR}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info("=" * 60)

    # Load config
    config = Config.load(CONFIG_FILE)
    logger.info(f"Loaded config with {len(config.keywords)} keywords")

    # Load or reset state
    if args.reset_state:
        logger.info("Resetting state file")
        state = State()
    else:
        state = State.load(STATE_FILE)
        logger.info(f"Loaded state with {len(state.seen_ids)} seen items")

    # Run monitors
    try:
        monitor = SourceMonitor(config, state, dry_run=args.dry_run)
        new_lead_count = monitor.run()

        if new_lead_count == 0:
            logger.info("No new leads found today.")
        else:
            logger.info(f"Found {new_lead_count} new leads!")

        return 0

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 1
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
