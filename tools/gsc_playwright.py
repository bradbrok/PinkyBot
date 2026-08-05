#!/usr/bin/env python3
"""
GSC Playwright Tool — Google Search Console scraper via browser automation.
Uses Playwright with Chromium. Simulates human behavior to avoid detection.

Usage:
    python3 gsc_playwright.py --action overview
    python3 gsc_playwright.py --action performance --days 28
    python3 gsc_playwright.py --action coverage
    python3 gsc_playwright.py --action sitemaps

Credentials: loaded from env or passed as args.
    GSC_EMAIL, GSC_PASSWORD

Output: JSON to stdout.
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

# --- Config ---
SITE_URL = "https://bitcoinmarket.net"
GSC_BASE = "https://search.google.com/search-console"
DEFAULT_EMAIL = os.environ.get("GSC_EMAIL", "satoshipinky@gmail.com")
DEFAULT_PASSWORD = os.environ.get("GSC_PASSWORD", "")
USER_DATA_DIR = os.path.expanduser("~/.cache/gsc_playwright_session")


def human_delay(min_s=1.5, max_s=4.0):
    """Random delay to simulate human behavior."""
    time.sleep(random.uniform(min_s, max_s))


def human_type(page, selector, text, delay_range=(80, 200)):
    """Type text character by character with random delays."""
    page.click(selector)
    human_delay(0.3, 0.8)
    for char in text:
        page.keyboard.type(char)
        time.sleep(random.randint(*delay_range) / 1000)


def login_google(page, email, password):
    """Login to Google account."""
    print("[gsc] Navigating to Google login...", file=sys.stderr)
    page.goto("https://accounts.google.com/signin/v2/identifier", wait_until="networkidle")
    human_delay(2, 4)

    # Enter email
    human_type(page, 'input[type="email"]', email)
    human_delay(0.5, 1.5)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    human_delay(2, 4)

    # Check if blocked or challenge
    if "sorry" in page.url.lower() or "challenge" in page.url.lower():
        return {"error": "Google security challenge detected — manual intervention needed"}

    # Enter password
    try:
        page.wait_for_selector('input[type="password"]', timeout=10000)
    except Exception:
        return {"error": "Password field not found — possible CAPTCHA or flow change"}

    human_type(page, 'input[type="password"]', password)
    human_delay(0.8, 2.0)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    human_delay(3, 5)

    # Check for 2FA or security step
    if "challenge" in page.url or "signin/challenge" in page.url:
        return {"error": "2FA/security challenge required — cannot proceed automatically"}

    # Check if login succeeded
    if "myaccount.google.com" in page.url or "google.com" in page.url:
        print("[gsc] Login successful", file=sys.stderr)
        return {"success": True}

    return {"error": f"Unexpected URL after login: {page.url}"}


def navigate_to_gsc(page):
    """Navigate to GSC for the target site."""
    print(f"[gsc] Navigating to GSC for {SITE_URL}...", file=sys.stderr)
    encoded_site = SITE_URL.replace(":", "%3A").replace("/", "%2F")
    gsc_url = f"{GSC_BASE}/performance/search-analytics?resource_id={encoded_site}"
    page.goto(gsc_url, wait_until="networkidle", timeout=30000)
    human_delay(3, 5)

    # Check if we landed on GSC
    if "search-console" not in page.url:
        # Try going to GSC home first
        page.goto(GSC_BASE, wait_until="networkidle", timeout=30000)
        human_delay(2, 4)

    return page.url


def action_overview(page):
    """Get GSC overview: performance snapshot."""
    url = navigate_to_gsc(page)
    human_delay(2, 4)

    result = {
        "action": "overview",
        "timestamp": datetime.now().isoformat(),
        "url_visited": url,
        "site": SITE_URL,
    }

    # Try to extract basic performance numbers visible on the page
    try:
        # Wait for performance card to load
        page.wait_for_selector('[data-card-title]', timeout=15000)
        cards = page.query_selector_all('[data-card-title]')
        metrics = {}
        for card in cards:
            title = card.get_attribute('data-card-title') or ""
            value_el = card.query_selector('.nnLLaf')
            value = value_el.inner_text() if value_el else "N/A"
            metrics[title] = value
        result["metrics"] = metrics
    except Exception as e:
        result["note"] = f"Could not extract card metrics: {e}"

    # Take a screenshot for verification
    screenshot_path = "/tmp/gsc_overview.png"
    page.screenshot(path=screenshot_path, full_page=False)
    result["screenshot"] = screenshot_path

    return result


def action_performance(page, days=28):
    """Get top queries and pages from performance report."""
    navigate_to_gsc(page)
    human_delay(2, 4)

    result = {
        "action": "performance",
        "days": days,
        "timestamp": datetime.now().isoformat(),
        "site": SITE_URL,
    }

    screenshot_path = "/tmp/gsc_performance.png"
    page.screenshot(path=screenshot_path, full_page=False)
    result["screenshot"] = screenshot_path
    result["note"] = "Screenshot captured. Manual review needed for detailed data extraction."

    return result


def action_coverage(page):
    """Get index coverage status."""
    encoded_site = SITE_URL.replace(":", "%3A").replace("/", "%2F")
    url = f"{GSC_BASE}/index?resource_id={encoded_site}"
    page.goto(url, wait_until="networkidle", timeout=30000)
    human_delay(3, 5)

    result = {
        "action": "coverage",
        "timestamp": datetime.now().isoformat(),
        "site": SITE_URL,
    }

    screenshot_path = "/tmp/gsc_coverage.png"
    page.screenshot(path=screenshot_path, full_page=False)
    result["screenshot"] = screenshot_path

    return result


def action_sitemaps(page):
    """Get sitemap status."""
    encoded_site = SITE_URL.replace(":", "%3A").replace("/", "%2F")
    url = f"{GSC_BASE}/sitemaps?resource_id={encoded_site}"
    page.goto(url, wait_until="networkidle", timeout=30000)
    human_delay(3, 5)

    result = {
        "action": "sitemaps",
        "timestamp": datetime.now().isoformat(),
        "site": SITE_URL,
    }

    screenshot_path = "/tmp/gsc_sitemaps.png"
    page.screenshot(path=screenshot_path, full_page=False)
    result["screenshot"] = screenshot_path

    return result


def run(action, email, password, days=28, headless=True):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # Use persistent context to save session (avoid re-login)
        os.makedirs(USER_DATA_DIR, exist_ok=True)

        browser = p.chromium.launch_persistent_context(
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

        page = browser.new_page()

        # Check if already logged in
        page.goto("https://accounts.google.com", wait_until="networkidle")
        human_delay(2, 3)

        needs_login = (
            "signin" in page.url
            or "accounts.google.com" == page.url.rstrip("/")
            or "ServiceLogin" in page.url
        )

        if needs_login:
            login_result = login_google(page, email, password)
            if "error" in login_result:
                browser.close()
                return login_result

        # Execute requested action
        if action == "overview":
            result = action_overview(page)
        elif action == "performance":
            result = action_performance(page, days=days)
        elif action == "coverage":
            result = action_coverage(page)
        elif action == "sitemaps":
            result = action_sitemaps(page)
        else:
            result = {"error": f"Unknown action: {action}"}

        browser.close()
        return result


def main():
    parser = argparse.ArgumentParser(description="GSC Playwright Tool")
    parser.add_argument("--action", default="overview",
                        choices=["overview", "performance", "coverage", "sitemaps"])
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    if not args.password:
        print(json.dumps({"error": "No password provided. Set GSC_PASSWORD env or --password"}))
        sys.exit(1)

    result = run(
        action=args.action,
        email=args.email,
        password=args.password,
        days=args.days,
        headless=not args.no_headless,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
