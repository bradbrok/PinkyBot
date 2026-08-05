#!/usr/bin/env python3
"""
BitcoinMarket.net RSS Feed Generator
Scansiona /magazine/ per articoli EN (lang="en"), genera feed.xml.
Servito localmente da nginx — nessun FTP necessario.

Usage:
    python3 bm_rss_update.py
"""

import html as html_module
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

MAGAZINE_DIR = Path("/var/www/bitcoinmarket.net/magazine")
FEED_XML     = Path("/var/www/bitcoinmarket.net/feed.xml")
SITE_URL     = "https://bitcoinmarket.net"
MAX_ITEMS    = 20   # Max entries in feed

RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>BitcoinMarket.net — Crypto News &amp; Analysis</title>
    <link>{SITE_URL}</link>
    <description>Crypto news, exchange reviews, MiCA updates and investigative journalism.</description>
    <language>en</language>
    <atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
    {ITEMS}
  </channel>
</rss>
"""

ITEM_TEMPLATE = """    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>{description}</description>
      <pubDate>{pubDate}</pubDate>
      <category>{category}</category>
      <author>editorial@bitcoinmarket.net (BitcoinMarket)</author>
      <guid isPermaLink="true">{guid}</guid>
    </item>
"""


def format_rfc822(iso_date: str) -> str:
    """Convert YYYY-MM-DD or ISO8601 to RFC 2822."""
    try:
        if "T" in iso_date:
            dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return iso_date


def extract_meta(path: Path) -> dict | None:
    """Extract metadata from an article HTML file."""
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")

        # Language check
        lang_m = re.search(r'<html[^>]+lang="([\w-]+)"', txt)
        if not lang_m or lang_m.group(1) != "en":
            return None

        # Title
        title_m = re.search(r"<title>(.*?)</title>", txt, re.IGNORECASE)
        if not title_m:
            return None
        title = title_m.group(1).split("|")[0].strip()
        title = re.sub(r"&amp;", "&", title)
        if not title or len(title) < 5:
            return None

        # Description
        desc_m = re.search(r'name="description"\s+content="([^"]+)"', txt, re.IGNORECASE)
        if not desc_m:
            desc_m = re.search(r'content="([^"]+)"\s+name="description"', txt, re.IGNORECASE)
        description = html_module.unescape(desc_m.group(1)) if desc_m else title

        # datePublished from schema.org JSON-LD
        date_m = re.search(r'"datePublished":\s*"([^"]+)"', txt)
        if not date_m:
            # Fallback: og:article:published_time
            date_m = re.search(r'property="article:published_time"\s+content="([^"]+)"', txt)
        pub_date = date_m.group(1)[:10] if date_m else "2026-01-01"

        # Category from schema or og
        cat_m = re.search(r'"articleSection":\s*"([^"]+)"', txt)
        if not cat_m:
            cat_m = re.search(r'property="article:section"\s+content="([^"]+)"', txt)
        category = cat_m.group(1) if cat_m else "Crypto"

        return {
            "slug": path.name,
            "title": title,
            "description": description[:200],
            "pub_date": pub_date,
            "category": category,
            "url": f"{SITE_URL}/magazine/{path.name}",
        }
    except Exception as e:
        print(f"  WARN: {path.name}: {e}")
        return None


def generate_feed(articles: list) -> str:
    """Build RSS XML from article list."""
    items = []
    for a in articles:
        item = ITEM_TEMPLATE.format(
            title=escape(a["title"]),
            link=escape(a["url"]),
            description=escape(a["description"]),
            pubDate=format_rfc822(a["pub_date"]),
            category=escape(a["category"]),
            guid=escape(a["url"]),
        )
        items.append(item)

    feed = RSS_TEMPLATE.replace("{SITE_URL}", SITE_URL)
    feed = feed.replace("{ITEMS}", "\n".join(items))
    return feed


def write_atomic(path: Path, content: str) -> None:
    """Atomic write: tempfile + os.replace. Sets 644 perms for nginx."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".xml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    print("BitcoinMarket.net RSS Feed Generator")
    print("=" * 50)

    # Scan magazine directory
    articles = []
    for html_file in MAGAZINE_DIR.glob("*.html"):
        if html_file.name == "index.html":
            continue
        meta = extract_meta(html_file)
        if meta:
            articles.append(meta)

    # Sort by date descending, limit
    articles.sort(key=lambda x: x["pub_date"], reverse=True)
    articles = articles[:MAX_ITEMS]

    print(f"EN articles found: {len(articles)}")
    for a in articles[:5]:
        print(f"  {a['pub_date']} | {a['title'][:55]}")

    if not articles:
        print("ERROR: no articles found")
        return

    # Generate and write feed
    feed_xml = generate_feed(articles)
    write_atomic(FEED_XML, feed_xml)
    print(f"\nGenerated: {FEED_XML}")
    print(f"Feed URL: {SITE_URL}/feed.xml")
    print("SUCCESS")


if __name__ == "__main__":
    main()
