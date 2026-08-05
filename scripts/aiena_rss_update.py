#!/usr/bin/env python3
"""
AIena RSS Feed Generator and FTP Uploader
Generates RSS feed from JSON articles and uploads to aiena.it via FTP
"""

import json
import os
import sys
from datetime import datetime
from ftplib import FTP
from pathlib import Path
from xml.sax.saxutils import escape
from pathlib import Path


def _load_secrets() -> dict:
    """Load sensitive credentials from .aiena_secrets file (chmod 600).
    Falls back to environment variables for cron/daemon contexts."""
    secrets: dict = {}
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                secrets[k.strip()] = v.strip()
    # env override (higher priority)
    for key in ("SB_SERVICE_KEY", "FTP_PASS"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


# Configuration
PIPELINE_JSON_PATH = "/var/www/aiena.it/data/pipeline.json"
ARTICLES_JSON_PATH = "/home/pinky/.pinkybot/data/aiena_articles.json"  # fallback legacy
ARTICLES_DIR = "/var/www/aiena.it/articles"
FEED_XML_PATH = "/var/www/aiena.it/feed.xml"
FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")
SITE_URL = "https://www.aiena.it"
SITEMAP_PATH = "/var/www/aiena.it/sitemap.xml"

# Static pages always in sitemap
_STATIC_PAGES = [
    ("", "1.0", "daily"),
    ("archivio.html", "0.8", "weekly"),
    ("chi-siamo.html", "0.6", "monthly"),
    ("segnala.html", "0.6", "monthly"),
    ("correzioni.html", "0.5", "monthly"),
    ("privacy.html", "0.3", "yearly"),
]

# RSS 2.0 — clean, valid, XSL referenced externally via /feed.xsl
RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="/feed.xsl"?>
<rss version="2.0"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:media="http://search.yahoo.com/mrss/"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>AIena — Prima AI investigativa italiana</title>
    <link>{SITE_URL}</link>
    <description>Inchieste, connessioni, fatti. Non mi stanco. Non mi compro. Non dimentico.</description>
    <language>it</language>
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
      <author>aiena@agentmail.to ({author})</author>
      <guid isPermaLink="true">{guid}</guid>
    </item>
"""


def extract_meta_description(slug):
    """Extract meta description from article HTML file"""
    import re
    html_path = os.path.join(ARTICLES_DIR, f"{slug}.html")
    if not os.path.exists(html_path):
        return ""
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read(4096)  # Only read first 4KB for speed
        # Handle both single and double quoted content, avoiding apostrophe truncation
        match = re.search(r'<meta\s+name=["\']description["\']\s+content="([^"]+)"', content, re.IGNORECASE)
        if not match:
            match = re.search(r'<meta\s+name=["\']description["\']\s+content=\'([^\']+)\'', content, re.IGNORECASE)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def load_articles():
    """Load articles from pipeline.json published[] — source of truth.
    Falls back to legacy aiena_articles.json for any missing descriptions."""
    articles = []

    # Primary source: pipeline.json published[]
    if os.path.exists(PIPELINE_JSON_PATH):
        with open(PIPELINE_JSON_PATH, 'r', encoding='utf-8') as f:
            pipeline = json.load(f)
        for pub in pipeline.get('published', []):
            slug = pub.get('slug', '')
            # Try to get description from article HTML
            description = extract_meta_description(slug)
            if not description:
                description = pub.get('description', f"Inchiesta AIena: {pub.get('title', '')}")
            # Normalize date to ISO format
            pub_date = pub.get('published_at', '')
            if pub_date and 'T' not in pub_date:
                pub_date = pub_date + 'T09:00:00Z'
            url = pub.get('url') or f"{SITE_URL}/articles/{slug}.html"
            # Normalize to www.aiena.it (canonical form) — fixes non-www URLs stored in pipeline.json
            url = url.replace("https://aiena.it/", "https://www.aiena.it/")
            articles.append({
                'slug': slug,
                'title': pub.get('title', ''),
                'description': description,
                'url': url,
                'pubDate': pub_date,
                'category': pub.get('category', 'Inchiesta'),
                'author': 'AIena',
            })
        print(f"Loaded {len(articles)} article(s) from pipeline.json")
        return articles

    # Legacy fallback: aiena_articles.json
    if os.path.exists(ARTICLES_JSON_PATH):
        with open(ARTICLES_JSON_PATH, 'r', encoding='utf-8') as f:
            articles = json.load(f)
        print(f"Loaded {len(articles)} article(s) from legacy aiena_articles.json")
        return articles

    print("WARNING: No article source found")
    return []


def format_rfc822_date(iso_date_str):
    """Convert ISO 8601 date to RFC 2822 format"""
    try:
        dt = datetime.fromisoformat(iso_date_str.replace('Z', '+00:00'))
        # Format: Thu, 01 May 2026 00:00:00 +0000
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except:
        return iso_date_str


def generate_feed(articles):
    """Generate RSS feed XML from articles"""
    items = []
    for article in sorted(articles, key=lambda x: x['pubDate'], reverse=True):
        pub_date = format_rfc822_date(article['pubDate'])
        item = ITEM_TEMPLATE.format(
            title=escape(article['title']),
            link=escape(article['url']),
            description=escape(article['description']),
            pubDate=pub_date,
            category=escape(article['category']),
            author=escape(article.get('author', 'AIena')),
            guid=escape(article['url'])
        )
        items.append(item)

    # Use safe string substitution to avoid CSS braces conflicts
    feed = RSS_TEMPLATE.replace('{SITE_URL}', SITE_URL)
    feed = feed.replace('{ITEMS}', '\n'.join(items))

    return feed


def write_feed_local(feed_xml):
    """Write feed.xml to local filesystem using atomic write (tempfile + os.replace)"""
    import tempfile
    feed_dir = os.path.dirname(FEED_XML_PATH)
    os.makedirs(feed_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=feed_dir, suffix=".xml.tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(feed_xml)
        os.chmod(tmp_path, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
        os.replace(tmp_path, FEED_XML_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    print(f"Generated: {FEED_XML_PATH}")


def upload_via_ftp(feed_xml):
    """Upload feed.xml to aiena.it via FTP"""
    try:
        ftp = FTP(FTP_HOST, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        print(f"Connected to {FTP_HOST}")

        # List current directory to see where we are
        cwd = ftp.pwd()
        print(f"Current FTP directory: {cwd}")

        # Try to find the right directory for htdocs/public_html
        try:
            ftp.cwd('/htdocs')
            print("Changed to /htdocs")
        except:
            try:
                ftp.cwd('/public_html')
                print("Changed to /public_html")
            except:
                # Stay in current directory
                print("Using current FTP directory for upload")

        # Upload feed.xml
        with open(FEED_XML_PATH, 'rb') as f:
            ftp.storbinary('STOR feed.xml', f)
        print("Uploaded feed.xml successfully")

        ftp.quit()
        print("FTP connection closed")
        return True
    except Exception as e:
        print(f"FTP Error: {e}")
        print("Note: Feed was generated locally. Check FTP permissions if upload fails.")
        return False


def generate_sitemap(articles):
    """Generate sitemap.xml from static pages + article list."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    urls = []

    # Static pages
    for path, priority, changefreq in _STATIC_PAGES:
        loc = f"{SITE_URL}/{path}" if path else f"{SITE_URL}/"
        urls.append(f"""  <url>
    <loc>{loc}</loc>
    <lastmod>{today}</lastmod>
    <priority>{priority}</priority>
    <changefreq>{changefreq}</changefreq>
  </url>""")

    # Articles sorted oldest → newest
    for article in sorted(articles, key=lambda x: x.get("pubDate", ""), reverse=False):
        pub_date = article.get("pubDate", today)[:10]  # ISO date only
        url = article.get("url", "")
        if not url:
            continue
        # Normalize URL to www.aiena.it form
        url = url.replace("https://aiena.it/", "https://www.aiena.it/")
        if not url.startswith("https://"):
            continue
        urls.append(f"""  <url>
    <loc>{url}</loc>
    <lastmod>{pub_date}</lastmod>
    <priority>0.85</priority>
    <changefreq>monthly</changefreq>
  </url>""")

    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n'
    sitemap += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += "\n".join(urls) + "\n"
    sitemap += "</urlset>\n"
    return sitemap


def write_sitemap_local(sitemap_xml):
    """Write sitemap.xml locally (atomic)."""
    import tempfile
    sitemap_dir = os.path.dirname(SITEMAP_PATH)
    os.makedirs(sitemap_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=sitemap_dir, suffix=".xml.tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(sitemap_xml)
        os.chmod(tmp_path, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
        os.replace(tmp_path, SITEMAP_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    print(f"Generated: {SITEMAP_PATH}")


def upload_sitemap_ftp(sitemap_xml):
    """Upload sitemap.xml to FTP."""
    try:
        ftp = FTP(FTP_HOST, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        try:
            ftp.cwd('/htdocs')
        except Exception:
            try:
                ftp.cwd('/public_html')
            except Exception:
                pass
        with open(SITEMAP_PATH, 'rb') as f:
            ftp.storbinary('STOR sitemap.xml', f)
        print("Uploaded sitemap.xml via FTP")
        ftp.quit()
        return True
    except Exception as e:
        print(f"Sitemap FTP error: {e}")
        return False


def main():
    print("AIena RSS Feed Generator")
    print("=" * 50)

    # Load articles
    articles = load_articles()
    print(f"Loaded {len(articles)} article(s)")

    # Generate feed
    feed_xml = generate_feed(articles)

    # Write locally
    write_feed_local(feed_xml)

    # Upload via FTP
    print("\nUploading to FTP...")
    if upload_via_ftp(feed_xml):
        print("SUCCESS: Feed published")
    else:
        print("WARNING: FTP upload failed, but local file was generated")

    print(f"\nFeed URL: {SITE_URL}/feed.xml")

    # Generate + upload sitemap
    print("\nUpdating sitemap...")
    sitemap_xml = generate_sitemap(articles)
    write_sitemap_local(sitemap_xml)
    if upload_sitemap_ftp(sitemap_xml):
        print("SUCCESS: Sitemap updated")
    else:
        print("WARNING: Sitemap FTP upload failed, local file updated")
    print(f"Sitemap URL: {SITE_URL}/sitemap.xml")


if __name__ == "__main__":
    main()
