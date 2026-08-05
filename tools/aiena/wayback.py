#!/usr/bin/env python3
"""
Wayback Machine Search - Trova versioni archiviate di pagine web
Parte del toolkit OSINT di AIena.
"""

import argparse
import json
import sys
import time
from urllib.parse import quote

import requests


def search_wayback(url: str, from_date: str | None = None) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    result = {
        "source": "Internet Archive - Wayback Machine",
        "url": url,
        "snapshots": [],
        "errors": [],
        "links": {
            "browse_history": f"https://web.archive.org/web/*/{url}",
            "latest": f"https://web.archive.org/web/{url}",
        }
    }

    # Availability API (simple, non rate-limited)
    try:
        r = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            closest = data.get("archived_snapshots", {}).get("closest", {})
            if closest:
                result["snapshots"].append({
                    "timestamp": closest.get("timestamp"),
                    "url_archivio": closest.get("url"),
                    "status": closest.get("status"),
                    "note": "snapshot più recente"
                })
    except Exception as e:
        result["errors"].append(f"Availability API: {e}")

    # CDX API con backoff
    time.sleep(2)
    try:
        params = {
            "url": url,
            "output": "json",
            "limit": 20,
            "fl": "timestamp,statuscode,digest",
            "collapse": "digest",
            "filter": "statuscode:200"
        }
        if from_date:
            params["from"] = from_date.replace("-", "")

        r = requests.get(
            "https://web.archive.org/cdx/search/cdx",
            params=params,
            timeout=20
        )
        if r.status_code == 200:
            rows = r.json()
            if len(rows) > 1:
                headers = rows[0]
                for row in rows[1:6]:  # max 5
                    snap = dict(zip(headers, row))
                    ts = snap.get("timestamp", "")
                    result["snapshots"].append({
                        "timestamp": ts,
                        "url_archivio": f"https://web.archive.org/web/{ts}/{url}",
                        "status": snap.get("statuscode")
                    })
                result["total_snapshots_found"] = len(rows) - 1
        elif r.status_code == 429:
            result["errors"].append("CDX API: rate limited — usa il link browse_history per visualizzare manualmente")
    except Exception as e:
        result["errors"].append(f"CDX API: {e}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Wayback Machine OSINT per AIena")
    parser.add_argument("--url", required=True, help="URL da cercare")
    parser.add_argument("--from-date", help="Data inizio (YYYY-MM-DD)")
    args = parser.parse_args()

    data = search_wayback(args.url, args.from_date)
    print(json.dumps(data, indent=2, ensure_ascii=False))

    print("\n" + "="*60)
    print(f"URL cercato: {data['url']}")
    print(f"Snapshot trovati: {len(data['snapshots'])}")
    for s in data["snapshots"]:
        print(f"  → {s.get('timestamp', '?')} | {s.get('url_archivio', '')}")
    print(f"Storico completo: {data['links']['browse_history']}")
    if data["errors"]:
        for e in data["errors"]:
            print(f"  ! {e}")


if __name__ == "__main__":
    main()
