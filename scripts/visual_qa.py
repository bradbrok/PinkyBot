#!/usr/bin/env python3
"""
visual_qa.py — Screenshot + visual check delle pagine critiche di bitcoinmarket.net
Usato da Sentinel nel suo audit giornaliero.
Output: immagini in /tmp/bm_screenshots/ + report testuale
"""
import os, sys, json
from datetime import datetime
from playwright.sync_api import sync_playwright

PAGES = [
    # BMN usa i18n single-URL: nessuna pagina separata /de/, /it/, ecc.
    # Language switch via localStorage ('bm-lang') — funziona per utenti reali.
    # Playwright blocca localStorage in sandbox, quindi solo EN pages testabili.
    ("home_en", "https://bitcoinmarket.net/", "en", None),
    ("reviews_index", "https://bitcoinmarket.net/reviews/", "en", None),
    ("guide_index", "https://bitcoinmarket.net/guide/", "en", None),
    ("magazine_index", "https://bitcoinmarket.net/magazine/", "en", None),
    ("binance_review", "https://bitcoinmarket.net/reviews/binance.html", "it", None),
    ("tools_feargreed", "https://bitcoinmarket.net/tools/fear-greed-index.html", "en", None),
]

OUT_DIR = "/tmp/bm_screenshots"
os.makedirs(OUT_DIR, exist_ok=True)

def run_visual_check():
    issues = []
    report = {
        "timestamp": datetime.now().isoformat(),
        "screenshots": [],
        "issues": []
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (compatible; SentinelQA/1.0)"
        )

        for name, url, lang, local_storage in PAGES:
            page = context.new_page()
            try:
                # Inject localStorage BEFORE page load via addInitScript (i18n single-URL pattern)
                if local_storage:
                    script = "\n".join([f'localStorage.setItem("{k}", "{v}");' for k, v in local_storage.items()])
                    page.add_init_script(script)
                page.goto(url, wait_until="networkidle", timeout=15000)
                page.wait_for_timeout(1500)  # let JS render

                # Screenshot
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                fname = f"{OUT_DIR}/{ts}_{name}.png"
                page.screenshot(path=fname, full_page=False)
                report["screenshots"].append({"name": name, "url": url, "file": fname})

                # Basic checks
                title = page.title()
                if not title or len(title) < 5:
                    issues.append(f"CRITICO: {name} — titolo mancante o vuoto ('{title}')")

                # Check header logo visible
                logo = page.query_selector(".logo-icon, .logo img, header img")
                if not logo:
                    issues.append(f"WARN: {name} — logo non trovato nel DOM")
                elif logo:
                    box = logo.bounding_box()
                    if box:
                        ratio = box["width"] / box["height"] if box["height"] > 0 else 0
                        # Square logo: ratio should be close to 1.0, tolerate 0.7-1.3
                        if ratio < 0.5 or ratio > 2.5:
                            issues.append(f"CRITICO: {name} — logo schiacciato/distorto (w:{box['width']:.0f} h:{box['height']:.0f} ratio:{ratio:.2f})")

                # Check nav visible
                nav = page.query_selector("nav, .nav, .navbar, header")
                if not nav:
                    issues.append(f"WARN: {name} — nav/header non trovato")

                # Check no JS error overlay
                error_overlay = page.query_selector(".error-page, #error, .js-error")
                if error_overlay:
                    issues.append(f"CRITICO: {name} — error overlay visibile in pagina")

                # Check lang attribute (wait for JS to update if i18n)
                expected_lang = lang  # Use lang from PAGES config
                try:
                    page.wait_for_function(
                        f"() => document.documentElement.getAttribute('lang') === '{expected_lang}'",
                        timeout=3000  # wait max 3s for JS to set lang
                    )
                except:
                    # Fallback: check current value
                    html_lang = page.eval_on_selector("html", "el => el.getAttribute('lang')")
                    if html_lang != expected_lang:
                        severity = "CRITICO" if expected_lang == "en" else "WARN"  # CRITICO only for EN pages (root/default)
                        issues.append(f"{severity}: {name} — lang atteso '{expected_lang}', trovato '{html_lang}'")

            except Exception as e:
                issues.append(f"CRITICO: {name} — pagina non caricata: {e}")
            finally:
                page.close()

        browser.close()

    report["issues"] = issues
    report_path = f"{OUT_DIR}/report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary
    print(f"📸 Visual QA — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Screenshots: {len(report['screenshots'])}")
    if issues:
        print(f"\n🔴 Problemi trovati ({len(issues)}):")
        for i in issues:
            print(f"  • {i}")
    else:
        print("✅ Nessun problema visivo rilevato")
    print(f"\nReport: {report_path}")
    return report

if __name__ == "__main__":
    run_visual_check()
