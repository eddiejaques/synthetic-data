"""BigQuery schema.json (+ BQ-safe NDJSON: `$` stripped from field names) and load script."""
from __future__ import annotations

import json
from pathlib import Path

from ..marketing import channel_for

S, I, F, B, T = "STRING", "INTEGER", "FLOAT", "BOOLEAN", "TIMESTAMP"
FIELDS = [
    ("event", S, "REQUIRED"), ("timestamp", T, "REQUIRED"), ("distinct_id", S, "REQUIRED"),
    ("insert_id", S, "REQUIRED"), ("session_id", S, "NULLABLE"), ("user_id", S, "NULLABLE"),
    ("anonymous_id", S, "REQUIRED"), ("user_segment", S, "REQUIRED"), ("platform", S, "REQUIRED"),
    ("device_id", S, "REQUIRED"), ("device_type", S, "REQUIRED"), ("device_brand", S, "NULLABLE"),
    ("device_model", S, "NULLABLE"), ("os", S, "NULLABLE"), ("os_version", S, "NULLABLE"),
    ("app_version", S, "NULLABLE"), ("viewport_width", I, "NULLABLE"), ("viewport_height", I, "NULLABLE"),
    ("country", S, "NULLABLE"), ("locale", S, "NULLABLE"), ("event_source", S, "NULLABLE"),
    ("screen_name", S, "NULLABLE"), ("previous_screen", S, "NULLABLE"), ("lane_id", S, "NULLABLE"),
    ("lane_name", S, "NULLABLE"), ("lane_type", S, "NULLABLE"), ("lane_position", I, "NULLABLE"),
    ("content_id", S, "NULLABLE"), ("title_id", S, "NULLABLE"), ("title", S, "NULLABLE"),
    ("episode_title", S, "NULLABLE"), ("season", I, "NULLABLE"), ("episode", I, "NULLABLE"),
    ("genre", S, "NULLABLE"), ("content_type", S, "NULLABLE"), ("licence_type", S, "NULLABLE"),
    ("asset_position", I, "NULLABLE"), ("playback_event", S, "NULLABLE"), ("playback_session_id", S, "NULLABLE"),
    ("duration_seconds", I, "NULLABLE"), ("watch_time_seconds", I, "NULLABLE"), ("completion_rate", F, "NULLABLE"),
    ("method", S, "NULLABLE"), ("success", B, "NULLABLE"), ("plan", S, "NULLABLE"), ("price", F, "NULLABLE"),
    ("amount", F, "NULLABLE"), ("currency", S, "NULLABLE"), ("payment_type", S, "NULLABLE"),
    ("transaction_id", S, "NULLABLE"), ("failure_reason", S, "NULLABLE"), ("attempt_number", I, "NULLABLE"),
    ("anon_distinct_id", S, "NULLABLE"),
    ("utm_source", S, "NULLABLE"), ("utm_medium", S, "NULLABLE"), ("utm_campaign", S, "NULLABLE"),
    ("utm_content", S, "NULLABLE"), ("utm_term", S, "NULLABLE"), ("referrer", S, "NULLABLE"),
    ("referring_domain", S, "NULLABLE"), ("gclid", S, "NULLABLE"), ("fbclid", S, "NULLABLE"),
    ("ttclid", S, "NULLABLE"), ("landing_url", S, "NULLABLE"),
    ("channel", S, "NULLABLE"),          # derived default channel grouping
]


def schema() -> list[dict]:
    out = [{"name": n, "type": t, "mode": m} for n, t, m in FIELDS]
    out.insert(27, {"name": "assets_list", "type": "RECORD", "mode": "REPEATED", "fields": [
        {"name": "content_id", "type": S, "mode": "REQUIRED"}, {"name": "title", "type": S, "mode": "NULLABLE"},
        {"name": "asset_position", "type": I, "mode": "REQUIRED"}, {"name": "licence_type", "type": S, "mode": "NULLABLE"}]})
    return out


LOAD_SH = """#!/usr/bin/env bash
# Load the events (day-partitioned, clustered), the identity tables and the person view.
# Needs the gcloud SDK (`bq`) and BQ_PROJECT / BQ_DATASET (see .env.example).
set -euo pipefail
cd "$(dirname "$0")"
: "${BQ_PROJECT:?set BQ_PROJECT}"; BQ_DATASET="${BQ_DATASET:-joyn_synthetic}"; TABLE="${BQ_TABLE:-events}"
DS="$BQ_PROJECT:$BQ_DATASET"
bq --project_id="$BQ_PROJECT" mk -f --dataset --location=EU "$DS" >/dev/null || true
bq --project_id="$BQ_PROJECT" rm -f -t "$DS.$TABLE" || true
bq --project_id="$BQ_PROJECT" mk --table \\
  --time_partitioning_field timestamp --time_partitioning_type DAY \\
  --clustering_fields event,platform \\
  "$DS.$TABLE" schema.json
bq --project_id="$BQ_PROJECT" load --source_format=NEWLINE_DELIMITED_JSON "$DS.$TABLE" events_bq.ndjson schema.json
# identity: one row per device link (anonymous id -> user id), one row per user (email hash, crm id, ...)
bq --project_id="$BQ_PROJECT" load --replace --source_format=CSV --skip_leading_rows=1 \\
  "$DS.identity_map" ../identity_map.csv identity_map_schema.json
bq --project_id="$BQ_PROJECT" load --replace --source_format=CSV --skip_leading_rows=1 --autodetect \\
  --allow_quoted_newlines "$DS.users" ../users.csv
sed "s/{{PROJECT}}/$BQ_PROJECT/g; s/{{DATASET}}/$BQ_DATASET/g; s/{{TABLE}}/$TABLE/g" views.sql \\
  | bq --project_id="$BQ_PROJECT" query --use_legacy_sql=false
echo "loaded $DS.$TABLE (partitioned by DATE(timestamp), clustered by event, platform), identity_map, users"
echo "view: $DS.events_identified  (person_id = one id per human across devices and logins)"
"""

VIEWS_SQL = """-- One person id per human: the user id once known, else the device's anonymous id.
-- Pre-login events of a device that later logs in are attributed to that user (as PostHog/Mixpanel do).
CREATE OR REPLACE VIEW `{{PROJECT}}.{{DATASET}}.events_identified` AS
SELECT
  e.*,
  COALESCE(e.user_id, m.user_id, e.anonymous_id) AS person_id,
  COALESCE(e.user_id, m.user_id) IS NOT NULL      AS is_identified_person,
  u.email_sha256,
  u.crm_id
FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}` AS e
LEFT JOIN `{{PROJECT}}.{{DATASET}}.identity_map` AS m ON m.anon_distinct_id = e.anonymous_id
LEFT JOIN `{{PROJECT}}.{{DATASET}}.users` AS u ON u.user_id = COALESCE(e.user_id, m.user_id);
"""

IDENTITY_SCHEMA = [
    {"name": "user_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "anon_distinct_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "device_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "platform", "type": "STRING", "mode": "NULLABLE"},
    {"name": "first_identified_at", "type": "TIMESTAMP", "mode": "NULLABLE"},
]


def write_bigquery(events: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "schema.json").write_text(json.dumps(schema(), indent=1))
    with open(events) as fin, open(out_dir / "events_bq.ndjson", "w") as fout:
        for line in fin:
            e = json.loads(line)
            row = {k.lstrip("$"): v for k, v in e.items()}
            row["channel"] = channel_for(e)
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out_dir / "views.sql").write_text(VIEWS_SQL)
    (out_dir / "identity_map_schema.json").write_text(json.dumps(IDENTITY_SCHEMA, indent=1))
    sh = out_dir / "load_bigquery.sh"
    sh.write_text(LOAD_SH)
    sh.chmod(0o755)
