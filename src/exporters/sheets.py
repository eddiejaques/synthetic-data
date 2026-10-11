"""Daily aggregates (always written as CSV; optionally pushed to Google Sheets).

Sheets caps a spreadsheet at 10M cells, so event-level data never goes there.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

COLS = ["date", "platform", "events", "active_users", "sessions", "video_users", "watch_hours",
        "registrations", "subscriptions", "payments_received", "payments_failed", "gateway_timeouts",
        "lane_impressions", "asset_clicks"]


def daily_aggregates(events: Path, tz: str) -> list[dict]:
    z = ZoneInfo(tz)
    agg: dict = defaultdict(lambda: defaultdict(float))
    sets: dict = defaultdict(lambda: defaultdict(set))
    last_watch: dict[str, tuple[tuple, int]] = {}
    with open(events) as f:
        for line in f:
            e = json.loads(line)
            d = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone(z).date().isoformat()
            key = (d, e["platform"])
            a, s = agg[key], sets[key]
            a["events"] += 1
            s["active_users"].add(e["user_id"] or e["anonymous_id"])
            if e["session_id"]:
                s["sessions"].add(e["session_id"])
            ev = e["event"]
            if ev == "video_view":
                s["video_users"].add(e["user_id"] or e["anonymous_id"])
                last_watch[e["playback_session_id"]] = (key, e["watch_time_seconds"])
            a["registrations"] += ev == "registration"
            a["subscriptions"] += ev == "subscribed"
            a["payments_received"] += ev == "payment_received"
            a["payments_failed"] += ev == "payment_failed"
            a["gateway_timeouts"] += ev == "payment_failed" and e.get("failure_reason") == "gateway_timeout"
            a["lane_impressions"] += ev == "lane_viewed"
            a["asset_clicks"] += ev == "asset_clicked"
    for key, w in last_watch.values():
        agg[key]["watch_hours"] += w / 3600
    rows = []
    for (d, p) in sorted(agg):
        r = {"date": d, "platform": p, **{k: int(v) for k, v in agg[(d, p)].items() if k != "watch_hours"}}
        r["watch_hours"] = round(agg[(d, p)]["watch_hours"], 2)
        for k, v in sets[(d, p)].items():
            r[k] = len(v)
        rows.append({c: r.get(c, 0) for c in COLS})
    return rows


def write_daily(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def push_to_sheets(rows: list[dict], sheet_id: str, worksheet: str = "daily_aggregates") -> None:
    """Needs `pip install gspread` and GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account
    JSON that has edit access to the sheet."""
    import gspread
    gc = gspread.service_account()
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(worksheet, rows=len(rows) + 10, cols=len(COLS))
    ws.update([COLS] + [[r[c] for c in COLS] for r in rows], value_input_option="RAW")
