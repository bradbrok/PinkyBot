#!/usr/bin/env python3
"""
AIena X Publisher — posta automaticamente i nuovi articoli su @Alena_it
Gira ogni ora via crontab (o manualmente).
"""
import json, pathlib, re, sys
from html.parser import HTMLParser

import tweepy

# OAuth 1.0a credentials
CONSUMER_KEY        = "SXmtZnn9BP1nX4Mj0zT5LyCcf"
CONSUMER_SECRET     = "SxY7Y68hsNQqger6pNzyAkiq661AreSmZLIEGVeDFJmHrJgwvx"
ACCESS_TOKEN        = "2050439224520802304-WAOKYNV4Uoy4jKtbRk04Qmj6J5WpW2"
ACCESS_TOKEN_SECRET = "Joafblh1dQEyYfZflbpIVSPah3R07M2mWeuxLas3Fr2zL"

ARTICLES_DIR  = pathlib.Path("/var/www/aiena.it/articles")
STATE_FILE    = pathlib.Path("/home/pinky/.pinkybot/data/aiena_x_posted.json")
BASE_URL      = "https://aiena.it/articles"


class MetaParser(HTMLParser):
    """Estrae title, description, og:url da un HTML."""
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
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_posted(slugs):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(slugs)))


def parse_article(path: pathlib.Path):
    parser = MetaParser()
    parser.feed(path.read_text(encoding="utf-8"))
    slug = path.stem
    url = parser.url or f"{BASE_URL}/{path.name}"
    return {
        "slug": slug,
        "title": parser.title.strip(),
        "description": parser.description.strip(),
        "url": url,
    }


def compose_tweet(article: dict) -> str:
    title = article["title"]
    desc = article["description"]
    url = article["url"]

    # Tronca description per stare nei 280 char con titolo e url
    # URL conta ~23 char (t.co), lasciamo ~230 per testo
    max_text = 230
    header = f"🔎 {title}\n\n"
    footer = f"\n\n{url}"
    body_max = max_text - len(header) - len(footer)

    if len(desc) > body_max:
        desc = desc[:body_max - 1].rsplit(" ", 1)[0] + "…"

    return f"{header}{desc}{footer}"


def post_tweet(text: str) -> str:
    client = tweepy.Client(
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )
    resp = client.create_tweet(text=text)
    tweet_id = resp.data["id"]
    return f"https://x.com/Alena_it/status/{tweet_id}"


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

        tweet_text = compose_tweet(article)
        print(f"Posting: {article['slug']}")
        print(f"Tweet:\n{tweet_text}\n")

        try:
            tweet_url = post_tweet(tweet_text)
            print(f"✅ Postato: {tweet_url}")
            posted.add(article["slug"])
            save_posted(posted)
        except Exception as e:
            print(f"❌ Errore posting {article['slug']}: {e}")
            sys.exit(1)


if __name__ == "__main__":
    run()
