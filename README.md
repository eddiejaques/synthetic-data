# Joyn synthetic event dataset

Synthetic, internally consistent product-analytics events for a German OTT service (Joyn), for
funnel, retention, YoY and device demos in PostHog, Mixpanel, BigQuery and Looker Studio.

**LLM generates behaviour, Python guarantees correctness.** Gemini Flash-Lite writes *what people
do* (which screens, lanes, titles, how much they watch, when they give up). Python owns everything
that must be right: user counts, segments, devices, ids, timestamps, heartbeats, identity, billing,
and every number the validator checks.

```
catalog (Gemini, ~10 calls) ─┐
personas (Python, seeded) ───┼─> journeys (Gemini, 1 call / user / active month) ─> expand (Python) ─> export ─> validate
                             └─ every hard target is set here, by construction
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add GEMINI_API_KEY_1..5, confirm GEMINI_MODEL
.venv/bin/python main.py all --offline     # 1 min, no API calls; writes to cache_offline/, out_offline/
.venv/bin/python main.py all               # the real run (~1,100–1,450 Gemini calls)
```

If the keys run out of daily quota the run checkpoints and prints `Resume after HH:MM`; then:

```bash
.venv/bin/python main.py journeys --resume && .venv/bin/python main.py expand export validate
```

### CLI

| Command | What it does |
|---|---|
| `catalog` | Seed table → seasons, German episode titles, ~15 extra titles, 12–18 lanes (cached in `cache/catalog/`) |
| `personas` | 1,000 seeded briefs → `cache/personas.json` |
| `journeys [--resume] [--refresh u0001,u0002] [--limit N]` | One Gemini call per user per active month, cached in `cache/journeys/<user>_<period>.json` |
| `expand` | Validated journeys → `out/events.ndjson` (+ `identity_map.csv`, `expand_meta.json`) |
| `export [--sheets SHEET_ID]` | All output formats (below) |
| `validate` | Recomputes every target from `events.ndjson` → `out/validation_report.md`; exit 1 on failure |
| `all [--offline]` | Everything in order. `--offline` uses the Python fallbacks instead of Gemini |
| `generate --spec FILE [--offline] [--limit N]` | Add a batch (more users / new months), then expand, export and validate everything |
| `batches` | List base + batches with their journey progress |

Exit codes: `0` ok, `1` validation failed, `75` keys exhausted or token budget hit (resume later).

## Gemini, keys and cost

- Keys: `GEMINI_API_KEY_1..5` in `.env` (never logged; logs use the env label). Duplicate keys are
  dropped, because quota is per key. A key Google rejects as invalid is removed from the pool for the run.
- Model: `GEMINI_MODEL` (default `gemini-3.5-flash-lite`). **Confirm it** with
  `client.models.list()` for your keys.
- Limits: set `gemini.rpm` / `gemini.rpd` (or `per_key_overrides`) in `config.yaml` to the quotas
  AI Studio shows for your keys. The pool sends each call to the key with the most headroom, cools a
  key on a per-minute 429 (honouring `retryDelay`, else exponential backoff + jitter) and re-queues the
  job elsewhere. On a daily 429 the key is dead until midnight Pacific. Daily usage is persisted in
  `logs/keypool_state.json`, so a resumed run knows what it already spent.
- Cost guard: `gemini.max_output_tokens_total` (default 8M). Usage per key is in `logs/gemini_usage.json`.
- Measured on a 24-journey live sample: ~4.6k input + ~1.8k output tokens per journey call, ~1.3
  calls per journey (retries). Full run ≈ 6–7M input and ~2.5M output tokens. At 15 RPM × 3 keys it
  takes about 30–40 minutes.
- Reproducibility: LLM output is not deterministic, so **the cache is the reproducibility mechanism**.
  Expansion is byte-identical given the same cache and seed (tested across processes).

### When the model gets it wrong

Each journey goes through `validate_journey.py`. Mechanical slips are repaired silently: sorting
sessions, adding the lane a click came from, inserting a skipped detail screen, dropping a click that
leads nowhere, aligning `outcome` with the watch fraction, inserting a login for a registered user on
a new device, and routing a gated click to the right wall. Semantic breaks are violations: wrong
gating, invented ids, unreleased episodes, a missing required registration, a non-video user playing.
On a violation the call is retried once with the exact violation messages appended. On a second
failure the user-month is logged to `logs/failed_journeys.jsonl` and the seeded Markov fallback is
used, so the run always completes. The report flags the run if more than 10% of journeys came from
fallback; the live sample ran at about 8%.

## What the data contains

- **1,000 users**: MAU 500 (Sep 2025, "STLY") and 600 (Sep 2026, "CURRENT"), 100 active in both.
- **MVU/MAU 0.60** in each period → MVU 300 → 360 (+20% YoY), enforced with `is_video_user` in the brief.
- **Segments** 60/30/10 anonymous/registered/subscribed. **Platforms** 40/20/20/20 ott/android/ios/web
  (per user; 15% of registered/subscribed users get a second device).
- 10 event types: `screen_viewed`, `lane_viewed` (nested `assets_list`), `asset_clicked`,
  `video_view` (`start` / `heartbeat` every 300 s / `complete`), `login`, `registration`,
  `subscribed`, `payment_received`, `payment_failed`, `$identify`.
- Identity: anonymous device ids (`anon_…`) until registration or first login on a device, then
  `$identify` with `$anon_distinct_id`; `out/identity_map.csv` has every stitch.

### Planted stories

| # | Story | How it is planted | Checked as |
|---|---|---|---|
| 1 | ott share of watch time +8pp YoY, web declining | Cohort platform skew + watch intensity in briefs; expansion fine-tunes a bounded ott factor | ott share Δ = 8 ± 2pp; web Δ < 0 |
| 2 | The Voice premiere spike, week 3 of Sept 2026; smaller 2025 bump | Season 3 drops 17.09.2026 (season 2 on 18.09.2025), Thu/Sun releases; "Voice fan" flag (35% / 15% of video users) | week-3 watch ÷ mean(weeks 1–2): ≥ 1.8 (2026), 1.1–1.8 (2025). **WARN only**: steered via the LLM |
| 3 | 2026-09-17 payment-gateway incident | ≥ 6 renewals land that day; gateway timeouts are added so the timeout **rate per payment attempt** is 5× the month's baseline | 5 ± 1×, ≥ 5 events |
| 4 | hero_lane CTR ≈ 2× standard_lane; CTR decays with position | Scrolled-past lane impressions are padded to hit the ratio; clicked tiles sit at a geometrically decaying position in each impression's `assets_list` | ratio 2.0 ± 0.25; position CTR declines |

## Marketing analytics layer

Full plan and training exercises: [MARKETING_ANALYTICS.md](MARKETING_ANALYTICS.md). Python adds it
during expansion (no extra Gemini calls, cached journeys stay valid), so every source fits what the
session did.

- **Every client event** carries its session's `utm_source`, `utm_medium`, `utm_campaign`,
  `utm_content`, `utm_term`, `$referrer`, `$referring_domain`, `gclid` / `fbclid` / `ttclid` and
  `landing_url` (web URL or app deep link). Direct sessions have `$referrer = "$direct"` and null UTMs.
  Server billing events have none.
- **Platform realism**: web has referrers and UTM landing URLs; apps have deep links with UTMs; TV
  (ott) sessions are always direct, with TV promo / smart-TV store / pre-installed visible only as
  first touch.
- **First touch per user**: exact quotas per period × platform from `marketing.acquisition_mix`.
  Written to `users.csv`, PostHog `$set_once` (`$initial_utm_*`) on each person's first event, and
  `mixpanel_profiles.jsonl` (upload with `scripts/upload_mixpanel.py --profiles`, needs
  `MIXPANEL_PROJECT_TOKEN`).
- **Campaign calendar, costs and rules** live in `config.yaml → marketing`.

| # | Marketing story | Check |
|---|---|---|
| M1 | The Voice launch campaign drives the week-3 spike | ≥ 25% of week-3 Voice sessions; campaign spend wk 3 ≥ 2× wks 1–2 |
| M2 | Paid social up YoY (TikTok new in 2026), display down | paid-social share of new users ≥ +5pp |
| M3 | First touch ≠ last touch for subscribers | last touch email/brand search ≥ 50%; first touch ≤ 30% |
| M4 | CAC by channel | brand search < paid social < display |
| M5 | Payment-incident follow-up email | only after 17.09.2026, only to users with a failed payment |
| M6 | TV attribution gap | 0 UTMs on ott sessions |

## Outputs (`out/`)

| File | For |
|---|---|
| `events.ndjson` | Canonical nested events (UTC ISO-8601, ms) |
| `events_flat.csv` | Looker Studio / Sheets (`assets_list` as JSON + count) |
| `posthog_batch/*.jsonl` + `scripts/upload_posthog.py` | PostHog `/batch` with `historical_migration: true`, ≤ 1,000 per file; event `uuid` = `$insert_id` |
| `mixpanel_import/*.jsonl` + `scripts/upload_mixpanel.py` | Mixpanel `/import`: `time` in epoch ms, `$insert_id`, `$device_id`/`$user_id` (Simplified ID Merge by default), ≤ 2,000 per request, EU host by default |
| `bigquery/schema.json`, `events_bq.ndjson`, `load_bigquery.sh` | Partitioned by `DATE(timestamp)`, clustered by `event, platform`; `assets_list` is a REPEATED RECORD; `$` is stripped from field names for BigQuery. The script also loads `identity_map` and `users` and creates the `events_identified` view (`views.sql`) |
| `users.csv`, `content_catalog.csv`, `identity_map.csv` | Dimensions |
| `events_dirty.ndjson` + `dirty_manifest.csv` | 1.5% of rows broken on purpose (duplicates, casing, missing session, local timestamps, negative watch time, null distinct_id, screen-name typos, late arrivals), for data-quality demos. The clean file is untouched |
| `daily_aggregates.csv` (+ `--sheets SHEET_ID`) | Daily × platform aggregates; Sheets' 10M-cell limit cannot hold event level. Needs `GOOGLE_APPLICATION_CREDENTIALS` for a service account with edit access |
| `marketing/campaigns.csv` | Campaign calendar: name, objective, channels, dates, paid vs owned |
| `marketing/marketing_spend_daily.csv` | Day × campaign × source × content × term: impressions, clicks, cost (paid); sends, opens, clicks (CRM). Join on `utm_campaign` + date for CPC, CTR, CAC, ROAS |
| `marketing/user_acquisition.csv` | First touch per user |
| `marketing/channel_mapping.csv` | Source/medium/referrer → default channel grouping (also as a derived `channel` column in the flat CSV and BigQuery) |
| `marketing/daily_channel_aggregates.csv` | Day × channel: sessions, users, new users, registrations, subscriptions, initial revenue, impressions, clicks, cost (ready for Looker Studio) |
| `mixpanel_profiles.jsonl` | Mixpanel user profiles with first-touch properties |
| `validation_report.md` | All checks, rule violations, marketing stories, hourly curve, LLM stats |

The upload scripts read keys from `.env` (see `.env.example`). See "Uploading" below for limits,
parallelism and resume.

## Identity: anonymous devices, accounts, hashed emails

| Who | Identifiers |
|---|---|
| Every device | `anonymous_id` (`anon_…`) from its first event; `device_id` |
| Users with an account (registered at some point) | `user_id` (`u0123`), plus synthetic `email` (`viewer.u0123@example.invalid`, a reserved domain that can't reach a real inbox), `email_sha256` (trimmed, lower-cased, SHA-256, as Meta CAPI and Google Enhanced Conversions expect) and `crm_id` |
| Anonymous users (never registered) | Only their device's `anonymous_id`; each counts as one person |

**When the link happens.** At registration, or at the first login on a device, the canonical data
emits `$identify` (`distinct_id` = user id, `$anon_distinct_id` = device id). From then on that
device's events carry the user id.
- A second device is linked at its own first login.
- A device is linked once, ever, even across batches.
- `out/identity_map.csv` has one row per link.
- Email, hash and CRM id are added at export time; `events.ndjson` itself is unchanged.

| Destination | How the link is made |
|---|---|
| **PostHog** | The `$identify` event merges the device's anonymous person into the user, including earlier events, and sets `email`, `email_sha256`, `crm_id` on the person. First touch goes in `$set_once` on each person's first event. |
| **Mixpanel** | `exports.mixpanel_id_merge` in `config.yaml` must match Project Settings → Identity Merge. **`simplified`** (default; projects since 2023): every event has `$device_id`, and logged-in events also have `$user_id`; `distinct_id` is the user id or `$device:<id>`; Mixpanel merges on the first event carrying both, and no `$identify` is sent. **`original`**: `$identify` events with `$identified_id` / `$anon_id`. `mixpanel_profiles.jsonl` uses the same ids and sets `$email`, `email_sha256`, `crm_id`. |
| **BigQuery** | Tables `events`, `identity_map`, `users` plus the view `events_identified`, where `person_id = COALESCE(user_id, identity_map.user_id, anonymous_id)`. Pre-login events count toward the user, as in PostHog and Mixpanel. Join on `email_sha256` or `crm_id` for CRM-style exercises. |

New vs returning people across months is left to each tool: they see a returning user as the same
person whenever the same id shows up again.

## Uploading (parallel, limit-aware, resumable)

```bash
python scripts/upload_posthog.py --dry-run           # plan: chunks, MB, minimum time at the limits
python scripts/upload_posthog.py
python scripts/upload_mixpanel.py --profiles         # events, then user profiles
bash out/bigquery/load_bigquery.sh                   # needs BQ_PROJECT (and the gcloud SDK)
```

Both uploaders share `scripts/ingest.py`, which works like a small Spark job without the JVM:
- **Partitions:** events are cut into fixed chunks that respect each API's per-request limits.
- **Parallel workers:** a pool of workers sends chunks under a shared rate limiter (requests per
  second and MB per minute).
- **Retries:** 429 and 5xx are retried with exponential backoff and jitter, honouring `Retry-After`.
- **Fatal errors:** any other 4xx stops the run with the API's message.
- **Resume:** finished chunks are recorded in `out/.upload_state/`, so rerunning after any stop
  resumes without re-sending. `--reset` starts over. Re-sends are harmless anyway: PostHog dedupes
  on `uuid`, Mixpanel on `$insert_id`.

Limits live in `config.yaml → ingestion` (override with `--workers`, `--rps`, `--mb-per-min`).
Defaults stay below the published limits:

| | Per request | Rate | Notes |
|---|---|---|---|
| PostHog `/batch` | ≤ 1,000 events, ≤ 15 MB (API rejects ~20 MB) | 8 workers, 20 req/s, 600 MB/min | gzip; `historical_migration: true` routes to the backfill pipeline |
| Mixpanel `/import` | ≤ 2,000 events, ≤ 9 MB uncompressed (API max 10 MB) | 10 workers, 30 req/s, 1.5 GB/min (API max 2 GB/min) | gzip, `strict=1` (a bad record stops the run with Mixpanel's reason) |
| Mixpanel `/engage` | ≤ 2,000 profile operations | shares the limiter | needs `MIXPANEL_PROJECT_TOKEN` |

**Mixpanel auth:** a service account (`MIXPANEL_PROJECT_ID` + `MIXPANEL_SERVICE_ACCOUNT_USER` /
`_SECRET`) or, on the Free plan, just `MIXPANEL_PROJECT_TOKEN`. The token is used when no service
account is set. The data is historical, so it goes through `/import`, never `/track`. The Free plan
allows 1M events a month: the base dataset is ~325k.

## Spec decisions (contradictions and gaps found)

1. **"Unique `$insert_id` and `session_id` per event"**: `session_id` is shared by all events of a
   session; `$insert_id` (UUID5) is unique per event.
2. **Event property spec was not attached.** The property set in `src/models.py` is my own,
   tracking-plan style. All event models live in that one file; swapping in the real spec touches only
   it and the expander.
3. **LIVE playback can never `complete`**: `completion_rate` is null for LIVE and `complete` needs ≥ 0.9.
   LIVE playback ends with a final heartbeat.
4. **"Pass only the catalog subset the user may play" would make walls impossible.** The prompt lists
   playable episodes only, but all titles, with "NO → wall" on gated ones, so abandoned sign-ups happen.
5. **Story 3 is invisible as a raw count**: ~100 subscribers make ~2 payment attempts a day. It is
   measured as gateway-timeout rate per attempt; the count spike is shown alongside.
6. **Row target implies heavy viewing.** 250k–450k rows with 300 s heartbeats need ~30 h/month per
   video user. Watch hours are lognormal (heavy tail), and expansion rescales watch time within
   `watch_scale_bounds`.
7. **Gated click.** The spec says a gated click goes to the wall (not a detail page with the wall on
   play). Kept; detail-then-wall is used only for mixed titles (NCIS: latest season RVOD, back
   catalogue SVOD).
8. Other assumptions:
   - Lanes hold titles (`joyn_<slug>`); episodes (`joyn_<slug>_sSSeEE`) are what gets played.
   - Movie (90–130 min) and News VOD (25–45 min) durations were not given.
   - Segment = highest tier a user ever reached.
   - Renewals and retries are server-side events with `session_id: null`, `event_source: "server"`.
   - Login happens on a registered user's first session per device per month.
   - A both-period user churns only in their last active month.
   - An anonymous user has one device (a second device appears only after registration, so it can be stitched).
   - Billing runs 06:00–22:00 local.
   - Monthly/annual prices are €6.99/€69.99, set in config.

## Growing the dataset: more users or new months (batches)

The dataset is a list of **batches**. The original 1,000 users (Sep 2025 + Sep 2026, from
`config.yaml`) are batch `base`. Add more with a small spec file, then run:

```bash
python main.py generate --spec specs/example_oct_2026.yaml        # add --offline to try it without Gemini
python main.py batches                                            # what's in the dataset
```

`generate` registers the batch, generates only its journeys (Gemini calls for the new user-months
only), then re-expands, exports and validates the merged dataset. It is safe to rerun: a quota stop
resumes where it left off.

**Two ready-made examples** in `specs/` (field reference: [specs/README.md](specs/README.md)):

| Spec | What it does |
|---|---|
| `example_more_users_sept.yaml` | 1,000 more users in the same two Septembers (500 + 500, 100 of them in both) |
| `example_oct_2026.yaml` | October 2026: 300 new users + 350 returning users, a GNTM premiere, a payment incident, a launch campaign |

**Guarantees**

- **Additive.** A batch never changes earlier batches. Their personas, cached journeys and events stay
  byte-identical (tested), so data already uploaded to PostHog/Mixpanel/BigQuery stays valid; upload
  only the new batch's rows if you want (`out/batches/<run_id>/`).
- **Returning users carry their state.** They keep id, devices and account state (registered,
  subscribed, churned, plan, renewal day), with renewals continuing. Some anonymous returners register
  and some registered ones subscribe (`lifecycle` in the spec). They get no new first touch; their
  sessions use the return-visit marketing rules. Devices already stitched never fire `$identify` again.
- **New users** get fresh ids (`u1001`, …), exact segment/platform quotas and their own first touch.
- **Exact targets per period**: new users, returning users and MVU/MAU are met by construction and
  re-checked from the events.
- **Stories per period**: premieres (adds a new season to an existing show, with release dates),
  payment incidents and campaigns are set in the spec. They apply to that batch and later ones, never
  earlier ones.
- **Stale-cache protection**: every cached journey stores a hash of its brief; a changed brief is
  regenerated instead of reused. Journeys cached before this feature have no hash and are trusted.

**Output layout**: each batch expands into `out/batches/<run_id>/` (events, identity map, marketing
tables, expand meta). `out/events.ndjson` and all exports are the merged dataset.
`validation_report.md` checks MAU, MVU and returning users per period, segment/platform splits per
batch's new users, row bounds (base totals + `rows_per_user_month` × user-months for each batch),
and every planted story it can find.

**Rules a spec must follow** (checked with a clear error):

- `run_id` is new and not `base`.
- A period name that already exists must keep its dates.
- New periods must not overlap existing ones.
- Each period lasts at most 93 days.
- Enough users must be eligible to return. A user returns only into a period after their last one,
  and is active at most once per period.

## Tests

```bash
.venv/bin/python -m pytest -q
```

45 tests: KeyPool rotation with mocked 429s, cooling, RPD exhaustion, reset, persisted state and
duplicate keys; the client re-queueing a 429 on another key; resume from checkpoint and `--refresh`;
retry-then-fallback; the journey validator on hand-written bad journeys and its repairs; and
expansion determinism with 100% schema validation of clean events; and the marketing layer
(campaign names, click ids, no UTMs on TV, spend ≥ landing sessions, channel grouping, first-touch quotas);
and batches (a new month leaves earlier batches byte-identical, returning users carry their state, devices are
stitched once, more users in the same months, stale-journey detection, spec safety errors); identity exports
(Mixpanel simplified/original ID merge, PostHog $identify person properties, hashed emails, BigQuery view); and
the uploader engine (chunk limits, resume without re-sending, 429 retry, fatal 4xx).
