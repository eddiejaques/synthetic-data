"""Journeys stage: checkpoint on quota exhaustion, then resume without re-calling cached users."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.gemini_client import CallResult
from src.journeys import build_prompt, cache_path, fallback_journey, run_journeys
from src.keypool import AllKeysExhausted


class FakeClient:
    """Answers with a valid journey for the prompt's user; dies after `budget` calls."""

    def __init__(self, answers: dict[str, str], budget: int):
        self.answers, self.budget, self.calls = answers, budget, 0

    async def generate(self, prompt, schema, temperature, system=None):
        if self.calls >= self.budget:
            raise AllKeysExhausted(datetime(2026, 9, 11, tzinfo=ZoneInfo("America/Los_Angeles")))
        self.calls += 1
        await asyncio.sleep(0)
        return CallResult(text=self.answers[prompt], key_label="GEMINI_API_KEY_1", input_tokens=1, output_tokens=1)

    def write_usage(self):
        pass


def _answers(cfg, cat, briefs):
    return {build_prompt(cfg, b, b.periods[p], cat): json.dumps(fallback_journey(cfg, b, b.periods[p], cat).model_dump())
            for b in briefs for p in b.periods}


def test_checkpoint_then_resume(world):
    cfg, cat, briefs = world
    total = sum(len(b.periods) for b in briefs)
    answers = _answers(cfg, cat, briefs)

    first = FakeClient(answers, budget=25)
    with pytest.raises(AllKeysExhausted):
        asyncio.run(run_journeys(cfg, cat, briefs, first))
    cached = list((cfg.cache / "journeys").glob("*.json"))
    assert len(cached) == 25 == first.calls
    assert all(json.loads(p.read_text())["source"] == "llm" for p in cached)

    second = FakeClient(answers, budget=10_000)
    asyncio.run(run_journeys(cfg, cat, briefs, second))
    assert second.calls == total - 25                       # cached user-months were not re-called
    assert len(list((cfg.cache / "journeys").glob("*.json"))) == total

    third = FakeClient(answers, budget=10_000)
    asyncio.run(run_journeys(cfg, cat, briefs, third))
    assert third.calls == 0                                 # a full rerun is free


def test_refresh_regenerates_only_named_users(world):
    cfg, cat, briefs = world
    answers = _answers(cfg, cat, briefs)
    asyncio.run(run_journeys(cfg, cat, briefs, FakeClient(answers, 10_000)))
    u = briefs[0].user_id
    before = {p: cache_path(cfg, u, p).stat().st_mtime_ns for p in briefs[0].periods}
    again = FakeClient(answers, 10_000)
    asyncio.run(run_journeys(cfg, cat, briefs, again, refresh={u}))
    assert again.calls == len(briefs[0].periods)
    assert all(cache_path(cfg, u, p).stat().st_mtime_ns >= before[p] for p in before)


def test_bad_llm_output_retries_once_then_falls_back(world):
    cfg, cat, briefs = world
    answers = {k: '{"sessions": "not a list"}' for k in _answers(cfg, cat, briefs)}

    class Echo(FakeClient):
        async def generate(self, prompt, schema, temperature, system=None):
            self.calls += 1
            return CallResult(text='{"sessions": "nope"}', key_label="GEMINI_API_KEY_1", input_tokens=1, output_tokens=1)

    c = Echo(answers, 10_000)
    asyncio.run(run_journeys(cfg, cat, briefs[:1], c))
    n = len(briefs[0].periods)
    assert c.calls == 2 * n
    rec = json.loads(cache_path(cfg, briefs[0].user_id, next(iter(briefs[0].periods))).read_text())
    assert rec["source"] == "fallback"
    assert (cfg.logs / "failed_journeys.jsonl").read_text().count("\n") == n
