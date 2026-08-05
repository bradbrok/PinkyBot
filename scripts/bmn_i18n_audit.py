#!/usr/bin/env python3
"""
Audit i18n drift su bitcoinmarket.net — SOLO DIAGNOSI, non modifica nulla.

Problema: gli elementi con data-i18n-html="KEY" vengono rimpiazzati a runtime
dal valore della chiave KEY in i18n/<lang>.js. Se l'HTML viene aggiornato ma la
chiave no, in produzione si continua a vedere il contenuto vecchio.

Lo script confronta, per ogni contenitore data-i18n-html, il contenuto HTML con
il valore della chiave nella lingua DELLA PAGINA e riporta le divergenze.

Uso: python3 i18n_audit.py [--root /var/www/bitcoinmarket.net] [--all-langs]
"""
import argparse
import difflib
import html
import json
import os
import re
import sys
from collections import defaultdict

LANGS = ("it", "en", "es", "de")

# ---------------------------------------------------------------- i18n parsing

KEY_RE = re.compile(r"""(['"])([A-Za-z0-9_.\-]+)\1\s*:\s*""")


def parse_i18n_file(path):
    """Estrae {chiave: valore} da un i18n/<lang>.js.

    I valori sono literal JS: template literal con backtick, oppure stringa
    singola/doppia con escape. Nessun eval: scansione carattere per carattere
    che rispetta gli escape, così un apostrofo escapato non tronca il valore.
    """
    src = open(path, encoding="utf-8").read()
    out = {}
    pos = 0
    while True:
        m = KEY_RE.search(src, pos)
        if not m:
            break
        key = m.group(2)
        i = m.end()
        if i >= len(src) or src[i] not in "`'\"":
            # non è una stringa (oggetto annidato, numero, funzione): salta
            pos = m.end()
            continue
        quote = src[i]
        i += 1
        buf = []
        while i < len(src):
            c = src[i]
            if c == "\\":
                buf.append(src[i:i + 2])
                i += 2
                continue
            if c == quote:
                break
            buf.append(c)
            i += 1
        raw = "".join(buf)
        # de-escape minimale, sufficiente per il confronto testuale
        val = (raw.replace("\\'", "'").replace('\\"', '"')
                  .replace("\\`", "`").replace("\\n", "\n").replace("\\\\", "\\"))
        # ultima occorrenza vince (come farebbe JS su chiavi duplicate)
        out[key] = val
        pos = i + 1
    return out


# ---------------------------------------------------------------- HTML parsing

ATTR_RE = re.compile(r'data-i18n-html\s*=\s*"([^"]+)"')
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>", re.S)
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "area", "col"}


def find_container_inner(src, attr_pos):
    """Dato l'offset dell'attributo data-i18n-html, ritorna l'innerHTML del tag."""
    lt = src.rfind("<", 0, attr_pos)
    if lt < 0:
        return None
    m = re.match(r"<([A-Za-z][A-Za-z0-9]*)", src[lt:])
    if not m:
        return None
    tag = m.group(1).lower()
    if tag in VOID_TAGS:
        return None
    gt = src.find(">", attr_pos)
    if gt < 0:
        return None
    if src[gt - 1] == "/":  # self-closing: nessun contenuto
        return ""
    open_re = re.compile(r"<%s\b" % re.escape(tag), re.I)
    close_re = re.compile(r"</%s\s*>" % re.escape(tag), re.I)
    depth = 1
    i = gt + 1
    start = i
    while i < len(src):
        no = open_re.search(src, i)
        nc = close_re.search(src, i)
        if not nc:
            return None  # tag non chiuso
        if no and no.start() < nc.start():
            depth += 1
            i = no.end()
            continue
        depth -= 1
        if depth == 0:
            return src[start:nc.start()]
        i = nc.end()
    return None


def page_lang(path, src):
    """Lingua della pagina: slug del filename, poi attributo lang, default it."""
    base = os.path.basename(path)
    m = re.search(r"-([a-z]{2})\.html$", base)
    if m and m.group(1) in LANGS:
        return m.group(1)
    m = re.search(r'<html[^>]*\blang\s*=\s*"([a-z]{2})', src, re.I)
    if m and m.group(1) in LANGS:
        return m.group(1)
    return "it"


# ------------------------------------------------------------- normalizzazione

def to_text(fragment):
    """HTML -> testo visibile normalizzato, per un confronto stabile."""
    s = COMMENT_RE.sub(" ", fragment)
    s = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", s, flags=re.S | re.I)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


DATE_RE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b"
    r"|\b(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|"
    r"ottobre|novembre|dicembre|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+\d{4}\b",
    re.I,
)


def date_markers(text):
    return sorted({m.group(0).lower() for m in DATE_RE.finditer(text)})


# ---------------------------------------------------------------------- report

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/var/www/bitcoinmarket.net")
    ap.add_argument("--all-langs", action="store_true",
                    help="confronta ogni chiave con tutte e 4 le lingue, "
                         "non solo con quella della pagina")
    ap.add_argument("--json", metavar="PATH", help="scrive il report grezzo in JSON")
    ap.add_argument("--min-ratio", type=float, default=0.995,
                    help="sotto questa similarità il caso è segnalato come drift")
    args = ap.parse_args()

    root = args.root
    trans = {}
    for lang in LANGS:
        p = os.path.join(root, "i18n", "%s.js" % lang)
        if not os.path.exists(p):
            print("i18n mancante: %s" % p, file=sys.stderr)
            sys.exit(1)
        trans[lang] = parse_i18n_file(p)

    html_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for fn in filenames:
            if fn.endswith(".html"):
                html_files.append(os.path.join(dirpath, fn))
    html_files.sort()

    results = []
    key_pages = defaultdict(list)

    for path in html_files:
        src = open(path, encoding="utf-8", errors="replace").read()
        if "data-i18n-html" not in src:
            continue
        lang = page_lang(path, src)
        rel = os.path.relpath(path, root)
        for m in ATTR_RE.finditer(src):
            key = m.group(1)
            key_pages[key].append(rel)
            inner = find_container_inner(src, m.start())
            line = src.count("\n", 0, m.start()) + 1
            rec = {
                "page": rel, "line": line, "key": key, "page_lang": lang,
                "status": None, "ratio": None,
                "html_dates": [], "i18n_dates": [],
                "html_chars": None, "i18n_chars": None,
            }
            if inner is None:
                rec["status"] = "PARSE_FAIL"
                results.append(rec)
                continue
            h_text = to_text(inner)
            rec["html_chars"] = len(h_text)
            rec["html_dates"] = date_markers(h_text)

            langs_to_check = LANGS if args.all_langs else (lang,)
            per_lang = {}
            for lg in langs_to_check:
                val = trans[lg].get(key)
                if val is None:
                    per_lang[lg] = {"status": "MISSING_KEY"}
                    continue
                i_text = to_text(val)
                ratio = difflib.SequenceMatcher(None, h_text, i_text).ratio()
                per_lang[lg] = {
                    "status": "OK" if ratio >= args.min_ratio else "DRIFT",
                    "ratio": round(ratio, 4),
                    "i18n_chars": len(i_text),
                    "i18n_dates": date_markers(i_text),
                }
            rec["per_lang"] = per_lang
            own = per_lang.get(lang, {})
            rec["status"] = own.get("status", "N/A")
            rec["ratio"] = own.get("ratio")
            rec["i18n_chars"] = own.get("i18n_chars")
            rec["i18n_dates"] = own.get("i18n_dates", [])
            results.append(rec)

    # ---- output
    drift = [r for r in results if r["status"] == "DRIFT"]
    missing = [r for r in results if r["status"] == "MISSING_KEY"]
    ok = [r for r in results if r["status"] == "OK"]
    fail = [r for r in results if r["status"] == "PARSE_FAIL"]

    print("=" * 78)
    print("AUDIT i18n data-i18n-html vs HTML — %s" % root)
    print("=" * 78)
    print("contenitori analizzati : %d  (%d pagine, %d chiavi distinte)"
          % (len(results), len({r['page'] for r in results}), len(key_pages)))
    print("  allineati (OK)       : %d" % len(ok))
    print("  DRIFT                : %d" % len(drift))
    print("  chiave mancante      : %d" % len(missing))
    print("  parse fallito        : %d" % len(fail))
    print()

    if drift:
        print("-" * 78)
        print("DRIFT — l'HTML e la chiave i18n divergono (vince la chiave, in prod)")
        print("-" * 78)
        drift.sort(key=lambda r: r["ratio"])
        for r in drift:
            print("\n[%s] %s:%d   (lang pagina: %s)"
                  % (r["key"], r["page"], r["line"], r["page_lang"]))
            print("    similarità %.1f%%   html %d char / i18n %d char (%+d)"
                  % (r["ratio"] * 100, r["html_chars"], r["i18n_chars"],
                     r["i18n_chars"] - r["html_chars"]))
            only_html = [d for d in r["html_dates"] if d not in r["i18n_dates"]]
            only_i18n = [d for d in r["i18n_dates"] if d not in r["html_dates"]]
            if only_html or only_i18n:
                print("    date solo in HTML : %s" % (", ".join(only_html) or "-"))
                print("    date solo in i18n : %s" % (", ".join(only_i18n) or "-"))
            if len(key_pages[r["key"]]) > 1:
                print("    ⚠ chiave condivisa da %d pagine: %s"
                      % (len(key_pages[r["key"]]), ", ".join(key_pages[r["key"]])))

    if missing:
        print("\n" + "-" * 78)
        print("CHIAVE MANCANTE nella lingua della pagina")
        print("     (il contenitore resta con l'HTML statico: non è un bug di per sé,")
        print("      ma se la chiave esiste in altre lingue è un buco di traduzione)")
        print("-" * 78)
        for r in missing:
            others = [lg for lg in LANGS if r["key"] in trans[lg]]
            print("  %-40s %s:%d  lang=%s  presente in: %s"
                  % (r["key"], r["page"], r["line"], r["page_lang"],
                     ", ".join(others) or "nessuna"))

    if fail:
        print("\n" + "-" * 78)
        print("PARSE FALLITO (tag non bilanciato) — da guardare a mano")
        print("-" * 78)
        for r in fail:
            print("  %s:%d  %s" % (r["page"], r["line"], r["key"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
        print("\nreport JSON: %s" % args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
