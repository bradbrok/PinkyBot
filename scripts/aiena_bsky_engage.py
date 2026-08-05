#!/usr/bin/env python3
"""
AIena Bluesky Engagement — comportamento social autonomo per @aiena-it.bsky.social
Eseguito ogni ora via crontab. Cerca, mette like, segue, risponde, posta.
"""

import fcntl
import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from atproto import Client, models

from aiena_secrets import _load_secrets

# Pipeline file — per post originali basati sull'indagine reale
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")

# =============================================================================
# CONFIGURATION
# =============================================================================

HANDLE = "aiena-it.bsky.social"
APP_PASS = _load_secrets().get("BSKY_APP_PASS", "")

STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_bsky_state.json")
BSKY_POSTS_CACHE = Path("/home/pinky/.pinkybot/data/aiena_bsky_posts_cache.json")

# Search topics (Italian public affairs / investigations)
SEARCH_TOPICS = [
    "appalti italia",
    "PNRR corruzione",
    "corruzione sanità",
    "ANAC appalti",
    "Corte dei Conti",
    "trattativa diretta",
    "affidamento diretto",
    "gare pubbliche",
    "trasparenza PA",
    "anticorruzione",
    "spesa pubblica",
    "fondi europei italia",
]

# Institutional/journalist accounts to prioritize (partial match on handle)
TRUSTED_HANDLES = [
    "anaborsa", "anaborsa.bsky", "anaborsa.it",
    "corfrankfurt", "corriere",
    "repubblica", "ilsole24ore", "ansa",
    "fanpage", "ilfattoquotidiano", "wired",
    "transparency", "openpolis", "info.data",
    "anac", "cortedeiconti", "mef", "anticorruzione",
]

# Limits per run (MASSIMI — non obiettivi da raggiungere)
MAX_LIKES_PER_RUN = 8
MAX_FOLLOWS_PER_RUN = 3
MAX_REPLIES_PER_RUN = 1
ORIGINAL_POST_COOLDOWN_HOURS = 12  # Non postare più di 1 originale ogni N ore

# Like scoring thresholds — soglia alta, meglio non fare nulla che fare a caso
LIKE_THRESHOLD = 3  # Minimum score to like a post (was 2)

# Qualità minima: se non trova almeno N post di qualità, salta il like phase
MIN_QUALITY_POSTS_TO_ACT = 3

# Reply thresholds — molto selettiva
MIN_LIKES_FOR_REPLY = 30  # Only reply to posts with this many likes (was 10)

# Probabilità di saltare un run del tutto (AIena "sta solo leggendo")
SKIP_RUN_PROBABILITY = 0.35  # 35% delle volte non fa nulla — è normale

# Timing
ACTIVE_HOURS = (8, 22)  # Rome timezone
MIN_DELAY_SECONDS = 2
MAX_DELAY_SECONDS = 5

# AIena's voice templates for original posts
# REGOLA: niente numeri inventati, niente dati random. Solo quello che sta davvero facendo.
ORIGINAL_POST_TEMPLATES = [
    "Sto lavorando su appalti sanitari italiani. Pattern ricorrente: affidamenti diretti ripetuti agli stessi soggetti. I documenti ci sono. Prossimamente.",
    "Incrocio dati PNRR con i rendiconti comunali. Molti comuni non hanno ancora rendicontato fondi in scadenza a giugno 2026. Ne parlero'.",
    "Analisi in corso su gare pubbliche. Quando manca la concorrenza, manca anche il controllo. Sto documentando.",
    "Continuo a leggere. Continuo a verificare. Quando ho qualcosa di solido, lo pubblico. Sempre con le fonti.",
    "Patrimonio pubblico. Aste. Prezzi. Acquirenti. Una storia che torna spesso. Sto guardando.",
]

# AIena's voice templates for replies
REPLY_TEMPLATES = [
    "Questo dato coincide con quanto emerge dai contratti {source}. Prossimamente.",
    "Ho verificato. I numeri confermano.",
    "Interessante. Sto incrociando con i dati {source}.",
    "Lo schema e' noto. Lo documento da mesi.",
    "Dati utili. Li aggiungo all'analisi in corso.",
    "Confermo. Ho trovato pattern simili in altre regioni.",
]

# =============================================================================
# STATE MANAGEMENT
# =============================================================================


def load_state() -> dict:
    """Load engagement state from JSON file."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Ensure all keys exist
            data.setdefault("already_liked", [])
            data.setdefault("already_followed", [])
            data.setdefault("already_replied", [])
            data.setdefault("last_original_post", None)
            data.setdefault("last_original_text", None)
            data.setdefault("diary_post_slug", None)
            data.setdefault("diary_post_index", -1)
            data.setdefault("posted_research_notes", [])
            return data
        except json.JSONDecodeError:
            pass
    return {
        "already_liked": [],
        "already_followed": [],
        "already_replied": [],
        "last_original_post": None,
        "last_original_text": None,
        "diary_post_slug": None,
        "diary_post_index": -1,
        "posted_research_notes": [],
    }


def save_state(state: dict):
    """Save engagement state to JSON file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep only last 1000 entries to prevent unbounded growth
    state["already_liked"] = state["already_liked"][-1000:]
    state["already_followed"] = state["already_followed"][-500:]
    state["already_replied"] = state["already_replied"][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _append_posts_cache(new_posts: list[dict]):
    """
    Appende post (uri + text) alla cache condivisa con il signal extractor.
    Deduplicazione per URI. Rolling window: ultimi 500 post.
    Non rompe nulla se fallisce — silenzioso.
    """
    if not new_posts:
        return
    try:
        existing: list[dict] = []
        if BSKY_POSTS_CACHE.exists():
            try:
                existing = json.loads(BSKY_POSTS_CACHE.read_text()).get("posts", [])
            except Exception:
                existing = []

        seen_uris = {p["uri"] for p in existing if p.get("uri")}
        for p in new_posts:
            if p.get("uri") and p["uri"] not in seen_uris:
                existing.append({
                    "uri": p["uri"],
                    "text": p.get("text", ""),
                    "added_at": datetime.now(ZoneInfo("Europe/Rome")).isoformat(),
                })
                seen_uris.add(p["uri"])

        # Rolling window
        existing = existing[-500:]
        BSKY_POSTS_CACHE.write_text(json.dumps({"posts": existing}, indent=2))
    except Exception:
        pass  # Non bloccare mai bsky_engage per un errore di cache


# =============================================================================
# UTILITIES
# =============================================================================


def log(action: str, description: str):
    """Log an action with timestamp."""
    now = datetime.now(ZoneInfo("Europe/Rome"))
    print(f"[{now.strftime('%H:%M')}] {action}: {description}")


def is_active_hour() -> bool:
    """Check if current hour is within active range (Rome timezone)."""
    now = datetime.now(ZoneInfo("Europe/Rome"))
    return ACTIVE_HOURS[0] <= now.hour < ACTIVE_HOURS[1]


def random_delay():
    """Add random delay between actions to appear human."""
    delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
    time.sleep(delay)


def score_post(post) -> int:
    """
    Score a post for quality/relevance.
    +1 if has numbers/dates
    +1 if has links
    +1 if from trusted account
    +1 if >5 words
    +1 if mentions specific entities (ANAC, Corte dei Conti, etc.)
    """
    score = 0
    text = post.record.text if hasattr(post.record, "text") else ""
    handle = post.author.handle.lower() if hasattr(post.author, "handle") else ""

    # Has numbers or dates
    if re.search(r'\d{2,}', text):
        score += 1

    # Has links
    if "http" in text or hasattr(post.record, "embed"):
        score += 1

    # From trusted account
    if any(t in handle for t in TRUSTED_HANDLES):
        score += 1

    # Substantial content (>5 words)
    if len(text.split()) > 5:
        score += 1

    # Mentions specific entities
    entities = ["anac", "corte dei conti", "consip", "pnrr", "anticorruzione", "trasparenza"]
    if any(e in text.lower() for e in entities):
        score += 1

    return score


def is_trusted_account(handle: str) -> bool:
    """Check if account is institutional/journalist."""
    handle_lower = handle.lower()
    return any(t in handle_lower for t in TRUSTED_HANDLES)


def should_follow(profile) -> bool:
    """
    Decide if we should follow this account.
    Follow if: journalist, investigator, or institutional.
    """
    handle = profile.handle.lower() if hasattr(profile, "handle") else ""
    display = (profile.display_name or "").lower()
    bio = (profile.description or "").lower()

    # Already following?
    if hasattr(profile, "viewer") and profile.viewer and profile.viewer.following:
        return False

    # Trusted handles
    if any(t in handle for t in TRUSTED_HANDLES):
        return True

    # Keywords in bio
    follow_keywords = [
        "giornalista", "journalist", "reporter", "investigat",
        "anticorruzione", "trasparenza", "open data", "data journalist",
        "anac", "corte dei conti", "magistrat", "procur",
    ]
    if any(k in bio or k in display for k in follow_keywords):
        return True

    return False


def can_post_original(state: dict) -> bool:
    """Check if enough time has passed since last original post."""
    last_post = state.get("last_original_post")
    if not last_post:
        return True

    last_time = datetime.fromisoformat(last_post)
    now = datetime.now(ZoneInfo("Europe/Rome"))
    cooldown = timedelta(hours=ORIGINAL_POST_COOLDOWN_HOURS)

    return now - last_time.replace(tzinfo=ZoneInfo("Europe/Rome")) > cooldown


def load_current_investigation() -> dict | None:
    """Legge l'indagine corrente da pipeline.json."""
    try:
        data = json.loads(PIPELINE_JSON.read_text())
        investigations = data.get("investigations", [])
        return investigations[0] if investigations else None
    except Exception:
        return None


def generate_original_post(state: dict) -> str:
    """Generate an original post in AIena's voice.
    Cicla tutti i diary entries della current_investigation, uno alla volta.
    Tiene traccia dell'indice corrente in state['diary_post_index'].
    """
    inv = load_current_investigation()

    if inv:
        title = inv.get("title", "")
        notes = inv.get("research_notes", [])
        diary_entries = inv.get("diary", [])

        if diary_entries:
            inv_slug = inv.get("slug", title)
            last_slug = state.get("diary_post_slug")

            # Reset posted set se l'indagine è cambiata
            if last_slug != inv_slug:
                state["diary_posted_indices"] = []
                state["diary_post_slug"] = inv_slug

            posted_indices = set(state.get("diary_posted_indices", []))
            unposted = [i for i in range(len(diary_entries)) if i not in posted_indices]

            if unposted:
                # Posta il prossimo entry non ancora pubblicato (in ordine)
                next_idx = unposted[0]
                entry = diary_entries[next_idx]
                note = re.sub(r'<[^>]+>', '', entry.get("text", "")).strip()

                if note and len(note) > 20:
                    # MAX_NOTE: 300 (Bluesky limit) - len(" Sto documentando.") = 282
                    # Target: 220 chars — taglia alla fine di una frase se possibile
                    MAX_NOTE = 220
                    if len(note) > MAX_NOTE:
                        cut = note[:MAX_NOTE]
                        last_sent = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
                        if last_sent > 80:
                            note = cut[:last_sent + 1]
                        else:
                            note = cut.rsplit(' ', 1)[0] + '…'

                    # Segna come postato
                    posted_indices.add(next_idx)
                    state["diary_posted_indices"] = list(posted_indices)
                    return f"{note} Sto documentando."
            # Se tutti i diary entry già postati → cade nel fallback (voce spontanea)

        # Usa una research note come spunto (traccia quali sono state pubblicate)
        # FIX: escludi note di arricchimento interno (ENRICHMENT, BOOST, etc.) — non adatte a post pubblici
        def _is_public_note(n: str) -> bool:
            skip_prefixes = ("[ENRICHMENT", "[BOOST", "[SIGNAL", "[KG-")
            return not any(n.strip().startswith(p) for p in skip_prefixes)

        public_notes = [n for n in notes if _is_public_note(n)]
        if public_notes:
            posted_notes = set(state.get("posted_research_notes", []))
            unposted_notes = [n for n in public_notes if n not in posted_notes]

            if unposted_notes:
                note = unposted_notes[0]
                if len(note) < 180:
                    posted_notes.add(note)
                    state["posted_research_notes"] = list(posted_notes)
                    return f"Indagine in corso: {title}. {note}. Prossimamente."

        # Fallback con titolo
        if title:
            return f"Sto lavorando su: {title}. Fonti verificate, documenti alla mano. Prossimamente."

    # Fallback assoluto: template statici
    return random.choice(ORIGINAL_POST_TEMPLATES)


def generate_reply(post_text: str) -> str:
    """Generate a reply in AIena's voice."""
    template = random.choice(REPLY_TEMPLATES)

    replacements = {
        "source": random.choice(["ANAC", "CONSIP 2023", "Corte dei Conti", "dati OpenPolis"]),
    }

    for key, value in replacements.items():
        template = template.replace("{" + key + "}", value)

    return template


# =============================================================================
# ENGAGEMENT ACTIONS
# =============================================================================


def search_posts(client: Client, query: str, limit: int = 25) -> list:
    """Search for posts matching query."""
    try:
        response = client.app.bsky.feed.search_posts(
            params={"q": query, "limit": limit, "lang": "it"}
        )
        return response.posts if response.posts else []
    except Exception as e:
        log("ERROR", f"Search failed for '{query}': {e}")
        return []


def do_likes(client: Client, state: dict) -> int:
    """Like high-quality posts. Returns count of new likes."""
    liked_uris = set(state["already_liked"])
    likes_count = 0
    _cache_batch: list[dict] = []  # Raccoglie post liked per cache

    # Quality gate: prima conta quanti post di qualità ci sono
    topics_sample = random.sample(SEARCH_TOPICS, min(3, len(SEARCH_TOPICS)))
    all_candidate_posts = []
    for topic in topics_sample:
        posts = search_posts(client, topic, limit=15)
        for post in posts:
            if post.uri not in liked_uris:
                if score_post(post) >= LIKE_THRESHOLD:
                    all_candidate_posts.append(post)

    if len(all_candidate_posts) < MIN_QUALITY_POSTS_TO_ACT:
        log("SKIP", f"Qualità insufficiente: solo {len(all_candidate_posts)} post sopra soglia (min {MIN_QUALITY_POSTS_TO_ACT}). Nessun like.")
        return 0

    for topic in topics_sample:
        if likes_count >= MAX_LIKES_PER_RUN:
            break

        posts = search_posts(client, topic, limit=15)

        for post in posts:
            if likes_count >= MAX_LIKES_PER_RUN:
                break

            uri = post.uri
            if uri in liked_uris:
                continue

            # Check if already liked by us
            if hasattr(post, "viewer") and post.viewer and post.viewer.like:
                liked_uris.add(uri)
                continue

            score = score_post(post)
            if score >= LIKE_THRESHOLD:
                try:
                    random_delay()
                    client.like(uri=uri, cid=post.cid)
                    liked_uris.add(uri)
                    likes_count += 1
                    author = post.author.handle
                    text_preview = (post.record.text[:50] + "...") if len(post.record.text) > 50 else post.record.text
                    log("LIKE", f"@{author}: {text_preview} (score={score})")
                    # Salva in cache per signal extractor
                    _cache_batch.append({"uri": uri, "text": post.record.text})
                except Exception as e:
                    log("ERROR", f"Like failed: {e}")

    state["already_liked"] = list(liked_uris)
    _append_posts_cache(_cache_batch)
    return likes_count


def do_follows(client: Client, state: dict) -> int:
    """Follow relevant accounts. Returns count of new follows."""
    followed_dids = set(state["already_followed"])
    follows_count = 0

    for topic in random.sample(SEARCH_TOPICS, min(2, len(SEARCH_TOPICS))):
        if follows_count >= MAX_FOLLOWS_PER_RUN:
            break

        posts = search_posts(client, topic, limit=20)

        for post in posts:
            if follows_count >= MAX_FOLLOWS_PER_RUN:
                break

            author = post.author
            did = author.did

            if did in followed_dids:
                continue

            if did == client.me.did:  # Don't follow self
                continue

            # Get full profile to check if we should follow
            try:
                profile = client.get_profile(actor=did)
                if should_follow(profile):
                    random_delay()
                    client.follow(subject=did)
                    followed_dids.add(did)
                    follows_count += 1
                    log("FOLLOW", f"@{author.handle}")
            except Exception as e:
                log("ERROR", f"Follow check/action failed: {e}")

    state["already_followed"] = list(followed_dids)
    return follows_count


def do_replies(client: Client, state: dict) -> int:
    """Reply to relevant posts. Returns count of replies."""
    replied_uris = set(state["already_replied"])
    replies_count = 0
    _cache_batch: list[dict] = []  # Raccoglie post replied per cache

    for topic in random.sample(SEARCH_TOPICS, min(2, len(SEARCH_TOPICS))):
        if replies_count >= MAX_REPLIES_PER_RUN:
            break

        posts = search_posts(client, topic, limit=15)

        for post in posts:
            if replies_count >= MAX_REPLIES_PER_RUN:
                break

            uri = post.uri
            if uri in replied_uris:
                continue

            # Only reply to popular posts or trusted accounts
            like_count = post.like_count if hasattr(post, "like_count") else 0
            is_trusted = is_trusted_account(post.author.handle)

            if like_count < MIN_LIKES_FOR_REPLY and not is_trusted:
                continue

            # Generate and send reply
            try:
                reply_text = generate_reply(post.record.text)

                # Build reply reference
                root_ref = models.create_strong_ref(post)
                parent_ref = models.create_strong_ref(post)

                random_delay()
                client.send_post(
                    text=reply_text,
                    reply_to=models.AppBskyFeedPost.ReplyRef(
                        root=root_ref,
                        parent=parent_ref,
                    )
                )

                replied_uris.add(uri)
                replies_count += 1
                author = post.author.handle
                log("REPLY", f"to @{author}: '{reply_text}'")
                # Salva in cache per signal extractor
                _cache_batch.append({"uri": uri, "text": post.record.text})

            except Exception as e:
                log("ERROR", f"Reply failed: {e}")

    state["already_replied"] = list(replied_uris)
    _append_posts_cache(_cache_batch)
    return replies_count


def do_original_post(client: Client, state: dict) -> bool:
    """Post an original observation. Returns True if posted."""
    if not can_post_original(state):
        log("SKIP", "Original post cooldown not elapsed")
        return False

    try:
        post_text = generate_original_post(state)

        # Deduplication: skip if text is identical to last posted text
        if post_text == state.get("last_original_text"):
            log("SKIP", "Testo identico all'ultimo post — nessun duplicato")
            return False

        random_delay()
        response = client.send_post(text=post_text)

        state["last_original_post"] = datetime.now(ZoneInfo("Europe/Rome")).isoformat()
        state["last_original_text"] = post_text

        rkey = response.uri.split("/")[-1]
        post_url = f"https://bsky.app/profile/{HANDLE}/post/{rkey}"
        log("POST", f"'{post_text}' -> {post_url}")
        return True

    except Exception as e:
        log("ERROR", f"Original post failed: {e}")
        return False


# =============================================================================
# MAIN
# =============================================================================


def run():
    """Main engagement loop."""
    # File locking: prevent concurrent cron processes
    lock_file = Path("/tmp/aiena_bsky_engage.lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = lock_file.open("w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        # Lock already held by another process
        log("SKIP", "Another instance is already running (lock held)")
        return

    try:
        log("START", "AIena Bluesky engagement run")

        # Check active hours
        if not is_active_hour():
            log("SKIP", "Outside active hours (08:00-22:00 Rome)")
            return

        # AIena può decidere di non fare nulla — sta leggendo, non è un bot meccanico
        if random.random() < SKIP_RUN_PROBABILITY:
            log("SKIP", "AIena sta solo osservando questo giro — nessuna azione")
            return

        # Load state
        state = load_state()

        # Login
        try:
            client = Client()
            client.login(HANDLE, APP_PASS)
            log("AUTH", f"Logged in as @{HANDLE}")
        except Exception as e:
            log("ERROR", f"Login failed: {e}")
            return

        # Engagement actions — solo se trova contenuto di qualità
        likes = do_likes(client, state)
        follows = do_follows(client, state)
        replies = do_replies(client, state)
        posted = do_original_post(client, state)

        # Save state
        save_state(state)

        # Summary
        log("DONE", f"Likes: {likes}, Follows: {follows}, Replies: {replies}, Original: {'yes' if posted else 'no'}")
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    run()
