"""events_dirty.ndjson: the clean data with DIRTY_RATE of rows broken on purpose, plus a manifest.

The clean file is never touched; this is a separate copy for data-quality demos.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import rng_for

KINDS = ["duplicate_event", "platform_casing", "missing_session_id", "local_timestamp_no_tz",
         "negative_watch_time", "null_distinct_id", "screen_name_typo", "late_arriving_event"]


def _dirty(e: dict, kind: str, tz: ZoneInfo) -> list[dict]:
    d = dict(e)
    if kind == "duplicate_event":
        return [e, dict(e)]                                  # same $insert_id twice
    if kind == "platform_casing":
        d["platform"] = {"ios": "iOS", "android": "Android", "ott": "OTT", "web": "Web"}[e["platform"]]
    elif kind == "missing_session_id":
        d["session_id"] = None
    elif kind == "local_timestamp_no_tz":
        d["timestamp"] = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    elif kind == "negative_watch_time":
        d["watch_time_seconds"] = -abs(e.get("watch_time_seconds") or 300)
    elif kind == "null_distinct_id":
        d["distinct_id"] = None
    elif kind == "screen_name_typo":
        d["screen_name"] = (e.get("screen_name") or "home").title() + " "
    elif kind == "late_arriving_event":
        t = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) + timedelta(days=3)
        d["timestamp"] = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
        d["ingested_late"] = True
    return [d]


def write_dirty(events: Path, out: Path, manifest: Path, rate: float, seed: int, tz: str) -> int:
    rng = rng_for(seed, "dirty")
    z = ZoneInfo(tz)
    n = 0
    with open(events) as fin, open(out, "w") as fout, open(manifest, "w", newline="") as fm:
        w = csv.writer(fm)
        w.writerow(["insert_id", "event", "dirty_type"])
        for line in fin:
            e = json.loads(line)
            if rng.random() < rate:
                kinds = [k for k in KINDS if k != "negative_watch_time" or e["event"] == "video_view"]
                kinds = [k for k in kinds if k != "screen_name_typo" or "screen_name" in e]
                kind = rng.choice(kinds)
                for d in _dirty(e, kind, z):
                    fout.write(json.dumps(d, ensure_ascii=False) + "\n")
                w.writerow([e["$insert_id"], e["event"], kind])
                n += 1
            else:
                fout.write(line)
    return n
