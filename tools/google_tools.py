#!/usr/bin/env python3
"""
Google Tools — GSC + GA4 via Playwright cookie injection.
Bypasses VPS IP block by using pre-exported session cookies.

Cookies: /home/pinky/.cache/gsc_cookies_satoshi.json (satoshipinky@gmail.com)
         /home/pinky/.cache/gsc_cookies.json (ziomik account)

Usage:
    python3 google_tools.py gsc overview
    python3 google_tools.py gsc performance [--days 7|28|90]
    python3 google_tools.py gsc coverage
    python3 google_tools.py gsc sitemaps
    python3 google_tools.py gsc errors
    python3 google_tools.py ga4 overview
    python3 google_tools.py ga4 pages [--days 7|28]
    python3 google_tools.py ga4 traffic [--days 7|28]

Output: JSON to stdout. Screenshots saved to /tmp/.
"""

import argparse
import json
import os
import sys
import time

# Paths
GSC_COOKIES = os.path.expanduser("~/.cache/gsc_cookies_satoshi.json")
GA4_COOKIES = os.path.expanduser("~/.cache/gsc_cookies_satoshi.json")
USER_DATA_DIR = os.path.expanduser("~/.cache/google_tools_session")
LD_LIBRARY_PATH = (
    "/home/pinky/.local/lib/playwright-deps/extracted/usr/lib/x86_64-linux-gnu:"
    "/home/pinky/.local/lib/playwright-deps/extracted/usr/lib"
)

# Site config
SITE_URL = "https://bitcoinmarket.net"
GSC_PROPERTY = "sc-domain:bitcoinmarket.net"
GA4_ACCOUNT = "a57881251"
GA4_PROPERTY = "p528467412"


def set_env():
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if LD_LIBRARY_PATH not in existing:
        os.environ["LD_LIBRARY_PATH"] = f"{LD_LIBRARY_PATH}:{existing}".rstrip(":")


def load_cookies(path):
    """Load and convert Cookie-Editor JSON format to Playwright format."""
    with open(path) as f:
        raw = json.load(f)

    cookies = []
    for c in raw:
        pw = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
        }
        sm = c.get("sameSite")
        if sm == "no_restriction":
            pw["sameSite"] = "None"
        elif sm == "lax":
            pw["sameSite"] = "Lax"
        elif sm == "strict":
            pw["sameSite"] = "Strict"
        if c.get("expirationDate"):
            pw["expires"] = int(c["expirationDate"])
        cookies.append(pw)
    return cookies


def make_browser(p, headless=True):
    """Launch browser with stealth settings."""
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="it-IT",
    )


def inject_cookies(context, cookies):
    """Inject cookies into browser context."""
    context.add_cookies(cookies)


def screenshot(page, name):
    path = f"/tmp/google_tools_{name}.png"
    try:
        page.screenshot(path=path, full_page=False)
    except Exception:
        pass
    return path


# ── GSC ────────────────────────────────────────────────────────────────────────

GSC_BASE = "https://search.google.com/search-console"


def gsc_url(path):
    encoded = GSC_PROPERTY.replace(":", "%3A").replace("/", "%2F")
    return f"{GSC_BASE}/{path}?resource_id={encoded}"


def gsc_overview(page):
    url = gsc_url("performance/search-analytics")
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(4)

    result = {"action": "gsc_overview", "url": page.url, "screenshot": screenshot(page, "gsc_overview")}

    # Try to extract metric cards
    try:
        page.wait_for_selector("text=Clic totali", timeout=10000)
        content = page.inner_text("body")
        result["page_text_sample"] = content[:2000]
    except Exception as e:
        result["note"] = str(e)

    return result


def gsc_performance(page, days=28):
    url = gsc_url("performance/search-analytics")
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(4)

    result = {
        "action": "gsc_performance",
        "days": days,
        "url": page.url,
        "screenshot": screenshot(page, "gsc_performance"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:3000]
    except Exception as e:
        result["note"] = str(e)

    return result


def gsc_coverage(page):
    url = gsc_url("index")
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(4)

    result = {
        "action": "gsc_coverage",
        "url": page.url,
        "screenshot": screenshot(page, "gsc_coverage"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:3000]
    except Exception as e:
        result["note"] = str(e)

    return result


def gsc_sitemaps(page):
    url = gsc_url("sitemaps")
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(4)

    result = {
        "action": "gsc_sitemaps",
        "url": page.url,
        "screenshot": screenshot(page, "gsc_sitemaps"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:2000]
    except Exception as e:
        result["note"] = str(e)

    return result


def gsc_errors(page):
    """Check Core Web Vitals / page experience."""
    url = gsc_url("core-web-vitals")
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(4)

    result = {
        "action": "gsc_errors",
        "url": page.url,
        "screenshot": screenshot(page, "gsc_errors"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:3000]
    except Exception as e:
        result["note"] = str(e)

    return result


# ── GA4 ────────────────────────────────────────────────────────────────────────

GA4_BASE = f"https://analytics.google.com/analytics/web/#/{GA4_ACCOUNT}{GA4_PROPERTY}"


def ga4_overview(page):
    url = f"{GA4_BASE}/reports/intelligenthome"
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(5)

    result = {
        "action": "ga4_overview",
        "url": page.url,
        "screenshot": screenshot(page, "ga4_overview"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:3000]
    except Exception as e:
        result["note"] = str(e)

    return result


def ga4_pages(page, days=28):
    url = f"{GA4_BASE}/reports/explorer"
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(5)

    result = {
        "action": "ga4_pages",
        "days": days,
        "url": page.url,
        "screenshot": screenshot(page, "ga4_pages"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:3000]
    except Exception as e:
        result["note"] = str(e)

    return result


def ga4_traffic(page, days=28):
    url = f"{GA4_BASE}/reports/defaultchannelgroup"
    page.goto(url, wait_until="networkidle", timeout=45000)
    time.sleep(5)

    result = {
        "action": "ga4_traffic",
        "days": days,
        "url": page.url,
        "screenshot": screenshot(page, "ga4_traffic"),
    }

    try:
        content = page.inner_text("body")
        result["page_text_sample"] = content[:3000]
    except Exception as e:
        result["note"] = str(e)

    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def run(service, action, days=28, headless=True):
    set_env()
    from playwright.sync_api import sync_playwright

    cookie_path = GSC_COOKIES if service == "gsc" else GA4_COOKIES
    if not os.path.exists(cookie_path):
        return {"error": f"Cookie file not found: {cookie_path}"}

    cookies = load_cookies(cookie_path)

    with sync_playwright() as p:
        browser = make_browser(p, headless=headless)
        inject_cookies(browser, cookies)
        page = browser.new_page()

        try:
            if service == "gsc":
                if action == "overview":
                    result = gsc_overview(page)
                elif action == "performance":
                    result = gsc_performance(page, days=days)
                elif action == "coverage":
                    result = gsc_coverage(page)
                elif action == "sitemaps":
                    result = gsc_sitemaps(page)
                elif action == "errors":
                    result = gsc_errors(page)
                else:
                    result = {"error": f"Unknown GSC action: {action}"}
            elif service == "ga4":
                if action == "overview":
                    result = ga4_overview(page)
                elif action == "pages":
                    result = ga4_pages(page, days=days)
                elif action == "traffic":
                    result = ga4_traffic(page, days=days)
                else:
                    result = {"error": f"Unknown GA4 action: {action}"}
            else:
                result = {"error": f"Unknown service: {service}"}
        except Exception as e:
            result = {"error": str(e), "url": page.url if page else "unknown"}
        finally:
            browser.close()

    return result


def main():
    parser = argparse.ArgumentParser(description="Google Tools — GSC + GA4")
    parser.add_argument("service", choices=["gsc", "ga4"])
    parser.add_argument("action")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    result = run(
        service=args.service,
        action=args.action,
        days=args.days,
        headless=not args.no_headless,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
