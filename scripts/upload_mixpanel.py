#!/usr/bin/env python3
"""Upload out/mixpanel_import/*.jsonl to Mixpanel's /import API, in parallel, within limits;
optionally the user profiles (out/mixpanel_profiles.jsonl) to /engage.

/import limits (config.yaml -> ingestion.mixpanel): <= 2,000 events and <= 10 MB uncompressed per
request, <= 2 GB uncompressed per minute per project; 429 means slow down. Bodies are gzipped and
sent with strict=1, so a malformed record stops the run with Mixpanel's error message. $insert_id
makes re-sends idempotent.

Auth, either:
  - service account: MIXPANEL_PROJECT_ID, MIXPANEL_SERVICE_ACCOUNT_USER, MIXPANEL_SERVICE_ACCOUNT_SECRET
  - or just the project token (works on the Free plan): MIXPANEL_PROJECT_TOKEN (sent as the basic-auth
    username with an empty password, as Mixpanel's ingestion docs allow)
MIXPANEL_PROJECT_TOKEN is also needed for --profiles.
MIXPANEL_API_HOST: default https://api-eu.mixpanel.com (EU data residency); https://api.mixpanel.com for US.

Usage:
  python scripts/upload_mixpanel.py --dry-run
  python scripts/upload_mixpanel.py [--profiles]     # rerun to resume after any stop
  python scripts/upload_mixpanel.py --profiles-only
  python scripts/upload_mixpanel.py --reset          # forget progress and send everything again
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # noqa: E402


def main() -> int:
    load_dotenv(ingest.ROOT / ".env")
    lim = ingest.load_limits("mixpanel")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(ingest.ROOT / "out" / "mixpanel_import"))
    ap.add_argument("--workers", type=int, default=int(lim.get("workers", 10)))
    ap.add_argument("--rps", type=float, default=float(lim.get("max_requests_per_second", 30)))
    ap.add_argument("--mb-per-min", type=float, default=float(lim.get("max_mb_per_minute", 1500)))
    ap.add_argument("--profiles", action="store_true", help="also upload mixpanel_profiles.jsonl")
    ap.add_argument("--profiles-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    env = os.environ
    host = env.get("MIXPANEL_API_HOST", "https://api-eu.mixpanel.com").rstrip("/")
    need = ["MIXPANEL_PROJECT_ID", "MIXPANEL_SERVICE_ACCOUNT_USER", "MIXPANEL_SERVICE_ACCOUNT_SECRET"]
    use_sa = all(env.get(k) for k in need)
    token = env.get("MIXPANEL_PROJECT_TOKEN", "")
    if not a.dry_run and not a.profiles_only and not (use_sa or token):
        sys.exit("set either MIXPANEL_PROJECT_TOKEN, or " + ", ".join(need) + " in .env")
    limiter = ingest.RateLimiter(a.rps, a.mb_per_min)
    state_dir = Path(a.dir).parent / ".upload_state"
    max_bytes = int(float(lim.get("max_request_mb", 9)) * 1024 * 1024)

    if not a.profiles_only:
        chunks = ingest.chunk_records(ingest.files_in(a.dir), int(lim.get("events_per_request", 2000)), max_bytes,
                                      base_dir=Path(a.dir))
        dest = env.get("MIXPANEL_PROJECT_ID") if use_sa else token
        progress = ingest.Progress(state_dir / "mixpanel_events.json", f"{host}|{dest}", a.reset)
        if use_sa:
            auth = (env["MIXPANEL_SERVICE_ACCOUNT_USER"], env["MIXPANEL_SERVICE_ACCOUNT_SECRET"])
            params = {"strict": "1", "project_id": env["MIXPANEL_PROJECT_ID"]}
        else:
            auth, params = (token, ""), {"strict": "1"}          # project-token auth (Free plan friendly)
        print(f"mixpanel events: auth = {'service account' if use_sa else 'project token'}")

        def send(c: ingest.Chunk) -> None:
            r = ingest.post_with_retry(
                f"{host}/import", params=params,
                data=ingest.gz(ingest.jsonl(c.records)), auth=auth,
                headers={"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip"})
            body = r.json() if r.content else {}
            if body.get("num_records_imported", len(c.records)) != len(c.records):
                raise ingest.FatalUpload(f"{c.chunk_id}: imported {body.get('num_records_imported')} of {len(c.records)}: {body}")

        ingest.run(chunks, send, workers=a.workers, limiter=limiter, progress=progress, label="mixpanel events",
                   dry_run=a.dry_run)

    if a.profiles or a.profiles_only:
        if not token and not a.dry_run:
            sys.exit("set MIXPANEL_PROJECT_TOKEN in .env for --profiles")
        path = Path(a.dir).parent / "mixpanel_profiles.jsonl"
        ops = []
        for line in path.read_text().splitlines():   # Engage takes one operation per record
            r = json.loads(line)
            for op in ("$set_once", "$set"):
                if r.get(op):
                    ops.append(json.dumps({"$token": token, "$distinct_id": r["$distinct_id"], op: r[op]}))
        chunks = ingest.chunk_list("profiles", ops, int(lim.get("profiles_per_request", 2000)), max_bytes)
        for c in chunks:                       # profiles are rewritten on every export: id by content
            c.chunk_id = ingest.content_id(c)
        progress = ingest.Progress(state_dir / "mixpanel_profiles.json", f"{host}|{token}", a.reset)

        def send_profiles(c: ingest.Chunk) -> None:
            r = ingest.post_with_retry(f"{host}/engage", params={"verbose": "1"},
                                       data=("[" + ",".join(c.records) + "]").encode(),
                                       headers={"Content-Type": "application/json"})
            body = r.json() if r.content else {}
            if body.get("status", 1) != 1:
                raise ingest.FatalUpload(f"{c.chunk_id}: {body}")

        ingest.run(chunks, send_profiles, workers=min(a.workers, 4), limiter=limiter, progress=progress,
                   label="mixpanel profiles", dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
