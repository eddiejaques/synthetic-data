"""Batches: additive growth (more users, new periods), state carry-over, safety checks."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter

import pytest

from src.batches import Registry, SpecError, build_batch, end_state, load_spec, register_batch
from src.catalog import extend_for_premieres, load_catalog
from src.expand import expand_all
from src.journeys import brief_hash, is_fresh, run_journeys


def _spec(tmp_path, text: str) -> dict:
    p = tmp_path / "spec.yaml"
    p.write_text(text)
    return load_spec(p)


OCT = """
run_id: oct
periods:
  - {name: OCT_2026, start: 2026-10-01, end: 2026-10-31, new_users: 20, returning_users: 15}
stories:
  premieres: [{title_id: joyn_gntm, date: 2026-10-15, fan_share: 0.5, episodes: 4}]
  payment_incidents: [{date: 2026-10-21, multiplier: 5.0}]
  campaigns:
    - {key: launch_gntm, objective: launch, topic: gntm, title_id: joyn_gntm, channels: [paid_social, crm_push],
       start: 2026-10-10, end: 2026-10-31, contents: [video15s_gntm_a, push_gntm]}
"""


def _journeys_all(cfg, cat):
    reg = Registry.load(cfg)
    for bt in reg.batches:
        cfgv, views = reg.batch_view(bt)
        asyncio.run(run_journeys(cfgv, cat, views, None, offline=True))
    return reg


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add(cfg, tmp_path, text):
    spec = _spec(tmp_path, text)
    reg = Registry.load(cfg)
    new, ext = build_batch(reg, spec)
    register_batch(cfg, spec, new, ext)
    for prem in spec["stories"].get("premieres") or []:
        asyncio.run(extend_for_premieres(cfg, None, load_catalog(cfg), [prem]))
    return spec, new, ext


def test_new_month_is_additive_and_carries_state(world, tmp_path):
    cfg, cat, briefs = world
    reg = _journeys_all(cfg, cat)
    expand_all(reg, cat)
    base_events = cfg.out / "batches" / "base" / "events.ndjson"
    before = _sha(base_events)

    spec, new, ext = _add(cfg, tmp_path, OCT)
    cat = load_catalog(cfg)
    assert [b.user_id for b in new] == [f"u{i:04d}" for i in range(61, 81)]       # ids continue
    assert len(ext) == 15 and all(set(per) == {"OCT_2026"} for per in ext.values())
    reg = Registry.load(cfg)
    for u, per in ext.items():                                # returning users start where they left off
        prev = [pb for p, pb in reg.briefs[u].periods.items() if p != "OCT_2026"][-1]
        assert per["OCT_2026"].segment_at_start == end_state(prev)[0]
    assert any(p.title_id == "joyn_gntm" and str(p.available_from) == "2026-10-15" for p in cat.playables)

    reg = _journeys_all(cfg, cat)
    expand_all(reg, cat)
    assert _sha(base_events) == before                         # earlier batch byte-identical

    rows = [json.loads(l) for l in (cfg.out / "events.ndjson").read_text().splitlines()]
    assert len({r["$insert_id"] for r in rows}) == len(rows)
    ids = Counter(r["$anon_distinct_id"] for r in rows if r["event"] == "$identify")
    assert max(ids.values()) == 1                             # a device is stitched once, ever
    oct_users = {r["user_id"] or r["anonymous_id"] for r in rows if r["timestamp"].startswith("2026-10")}
    assert oct_users                                           # October exists in the merged file
    camps = {r.get("utm_campaign") for r in rows if r.get("utm_campaign")}
    assert all("_10_" in c or "_09_" in c or c.startswith("2026_10") for c in camps)


def test_more_users_same_timeframe(world, tmp_path):
    cfg, cat, briefs = world
    _journeys_all(cfg, cat)
    spec, new, ext = _add(cfg, tmp_path, """
run_id: more
returning_from: batch
periods:
  - {name: STLY, start: 2025-09-01, end: 2025-09-30, new_users: 30, returning_users: 0}
  - {name: CURRENT, start: 2026-09-01, end: 2026-09-30, new_users: 30, returning_users: 6}
""")
    assert len(new) == 60 and not ext                          # returners came from this batch only
    assert sum(1 for b in new if set(b.periods) == {"STLY", "CURRENT"}) == 6
    reg = _journeys_all(cfg, load_catalog(cfg))
    assert sum(1 for b in reg.briefs.values() if "CURRENT" in b.periods) == 36 + 36
    expand_all(reg, load_catalog(cfg))
    acq = (cfg.out / "marketing" / "user_acquisition.csv").read_text().splitlines()
    assert len(acq) == 1 + 120                                 # one first-touch row per user, no duplicates


def test_stale_journey_is_detected(world):
    cfg, cat, briefs = world
    b = briefs[0]
    p = next(iter(b.periods))
    rec = {"brief_hash": brief_hash(b, p)}
    assert is_fresh(rec, b, p) and is_fresh({}, b, p)         # unstamped legacy records are trusted
    b.periods[p].target_watch_hours += 5
    assert not is_fresh(rec, b, p)


def test_spec_safety_checks(world, tmp_path):
    cfg, cat, briefs = world
    reg = Registry.load(cfg)
    with pytest.raises(SpecError, match="eligible"):
        build_batch(reg, _spec(tmp_path, """
run_id: toomany
periods: [{name: OCT_2026, start: 2026-10-01, end: 2026-10-31, new_users: 0, returning_users: 1000}]
"""))
    with pytest.raises(SpecError, match="already exists"):
        build_batch(reg, _spec(tmp_path, """
run_id: clash
periods: [{name: CURRENT, start: 2026-09-02, end: 2026-09-30, new_users: 5}]
"""))
    with pytest.raises(SpecError, match="overlaps"):
        build_batch(reg, _spec(tmp_path, """
run_id: overlap
periods: [{name: LATE_SEP, start: 2026-09-20, end: 2026-10-10, new_users: 5}]
"""))
    with pytest.raises(SpecError):
        _spec(tmp_path, "run_id: base\nperiods: [{name: X1, start: 2026-10-01, end: 2026-10-31, new_users: 1}]")
