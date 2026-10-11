"""KeyPool rotation: mocked 429s, cooling, daily quota exhaustion, resume time, persistence."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from google.genai import errors

from src.gemini_client import GeminiClient, classify_429
from src.keypool import AllKeysExhausted, KeyPool, load_keys_from_env

TZ = ZoneInfo("America/Los_Angeles")


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0
        self.wall = datetime(2026, 9, 10, 12, 0, tzinfo=TZ)

    def clock(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.wall

    async def sleep(self, s: float) -> None:
        self.t += s
        self.wall += timedelta(seconds=s)


def pool(n=3, rpm=60, rpd=100, **kw):
    ft = FakeTime()
    p = KeyPool([(f"GEMINI_API_KEY_{i}", f"secret-{i}") for i in range(1, n + 1)], rpm=rpm, rpd=rpd,
                clock=ft.clock, now=ft.now, sleep=ft.sleep, **kw)
    return p, ft


def run(c):
    return asyncio.run(c)


def test_picks_key_with_most_headroom():
    p, _ = pool()
    p.keys[0].used_today = 50
    p.keys[1].used_today = 10
    p.keys[2].used_today = 30
    assert run(p.acquire()).label == "GEMINI_API_KEY_2"


def test_429_cools_key_and_rotates():
    p, ft = pool()
    k = run(p.acquire())
    p.report_rate_limited(k, retry_after=30.0, daily=False)
    nxt = [run(p.acquire()).label for _ in range(4)]
    assert k.label not in nxt
    ft.t += 31
    for other in p.keys:                      # cooled key is usable again once the others are busy
        if other is not k:
            p.report_rate_limited(other, retry_after=60.0, daily=False)
    assert run(p.acquire()).label == k.label


def test_backoff_without_retry_after_grows():
    p, _ = pool(n=1)
    k = p.keys[0]
    p.report_rate_limited(k, None, daily=False)
    first = k.cooldown_until - p.clock()
    p.report_rate_limited(k, None, daily=False)
    second = k.cooldown_until - p.clock()
    assert second > first > 0


def test_rpm_bucket_waits_instead_of_overrunning():
    p, ft = pool(n=1, rpm=2)
    t0 = ft.t
    for _ in range(4):
        run(p.acquire())
    assert ft.t - t0 >= 59        # 2 immediately, then ~30 s per token


def test_daily_exhaustion_marks_dead_until_reset_and_raises_when_all_dead():
    p, ft = pool(n=2)
    for k in p.keys:
        p.report_rate_limited(k, None, daily=True)
    with pytest.raises(AllKeysExhausted) as ei:
        run(p.acquire())
    assert ei.value.resume_at == datetime(2026, 9, 11, 0, 0, tzinfo=TZ)


def test_rpd_counter_exhausts_key_locally():
    p, _ = pool(n=2, rpd=3)
    labels = [run(p.acquire()).label for _ in range(6)]
    assert labels.count("GEMINI_API_KEY_1") == 3 and labels.count("GEMINI_API_KEY_2") == 3
    with pytest.raises(AllKeysExhausted):
        run(p.acquire())


def test_keys_revive_after_reset():
    p, ft = pool(n=1, rpd=1)
    run(p.acquire())
    with pytest.raises(AllKeysExhausted):
        run(p.acquire())
    ft.wall = datetime(2026, 9, 11, 0, 5, tzinfo=TZ)
    assert run(p.acquire()).label == "GEMINI_API_KEY_1"


def test_state_persists_usage_by_fingerprint(tmp_path):
    sp = tmp_path / "state.json"
    p, ft = pool(n=1, state_path=sp)
    for _ in range(5):
        run(p.acquire())
    p.save_state()
    p2 = KeyPool([("GEMINI_API_KEY_1", "secret-1")], rpm=60, rpd=100, clock=ft.clock, now=ft.now, state_path=sp)
    assert p2.keys[0].used_today == 5
    p3 = KeyPool([("GEMINI_API_KEY_1", "other-key")], rpm=60, rpd=100, clock=ft.clock, now=ft.now, state_path=sp)
    assert p3.keys[0].used_today == 0
    assert "secret-1" not in sp.read_text()


def test_duplicate_env_keys_are_dropped(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_1", "aaa")
    monkeypatch.setenv("GEMINI_API_KEY_2", "aaa")
    monkeypatch.setenv("GEMINI_API_KEY_3", "bbb")
    for i in (4, 5):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    assert [l for l, _ in load_keys_from_env()] == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_3"]


def _err(daily: bool, retry: str | None = "7s"):
    det = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel" if daily else "GenerateRequestsPerMinute"}]}]
    if retry:
        det.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": det}})


def test_classify_429():
    assert classify_429(_err(False)) == (False, 7.0)
    assert classify_429(_err(True, None)) == (True, None)


class _Resp:
    text = '{"ok": true}'

    class usage_metadata:
        prompt_token_count = 10
        candidates_token_count = 5
        thoughts_token_count = 0


def test_client_requeues_429_on_another_key(tmp_path):
    p, _ = pool(n=2)
    calls = []

    def factory(key):
        class M:
            async def generate_content(self, model, contents, config):
                calls.append(key)
                if key == "secret-1":
                    raise _err(False)
                return _Resp()

        class C:
            class aio:
                models = M()
        return C()

    g = {"concurrency": 2, "max_output_tokens_per_call": 100, "max_output_tokens_total": 10_000,
         "max_attempts_per_job": 5}
    client = GeminiClient(p, "m", g, tmp_path / "usage.json", client_factory=factory)
    p.keys[1].used_today = 50          # key 1 has more headroom, so it is tried (and 429s) first
    res = asyncio.run(client.generate("hi", {"type": "object"}, 0.5))
    assert res.key_label == "GEMINI_API_KEY_2"
    assert calls[0] == "secret-1" and calls[-1] == "secret-2"
    assert p.keys[0].stats["rate_limited"] == 1
    assert client.totals["output_tokens"] == 5


def test_client_daily_quota_on_all_keys_raises(tmp_path):
    p, _ = pool(n=2)

    def factory(key):
        class M:
            async def generate_content(self, model, contents, config):
                raise _err(True)

        class C:
            class aio:
                models = M()
        return C()

    g = {"concurrency": 2, "max_output_tokens_per_call": 100, "max_output_tokens_total": 10_000,
         "max_attempts_per_job": 5}
    client = GeminiClient(p, "m", g, tmp_path / "usage.json", client_factory=factory)
    with pytest.raises(AllKeysExhausted):
        asyncio.run(client.generate("hi", {"type": "object"}, 0.5))
