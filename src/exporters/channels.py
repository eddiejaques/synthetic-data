"""daily_channel_aggregates.csv: a ready Looker Studio source for channel / campaign performance."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..marketing import channel_for

COLS = ["date", "channel", "sessions", "users", "new_users", "registrations", "subscriptions",
        "initial_revenue_eur", "impressions", "clicks", "cost_eur"]


def write_daily_channels(events: Path, spend_csv: Path, acquisitions: dict, out: Path, tz: str) -> int:
    """Sessions/users/conversions by session channel (last touch), new users by first-touch channel,
    and paid delivery/cost from the spend table, all per local day."""
    z = ZoneInfo(tz)
    from ..marketing import CHANNEL_LABEL
    agg: dict = defaultdict(lambda: defaultdict(float))
    sess: dict = defaultdict(set)
    users: dict = defaultdict(set)
    with open(events) as f:
        for line in f:
            e = json.loads(line)
            ch = channel_for(e)
            if ch is None:
                continue
            d = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone(z).date().isoformat()
            k = (d, ch)
            sess[k].add(e["session_id"])
            users[k].add(e["user_id"] or e["anonymous_id"])
            if e["event"] == "registration":
                agg[k]["registrations"] += 1
            elif e["event"] == "subscribed":
                agg[k]["subscriptions"] += 1
            elif e["event"] == "payment_received" and e.get("payment_type") == "initial":
                agg[k]["initial_revenue_eur"] += e["amount"]
    for a in acquisitions.values():
        if str(a.get("acquired_in_window")) == "True":
            agg[(a["acquisition_date"], CHANNEL_LABEL[a["acquisition_channel"]])]["new_users"] += 1
    if spend_csv.exists():
        with open(spend_csv) as f:
            for r in csv.DictReader(f):
                k = (r["date"], r["channel"])
                for c in ("impressions", "clicks", "cost_eur"):
                    agg[k][c] += float(r[c] or 0)
    keys = sorted(set(agg) | set(sess))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for k in keys:
            a = agg[k]
            w.writerow({"date": k[0], "channel": k[1], "sessions": len(sess[k]), "users": len(users[k]),
                        "new_users": int(a["new_users"]), "registrations": int(a["registrations"]),
                        "subscriptions": int(a["subscriptions"]),
                        "initial_revenue_eur": round(a["initial_revenue_eur"], 2),
                        "impressions": int(a["impressions"]), "clicks": int(a["clicks"]),
                        "cost_eur": round(a["cost_eur"], 2)})
    return len(keys)
