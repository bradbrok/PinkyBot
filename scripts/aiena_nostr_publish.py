#!/usr/bin/env python3
"""
AIena Nostr Publisher
Pubblica articoli di AIena su Nostr come long-form content (kind 30023).

Usage:
  python3 aiena_nostr_publish.py [--relay RELAY_URL]

State file: /home/pinky/.pinkybot/data/aiena_nostr_published.json
Log file: /home/pinky/.pinkybot/data/aiena_nostr_publish.log
Articles: /home/pinky/.pinkybot/data/aiena_articles.json
"""

import json
import hashlib
import logging
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import sys
import os

sys.path.insert(0, "/home/pinky/lib")
import broker_auth  # noqa: E402  (path aggiunto sopra)

# Ensure script dir is on path so aiena_secrets resolves under cron/abs-path execution
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from aiena_secrets import _load_secrets

# Optional imports
try:
    import websockets
except ImportError:
    websockets = None

try:
    import requests
except ImportError:
    requests = None

# Hard dependency: NIP-01 events require valid BIP-340 Schnorr signatures.
# Relays reject anything else, so fail fast at import time.
try:
    import coincurve  # noqa: F401
except ImportError as _coincurve_err:
    raise RuntimeError(
        "coincurve is required for BIP-340 Schnorr signatures (NIP-01). "
        "Install with: pip3 install coincurve"
    ) from _coincurve_err

# Nostr constants — privkey loaded from secrets file (never hardcoded)
# Lazy-load secrets to avoid crash if env/config unavailable at import time
_secrets_cache = None

def _get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = _load_secrets()
    return _secrets_cache

AIENA_PUBKEY = "d1a07628643eb9f446a6130e1ed221d939bd55ea2ed3ffb511aff0c745bce6c3"

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
    "wss://relay.snort.social",
]

DATA_DIR = Path("/home/pinky/.pinkybot/data")
ARTICLES_FILE = DATA_DIR / "aiena_articles.json"
STATE_FILE = DATA_DIR / "aiena_nostr_published.json"
LOG_FILE = DATA_DIR / "aiena_nostr_publish.log"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def hex_to_bytes(hex_str: str) -> bytes:
    """Convert hex string to bytes."""
    return bytes.fromhex(hex_str)


def bytes_to_hex(b: bytes) -> str:
    """Convert bytes to hex string."""
    return b.hex()


def schnorr_sign(message_hash: bytes, privkey_bytes: bytes) -> str:
    """
    Sign a message hash using BIP-340 Schnorr signature (NIP-01 requirement).

    Uses coincurve (libsecp256k1) — the only valid path. ECDSA/HMAC fallbacks
    were removed because they produce signatures rejected by all Nostr relays.

    Args:
        message_hash: 32-byte hash of the message
        privkey_bytes: 32-byte private key

    Returns:
        64-byte Schnorr signature as hex string

    Raises:
        RuntimeError: if signing fails (no silent fallback)
    """
    try:
        privkey_obj = coincurve.PrivateKey(privkey_bytes)
        sig = privkey_obj.sign_schnorr(message_hash)
        return bytes_to_hex(sig)
    except Exception as e:
        raise RuntimeError(f"BIP-340 Schnorr signing failed: {e}") from e


def create_nostr_event(
    privkey: str,
    pubkey: str,
    title: str,
    content: str,
    slug: str,
    url: str,
    category: str,
    created_at: int,
) -> Dict:
    """
    Create a Nostr event (kind 30023 - long-form content).

    Args:
        privkey: Private key in hex
        pubkey: Public key in hex
        title: Article title
        content: Article content (markdown)
        slug: Article slug (used as 'd' tag for addressability)
        url: Article URL
        category: Article category
        created_at: Unix timestamp

    Returns:
        Event dict with signature
    """
    privkey_bytes = hex_to_bytes(privkey)

    # Build tags
    tags = [
        ["d", slug],  # Identifier tag (required for kind 30023)
        ["title", title],
        ["published_at", str(created_at)],
        ["t", "giornalismo"],
        ["t", "italia"],
        ["t", category.lower()],
        ["r", url],  # Reference to original article
    ]

    # Create event object
    event = {
        "content": content,
        "created_at": created_at,
        "kind": 30023,
        "pubkey": pubkey,
        "tags": tags,
    }

    # Calculate event ID (SHA256 of canonical JSON)
    # NIP-01: event ID is SHA256([0, pubkey, created_at, kind, tags, content])
    event_str = json.dumps([0, pubkey, created_at, 30023, tags, content], separators=(',', ':'), ensure_ascii=False)
    event_id = hashlib.sha256(event_str.encode('utf-8')).hexdigest()

    # Sign the event
    event_id_bytes = hex_to_bytes(event_id)
    signature = schnorr_sign(event_id_bytes, privkey_bytes)

    # Add signature and ID to event
    event["id"] = event_id
    event["sig"] = signature

    # Debug logging
    logger.debug(f"Event creation: id={event_id[:16]}..., pubkey={pubkey[:16]}..., sig={signature[:16]}...")
    logger.debug(f"Event serialization: {event_str[:100]}...")

    return event


def load_articles() -> List[Dict]:
    """Load articles from JSON file."""
    if not ARTICLES_FILE.exists():
        logger.warning(f"Articles file not found: {ARTICLES_FILE}")
        return []

    with open(ARTICLES_FILE, 'r') as f:
        return json.load(f)


def load_published_state() -> Dict[str, str]:
    """Load published state (maps slug -> event_id)."""
    if not STATE_FILE.exists():
        return {}

    with open(STATE_FILE, 'r') as f:
        return json.load(f)


def save_published_state(state: Dict[str, str]) -> None:
    """Save published state to file."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


async def publish_to_relay_ws(event: Dict, relay_url: str, timeout: int = 5) -> bool:
    """
    Publish event to a relay via WebSocket.

    Args:
        event: Nostr event dict
        relay_url: Relay WebSocket URL
        timeout: Connection timeout in seconds

    Returns:
        True if published successfully
    """
    if not websockets:
        logger.warning("websockets module not available, skipping relay publish")
        return False

    try:
        logger.info(f"Publishing to {relay_url}...")

        # Connect to relay
        async with websockets.connect(relay_url, close_timeout=timeout) as websocket:
            # Send EVENT message: ["EVENT", <event>]
            message = json.dumps(["EVENT", event])
            await websocket.send(message)

            # Wait for OK response: ["OK", <event_id>, <success>, <message>]
            response = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            response_data = json.loads(response)

            if response_data[0] == "OK":
                if response_data[2]:
                    logger.info(f"Successfully published to {relay_url}")
                    return True
                else:
                    logger.warning(f"Relay rejected: {response_data[3]}")
                    return False
    except asyncio.TimeoutError:
        logger.warning(f"Timeout connecting to {relay_url}")
        return False
    except Exception as e:
        logger.error(f"Error publishing to {relay_url}: {e}")
        return False


async def publish_event_to_relays(event: Dict, relays: List[str]) -> None:
    """Publish event to multiple relays concurrently."""
    tasks = [publish_to_relay_ws(event, relay) for relay in relays]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    success_count = sum(1 for r in results if r is True)
    logger.info(f"Published to {success_count}/{len(relays)} relays")


def notify_telegram(event_id: str, article_title: str, url: str) -> bool:
    """
    Notify via Telegram about published article.

    Args:
        event_id: Nostr event ID
        article_title: Article title
        url: Article URL

    Returns:
        True if notification sent
    """
    if not requests:
        logger.warning("requests module not available, skipping Telegram notification")
        return False

    try:
        message = (
            f"✅ AIena article published on Nostr\n\n"
            f"📰 {article_title}\n"
            f"🔗 {url}\n"
            f"📡 Nostr event: {event_id[:16]}..."
        )

        # POST firmata: la richiesta non autenticata prendeva 401 e il warning
        # sotto era l'unica traccia — la notifica di pubblicazione non arrivava.
        broker_auth.send_message("32405655", message, agent="satoshi")
        logger.info("Telegram notification sent")
        return True
    except Exception as e:
        logger.error(f"/broker/send: notifica NON consegnata "
                     f"({type(e).__name__}: {e})")
        return False


def article_to_markdown(article: Dict) -> str:
    """
    Convert article dict to markdown content.

    Args:
        article: Article dict with title, description, etc.

    Returns:
        Markdown string
    """
    lines = []

    # Add metadata
    if "author" in article:
        lines.append(f"**Autore:** {article['author']}")

    if "pubDate" in article:
        lines.append(f"**Pubblicato:** {article['pubDate']}")

    if "category" in article:
        lines.append(f"**Categoria:** {article['category']}")

    lines.append("")

    # Add description as content
    if "description" in article:
        lines.append(article["description"])

    if "content" in article:
        lines.append("")
        lines.append(article["content"])

    lines.append("")
    lines.append(f"📖 Leggi l'articolo completo: {article.get('url', '')}")

    return "\n".join(lines)


def main():
    """Main entry point."""
    logger.info("Starting AIena Nostr Publisher")

    # Lazy-load private key from secrets file (never hardcoded)
    secrets = _get_secrets()
    aiena_privkey = secrets.get("NOSTR_PRIVKEY", "")

    # Validate private key was loaded from secrets file (never hardcoded)
    if not aiena_privkey or len(aiena_privkey) != 64:
        raise RuntimeError(
            "NOSTR_PRIVKEY missing or invalid in /home/pinky/.pinkybot/scripts/.aiena_secrets "
            "(expected 64-char hex)"
        )

    # Load articles
    articles = load_articles()
    if not articles:
        logger.info("No articles to publish")
        return

    # Load published state
    published = load_published_state()

    # Process each article
    for article in articles:
        slug = article.get("slug")
        if not slug:
            logger.warning("Article missing slug, skipping")
            continue

        # Check if already published
        if slug in published:
            logger.info(f"Article '{slug}' already published (event: {published[slug][:16]}...)")
            continue

        logger.info(f"Publishing article: {slug}")

        # Parse publication date
        pub_date_str = article.get("pubDate", "")
        try:
            if pub_date_str:
                pub_datetime = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                created_at = int(pub_datetime.timestamp())
            else:
                created_at = int(time.time())
        except Exception as e:
            logger.warning(f"Error parsing pubDate: {e}, using current time")
            created_at = int(time.time())

        # Create event
        content = article_to_markdown(article)
        event = create_nostr_event(
            privkey=aiena_privkey,
            pubkey=AIENA_PUBKEY,
            title=article.get("title", ""),
            content=content,
            slug=slug,
            url=article.get("url", ""),
            category=article.get("category", "Varie"),
            created_at=created_at,
        )

        logger.info(f"Event created: {event['id'][:16]}...")

        # Publish to relays
        asyncio.run(publish_event_to_relays(event, DEFAULT_RELAYS))

        # Notify Telegram
        notify_telegram(event['id'], article.get("title", ""), article.get("url", ""))

        # Save to state
        published[slug] = event['id']
        save_published_state(published)

        logger.info(f"Article published and state saved")

    logger.info("Nostr publisher finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
