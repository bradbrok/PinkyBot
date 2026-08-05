#!/usr/bin/env python3
"""
AIena Ticker Updater — aggiorna il ticker "In corso" di aiena.it
con gli hashtag della prossima indagine.

Uso:
  python3 aiena_update_ticker.py --titolo "Titolo indagine" --hashtags "#tag1 #tag2 #tag3"
  python3 aiena_update_ticker.py  # legge da /var/www/aiena.it/data/next-investigation.json
"""
import fcntl, json, os, re, sys, argparse, pathlib, tempfile

INDEX_HTML = pathlib.Path("/var/www/aiena.it/index.html")
CONFIG_JSON = pathlib.Path("/var/www/aiena.it/data/next-investigation.json")
INDEX_LOCK  = pathlib.Path("/tmp/aiena_index_html.lock")


def _atomic_write(path: pathlib.Path, content: str, mode: int = 0o644) -> None:
    """Scrive atomicamente: tempfile nella stessa dir + os.replace.

    mkstemp crea sempre 0600 e ignora l'umask, e os.replace preserva i permessi
    del tempfile: senza il chmod esplicito i file serviti da nginx (www-data)
    diventano illeggibili -> 403. Usare mode=0o600 per i file sotto data/, che
    NON devono essere serviti via HTTP (vedi data/.htaccess: Deny from all).
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_ticker_html(hashtags: list[str]) -> str:
    items = "\n          ".join(
        f'<span class="ticker-item">{tag}</span>' for tag in hashtags
    )
    return f"""      <div class="ticker-items">
        <div class="ticker-track">
          {items}
        </div>
        <div class="ticker-track" aria-hidden="true">
          {items}
        </div>
      </div>"""


def update_index(hashtags: list[str]) -> None:
    """Aggiorna il ticker in index.html con file lock per evitare race condition."""
    with open(INDEX_LOCK, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            html = INDEX_HTML.read_text(encoding="utf-8")
            new_block = build_ticker_html(hashtags)
            pattern = r'<div class="ticker-items">.*?</div>\s*</div>\s*</div>'
            replacement = new_block + "\n    </div>\n  </div>"
            new_html, n = re.subn(pattern, replacement, html, count=1, flags=re.DOTALL)
            if n == 0:
                print("ERRORE: pattern ticker-items non trovato in index.html")
                sys.exit(1)
            _atomic_write(INDEX_HTML, new_html)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
    print(f"✅ Ticker aggiornato con {len(hashtags)} hashtag")


def update_config(titolo: str, hashtags: list[str]) -> None:
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(CONFIG_JSON, json.dumps({
        "titolo": titolo,
        "hashtags": hashtags
    }, ensure_ascii=False, indent=2), mode=0o600)
    print(f"✅ Config aggiornata: {CONFIG_JSON}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--titolo", help="Titolo indagine")
    parser.add_argument("--hashtags", help="Hashtag separati da spazio (es: 'Tag1 Tag2')")
    args = parser.parse_args()

    if args.hashtags:
        hashtags = [h.lstrip("#") for h in args.hashtags.split()]
        titolo = args.titolo or "Prossima indagine"
    else:
        # Leggi da config
        config = json.loads(CONFIG_JSON.read_text())
        hashtags = config["hashtags"]
        titolo = config.get("titolo", "")

    print(f"Indagine: {titolo}")
    print(f"Hashtag: {hashtags}")

    update_config(titolo, hashtags)
    update_index(hashtags)


if __name__ == "__main__":
    main()
