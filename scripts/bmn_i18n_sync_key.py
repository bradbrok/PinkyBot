#!/usr/bin/env python3
"""
Riallinea una chiave i18n al contenuto HTML corrente del suo contenitore
data-i18n-html (parità 1:1), così il runtime non riporta indietro il contenuto.

Uso: python3 i18n_sync_key.py <pagina.html> <chiave> [<lang>]
Scrive solo se il contenuto è effettivamente cambiato. Rifiuta di procedere se
il frammento HTML contiene backtick o ${ (romperebbero il template literal).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from i18n_audit import ATTR_RE, find_container_inner  # noqa: E402

ROOT = "/var/www/bitcoinmarket.net"


def sync(page, key, lang="it"):
    src = open(os.path.join(ROOT, page), encoding="utf-8").read()
    inner = None
    for m in ATTR_RE.finditer(src):
        if m.group(1) == key:
            inner = find_container_inner(src, m.start())
            break
    if inner is None:
        raise SystemExit("contenitore data-i18n-html=%r non trovato in %s" % (key, page))
    inner = inner.strip("\n")
    if "`" in inner or "${" in inner:
        raise SystemExit("il frammento contiene ` o ${ : escape necessario, mi fermo")

    p = os.path.join(ROOT, "i18n", "%s.js" % lang)
    js = open(p, encoding="utf-8").read()
    marker = "'%s': `" % key
    if js.count(marker) != 1:
        raise SystemExit("chiave %r: %d occorrenze in %s.js (atteso 1)"
                         % (key, js.count(marker), lang))
    start = js.index(marker) + len(marker)
    end = js.find("`,", start)
    if end < 0:
        raise SystemExit("fine del valore non trovata per %r" % key)
    old = js[start:end]
    if old == inner:
        print("  = %-32s già allineata (%d char)" % (key, len(inner)))
        return False
    open(p, "w", encoding="utf-8").write(js[:start] + inner + js[end:])
    print("  ~ %-32s %s.js: %d -> %d char" % (key, lang, len(old), len(inner)))
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    sync(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "it")
