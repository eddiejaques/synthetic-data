"""Gemini client: structured JSON calls routed through the KeyPool, with usage accounting."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel

from .config import write_json
from .keypool import KeyPool, KeyState

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """The next call could push output tokens over MAX_OUTPUT_TOKENS_TOTAL."""


class JobFailed(Exception):
    """A job used up its transport attempts (429s / 5xx) without a response."""


@dataclass
class CallResult:
    text: str
    key_label: str
    input_tokens: int
    output_tokens: int


def classify_429(err: Exception) -> tuple[bool, Optional[float]]:
    """Return (is_daily_quota, retry_after_seconds) from a RESOURCE_EXHAUSTED error body."""
    body = getattr(err, "details", None) or {}
    if isinstance(body, dict) and "error" in body:
        body = body["error"]
    details = body.get("details", []) if isinstance(body, dict) else []
    daily, retry = False, None
    for d in details or []:
        t = str(d.get("@type", ""))
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []):
                qid = (str(v.get("quotaId", "")) + str(v.get("quotaMetric", ""))).lower()
                if "perday" in qid or "per_day" in qid or "daily" in qid:
                    daily = True
        if t.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                retry = float(m.group(1))
    msg = str(getattr(err, "message", "") or err).lower()
    if "per day" in msg or "perday" in msg:
        daily = True
    return daily, retry


def _status(err: Exception) -> tuple[Optional[int], str]:
    return getattr(err, "code", None), str(getattr(err, "status", "") or "")


class GeminiClient:
    """Async wrapper: picks a key, calls the model, re-queues on 429/5xx, counts tokens."""

    def __init__(self, pool: KeyPool, model: str, gcfg: dict, usage_path: Path,
                 client_factory: Optional[Callable[[str], Any]] = None):
        self.pool, self.model, self.g = pool, model, gcfg
        self.usage_path = usage_path
        self.sem = asyncio.Semaphore(int(gcfg.get("concurrency", 4)))
        self._clients: dict[str, Any] = {}
        self._factory = client_factory or self._default_factory
        self.totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "transport_retries": 0}

    def _default_factory(self, key: str) -> Any:
        from google import genai
        from google.genai import types
        return genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=int(self.g.get("request_timeout_s", 180)) * 1000))

    def _client(self, k: KeyState) -> Any:
        if k.label not in self._clients:
            self._clients[k.label] = self._factory(k.key)
        return self._clients[k.label]

    def _check_budget(self) -> None:
        cap = int(self.g["max_output_tokens_total"])
        if self.totals["output_tokens"] + int(self.g["max_output_tokens_per_call"]) > cap:
            raise BudgetExceeded(
                f"output tokens {self.totals['output_tokens']:,} + one more call could exceed "
                f"MAX_OUTPUT_TOKENS_TOTAL={cap:,}")

    def write_usage(self) -> None:
        write_json(self.usage_path, {"model": self.model, "totals": self.totals,
                                     "per_key": self.pool.stats()})
        self.pool.save_state()

    async def generate(self, prompt: str, schema: type[BaseModel] | dict, temperature: float,
                       system: Optional[str] = None) -> CallResult:
        """One structured call. Transport failures are retried on other keys; content is not checked here."""
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=int(self.g["max_output_tokens_per_call"]),
            system_instruction=system,
        )
        attempts = int(self.g.get("max_attempts_per_job", 8))
        last: Optional[Exception] = None
        for attempt in range(attempts):
            self._check_budget()
            k = await self.pool.acquire()          # may raise AllKeysExhausted
            try:
                async with self.sem:
                    resp = await self._client(k).aio.models.generate_content(
                        model=self.model, contents=prompt, config=cfg)
            except errors.APIError as e:
                last = e
                code, status = _status(e)
                self.totals["transport_retries"] += 1
                if code == 429 or status == "RESOURCE_EXHAUSTED":
                    daily, retry = classify_429(e)
                    self.pool.report_rate_limited(k, retry, daily)
                elif code in (400, 401, 403) and ("api key" in str(e).lower() or code in (401, 403)):
                    self.pool.report_error(k, invalid=True)
                elif code == 400:
                    raise                                   # our request is wrong; don't loop
                else:
                    self.pool.report_error(k)
                continue
            except (asyncio.TimeoutError, OSError) as e:      # network / timeout
                last = e
                self.totals["transport_retries"] += 1
                self.pool.report_error(k)
                continue
            um = getattr(resp, "usage_metadata", None)
            it = int(getattr(um, "prompt_token_count", 0) or 0)
            ot = int(getattr(um, "candidates_token_count", 0) or 0) + \
                int(getattr(um, "thoughts_token_count", 0) or 0)
            self.pool.report_success(k, it, ot)
            self.totals["calls"] += 1
            self.totals["input_tokens"] += it
            self.totals["output_tokens"] += ot
            self.write_usage()
            return CallResult(text=resp.text or "", key_label=k.label, input_tokens=it, output_tokens=ot)
        raise JobFailed(f"gave up after {attempts} transport attempts: {last}")


def parse_json(text: str) -> Any:
    """Parse model JSON; tolerate a stray ```json fence."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):] if "{" in t else t
    return json.loads(t)
