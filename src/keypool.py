"""Async pool of Gemini API keys with per-key RPM token buckets and RPD counters.

Keys are only ever referred to by their env label (GEMINI_API_KEY_n) in logs and state files.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from .config import read_json, write_json

log = logging.getLogger(__name__)


class AllKeysExhausted(Exception):
    """Every key is dead for the day. Carries the earliest time a key comes back."""

    def __init__(self, resume_at: datetime):
        self.resume_at = resume_at
        super().__init__(f"all keys exhausted; resume after {resume_at.isoformat()}")


@dataclass
class KeyState:
    label: str
    key: str = field(repr=False)
    rpm: int
    rpd: int
    tokens: float = 0.0
    last_refill: float = 0.0
    used_today: int = 0
    day: str = ""
    cooldown_until: float = 0.0          # monotonic seconds
    dead_until: Optional[datetime] = None
    invalid: bool = False
    consecutive_429: int = 0
    stats: dict = field(default_factory=lambda: {
        "calls": 0, "ok": 0, "rate_limited": 0, "daily_exhausted": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0})

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()[:10]


def load_keys_from_env(n: int = 5) -> list[tuple[str, str]]:
    """Read GEMINI_API_KEY_1..n; drop blanks and duplicates (warns by label, never by value)."""
    keys: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for i in range(1, n + 1):
        label = f"GEMINI_API_KEY_{i}"
        val = (os.environ.get(label) or "").strip()
        if not val:
            continue
        if val in seen:
            log.warning("%s duplicates %s; ignoring it (quota is per key, not per label)",
                        label, seen[val])
            continue
        seen[val] = label
        keys.append((label, val))
    return keys


class KeyPool:
    """Dispatches each request to the key with the most headroom.

    - RPM: token bucket per key (capacity = rpm, refills rpm/60 per second).
    - RPD: counter per key, reset at midnight in `reset_tz`; persisted so a resumed run knows
      what it already spent today.
    - 429 per-minute: key cools for retry-after (or exp backoff + jitter); job goes elsewhere.
    - 429 per-day: key is dead until the next reset.
    """

    def __init__(
        self,
        keys: list[tuple[str, str]],
        rpm: int,
        rpd: int,
        overrides: Optional[dict] = None,
        reset_tz: str = "America/Los_Angeles",
        state_path: Optional[Path] = None,
        backoff_base: float = 2.0,
        backoff_max: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        now: Optional[Callable[[], datetime]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        seed: int = 0,
    ):
        if not keys:
            raise ValueError("no Gemini keys found (set GEMINI_API_KEY_1..5 in .env)")
        self.tz = ZoneInfo(reset_tz)
        self.clock, self.sleep = clock, sleep
        self.now = now or (lambda: datetime.now(self.tz))
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.state_path = state_path
        self._rng = random.Random(seed)
        overrides = overrides or {}
        self.keys: list[KeyState] = []
        t0 = self.clock()
        for label, key in keys:
            o = overrides.get(label, {})
            k = KeyState(label=label, key=key, rpm=int(o.get("rpm", rpm)), rpd=int(o.get("rpd", rpd)))
            k.tokens, k.last_refill, k.day = float(k.rpm), t0, self._today()
            self.keys.append(k)
        self._load_state()

    # ---------------------------------------------------------------- persistence
    def _today(self) -> str:
        return self.now().astimezone(self.tz).date().isoformat()

    def _next_reset(self) -> datetime:
        n = self.now().astimezone(self.tz)
        return datetime.combine(n.date() + timedelta(days=1), datetime.min.time(), self.tz)

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        state = read_json(self.state_path)
        for k in self.keys:
            s = state.get(k.label)
            if not s or s.get("fingerprint") != k.fingerprint or s.get("day") != k.day:
                continue  # different key under this label, or a new quota day
            k.used_today = int(s.get("used_today", 0))
            if s.get("dead_until"):
                du = datetime.fromisoformat(s["dead_until"])
                k.dead_until = du if du > self.now() else None

    def save_state(self) -> None:
        if not self.state_path:
            return
        write_json(self.state_path, {
            k.label: {"fingerprint": k.fingerprint, "day": k.day, "used_today": k.used_today,
                      "dead_until": k.dead_until.isoformat() if k.dead_until else None,
                      "invalid": k.invalid}
            for k in self.keys})

    # ---------------------------------------------------------------- bookkeeping
    def _refresh(self, k: KeyState) -> None:
        t = self.clock()
        k.tokens = min(float(k.rpm), k.tokens + (t - k.last_refill) * k.rpm / 60.0)
        k.last_refill = t
        today = self._today()
        if today != k.day:                       # quota day rolled over
            k.day, k.used_today = today, 0
            if k.dead_until and k.dead_until <= self.now():
                k.dead_until = None
        if k.dead_until and k.dead_until <= self.now():
            k.dead_until = None
        if k.used_today >= k.rpd and not k.dead_until:
            k.dead_until = self._next_reset()

    def _alive(self, k: KeyState) -> bool:
        return not k.invalid and k.dead_until is None

    def headroom(self, k: KeyState) -> float:
        """0..2: share of daily quota left plus share of the minute bucket left."""
        return (k.rpd - k.used_today) / k.rpd + k.tokens / k.rpm

    async def acquire(self) -> KeyState:
        """Wait until some key can take a request, consume one unit from it, return it."""
        while True:
            for k in self.keys:
                self._refresh(k)
            alive = [k for k in self.keys if self._alive(k)]
            if not alive:
                dead = [k.dead_until for k in self.keys if k.dead_until]
                self.save_state()
                raise AllKeysExhausted(min(dead) if dead else self._next_reset())
            t = self.clock()
            ready = [k for k in alive if k.cooldown_until <= t and k.tokens >= 1.0]
            if ready:
                k = max(ready, key=self.headroom)
                k.tokens -= 1.0
                k.used_today += 1
                k.stats["calls"] += 1
                return k
            waits = []
            for k in alive:
                w_tok = 0.0 if k.tokens >= 1 else (1.0 - k.tokens) * 60.0 / k.rpm
                waits.append(max(k.cooldown_until - t, w_tok, 0.05))
            await self.sleep(min(waits))

    def report_success(self, k: KeyState, input_tokens: int = 0, output_tokens: int = 0) -> None:
        k.consecutive_429 = 0
        k.stats["ok"] += 1
        k.stats["input_tokens"] += int(input_tokens or 0)
        k.stats["output_tokens"] += int(output_tokens or 0)

    def report_rate_limited(self, k: KeyState, retry_after: Optional[float], daily: bool) -> None:
        if daily:
            k.stats["daily_exhausted"] += 1
            k.dead_until = self._next_reset()
            log.warning("%s hit its daily quota; dead until %s", k.label, k.dead_until.isoformat())
            self.save_state()
            return
        k.stats["rate_limited"] += 1
        k.consecutive_429 += 1
        if retry_after is None:
            back = min(self.backoff_max, self.backoff_base * 2 ** (k.consecutive_429 - 1))
            retry_after = back * (1.0 + self._rng.random() * 0.5)   # jitter
        k.cooldown_until = self.clock() + retry_after
        k.tokens = 0.0
        log.info("%s rate-limited; cooling %.1fs", k.label, retry_after)

    def report_error(self, k: KeyState, invalid: bool = False) -> None:
        k.stats["errors"] += 1
        if invalid:
            k.invalid = True
            log.error("%s rejected as invalid; removed from pool", k.label)
            return
        k.cooldown_until = self.clock() + min(self.backoff_max, self.backoff_base * 2)

    def stats(self) -> dict:
        return {k.label: {**k.stats, "used_today": k.used_today, "invalid": k.invalid,
                          "dead_until": k.dead_until.isoformat() if k.dead_until else None}
                for k in self.keys}
