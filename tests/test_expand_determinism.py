"""Expansion is deterministic given fixed cached journeys, and every clean event validates."""
from __future__ import annotations

import asyncio
import hashlib
import json

from src.expand import expand
from src.journeys import load_journeys, run_journeys
from src.models import validate_event


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_expand_twice_is_byte_identical(world):
    cfg, cat, briefs = world
    asyncio.run(run_journeys(cfg, cat, briefs, None, offline=True))
    journeys = load_journeys(cfg, briefs)

    cfg.raw["paths"]["out"] = "out_a"
    expand(cfg, cat, briefs, journeys)
    a = cfg.out / "events.ndjson"
    cfg.raw["paths"]["out"] = "out_b"
    # reload from cache so nothing mutated in memory leaks between runs
    expand(cfg, cat, briefs, load_journeys(cfg, briefs))
    b = cfg.out / "events.ndjson"
    assert _digest(a) == _digest(b)
    assert _digest(a.parent / "identity_map.csv") == _digest(b.parent / "identity_map.csv")

    rows = [json.loads(l) for l in a.read_text().splitlines()]
    assert rows
    for r in rows:
        validate_event(r)
    assert len({r["$insert_id"] for r in rows}) == len(rows)
