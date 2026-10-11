"""users.csv and content_catalog.csv."""
from __future__ import annotations

import csv
from pathlib import Path

from ..models import Catalog, UserBrief


ACQ_COLS = ["acquisition_channel", "acquisition_date", "acquisition_platform", "acquired_in_window",
            "acquisition_campaign", "initial_utm_source", "initial_utm_medium", "initial_utm_campaign",
            "initial_referring_domain"]


def write_users(briefs: list[UserBrief], path: Path, acquisitions: dict | None = None,
                periods: list[str] | None = None, identities: dict | None = None) -> None:
    """One row per user. Per-period columns (video_<P>, persona_<P>) follow the dataset's periods."""
    periods = periods or list(dict.fromkeys(p for b in briefs for p in b.periods))
    cols = (["user_id", "segment", "primary_platform", "second_platform", "periods_active"]
            + [f"video_{p}" for p in periods]
            + ["registered_before_window", "registration_period_day", "subscribed_before_window",
               "subscription_period_day", "plan", "churn_period_day"]
            + [f"persona_{p}" for p in periods] + ACQ_COLS + ["batch", "email", "email_sha256", "crm_id"])
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for b in briefs:
            reg = next((f"{p}:{pb.must_register_on_day}" for p, pb in b.periods.items() if pb.must_register_on_day), "")
            sub = next((f"{p}:{pb.must_subscribe_on_day}" for p, pb in b.periods.items() if pb.must_subscribe_on_day), "")
            churn = next((f"{p}:{pb.svod_until_day}" for p, pb in b.periods.items() if pb.svod_until_day), "")
            plan = next((pb.plan for pb in b.periods.values() if pb.plan), "")
            row = {
                "user_id": b.user_id, "segment": b.segment, "primary_platform": b.primary_platform,
                "second_platform": b.devices[1].platform if len(b.devices) > 1 else "",
                "periods_active": "|".join(b.periods),
                "registered_before_window": b.registered_before_window, "registration_period_day": reg,
                "subscribed_before_window": b.subscribed_before_window, "subscription_period_day": sub,
                "plan": plan, "churn_period_day": churn, "batch": b.batch,
                **{c: (acquisitions or {}).get(b.user_id, {}).get(c, "") for c in ACQ_COLS},
            }
            row.update((identities or {}).get(b.user_id, {}))
            for p in periods:
                row[f"video_{p}"] = b.periods[p].is_video_user if p in b.periods else ""
                row[f"persona_{p}"] = b.periods[p].persona_note if p in b.periods else ""
            w.writerow(row)


def write_catalog(cat: Catalog, path: Path) -> None:
    titles = cat.title_map()
    lanes_of: dict[str, list[str]] = {}
    for l in cat.lanes:
        for t in l.title_ids:
            lanes_of.setdefault(t, []).append(l.lane_id)
    cols = ["content_id", "title_id", "title", "episode_title", "season", "episode", "content_type",
            "asset_type", "genre", "licence_type", "duration_s", "available_from", "lanes"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for p in cat.playables:
            d = p.model_dump(mode="json")
            d.update(asset_type=titles[p.title_id].asset_type, lanes="|".join(lanes_of.get(p.title_id, [])))
            w.writerow({c: d.get(c) for c in cols})
