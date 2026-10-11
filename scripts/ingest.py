"""Shared parallel ingestion engine for the PostHog and Mixpanel uploaders.

Spark-style, without Spark: the data is cut into fixed chunks (partitions) that respect each API's
per-request limits; a pool of workers sends them in parallel under a shared rate limiter (requests
per second and MB per minute); 429/5xx are retried with exponential backoff + jitter, honouring
Retry-After; finished chunks are recorded so a rerun resumes and never re-sends them.

PySpark would add a JVM for no gain at this size (hundreds of thousands of events). The chunking
is deterministic, so chunk ids are stable across runs.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
RETRY_STATUS = {429, 500, 502, 503, 504}


def load_limits(service: str) -> dict:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    return dict((cfg.get("ingestion") or {}).get(service) or {})


# ----------------------------------------------------------------------------- chunking
@dataclass
class Chunk:
    chunk_id: str
    records: list[str]          # one serialized JSON record per item
    nbytes: int


def chunk_list(name: str, lines: Iterable[str], max_items: int, max_bytes: int, overhead: int = 2) -> list[Chunk]:
    """Cut serialized records into chunks of <= max_items records and <= max_bytes (uncompressed)."""
    chunks: list[Chunk] = []
    cur: list[str] = []
    size = part = 0
    for line in lines:
        if not line.strip():
            continue
        b = len(line.encode()) + overhead
        if b > max_bytes:
            raise ValueError(f"{name}: a single record is {b:,} bytes, above the {max_bytes:,} byte limit")
        if cur and (len(cur) >= max_items or size + b > max_bytes):
            chunks.append(Chunk(f"{name}#{part}", cur, size))
            cur, size, part = [], 0, part + 1
        cur.append(line)
        size += b
    if cur:
        chunks.append(Chunk(f"{name}#{part}", cur, size))
    return chunks


def chunk_records(files: list[Path], max_items: int, max_bytes: int, overhead: int = 2,
                  base_dir: Optional[Path] = None) -> list[Chunk]:
    """Chunk every JSONL file. Chunk ids are `<path relative to base_dir>#<n>`, stable across runs:
    base files keep their plain names, later batches are `<run_id>/batch_....jsonl`."""
    def name(fp: Path) -> str:
        return str(fp.relative_to(base_dir)) if base_dir else fp.name
    return [c for fp in files for c in chunk_list(name(fp), fp.read_text().splitlines(), max_items, max_bytes, overhead)]


def content_id(chunk: Chunk) -> str:
    """Id that changes when the content changes (for files that are rewritten, like profiles)."""
    return chunk.chunk_id + ":" + hashlib.sha256("\n".join(chunk.records).encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------- rate limiting
class RateLimiter:
    """Two token buckets shared by all workers: requests/second and bytes/minute."""

    def __init__(self, requests_per_second: float, mb_per_minute: float):
        self.rps = float(requests_per_second)
        self.bpm = float(mb_per_minute) * 1024 * 1024
        self.req_tokens, self.byte_tokens = self.rps, self.bpm
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, nbytes: int) -> None:
        nbytes = min(nbytes, int(self.bpm))           # a single huge chunk must still pass eventually
        while True:
            with self.lock:
                now = time.monotonic()
                dt, self.t = now - self.t, now
                self.req_tokens = min(self.rps, self.req_tokens + dt * self.rps)
                self.byte_tokens = min(self.bpm, self.byte_tokens + dt * self.bpm / 60)
                if self.req_tokens >= 1 and self.byte_tokens >= nbytes:
                    self.req_tokens -= 1
                    self.byte_tokens -= nbytes
                    return
                wait = max((1 - self.req_tokens) / self.rps, (nbytes - self.byte_tokens) * 60 / self.bpm, 0.01)
            time.sleep(min(wait, 5))


# ----------------------------------------------------------------------------- progress
class Progress:
    """Finished chunk ids, persisted after each success; keyed to the destination."""

    def __init__(self, path: Path, destination: str, reset: bool = False):
        self.path = path
        self.dest = hashlib.sha256(destination.encode()).hexdigest()[:12]   # never store keys in clear
        self.lock = threading.Lock()
        state = json.loads(path.read_text()) if path.exists() and not reset else {}
        if state.get("destination") != self.dest:
            state = {"destination": self.dest, "done": []}
        self.state = state
        self.done = set(state["done"])

    def mark(self, chunk_id: str) -> None:
        with self.lock:
            self.done.add(chunk_id)
            self.state["done"] = sorted(self.done)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            tmp.replace(self.path)


# ----------------------------------------------------------------------------- sending
class FatalUpload(Exception):
    pass


_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def post_with_retry(url: str, *, max_tries: int = 8, **kw) -> requests.Response:
    """POST; retry 429/5xx/network errors with backoff + jitter (Retry-After wins); raise on other 4xx."""
    for attempt in range(max_tries):
        try:
            r = session().post(url, timeout=kw.pop("timeout", 120), **kw)
        except requests.RequestException as e:
            r, err = None, e
        else:
            if r.status_code < 300:
                return r
            if r.status_code not in RETRY_STATUS:
                raise FatalUpload(f"HTTP {r.status_code}: {r.text[:500]}")
            err = f"HTTP {r.status_code}"
        ra = r.headers.get("Retry-After") if r is not None else None
        delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(60.0, 2 ** attempt) * (0.5 + random.random())
        time.sleep(delay)
    raise FatalUpload(f"gave up after {max_tries} tries: {err}")


def run(chunks: list[Chunk], send: Callable[[Chunk], None], *, workers: int, limiter: RateLimiter,
        progress: Progress, label: str, dry_run: bool = False) -> int:
    """Send every unfinished chunk in parallel. Returns the number of chunks sent now."""
    todo = [c for c in chunks if c.chunk_id not in progress.done]
    total_mb = sum(c.nbytes for c in todo) / 1024 / 1024
    n_items = sum(len(c.records) for c in todo)
    print(f"{label}: {len(chunks)} chunks, {len(chunks) - len(todo)} already sent, "
          f"{len(todo)} to send ({n_items:,} records, {total_mb:,.1f} MB) with {workers} workers, "
          f"<= {limiter.rps:g} req/s and {limiter.bpm / 1024 / 1024:,.0f} MB/min")
    if dry_run or not todo:
        est = max(len(todo) / limiter.rps, total_mb / (limiter.bpm / 1024 / 1024) * 60)
        print(f"{label}: estimated minimum time at these limits: {est:,.0f} s" + (" (dry run)" if dry_run else ""))
        return 0
    sent = 0
    t0 = time.monotonic()
    stop = threading.Event()

    def job(c: Chunk) -> str:
        if stop.is_set():
            return ""
        limiter.acquire(c.nbytes)
        send(c)
        progress.mark(c.chunk_id)
        return c.chunk_id

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, c) for c in todo]
        try:
            for f in as_completed(futures):
                if f.result():
                    sent += 1
                    if sent % 50 == 0 or sent == len(todo):
                        el = time.monotonic() - t0
                        print(f"{label}: {sent}/{len(todo)} chunks ({sent / el:.1f}/s)")
        except FatalUpload as e:
            stop.set()
            for f in futures:
                f.cancel()
            raise SystemExit(f"{label}: stopped: {e}\nFinished chunks are saved; fix the cause and rerun to resume.")
    return sent


def gz(body: bytes) -> bytes:
    return gzip.compress(body, compresslevel=6)


def files_in(directory: str | Path, pattern: str = "batch_*.jsonl") -> list[Path]:
    """Base batch files at the top level first, then every later batch's sub-folder."""
    d = Path(directory)
    files = sorted(d.glob(pattern)) + sorted(f for sub in sorted(d.iterdir()) if sub.is_dir()
                                             and not sub.name.startswith(".") for f in sub.glob(pattern))
    if not files:
        raise SystemExit(f"no {pattern} files in {directory}; run `python main.py export` first")
    return files


def jsonl(records: Iterable[str]) -> bytes:
    return ("\n".join(records) + "\n").encode()


def opt(value: Optional[float], default: float) -> float:
    return default if value is None else value
