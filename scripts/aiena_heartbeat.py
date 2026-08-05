#!/usr/bin/env python3
"""
AIena Heartbeat - Periodic RSS Feed Monitor
Checks article updates and refreshes the feed.xml every 6 hours
Can be run as a cron job
"""

import json
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

ARTICLES_JSON = "/home/pinky/.pinkybot/data/aiena_articles.json"
RSS_UPDATE_SCRIPT = "/home/pinky/.pinkybot/scripts/aiena_rss_update.py"
HEARTBEAT_LOG = "/home/pinky/.pinkybot/logs/aiena_heartbeat.log"

# Ensure logs directory exists
os.makedirs(os.path.dirname(HEARTBEAT_LOG), exist_ok=True)


def log_message(msg):
    """Log a message with timestamp"""
    timestamp = datetime.now().isoformat()
    log_entry = f"[{timestamp}] {msg}"
    print(log_entry)
    with open(HEARTBEAT_LOG, 'a', encoding='utf-8') as f:
        f.write(log_entry + '\n')


def check_articles_exist():
    """Verify articles JSON exists and is valid"""
    if not os.path.exists(ARTICLES_JSON):
        log_message("WARNING: Articles JSON not found, creating default...")
        return False

    try:
        with open(ARTICLES_JSON, 'r', encoding='utf-8') as f:
            articles = json.load(f)
        if not isinstance(articles, list):
            log_message("ERROR: Articles JSON is not a list")
            return False
        log_message(f"OK: Articles file contains {len(articles)} item(s)")
        return True
    except json.JSONDecodeError as e:
        log_message(f"ERROR: Invalid JSON in articles file: {e}")
        return False
    except Exception as e:
        log_message(f"ERROR: Cannot read articles file: {e}")
        return False


def run_rss_update():
    """Execute the RSS generation script"""
    try:
        result = subprocess.run(
            [sys.executable, RSS_UPDATE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            # Extract last line from stdout which contains the feed URL
            lines = result.stdout.strip().split('\n')
            for line in lines[-3:]:
                if line:
                    log_message(line)
            return True
        else:
            log_message(f"ERROR: RSS update failed with code {result.returncode}")
            if result.stderr:
                log_message(f"  {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        log_message("ERROR: RSS update script timeout")
        return False
    except Exception as e:
        log_message(f"ERROR: Failed to run RSS update: {e}")
        return False


def verify_feed_generated():
    """Check if feed.xml was recently generated"""
    try:
        feed_path = "/var/www/aiena.it/feed.xml"
        if not os.path.exists(feed_path):
            log_message("ERROR: feed.xml not generated")
            return False

        # Check file size (should be > 2KB)
        size = os.path.getsize(feed_path)
        if size < 2000:
            log_message(f"WARNING: feed.xml seems too small ({size} bytes)")
            return False

        mtime = os.path.getmtime(feed_path)
        mtime_str = datetime.fromtimestamp(mtime).isoformat()
        log_message(f"OK: feed.xml exists ({size} bytes, modified {mtime_str})")
        return True
    except Exception as e:
        log_message(f"ERROR: Cannot verify feed.xml: {e}")
        return False


def main():
    log_message("=" * 60)
    log_message("AIena Heartbeat - Starting")

    # Check articles
    if not check_articles_exist():
        log_message("FAILED: Articles validation")
        sys.exit(1)

    # Run RSS update
    if not run_rss_update():
        log_message("FAILED: RSS generation")
        sys.exit(1)

    # Verify output
    if not verify_feed_generated():
        log_message("FAILED: Feed verification")
        sys.exit(1)

    log_message("SUCCESS: Heartbeat completed")
    log_message("")


if __name__ == "__main__":
    main()
