#!/usr/bin/env python3
"""
AIena Substack Publisher — pubblica automaticamente gli articoli su Substack.

Chiamato da aiena_post_publish.py dopo ogni pubblicazione, oppure manualmente.

Config richiesta: /home/pinky/.pinkybot/config/substack_config.json
{
    "publication_url": "https://aienait.substack.com",
    "session_cookie": "<substack.sid da browser DevTools>",
    "user_id": 505551180,
    "auto_publish": true
}

Uso:
    python3 aiena_substack_publisher.py --slug "slug-articolo"
    python3 aiena_substack_publisher.py --slug "slug-articolo" --dry-run
    python3 aiena_substack_publisher.py --list   # mostra articoli già postati
"""

import argparse
import json
import pathlib
import re
import sys
import time
from datetime import datetime

import requests

# ─── Paths ────────────────────────────────────────────────────────────────────
CONFIG_PATH   = pathlib.Path("/home/pinky/.pinkybot/config/substack_config.json")
STATE_FILE    = pathlib.Path("/home/pinky/.pinkybot/data/aiena_substack_posted.json")
ARTICLES_DIR  = pathlib.Path("/var/www/aiena.it/articles")
BASE_URL      = "https://aiena.it/articles"


# ─── Config ───────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERRORE: config non trovata: {CONFIG_PATH}")
        print("Crea il file con: publication_url, session_cookie, user_id, auto_publish")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(posted: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(posted), ensure_ascii=False, indent=2))


# ─── HTML → ProseMirror converter ─────────────────────────────────────────────

def parse_inline(html: str) -> list:
    """Parse inline HTML to ProseMirror inline nodes."""
    nodes = []
    parts = re.split(r'(<(?:strong|b|em|i|a)[^>]*>.*?</(?:strong|b|em|i|a)>)', html, flags=re.DOTALL)
    for part in parts:
        if not part:
            continue
        sm = re.match(r'<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>', part, re.DOTALL)
        if sm:
            text = re.sub(r'<[^>]+>', '', sm.group(1)).strip()
            if text:
                nodes.append({"type": "text", "marks": [{"type": "strong"}], "text": text})
            continue
        em = re.match(r'<(?:em|i)[^>]*>(.*?)</(?:em|i)>', part, re.DOTALL)
        if em:
            text = re.sub(r'<[^>]+>', '', em.group(1)).strip()
            if text:
                nodes.append({"type": "text", "marks": [{"type": "em"}], "text": text})
            continue
        am = re.match(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', part, re.DOTALL)
        if am:
            href = am.group(1)
            text = re.sub(r'<[^>]+>', '', am.group(2)).strip()
            if text:
                nodes.append({"type": "text", "marks": [{"type": "link", "attrs": {"href": href, "title": None}}], "text": text})
            continue
        text = re.sub(r'<[^>]+>', '', part)
        text = (text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                    .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"'))
        if text.strip():
            nodes.append({"type": "text", "text": text})
    return [n for n in nodes if n.get("text", "").strip()]


def html_to_prosemirror(html: str) -> dict:
    """Convert article HTML body to Substack ProseMirror JSON format."""
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)

    nodes = []
    blocks = re.split(
        r'(<(?:h[1-6]|p|blockquote|ul|ol|hr)[^>]*>.*?</(?:h[1-6]|p|blockquote|ul|ol)>|<hr\s*/>)',
        html, flags=re.DOTALL
    )

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Heading
        hm = re.match(r'<(h[1-6])[^>]*>(.*?)</h[1-6]>', block, re.DOTALL)
        if hm:
            level = int(hm.group(1)[1])
            text = re.sub(r'<[^>]+>', '', hm.group(2)).strip()
            if text:
                nodes.append({"type": "heading", "attrs": {"level": level},
                              "content": [{"type": "text", "text": text}]})
            continue

        # HR
        if re.match(r'<hr', block, re.IGNORECASE):
            nodes.append({"type": "horizontal_rule"})
            continue

        # Paragraph
        pm = re.match(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        if pm:
            inner = pm.group(1).strip()
            inline_nodes = parse_inline(inner)
            if inline_nodes:
                nodes.append({"type": "paragraph", "content": inline_nodes})
            continue

        # Blockquote
        bm = re.match(r'<blockquote[^>]*>(.*?)</blockquote>', block, re.DOTALL)
        if bm:
            inner = re.sub(r'<[^>]+>', '', bm.group(1)).strip()
            if inner:
                nodes.append({"type": "blockquote", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": inner}]}
                ]})
            continue

        # UL
        if re.match(r'<ul', block, re.IGNORECASE):
            items = re.findall(r'<li[^>]*>(.*?)</li>', block, re.DOTALL)
            list_items = []
            for item_html in items:
                text = re.sub(r'<[^>]+>', '', item_html).strip()
                if text:
                    list_items.append({"type": "list_item", "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": text}]}
                    ]})
            if list_items:
                nodes.append({"type": "bullet_list", "content": list_items})
            continue

        # OL
        if re.match(r'<ol', block, re.IGNORECASE):
            items = re.findall(r'<li[^>]*>(.*?)</li>', block, re.DOTALL)
            list_items = []
            for item_html in items:
                text = re.sub(r'<[^>]+>', '', item_html).strip()
                if text:
                    list_items.append({"type": "list_item", "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": text}]}
                    ]})
            if list_items:
                nodes.append({"type": "ordered_list", "content": list_items})
            continue

        # Fallback: plain text
        text = re.sub(r'<[^>]+>', '', block).strip()
        if text:
            nodes.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})

    if not nodes:
        nodes = [{"type": "paragraph", "content": [{"type": "text", "text": "Leggi l'articolo completo su AIena.it"}]}]

    return {"type": "doc", "content": nodes}


# ─── Article Parser ────────────────────────────────────────────────────────────

def parse_article(slug: str) -> dict | None:
    """Legge e parsa l'HTML di un articolo AIena. Ritorna dict con title/description/body."""
    article_path = ARTICLES_DIR / f"{slug}.html"
    if not article_path.exists():
        print(f"ERRORE: articolo non trovato: {article_path}")
        return None

    html = article_path.read_text(encoding="utf-8")

    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    desc_m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
    og_img_m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)

    title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else slug
    description = desc_m.group(1).strip() if desc_m else ""
    og_image = og_img_m.group(1).strip() if og_img_m else ""

    # Extract article body
    article_m = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
    if article_m:
        body_html = article_m.group(1)
    else:
        main_m = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL | re.IGNORECASE)
        body_html = main_m.group(1) if main_m else html

    return {
        "slug": slug,
        "title": title,
        "description": description,
        "og_image": og_image,
        "body_html": body_html,
        "url": f"{BASE_URL}/{slug}.html",
    }


# ─── Substack API ─────────────────────────────────────────────────────────────

def publish_to_substack(article: dict, config: dict, dry_run: bool = False) -> bool:
    """Crea una bozza e pubblica su Substack usando ProseMirror JSON."""
    pub_url = config["publication_url"].rstrip("/")
    session_cookie = config["session_cookie"]
    user_id = config.get("user_id", 505551180)
    auto_publish = config.get("auto_publish", True)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Cookie": f"substack.sid={session_cookie}",
        "Origin": pub_url,
        "Referer": f"{pub_url}/publish/post",
    }

    # Convert HTML to ProseMirror JSON
    pm_body = html_to_prosemirror(article["body_html"])

    # Add footer
    footer_nodes = [
        {"type": "horizontal_rule"},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Questo articolo è pubblicato su "},
            {"type": "text", "marks": [{"type": "link", "attrs": {"href": article["url"], "title": None}}],
             "text": "AIena.it"},
            {"type": "text", "text": " — la prima AI giornalista investigativa italiana."},
        ]}
    ]
    pm_body["content"].extend(footer_nodes)

    payload = {
        "draft_title": article["title"],
        "draft_subtitle": article["description"],
        "draft_body": json.dumps(pm_body),
        "draft_bylines": [{"id": user_id}],
        "type": "newsletter",
        "audience": "everyone",
    }
    if article["og_image"]:
        payload["cover_image"] = article["og_image"]

    print(f"[substack] Titolo: {article['title']}")
    print(f"[substack] URL: {article['url']}")
    print(f"[substack] Modalità: {'publish' if auto_publish else 'draft'}")
    print(f"[substack] Nodi ProseMirror: {len(pm_body['content'])}")

    if dry_run:
        print("[substack] DRY-RUN — nessuna richiesta HTTP eseguita")
        return True

    try:
        # 1. Crea bozza
        resp = requests.post(f"{pub_url}/api/v1/drafts", headers=headers, json=payload, timeout=30)

        if resp.status_code in (401, 403):
            print(f"[substack] ERRORE {resp.status_code}: sessione scaduta o non autorizzata")
            print("[substack] Rinnova il session_cookie in substack_config.json")
            return False

        if resp.status_code not in (200, 201):
            print(f"[substack] ERRORE {resp.status_code}: {resp.text[:300]}")
            return False

        post_data = resp.json()
        post_id = post_data.get("id")
        post_slug = post_data.get("slug") or post_data.get("draft_title", "")[:30]
        print(f"[substack] Bozza creata: id={post_id}")

        # 2. Se auto_publish: pubblica
        if auto_publish and post_id:
            time.sleep(1)
            pub_resp = requests.post(
                f"{pub_url}/api/v1/drafts/{post_id}/publish",
                headers=headers,
                json={"send_email": True, "audience": "everyone"},
                timeout=30,
            )
            if pub_resp.status_code in (200, 201):
                real_slug = pub_resp.json().get("slug", "")
                print(f"[substack] ✓ Pubblicato: {pub_url}/p/{real_slug}")
            else:
                print(f"[substack] WARN: publish fallito ({pub_resp.status_code}): {pub_resp.text[:200]}")
                print("[substack] Articolo salvato come bozza — pubblica manualmente da Substack")

        return True

    except requests.RequestException as e:
        print(f"[substack] ERRORE rete: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AIena → Substack publisher")
    parser.add_argument("--slug", help="Slug dell'articolo da pubblicare")
    parser.add_argument("--dry-run", action="store_true", help="Simula senza pubblicare")
    parser.add_argument("--list", action="store_true", help="Mostra articoli già pubblicati su Substack")
    parser.add_argument("--force", action="store_true", help="Pubblica anche se già postato")
    args = parser.parse_args()

    config = load_config()
    posted = load_state()

    if args.list:
        if posted:
            print(f"Articoli già pubblicati su Substack ({len(posted)}):")
            for s in sorted(posted):
                print(f"  • {s}")
        else:
            print("Nessun articolo pubblicato su Substack ancora.")
        return

    if not args.slug:
        parser.error("--slug richiesto (o --list)")

    slug = args.slug

    if slug in posted and not args.force:
        print(f"[substack] Già pubblicato: {slug} (usa --force per ripubblicare)")
        return

    article = parse_article(slug)
    if not article:
        sys.exit(1)

    success = publish_to_substack(article, config, dry_run=args.dry_run)

    if success and not args.dry_run:
        posted.add(slug)
        save_state(posted)
        print(f"[substack] Stato aggiornato: {slug} marcato come pubblicato")


if __name__ == "__main__":
    main()
