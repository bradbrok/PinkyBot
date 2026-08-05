#!/usr/bin/env python3
"""
AIena Rank Tracker v1.3
Monitora posizioni Google + check tecnici SEO + backlink discovery per aiena.it.
Usa requests + BeautifulSoup — nessun browser headless richiesto.
Backlink: citation search multi-engine + verifica HTTP (nessun false positive).

Storico rank:     /home/pinky/.pinkybot/data/aiena_ranks.json
Storico backlink: /home/pinky/.pinkybot/data/aiena_backlinks.json

Usage:
    python3 aiena_rank_tracker.py              # run completo
    python3 aiena_rank_tracker.py --test       # test brand keyword + tech + backlink
    python3 aiena_rank_tracker.py --techonly   # solo check tecnici + backlink
    python3 aiena_rank_tracker.py --keyword "aiena investigativa"
    python3 aiena_rank_tracker.py --report     # mostra storico senza ricerche
"""

import json
import sys
import time
import random
import argparse
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Configurazione ────────────────────────────────────────────────────────────

TARGET_DOMAIN = "aiena.it"
DATA_FILE = Path("/home/pinky/.pinkybot/data/aiena_ranks.json")
MAX_RESULTS = 30     # Startpage restituisce ~10/pagina, 30 = 3 pagine
DELAY_MIN = 8        # secondi tra ricerche
DELAY_MAX = 15
ALERT_DELTA = 2      # segnala variazioni >= ±2 posizioni

# Keyword ottimizzate per aiena.it — brand + articoli pubblicati
KEYWORDS = [
    # Brand
    "aiena investigativa",
    "aiena inchieste AI",
    "aiena.it",
    # Articoli pubblicati
    "M5S Casaleggio Philip Morris",
    "appalti sanitari corruzione IA giornalismo",
    # Topic generali dove aiena.it punta a rankare
    "giornalismo investigativo intelligenza artificiale italia",
    "inchieste AI italia corruzione",
    "AI giornalismo investigativo italiano",
]

# ── Storico JSON ──────────────────────────────────────────────────────────────

def load_history() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            print("⚠️  Storico corrotto o illeggibile — reset")
            return {}
    return {}


def save_history(history: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_yesterday_position(history: dict, keyword: str) -> Optional[int]:
    yesterday = str(date.today() - timedelta(days=1))
    return history.get(yesterday, {}).get(keyword, {}).get("position")


# ── Startpage (Google proxy) ──────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


def _startpage_page(keyword: str, page: int = 0) -> list[str]:
    """Recupera una pagina di risultati Startpage. Ritorna lista URL."""
    resp = requests.get(
        "https://www.startpage.com/sp/search",
        params={"q": keyword, "language": "italiano", "cat": "web", "page": page},
        headers=_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    urls = []
    for a in soup.select(".result-link"):
        href = a.get("href", "").strip()
        if href.startswith("http") and "startpage" not in href:
            urls.append(href)
    return urls


def search_startpage(keyword: str, max_results: int = MAX_RESULTS) -> Optional[list[str]]:
    """Cerca su Startpage (Google proxy). Gestisce paginazione. Ritorna URL organici."""
    all_urls: list[str] = []
    seen: set[str] = set()

    for page_num in range(3):  # max 3 pagine × 10 = 30 risultati
        try:
            urls = _startpage_page(keyword, page_num)
            if not urls:
                break
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)
            if len(all_urls) >= max_results:
                break
            if len(urls) < 10:
                break  # ultima pagina
            time.sleep(random.uniform(2, 4))
        except requests.RequestException as e:
            print(f"  ⚠️  Startpage errore pagina {page_num}: {e}")
            if page_num == 0:
                return None  # fallback
            break

    return all_urls[:max_results] if all_urls else None


# ── DuckDuckGo HTML (fallback) ────────────────────────────────────────────────

def search_duckduckgo(keyword: str, max_results: int = MAX_RESULTS) -> Optional[list[str]]:
    """Fallback: DuckDuckGo HTML. Meno risultati ma senza CAPTCHA."""
    all_urls: list[str] = []
    seen: set[str] = set()

    try:
        for offset in [0, 30]:  # 2 pagine DDG
            resp = requests.post(
                "https://html.duckduckgo.com/html/",
                data={"q": keyword, "s": str(offset)} if offset else {"q": keyword},
                headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            found = 0
            for result in soup.select(".result"):
                # Salta ads
                if result.select_one(".badge--ad"):
                    continue
                url_span = result.select_one(".result__url")
                if url_span:
                    raw = url_span.get_text(strip=True)
                    url = raw if raw.startswith("http") else f"https://{raw}"
                    if url not in seen:
                        seen.add(url)
                        all_urls.append(url)
                        found += 1
            if found == 0:
                break
            time.sleep(random.uniform(2, 4))
    except requests.RequestException as e:
        print(f"  ❌ DuckDuckGo errore: {e}")

    return all_urls[:max_results] if all_urls else None


# ── Core: trova posizione aiena.it ────────────────────────────────────────────

def find_target(urls: list[str]) -> tuple[Optional[int], Optional[str]]:
    """Trova aiena.it nella lista URL. Ritorna (posizione_1based, url) o (None, None)."""
    for i, url in enumerate(urls, 1):
        if TARGET_DOMAIN in url:
            return i, url
    return None, None


def check_keyword(keyword: str) -> dict:
    """
    Controlla posizione per una keyword.
    Prova Startpage (Google proxy), poi DDG se fallisce.
    """
    # Tentativo 1: Startpage
    print(f"  🔍 Startpage: {keyword!r}")
    urls = search_startpage(keyword)

    if not urls:
        print(f"  🔄 Fallback DuckDuckGo")
        urls = search_duckduckgo(keyword)

    if not urls:
        return {
            "position": None,
            "url": None,
            "source": "error",
            "checked": 0,
        }

    pos, url = find_target(urls)
    source = "startpage" if urls else "duckduckgo"

    if pos:
        print(f"  ✅ pos {pos}/{len(urls)}: {url}")
    else:
        print(f"  — non in top {len(urls)}")

    return {
        "position": pos,
        "url": url,
        "source": source,
        "checked": len(urls),
    }


# ── Output e report ───────────────────────────────────────────────────────────

def _delta_str(today: Optional[int], yesterday: Optional[int]) -> str:
    if today is None and yesterday is None:
        return "—"
    if today is None:
        return "DROPPED"
    if yesterday is None:
        return "NEW"
    d = yesterday - today
    if d > 0:
        return f"↑{d}"
    elif d < 0:
        return f"↓{abs(d)}"
    return "="


def print_table(results: dict, history: dict) -> None:
    print("\n" + "=" * 72)
    print(f"📊 AIena.it — Posizioni | {date.today()}")
    print("=" * 72)
    print(f"{'Keyword':<44} {'Oggi':>6} {'Ieri':>6} {'Delta':>8} {'Src':>6}")
    print("-" * 72)
    for kw, data in results.items():
        pos = data.get("position")
        yday = get_yesterday_position(history, kw)
        delta = _delta_str(pos, yday)
        pos_str = str(pos) if pos else "—"
        yday_str = str(yday) if yday else "—"
        kw_s = (kw[:43] + "…") if len(kw) > 44 else kw
        src = data.get("source", "?")[:6]
        print(f"{kw_s:<44} {pos_str:>6} {yday_str:>6} {delta:>8} {src:>6}")
    print("=" * 72)


def build_telegram_section(results: dict, history: dict) -> str:
    """
    Genera testo per il report Telegram giornaliero di SEO Pro.
    Mostra top 3 per visibilità + variazioni significative.
    """
    lines = ["📊 AIena — Posizioni Google"]

    # Top 3 per posizione (le migliori)
    ranked = sorted(
        [(kw, d["position"]) for kw, d in results.items() if d.get("position")],
        key=lambda x: x[1],
    )

    if ranked:
        for kw, pos in ranked[:3]:
            yday = get_yesterday_position(history, kw)
            delta = _delta_str(pos, yday)
            lines.append(f"  #{pos} {kw[:36]} {delta}")
    else:
        lines.append("  Nessuna keyword in top 30 oggi")

    # Variazioni significative
    top3_kws = {kw for kw, _ in ranked[:3]}
    alerts = []
    for kw, data in results.items():
        if kw in top3_kws:
            continue
        pos = data.get("position")
        yday = get_yesterday_position(history, kw)
        if pos and yday:
            d = yday - pos
            if abs(d) >= ALERT_DELTA:
                sym = f"↑{d}" if d > 0 else f"↓{abs(d)}"
                alerts.append(f"  ⚡ {kw[:36]}: {yday}→{pos} ({sym})")
        elif pos and not yday:
            alerts.append(f"  🆕 NUOVO: {kw[:36]} — pos {pos}")
        elif not pos and yday:
            alerts.append(f"  ⛔ DROPPED: {kw[:36]} (era pos {yday})")

    if alerts:
        lines.append("  Variazioni:")
        lines.extend(alerts)

    not_found = [kw for kw, d in results.items() if not d.get("position")]
    if not_found:
        lines.append(f"  Non in top30: {len(not_found)}/{len(results)} keyword")

    return "\n".join(lines)


# ── Check Tecnici SEO ─────────────────────────────────────────────────────────

BASE_URL = "https://www.aiena.it"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
SAMPLE_ARTICLES = [
    f"{BASE_URL}/articles/m5s-casaleggio-philip-morris.html",
    f"{BASE_URL}/articles/appalti-sanitari-gare-che-non-tornano.html",
    BASE_URL + "/",
]
SPEED_THRESHOLD = 3.0  # secondi — segnala se >3s


def _fetch(url: str, timeout: int = 15) -> tuple[Optional[requests.Response], float]:
    """Scarica URL, ritorna (response, elapsed_seconds). Response = None su errore."""
    try:
        t0 = time.monotonic()
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        elapsed = time.monotonic() - t0
        return resp, elapsed
    except requests.RequestException as e:
        return None, 0.0


def check_robots() -> dict:
    """Verifica robots.txt: accessibile, Googlebot non bloccato, sitemap dichiarata."""
    result = {"ok": False, "sitemap_url": None, "issues": []}
    resp, elapsed = _fetch(ROBOTS_URL)

    if not resp or resp.status_code != 200:
        code = resp.status_code if resp else "timeout"
        result["issues"].append(f"robots.txt HTTP {code} — non accessibile")
        return result

    content = resp.text

    # Verifica che Googlebot non sia bloccato
    lines = content.lower().splitlines()
    in_googlebot_block = False
    googlebot_disallowed = []
    for line in lines:
        line = line.strip()
        if line.startswith("user-agent:"):
            agent = line.split(":", 1)[1].strip()
            in_googlebot_block = (agent in ("googlebot", "*"))
        elif in_googlebot_block and line.startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path in ("/", ""):
                googlebot_disallowed.append(path if path else "(vuoto)")

    if googlebot_disallowed:
        result["issues"].append(f"Googlebot BLOCCATO: Disallow: {googlebot_disallowed[0]}")

    # Estrai URL sitemap
    for line in content.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_url = line.split(":", 1)[1].strip()
            result["sitemap_url"] = sitemap_url
            break

    if not result["sitemap_url"]:
        result["issues"].append("Sitemap non dichiarata in robots.txt")

    result["ok"] = len(result["issues"]) == 0
    return result


def check_sitemap(sitemap_url: Optional[str]) -> dict:
    """Scarica sitemap, verifica XML valido, conta URL."""
    result = {"ok": False, "url_count": 0, "issues": []}

    if not sitemap_url:
        result["issues"].append("URL sitemap non disponibile (vedi robots check)")
        return result

    resp, elapsed = _fetch(sitemap_url)

    if not resp or resp.status_code != 200:
        code = resp.status_code if resp else "timeout"
        result["issues"].append(f"Sitemap HTTP {code} — non accessibile")
        return result

    # Verifica XML
    try:
        soup = BeautifulSoup(resp.text, "xml")
        urls = soup.find_all("loc")
        result["url_count"] = len(urls)
        if result["url_count"] == 0:
            result["issues"].append("Sitemap vuota — 0 URL trovati")
    except Exception as e:
        result["issues"].append(f"Sitemap XML non parsabile: {e}")
        return result

    result["ok"] = len(result["issues"]) == 0
    return result


def check_canonical_and_meta(urls: list[str]) -> dict:
    """
    Per ogni URL campionato: verifica canonical self-referencing,
    presenza e lunghezza meta title e description.
    """
    import re
    results = {}

    for url in urls:
        entry = {"ok": True, "issues": [], "title_len": None, "desc_len": None, "canonical": None}
        resp, elapsed = _fetch(url)

        if not resp or resp.status_code != 200:
            code = resp.status_code if resp else "timeout"
            entry["ok"] = False
            entry["issues"].append(f"HTTP {code}")
            results[url] = entry
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Canonical
        canon_tag = soup.find("link", rel="canonical")
        canon_href = canon_tag.get("href", "").strip() if canon_tag else None
        entry["canonical"] = canon_href

        if not canon_href:
            entry["ok"] = False
            entry["issues"].append("canonical MANCANTE")
        else:
            # Self-referencing: canonical deve puntare all'URL stesso (o variante www/no-www)
            url_clean = url.rstrip("/")
            canon_clean = canon_href.rstrip("/")
            # Accetta sia www che no-www della stessa pagina
            if not (url_clean.replace("www.", "") == canon_clean.replace("www.", "") or
                    "aiena.it" in canon_href):
                entry["ok"] = False
                entry["issues"].append(f"canonical non self-referencing: {canon_href[:60]}")

        # Meta title
        title_tag = soup.find("title")
        if not title_tag or not title_tag.get_text(strip=True):
            entry["ok"] = False
            entry["issues"].append("title MANCANTE")
        else:
            tlen = len(title_tag.get_text(strip=True))
            entry["title_len"] = tlen
            if tlen < 30:
                entry["issues"].append(f"title troppo corto ({tlen} chr)")
            elif tlen > 70:
                entry["issues"].append(f"title troppo lungo ({tlen} chr, max 70)")

        # Meta description
        desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        if not desc_tag or not desc_tag.get("content", "").strip():
            entry["ok"] = False
            entry["issues"].append("meta description MANCANTE")
        else:
            dlen = len(desc_tag.get("content", "").strip())
            entry["desc_len"] = dlen
            if dlen < 80:
                entry["issues"].append(f"desc troppo corta ({dlen} chr)")
            elif dlen > 165:
                entry["issues"].append(f"desc troppo lunga ({dlen} chr, max 165)")

        results[url] = entry

    return results


def check_speed() -> dict:
    """Misura tempo risposta homepage e primo articolo. Segnala se >SPEED_THRESHOLD."""
    urls_to_check = [
        BASE_URL + "/",
        SAMPLE_ARTICLES[0],  # articolo recente
    ]
    result = {"ok": True, "timings": {}, "issues": []}

    for url in urls_to_check:
        resp, elapsed = _fetch(url, timeout=20)
        elapsed_rounded = round(elapsed, 2)
        result["timings"][url] = elapsed_rounded

        if not resp or resp.status_code != 200:
            code = resp.status_code if resp else "timeout"
            result["ok"] = False
            result["issues"].append(f"HTTP {code}: {url}")
        elif elapsed > SPEED_THRESHOLD:
            result["ok"] = False
            result["issues"].append(f"Lento ({elapsed_rounded}s > {SPEED_THRESHOLD}s): {url}")

    return result


def run_tech_checks() -> dict:
    """Esegue tutti i check tecnici SEO. Ritorna dict con risultati strutturati."""
    print("\n🔧 Check tecnici aiena.it...")

    # 1. robots.txt
    print("  [1/4] robots.txt...", end=" ", flush=True)
    robots = check_robots()
    print("✅" if robots["ok"] else f"⚠️  {robots['issues']}")

    # 2. Sitemap
    print("  [2/4] sitemap...", end=" ", flush=True)
    sitemap = check_sitemap(robots.get("sitemap_url"))
    print(f"✅ {sitemap['url_count']} URL" if sitemap["ok"] else f"⚠️  {sitemap['issues']}")

    # 3-4. Canonical + meta
    print("  [3/4] canonical + meta tag (3 URL)...")
    meta_checks = check_canonical_and_meta(SAMPLE_ARTICLES)
    for url, entry in meta_checks.items():
        label = url.split("/")[-1] or "homepage"
        status = "✅" if entry["ok"] else f"⚠️  {entry['issues']}"
        print(f"    {label}: {status}")

    # 5. Speed
    print("  [4/4] velocità...", end=" ", flush=True)
    speed = check_speed()
    timing_str = " | ".join(f"{v}s" for v in speed["timings"].values())
    print(f"✅ {timing_str}" if speed["ok"] else f"⚠️  {speed['issues']}")

    return {
        "robots": robots,
        "sitemap": sitemap,
        "meta": meta_checks,
        "speed": speed,
    }


def format_tech_section(tech: dict) -> str:
    """Genera sezione '🔧 AIena — Check Tecnico' per il report Telegram."""
    lines = ["🔧 AIena — Check Tecnico"]

    robots = tech["robots"]
    sitemap = tech["sitemap"]
    speed = tech["speed"]
    meta = tech["meta"]

    # robots.txt
    if robots["ok"]:
        lines.append(f"  robots.txt ✅ | sitemap: {robots.get('sitemap_url', 'N/A')}")
    else:
        lines.append(f"  robots.txt ⚠️  {'; '.join(robots['issues'])}")

    # Sitemap
    if sitemap["ok"]:
        lines.append(f"  sitemap ✅ {sitemap['url_count']} URL")
    else:
        lines.append(f"  sitemap ⚠️  {'; '.join(sitemap['issues'])}")

    # Meta/canonical (mostra solo problemi)
    meta_issues = []
    for url, entry in meta.items():
        if not entry["ok"]:
            label = url.split("/")[-1] or "homepage"
            meta_issues.append(f"{label}: {', '.join(entry['issues'])}")
    if meta_issues:
        lines.append(f"  meta/canonical ⚠️  {' | '.join(meta_issues)}")
    else:
        lines.append("  canonical + meta ✅ (3 URL OK)")

    # Speed
    timings = speed["timings"]
    t_values = list(timings.values())
    if speed["ok"]:
        timing_str = " | ".join(f"{v}s" for v in t_values)
        lines.append(f"  velocità ✅ {timing_str}")
    else:
        lines.append(f"  velocità ⚠️  {'; '.join(speed['issues'])}")

    return "\n".join(lines)


def has_critical_tech_issues(tech: dict) -> list[str]:
    """Ritorna lista problemi critici (richiedono notifica immediata)."""
    critical = []
    robots = tech["robots"]
    sitemap = tech["sitemap"]
    meta = tech["meta"]

    # robots blocca Googlebot
    for issue in robots["issues"]:
        if "BLOCCATO" in issue or "non accessibile" in issue:
            critical.append(f"🚨 ROBOTS: {issue}")

    # Sitemap down
    for issue in sitemap["issues"]:
        if "non accessibile" in issue or "HTTP" in issue:
            critical.append(f"🚨 SITEMAP: {issue}")

    # Canonical assente su articoli (non homepage)
    for url, entry in meta.items():
        if "homepage" not in url and "/" != url.rstrip("/").split("/")[-1]:
            for issue in entry["issues"]:
                if "canonical MANCANTE" in issue:
                    label = url.split("/")[-1]
                    critical.append(f"🚨 CANONICAL assente: {label}")

    return critical


# ── Backlink Discovery ───────────────────────────────────────────────────────

BACKLINKS_FILE = Path("/home/pinky/.pinkybot/data/aiena_backlinks.json")

# Query per trovare candidati che citano aiena.it
CITATION_QUERIES = [
    '"aiena.it"',
    '"www.aiena.it"',
    'aiena.it inchiesta intelligenza artificiale',
]


def load_backlink_history() -> dict:
    if BACKLINKS_FILE.exists():
        try:
            with open(BACKLINKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_backlink_history(history: dict) -> None:
    BACKLINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BACKLINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _citation_search_ddg(query: str) -> dict[str, str]:
    """Cerca su DuckDuckGo HTML. Ritorna {url: title} dei candidati."""
    candidates = {}
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for r in soup.select(".result"):
            if r.select_one(".badge--ad"):
                continue
            url_span = r.select_one(".result__url")
            title_a = r.select_one(".result__a")
            if url_span:
                raw = url_span.get_text(strip=True)
                url = raw if raw.startswith("http") else f"https://{raw}"
                # Escludi aiena.it stesso e social/aggregatori rumorosi
                skip = ["aiena.it", "facebook.com", "twitter.com", "linkedin.com",
                        "instagram.com", "youtube.com", "t.co", "bit.ly"]
                if not any(s in url for s in skip):
                    title = title_a.get_text(strip=True) if title_a else ""
                    candidates[url] = title
    except requests.RequestException:
        pass
    return candidates


def _verify_backlink(url: str) -> tuple[bool, str]:
    """
    Fetch la pagina e verifica se contiene un <a href> verso aiena.it.
    Ritorna (found, anchor_text).
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=12, allow_redirects=True)
        if resp.status_code != 200:
            return False, ""
        # Cerca link ad aiena.it nel body HTML (veloce — regex prima di soup)
        import re
        if not re.search(r'aiena\.it', resp.text, re.IGNORECASE):
            return False, ""  # Fast exit — aiena.it non menzionato neanche come testo
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if "aiena.it" in a.get("href", ""):
                return True, a.get_text(strip=True)[:80]
        return False, ""
    except Exception:
        return False, ""


def _extract_domain(url: str) -> str:
    """Estrae dominio pulito da URL."""
    return url.replace("https://", "").replace("http://", "").split("/")[0].lower().strip()


def run_backlink_check() -> dict:
    """
    Esegue citation search su DDG, verifica HTTP ogni candidato.
    Ritorna dict con backlink verificati + delta rispetto a ieri.
    """
    print("\n🔗 Backlink discovery aiena.it...")

    # Raccogli candidati da tutte le query
    all_candidates: dict[str, str] = {}
    for q in CITATION_QUERIES:
        found = _citation_search_ddg(q)
        all_candidates.update(found)
        time.sleep(random.uniform(3, 5))

    print(f"  Candidati trovati: {len(all_candidates)} (da {len(CITATION_QUERIES)} query)")

    # Verifica HTTP — controlla backlink reali
    verified: dict[str, dict] = {}  # domain -> {url, anchor}
    seen_domains: set[str] = set()

    for url, title in list(all_candidates.items())[:25]:  # max 25 verifiche/giorno
        domain = _extract_domain(url)
        if domain in seen_domains:
            continue  # 1 verifica per dominio
        seen_domains.add(domain)

        has_link, anchor = _verify_backlink(url)
        if has_link:
            verified[domain] = {"url": url, "anchor": anchor, "title": title}
            print(f"  ✅ BACKLINK: {domain} | anchor: {anchor!r}")
        time.sleep(random.uniform(1, 2))

    # Carica storico e calcola delta
    bl_history = load_backlink_history()
    today_str = str(date.today())
    yesterday_str = str(date.today() - timedelta(days=1))

    today_domains = set(verified.keys())
    yesterday_domains = set(bl_history.get(yesterday_str, {}).keys())

    new_domains = today_domains - yesterday_domains
    lost_domains = yesterday_domains - today_domains

    # Salva
    bl_history[today_str] = {
        d: {"url": v["url"], "anchor": v["anchor"]}
        for d, v in verified.items()
    }
    save_backlink_history(bl_history)

    print(f"  Totale backlink verificati: {len(verified)} | NEW: {len(new_domains)} | LOST: {len(lost_domains)}")

    return {
        "verified": verified,
        "new": list(new_domains),
        "lost": list(lost_domains),
        "total": len(verified),
        "yesterday_total": len(yesterday_domains),
    }


def format_backlink_section(bl: dict) -> str:
    """Genera sezione '🔗 AIena — Backlink' per il report Telegram."""
    lines = ["🔗 AIena — Backlink"]
    verified = bl.get("verified", {})
    new = bl.get("new", [])
    lost = bl.get("lost", [])
    total = bl.get("total", 0)
    yday = bl.get("yesterday_total", 0)

    delta_str = f" (+{len(new)})" if new else (f" (-{len(lost)})" if lost else "")
    lines.append(f"  Totale domini linkanti: {total}{delta_str} (ieri: {yday})")

    if verified:
        # Top 5 domini
        lines.append("  Top domini:")
        for domain in list(verified.keys())[:5]:
            entry = verified[domain]
            anchor = entry.get("anchor", "")[:30]
            suffix = " 🆕" if domain in new else ""
            lines.append(f"    • {domain}{suffix} | {anchor}")
    else:
        lines.append("  Nessun backlink verificato ancora (sito nuovo)")

    if new:
        lines.append(f"  🆕 NEW: {', '.join(new[:5])}")
    if lost:
        lines.append(f"  ⚠️  LOST: {', '.join(lost[:5])}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AIena Rank Tracker")
    parser.add_argument("--test", action="store_true", help="Test su brand keyword")
    parser.add_argument("--techonly", action="store_true", help="Solo check tecnici")
    parser.add_argument("--keyword", type=str, help="Cerca keyword specifica")
    parser.add_argument("--report", action="store_true", help="Mostra ultimo storico")
    args = parser.parse_args()

    history = load_history()
    today_str = str(date.today())

    # Modalità --report: mostra storico senza fare ricerche
    if args.report:
        if today_str in history:
            print_table(history[today_str], history)
            print("\n--- SEZIONE TELEGRAM ---")
            print(build_telegram_section(history[today_str], history))
        else:
            print(f"Nessun dato per oggi ({today_str}).")
            if history:
                last_date = sorted(history.keys())[-1]
                print(f"Ultimo check: {last_date}")
                print_table(history[last_date], history)
        return

    print(f"\n🔍 AIena Rank Tracker v1.3 — {today_str}")
    print(f"Motore: Startpage (Google proxy) + DDG fallback | Target: {TARGET_DOMAIN}")
    print("-" * 60)

    # ── Check Tecnici (sempre eseguiti) ──
    tech = run_tech_checks()

    # Notifica immediata se ci sono problemi critici
    critical_issues = has_critical_tech_issues(tech)
    if critical_issues:
        print("\n🚨 PROBLEMI CRITICI RILEVATI — notifica a Satoshi...")
        _send_critical_alert(critical_issues)

    # ── Backlink Discovery (sempre eseguito) ──
    backlinks = run_backlink_check()

    # ── Rank Tracking (skip se --techonly) ──
    today_results = {}

    if not args.techonly:
        if args.test:
            keywords = ["aiena investigativa", "aiena inchieste AI"]
        elif args.keyword:
            keywords = [args.keyword]
        else:
            keywords = KEYWORDS

        print(f"\n🔍 Keyword check ({len(keywords)} keyword)...")

        for i, kw in enumerate(keywords):
            print(f"\n[{i+1}/{len(keywords)}] {kw}")
            today_results[kw] = check_keyword(kw)
            if i < len(keywords) - 1:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                print(f"  ⏳ {delay:.0f}s...")
                time.sleep(delay)

        # Salva storico
        history[today_str] = today_results
        save_history(history)
        print(f"\n💾 Salvato: {DATA_FILE}")

        # Output tabella rank
        print_table(today_results, history)

    # ── Report Telegram completo ──
    print("\n--- SEZIONE REPORT TELEGRAM ---")
    if today_results:
        print(build_telegram_section(today_results, history))
    print(format_tech_section(tech))
    print(format_backlink_section(backlinks))
    print("-------------------------------\n")

    return {"ranks": today_results, "tech": tech, "backlinks": backlinks}


def _send_critical_alert(issues: list[str]) -> None:
    """
    Invia notifica critica a Satoshi via API PinkyBot.
    Usato per anomalie gravi: robots blocca, sitemap down, canonical assente.
    """
    try:
        msg = "⚠️ AIena SEO — PROBLEMA CRITICO:\n" + "\n".join(issues)
        resp = requests.post(
            "http://localhost:8888/agents/satoshi/message",
            json={"message": msg, "content_type": "alert"},
            timeout=10,
        )
        if resp.status_code == 200:
            print("  ✅ Alert inviato a Satoshi")
        else:
            print(f"  ⚠️  Alert API risposta {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Impossibile inviare alert: {e}")
        # Fallback: scrivi in file di log
        alert_log = Path("/home/pinky/.pinkybot/data/aiena_tech_alerts.log")
        with open(alert_log, "a") as f:
            f.write(f"\n[{date.today()}] CRITICAL:\n" + "\n".join(issues) + "\n")


if __name__ == "__main__":
    main()
