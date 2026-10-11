"""Identity handling in the PostHog / Mixpanel / BigQuery exports."""
from __future__ import annotations

import asyncio
import hashlib
import json

from src.exporters import bigquery, mixpanel, posthog
from src.expand import expand
from src.identity import account_users, email_for
from src.journeys import load_journeys, run_journeys


def _world_events(world):
    cfg, cat, briefs = world
    asyncio.run(run_journeys(cfg, cat, briefs, None, offline=True))
    expand(cfg, cat, briefs, load_journeys(cfg, briefs))
    ev = cfg.out / "events.ndjson"
    return cfg, briefs, ev, [json.loads(l) for l in ev.read_text().splitlines()]


def _read(d):
    return [json.loads(l) for f in sorted(d.glob("batch_*.jsonl")) for l in f.read_text().splitlines()]


def test_identity_attributes_are_synthetic_and_hashed(world):
    cfg, cat, briefs = world
    ids = account_users(briefs)
    assert ids and all(b.segment != "anonymous" for b in briefs if b.user_id in ids)
    for u, i in ids.items():
        assert i["email"] == email_for(u) and i["email"].endswith("@example.invalid")
        assert i["email_sha256"] == hashlib.sha256(i["email"].strip().lower().encode()).hexdigest()


def test_mixpanel_simplified_id_merge(world, tmp_path):
    cfg, briefs, ev, rows = _world_events(world)
    mixpanel.write_mixpanel(ev, tmp_path / "mp", mode="simplified")
    recs = _read(tmp_path / "mp")
    assert len(recs) == sum(1 for r in rows if r["event"] != "$identify")
    assert not any(r["event"] == "$identify" for r in recs)
    for r in recs:
        p = r["properties"]
        assert p["$device_id"] == p["anonymous_id"]
        if p.get("user_id"):
            assert p["$user_id"] == p["user_id"] == p["distinct_id"]
        else:
            assert "$user_id" not in p and p["distinct_id"] == f"$device:{p['anonymous_id']}"
    # a device that logs in has events both before (device only) and after (device + user) -> Mixpanel merges
    linked = {p["$device_id"] for r in recs for p in [r["properties"]] if p.get("$user_id")}
    anon_only = {p["$device_id"] for r in recs for p in [r["properties"]] if not p.get("$user_id")}
    assert linked & anon_only
    # profiles use the same distinct_id convention as the events
    ids = account_users(briefs)
    mixpanel.write_mixpanel_profiles(tmp_path / "prof.jsonl", briefs, {}, ids, "simplified")
    prof = [json.loads(l) for l in (tmp_path / "prof.jsonl").read_text().splitlines()]
    event_ids = {r["properties"]["distinct_id"] for r in recs}
    assert all(p["$distinct_id"] in event_ids for p in prof)
    assert all(p["$set"]["email_sha256"] == ids[p["$distinct_id"]]["email_sha256"] for p in prof if p["$distinct_id"] in ids)


def test_mixpanel_original_id_merge_keeps_identify(world, tmp_path):
    cfg, briefs, ev, rows = _world_events(world)
    mixpanel.write_mixpanel(ev, tmp_path / "mp", mode="original")
    idents = [r for r in _read(tmp_path / "mp") if r["event"] == "$identify"]
    assert idents and all(r["properties"]["$anon_id"].startswith("anon_") and r["properties"]["$identified_id"]
                          for r in idents)


def test_posthog_identify_sets_person_identity(world, tmp_path):
    cfg, briefs, ev, rows = _world_events(world)
    ids = account_users(briefs)
    posthog.write_posthog(ev, tmp_path / "ph", identities=ids)
    idents = [r for r in _read(tmp_path / "ph") if r["event"] == "$identify"]
    assert idents
    for r in idents:
        assert r["properties"]["$anon_distinct_id"].startswith("anon_")
        assert r["properties"]["$set"]["email_sha256"] == ids[r["distinct_id"]]["email_sha256"]


def test_bigquery_identity_view_and_tables(world, tmp_path):
    cfg, briefs, ev, rows = _world_events(world)
    bigquery.write_bigquery(ev, tmp_path / "bq")
    sql = (tmp_path / "bq" / "views.sql").read_text()
    assert "COALESCE(e.user_id, m.user_id, e.anonymous_id) AS person_id" in sql
    sh = (tmp_path / "bq" / "load_bigquery.sh").read_text()
    assert "identity_map" in sh and "users" in sh and "\\\n" in sh
    assert json.loads((tmp_path / "bq" / "identity_map_schema.json").read_text())[0]["name"] == "user_id"
