#!/usr/bin/env python3
"""
GA4 API Tool — Google Analytics 4 via OAuth2 refresh token.
Full access: read data, manage properties, segments, filters.

Credentials: /home/pinky/.pinkybot/config/gsc_oauth_credentials.json
Property: properties/528467412 (bitcoinmarket.net)

Usage:
    python3 ga4_api.py overview [--days 28]
    python3 ga4_api.py traffic [--days 28]
    python3 ga4_api.py pages [--days 28] [--limit 20]
    python3 ga4_api.py devices [--days 28]
    python3 ga4_api.py countries [--days 28] [--limit 10]
    python3 ga4_api.py realtime

Output: JSON to stdout.
"""

import argparse
import json
import os
import sys

CREDS_PATH = os.path.expanduser("~/.pinkybot/config/gsc_oauth_credentials.json")
GA4_PROPERTY = "properties/528467412"


def get_client():
    from google.oauth2.credentials import Credentials
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

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
    return BetaAnalyticsDataClient(credentials=creds)


def run_report(client, dimensions, metrics, days=28, limit=20, order_by_metric=None):
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Metric, Dimension, OrderBy
    )

    order = []
    if order_by_metric:
        order = [OrderBy(metric=OrderBy.MetricOrderBy(metric_name=order_by_metric), desc=True)]

    request = RunReportRequest(
        property=GA4_PROPERTY,
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        limit=limit,
        order_bys=order,
    )
    return client.run_report(request)


def cmd_overview(days=28):
    from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric

    client = get_client()

    # Summary metrics
    from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension
    request = RunReportRequest(
        property=GA4_PROPERTY,
        metrics=[
            Metric(name="sessions"),
            Metric(name="activeUsers"),
            Metric(name="screenPageViews"),
            Metric(name="bounceRate"),
            Metric(name="averageSessionDuration"),
        ],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
    )
    r = client.run_report(request)
    row = r.rows[0] if r.rows else None

    # Compare with previous period
    request2 = RunReportRequest(
        property=GA4_PROPERTY,
        metrics=[
            Metric(name="sessions"),
            Metric(name="activeUsers"),
        ],
        date_ranges=[DateRange(start_date=f"{days*2}daysAgo", end_date=f"{days+1}daysAgo")],
    )
    r2 = client.run_report(request2)
    prev = r2.rows[0] if r2.rows else None

    result = {
        "action": "overview",
        "period_days": days,
        "summary": {},
        "vs_previous": {}
    }

    if row:
        mv = row.metric_values
        result["summary"] = {
            "sessions": int(mv[0].value),
            "active_users": int(mv[1].value),
            "pageviews": int(mv[2].value),
            "bounce_rate_pct": round(float(mv[3].value) * 100, 1),
            "avg_session_duration_sec": round(float(mv[4].value), 0),
        }

    if prev and row:
        curr_sess = int(row.metric_values[0].value)
        prev_sess = int(prev.metric_values[0].value)
        if prev_sess > 0:
            delta = round((curr_sess - prev_sess) / prev_sess * 100, 1)
            result["vs_previous"]["sessions_delta_pct"] = delta

    return result


def cmd_traffic(days=28):
    client = get_client()
    r = run_report(
        client,
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "activeUsers", "screenPageViews"],
        days=days,
        limit=10,
        order_by_metric="sessions"
    )

    rows = []
    for row in r.rows:
        rows.append({
            "channel": row.dimension_values[0].value,
            "sessions": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
            "pageviews": int(row.metric_values[2].value),
        })

    return {"action": "traffic", "period_days": days, "channels": rows}


def cmd_pages(days=28, limit=20):
    client = get_client()
    r = run_report(
        client,
        dimensions=["pagePath"],
        metrics=["screenPageViews", "activeUsers", "averageSessionDuration"],
        days=days,
        limit=limit,
        order_by_metric="screenPageViews"
    )

    rows = []
    for row in r.rows:
        rows.append({
            "page": row.dimension_values[0].value,
            "pageviews": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
            "avg_time_sec": round(float(row.metric_values[2].value), 0),
        })

    return {"action": "pages", "period_days": days, "top_pages": rows}


def cmd_devices(days=28):
    client = get_client()
    r = run_report(
        client,
        dimensions=["deviceCategory"],
        metrics=["sessions", "activeUsers"],
        days=days,
        limit=5,
        order_by_metric="sessions"
    )

    rows = []
    for row in r.rows:
        rows.append({
            "device": row.dimension_values[0].value,
            "sessions": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
        })

    return {"action": "devices", "period_days": days, "devices": rows}


def cmd_countries(days=28, limit=10):
    client = get_client()
    r = run_report(
        client,
        dimensions=["country"],
        metrics=["sessions", "activeUsers"],
        days=days,
        limit=limit,
        order_by_metric="sessions"
    )

    rows = []
    for row in r.rows:
        rows.append({
            "country": row.dimension_values[0].value,
            "sessions": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
        })

    return {"action": "countries", "period_days": days, "countries": rows}


def cmd_realtime():
    from google.analytics.data_v1beta.types import RunRealtimeReportRequest, Metric, Dimension

    client = get_client()
    request = RunRealtimeReportRequest(
        property=GA4_PROPERTY,
        dimensions=[Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
    )
    r = client.run_realtime_report(request)

    total = sum(int(row.metric_values[0].value) for row in r.rows)
    by_country = [
        {"country": row.dimension_values[0].value, "active": int(row.metric_values[0].value)}
        for row in r.rows
    ]

    return {"action": "realtime", "active_users_now": total, "by_country": by_country}


def main():
    parser = argparse.ArgumentParser(description="GA4 API Tool")
    parser.add_argument(
        "action",
        choices=["overview", "traffic", "pages", "devices", "countries", "realtime"],
    )
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if args.action == "overview":
        result = cmd_overview(days=args.days)
    elif args.action == "traffic":
        result = cmd_traffic(days=args.days)
    elif args.action == "pages":
        result = cmd_pages(days=args.days, limit=args.limit)
    elif args.action == "devices":
        result = cmd_devices(days=args.days)
    elif args.action == "countries":
        result = cmd_countries(days=args.days, limit=args.limit)
    elif args.action == "realtime":
        result = cmd_realtime()

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
