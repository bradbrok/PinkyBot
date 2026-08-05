#!/usr/bin/env python3
"""
Verifica in browser reale (Chromium headless) i casi di DRIFT trovati da
i18n_audit.py: misura quanto testo resta nel contenitore DOPO che l'i18n ha
applicato le traduzioni, e lo confronta con il testo statico dell'HTML.

Non modifica nulla. Legge audit.json e scrive render_report.json.
"""
import json
import sys
from collections import defaultdict

from playwright.sync_api import sync_playwright

BASE = "https://bitcoinmarket.net/"

JS = """
(key) => {
  const el = document.querySelector('[data-i18n-html="' + key + '"]');
  if (!el) return null;
  return { rendered: el.innerText.trim().length,
           orig: (el.dataset.i18nOrig || '').replace(/<[^>]+>/g, ' ')
                    .replace(/\\s+/g, ' ').trim().length };
}
"""


def main():
    audit = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "audit.json",
                           encoding="utf-8"))
    drift = [r for r in audit if r["status"] == "DRIFT"]
    by_page = defaultdict(list)
    for r in drift:
        by_page[r["page"]].append(r)

    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(45000)
        for i, (rel, recs) in enumerate(sorted(by_page.items()), 1):
            url = BASE + rel
            try:
                page.goto(url, wait_until="networkidle")
            except Exception as e:
                print("  ! %s -> %s" % (rel, type(e).__name__))
                continue
            for r in recs:
                try:
                    res = page.evaluate(JS, r["key"])
                except Exception as e:
                    res = None
                if not res:
                    continue
                rec = dict(r)
                rec["rendered_chars"] = res["rendered"]
                rec["orig_chars"] = res["orig"]
                # quanto contenuto sparisce all'utente rispetto all'HTML statico
                base = res["orig"] or r["html_chars"] or 0
                rec["loss_pct"] = round(100.0 * (base - res["rendered"]) / base, 1) if base else 0.0
                out.append(rec)
            print("[%d/%d] %s" % (i, len(by_page), rel))
        browser.close()

    json.dump(out, open("render_report.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    severe = sorted((r for r in out if r["loss_pct"] >= 20),
                    key=lambda r: -r["loss_pct"])
    print("\n" + "=" * 78)
    print("PERDITA DI CONTENUTO REALE IN BROWSER (>= 20%%): %d contenitori"
          % len(severe))
    print("=" * 78)
    for r in severe:
        print("  -%5.1f%%  %-34s %s"
              % (r["loss_pct"], r["key"], r["page"]))
        print("           statico %6d char -> renderizzato %6d char"
              % (r["orig_chars"], r["rendered_chars"]))
    gain = [r for r in out if r["loss_pct"] <= -20]
    if gain:
        print("\nContenitori dove l'i18n AGGIUNGE contenuto (HTML più povero "
              "della chiave): %d" % len(gain))
        for r in sorted(gain, key=lambda r: r["loss_pct"])[:15]:
            print("  +%5.1f%%  %-34s %s" % (-r["loss_pct"], r["key"], r["page"]))


if __name__ == "__main__":
    main()
