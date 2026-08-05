#!/usr/bin/env python3
"""
AIena RSS Feed Validator
Checks feed.xml for valid RSS structure and required elements
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FEED_PATH = "/var/www/aiena.it/feed.xml"


def validate_feed():
    """Validate RSS feed structure"""
    errors = []
    warnings = []

    # Check file exists
    if not Path(FEED_PATH).exists():
        errors.append(f"Feed file not found: {FEED_PATH}")
        return errors, warnings

    # Parse XML
    try:
        tree = ET.parse(FEED_PATH)
        root = tree.getroot()
    except ET.ParseError as e:
        errors.append(f"Invalid XML: {e}")
        return errors, warnings

    # Check root element
    if root.tag != "rss":
        errors.append(f"Root element should be 'rss', got '{root.tag}'")

    # Check version
    version = root.get("version")
    if version != "2.0":
        warnings.append(f"RSS version should be 2.0, got {version}")

    # Check channel
    channel = root.find("channel")
    if channel is None:
        errors.append("Missing <channel> element")
        return errors, warnings

    # Required channel elements
    required = ["title", "link", "description"]
    for elem in required:
        if channel.find(elem) is None:
            errors.append(f"Missing channel element: <{elem}>")
        else:
            text = channel.find(elem).text
            if not text or not text.strip():
                errors.append(f"Channel <{elem}> is empty")

    # Check items
    items = channel.findall("item")
    if not items:
        warnings.append("No items found in feed")
    else:
        print(f"Found {len(items)} item(s)")

        for i, item in enumerate(items, 1):
            item_required = ["title", "link", "description"]
            for elem in item_required:
                if item.find(elem) is None:
                    errors.append(f"Item {i}: Missing <{elem}>")

            # Check for at least pubDate or guid
            if item.find("pubDate") is None:
                warnings.append(f"Item {i}: No pubDate")
            if item.find("guid") is None:
                warnings.append(f"Item {i}: No guid")

            # Print item info
            title = item.find("title")
            link = item.find("link")
            if title is not None and link is not None:
                print(f"  {i}. {title.text}")
                print(f"     Link: {link.text}")

    return errors, warnings


def main():
    print("AIena RSS Feed Validator")
    print("=" * 60)

    errors, warnings = validate_feed()

    if errors:
        print("\nERRORS:")
        for err in errors:
            print(f"  [E] {err}")

    if warnings:
        print("\nWARNINGS:")
        for warn in warnings:
            print(f"  [W] {warn}")

    if not errors and not warnings:
        print("\n✓ Feed is valid!")

    print("\nFeed URL: https://aiena.it/feed.xml")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
