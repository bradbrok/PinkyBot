#!/usr/bin/env python3
"""
GSC API Tool — Google Search Console per aiena.it via OAuth2 refresh token.
Identico a gsc_api.py ma puntato su sc-domain:aiena.it

Credentials: /home/pinky/.pinkybot/config/gsc_oauth_credentials.json

Usage:
    python3 gsc_api_aiena.py performance [--days 7|28|90]
    python3 gsc_api_aiena.py performance_pages [--days 28]
    python3 gsc_api_aiena.py performance_queries [--days 28] [--limit 20]
    python3 gsc_api_aiena.py coverage
    python3 gsc_api_aiena.py sitemaps
    python3 gsc_api_aiena.py inspect --url https://www.aiena.it/page.html

Output: JSON to stdout.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

CREDS_PATH = os.path.expanduser("~/.pinkybot/config/gsc_oauth_credentials.json")
SITE = "sc-domain:aiena.it"
SITE_URL = "https://www.aiena.it/"


def get_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    with open(CREDS_PATH) as f:
        c = json.load(f)

    creds = Credentials(
        token=None,
        refresh_token=c["refresh_token"],
        token_uri=c["token_uri"],
        client_id=c["client_id"],
        client_secret=c["client_secret"],
        scopes=c["scopes"],
    )
    return build("webmasters", "v3", credentials=creds)


def date_range(days):
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def cmd_performance(days=28):
    svc = get_service()
    start, end = date_range(days)

    summary = svc.searchanalytics().query(
        siteUrl=SITE,
        body={
            "startDate": start,
            "endDate": end,
            "dimensions": [],
            "rowLimit": 1,
        }
    ).execute()

    row = summary.get("rows", [{}])[0]

    daily = svc.searchanalytics().query(
        siteUrl=SITE,
        body={
            "startDate": date_range(14)[0],
            "endDate": end,
            "dimensions": ["date"],
            "rowLimit": 14,
            "orderBy": [{"fieldName": "date", "sortOrder": "ASCENDING"}],
        }
    ).execute()

    return {
        "action": "performance",
        "site": SITE,
        "period": f"{start} → {end}",
        "days": days,
        "summary": {
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": round(row.get("ctr", 0) * 100, 2),
            "avg_position": round(row.get("position", 0), 1),
        },
        "daily_14d": [
            {
                "date": r["keys"][0],
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "position": round(r["position"], 1),
            }
            for r in daily.get("rows", [])
        ],
    }


def cmd_performance_pages(days=28, limit=20):
    svc = get_service()
    start, end = date_range(days)

    result = svc.searchanalytics().query(
        siteUrl=SITE,
        body={
            "startDate": start,
            "endDate": end,
            "dimensions": ["page"],
            "rowLimit": limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }
    ).execute()

    return {
        "action": "performance_pages",
        "site": SITE,
        "period": f"{start} → {end}",
        "top_pages": [
            {
                "page": r["keys"][0].replace("https://www.aiena.it", ""),
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "ctr": round(r["ctr"] * 100, 2),
                "position": round(r["position"], 1),
            }
            for r in result.get("rows", [])
        ],
    }


def cmd_performance_queries(days=28, limit=20):
    svc = get_service()
    start, end = date_range(days)

    result = svc.searchanalytics().query(
        siteUrl=SITE,
        body={
            "startDate": start,
            "endDate": end,
            "dimensions": ["query"],
            "rowLimit": limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }
    ).execute()

    return {
        "action": "performance_queries",
        "site": SITE,
        "period": f"{start} → {end}",
        "top_queries": [
            {
                "query": r["keys"][0],
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "ctr": round(r["ctr"] * 100, 2),
                "position": round(r["position"], 1),
            }
            for r in result.get("rows", [])
        ],
    }


def cmd_coverage():
    svc = get_service()

    sitemaps = svc.sitemaps().list(siteUrl=SITE).execute()

    sitemap_info = []
    for sm in sitemaps.get("sitemap", []):
        info = {
            "path": sm.get("path", ""),
            "lastSubmitted": sm.get("lastSubmitted", ""),
            "isPending": sm.get("isPending", False),
            "isSitemapsIndex": sm.get("isSitemapsIndex", False),
            "lastDownloaded": sm.get("lastDownloaded", ""),
            "warnings": sm.get("warnings", 0),
            "errors": sm.get("errors", 0),
        }
        contents = sm.get("contents", [])
        for c in contents:
            if c.get("type") == "web":
                info["submitted"] = c.get("submitted", 0)
                info["indexed"] = c.get("indexed", 0)
        sitemap_info.append(info)

    return {
        "action": "coverage",
        "site": SITE,
        "sitemaps": sitemap_info,
    }


def cmd_sitemaps():
    return cmd_coverage()


def cmd_inspect(url):
    """URL inspection via Search Console API v1."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    with open(CREDS_PATH) as f:
        c = json.load(f)

    creds = Credentials(
        token=None,
        refresh_token=c["refresh_token"],
        token_uri=c["token_uri"],
        client_id=c["client_id"],
        client_secret=c["client_secret"],
        scopes=c["scopes"],
    )

    try:
        svc = build("searchconsole", "v1", credentials=creds)
        result = svc.urlInspection().index().inspect(
            body={
                "inspectionUrl": url,
                "siteUrl": SITE_URL,
            }
        ).execute()
        return {"action": "inspect", "url": url, "result": result}
    except Exception as e:
        return {"action": "inspect", "url": url, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="GSC API Tool — aiena.it")
    parser.add_argument(
        "action",
        choices=["performance", "performance_pages", "performance_queries", "coverage", "sitemaps", "inspect"],
    )
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--url", type=str, default="")
    args = parser.parse_args()

    if args.action == "performance":
        result = cmd_performance(days=args.days)
    elif args.action == "performance_pages":
        result = cmd_performance_pages(days=args.days, limit=args.limit)
    elif args.action == "performance_queries":
        result = cmd_performance_queries(days=args.days, limit=args.limit)
    elif args.action in ("coverage", "sitemaps"):
        result = cmd_coverage()
    elif args.action == "inspect":
        if not args.url:
            print(json.dumps({"error": "--url required for inspect"}))
            sys.exit(1)
        result = cmd_inspect(args.url)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
