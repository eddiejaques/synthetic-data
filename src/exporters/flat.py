"""events_flat.csv for Looker Studio / Sheets: one row per event, assets_list as JSON + count."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..marketing import channel_for

COLUMNS = [
    "event", "timestamp", "distinct_id", "insert_id", "session_id", "user_id", "anonymous_id", "user_segment",
    "platform", "device_id", "device_type", "device_brand", "device_model", "os", "os_version", "app_version",
    "viewport_width", "viewport_height", "country", "locale", "event_source", "screen_name", "previous_screen",
    "lane_id", "lane_name", "lane_type", "lane_position", "assets_count", "assets_list_json", "content_id",
    "title_id", "title", "episode_title", "season", "episode", "genre", "content_type", "licence_type",
    "asset_position", "playback_event", "playback_session_id", "duration_seconds", "watch_time_seconds",
    "completion_rate", "method", "success", "plan", "price", "amount", "currency", "payment_type",
    "transaction_id", "failure_reason", "attempt_number", "anon_distinct_id",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "referrer", "referring_domain",
    "gclid", "fbclid", "ttclid", "landing_url", "channel",
]


def flatten(e: dict) -> dict:
    r = {k.lstrip("$"): v for k, v in e.items() if k != "assets_list"}
    if "assets_list" in e:
        r["assets_count"] = len(e["assets_list"])
        r["assets_list_json"] = json.dumps(e["assets_list"], ensure_ascii=False)
    r["channel"] = channel_for(e)          # derived default channel grouping (see channel_mapping.csv)
    return r


def write_flat(events: Path, out: Path) -> int:
    n = 0
    with open(events) as fin, open(out, "w", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for line in fin:
            w.writerow(flatten(json.loads(line)))
            n += 1
    return n
