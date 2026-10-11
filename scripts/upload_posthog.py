#!/usr/bin/env python3
"""Upload out/posthog_batch/*.jsonl to PostHog's /batch endpoint, in parallel, within limits.

historical_migration=true routes the events to PostHog's backfill pipeline (it doesn't compete
with live traffic). Event uuids are UUID5s of $insert_id, so a re-send does not double-count.

Env: POSTHOG_PROJECT_API_KEY (project key, phc_...), POSTHOG_HOST (default https://eu.i.posthog.com).
Limits: config.yaml -> ingestion.posthog (request size, events per request, workers, req/s, MB/min).

Usage:
  python scripts/upload_posthog.py --dry-run          # plan: chunks, MB, minimum time
  python scripts/upload_posthog.py                    # send; rerun to resume after any stop
  python scripts/upload_posthog.py --reset            # forget progress and send everything again
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # noqa: E402


def main() -> int:
    load_dotenv(ingest.ROOT / ".env")
    lim = ingest.load_limits("posthog")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(ingest.ROOT / "out" / "posthog_batch"))
    ap.add_argument("--workers", type=int, default=int(lim.get("workers", 8)))
    ap.add_argument("--rps", type=float, default=float(lim.get("max_requests_per_second", 20)))
    ap.add_argument("--mb-per-min", type=float, default=float(lim.get("max_mb_per_minute", 600)))
    ap.add_argument("--no-gzip", action="store_true", help="send uncompressed JSON")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("POSTHOG_PROJECT_API_KEY", "")
    host = os.environ.get("POSTHOG_HOST", "https://eu.i.posthog.com").rstrip("/")
    if not key and not a.dry_run:
        sys.exit("set POSTHOG_PROJECT_API_KEY in .env")
    files = ingest.files_in(a.dir)
    max_bytes = int(float(lim.get("max_request_mb", 15)) * 1024 * 1024)
    chunks = ingest.chunk_records(files, int(lim.get("events_per_request", 1000)), max_bytes, base_dir=Path(a.dir))
    progress = ingest.Progress(Path(a.dir).parent / ".upload_state" / "posthog.json", f"{host}|{key}", a.reset)
    limiter = ingest.RateLimiter(a.rps, a.mb_per_min)
    head = ('{"api_key":%s,"historical_migration":true,"batch":[' % ingest.json.dumps(key)).encode()

    def send(c: ingest.Chunk) -> None:
        body = head + ",".join(c.records).encode() + b"]}"
        headers = {"Content-Type": "application/json"}
        if not a.no_gzip:
            body, headers["Content-Encoding"] = ingest.gz(body), "gzip"
        ingest.post_with_retry(f"{host}/batch/", data=body, headers=headers)

    ingest.run(chunks, send, workers=a.workers, limiter=limiter, progress=progress, label="posthog",
               dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
