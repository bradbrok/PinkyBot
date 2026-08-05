#!/usr/bin/env python3
"""
AIena Bluesky Publisher — posta automaticamente i nuovi articoli su @aiena-it.bsky.social
Gira ogni ora via crontab (o manualmente).
"""
import json, os, pathlib, tempfile
from html.parser import HTMLParser

from atproto import Client, models

from aiena_secrets import _load_secrets

HANDLE   = "aiena-it.bsky.social"

# Lazy-load secrets to avoid crash if env/config unavailable at import time
_secrets_cache = None

def _get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = _load_secrets()
    return _secrets_cache

def _get_app_pass():
    """Get Bluesky app password lazily."""
    s = _get_secrets()
    return s.get("BSKY_APP_PASS", "")

ARTICLES_DIR = pathlib.Path("/var/www/aiena.it/articles")
STATE_FILE   = pathlib.Path("/home/pinky/.pinkybot/data/aiena_bsky_posted.json")
BASE_URL     = "https://aiena.it/articles"


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.url = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs.get("name", "") or attrs.get("property", "")
            content = attrs.get("content", "")
            if name == "description":
                self.description = content
            elif name == "og:url":
                self.url = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def load_posted():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_posted(slugs):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(list(slugs)))
        os.replace(tmp_path, STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def parse_article(path: pathlib.Path):
    parser = MetaParser()
    parser.feed(path.read_text(encoding="utf-8"))
    url = parser.url or f"{BASE_URL}/{path.name}"
    return {
        "slug": path.stem,
        "title": parser.title.strip().replace(" | AIena", ""),
        "description": parser.description.strip(),
        "url": url,
    }


def compose_post(article: dict) -> str:
    title = article["title"]
    desc = article["description"]
    url = article["url"]

    # Bluesky: max 300 chars
    header = f"🔎 {title}\n\n"
    footer = f"\n\n{url}"
    body_max = 280 - len(header) - len(footer)

    if len(desc) > body_max:
        desc = desc[:body_max - 1].rsplit(" ", 1)[0] + "…"

    return f"{header}{desc}{footer}"


def post_to_bluesky(article: dict) -> str:
    client = Client()
    app_pass = _get_app_pass()
    client.login(HANDLE, app_pass)

    text = compose_post(article)

    # Link card embed
    embed = models.AppBskyEmbedExternal.Main(
        external=models.AppBskyEmbedExternal.External(
            uri=article["url"],
            title=article["title"],
            description=article["description"][:300],
        )
    )

    resp = client.send_post(text=text, embed=embed)
    uri = resp.uri  # at://did:.../app.bsky.feed.post/...
    # Converti in URL web
    did = client.me.did
    rkey = uri.split("/")[-1]
    return f"https://bsky.app/profile/{HANDLE}/post/{rkey}"


def run():
    posted = load_posted()
    articles = sorted(ARTICLES_DIR.glob("*.html"))

    new_articles = [a for a in articles if a.stem not in posted]
    if not new_articles:
        print("Nessun nuovo articolo da postare.")
        return

    for path in new_articles:
        article = parse_article(path)
        if not article["title"]:
            print(f"Skip {path.name} — title mancante")
            continue

        print(f"Posting: {article['slug']}")
        try:
            post_url = post_to_bluesky(article)
            print(f"✅ Postato: {post_url}")
            posted.add(article["slug"])
            save_posted(posted)
        except Exception as e:
            print(f"❌ Errore: {e}")
            continue


if __name__ == "__main__":
    run()
