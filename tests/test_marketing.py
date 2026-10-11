"""Marketing layer: deterministic, internally consistent, platform-realistic."""
from __future__ import annotations

import asyncio
import csv
import json
from collections import Counter

from src.expand import expand
from src.journeys import load_journeys, run_journeys
from src.marketing import CHANNEL_LABEL, channel_for


def _run(world):
    cfg, cat, briefs = world
    asyncio.run(run_journeys(cfg, cat, briefs, None, offline=True))
    meta = expand(cfg, cat, briefs, load_journeys(cfg, briefs))
    rows = [json.loads(l) for l in (cfg.out / "events.ndjson").read_text().splitlines()]
    return cfg, rows, meta


def _csv(cfg, name):
    return list(csv.DictReader(open(cfg.out / "marketing" / name)))


def test_marketing_tables_and_events_are_consistent(world):
    cfg, rows, _ = _run(world)
    campaigns = {r["utm_campaign"] for r in _csv(cfg, "campaigns.csv")}
    client = [r for r in rows if r["event_source"] == "client"]
    server = [r for r in rows if r["event_source"] == "server"]
    assert all(r["utm_campaign"] in campaigns for r in client if r.get("utm_campaign"))
    assert not any(r.get("utm_source") for r in client if r["platform"] == "ott")          # TV gap
    assert not any(r.get("utm_source") or r.get("$referrer") for r in server)               # billing has no source
    # every event of a session shares the same source
    by_sess = {}
    for r in client:
        key = (r.get("utm_source"), r.get("utm_campaign"), r.get("$referring_domain"))
        assert by_sess.setdefault(r["session_id"], key) == key
    # click ids only where the ad platform sets them
    for r in client:
        assert bool(r.get("gclid")) == (r.get("utm_source") in ("google", "youtube"))
        assert bool(r.get("fbclid")) == (r.get("utm_source") in ("facebook", "instagram"))
        assert bool(r.get("ttclid")) == (r.get("utm_source") == "tiktok")
    # landing urls carry the same utm values
    for r in client:
        if r.get("utm_source") and r["platform"] == "web":
            assert f"utm_source={r['utm_source']}" in r["landing_url"]


def test_spend_covers_landing_sessions(world):
    cfg, rows, _ = _run(world)
    clicks = Counter()
    for r in _csv(cfg, "marketing_spend_daily.csv"):
        clicks[(r["utm_campaign"], r["utm_source"], r["utm_content"])] += int(r["clicks"])
        assert int(r["clicks"]) >= int(r["landing_sessions"]) or r["utm_medium"] == "tv_promo"
    landed = Counter()
    seen = set()
    for r in rows:
        if r["event_source"] == "client" and r.get("utm_campaign") and r["session_id"] not in seen:
            seen.add(r["session_id"])
            if r["utm_medium"] != "tv_promo":
                landed[(r["utm_campaign"], r["utm_source"], r.get("utm_content") or "")] += 1
    for k, n in landed.items():
        assert clicks[k] >= n, k


def test_channel_grouping_matches_assignment(world):
    cfg, rows, meta = _run(world)
    derived = Counter()
    seen = set()
    for r in rows:
        if r["event_source"] == "client" and r["session_id"] not in seen:
            seen.add(r["session_id"])
            derived[channel_for(r)] += 1
    assigned = Counter()
    for ch, n in meta["marketing"]["session_channels"].items():
        assigned[CHANNEL_LABEL[ch]] += n
    assert derived == assigned


def test_first_touch_is_exact_and_platform_realistic(world):
    cfg, _, _ = _run(world)
    acq = _csv(cfg, "user_acquisition.csv")
    assert len(acq) == 60
    for a in acq:
        if a["acquisition_platform"] == "ott":
            assert a["acquisition_channel"] in ("direct", "tv_promo", "app_store")
        if a["acquisition_platform"] == "web":
            assert a["acquisition_channel"] != "app_store"
