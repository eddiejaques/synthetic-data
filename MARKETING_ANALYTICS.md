# Marketing Analytics — plan

Goal: the same Joyn dataset can also teach marketing analytics: where users come from, which
campaigns bring them, what they cost, and how first-touch and last-touch attribution disagree. It
should work in PostHog, Mixpanel and Looker Studio (Data Studio) without extra cleaning.

## 1. Principle

Marketing data is **added by Python after the journeys exist**. Gemini still writes behaviour; Python
decides where each session came from, so:

- no new Gemini calls, and cached journeys stay valid;
- every source fits what the session actually did (someone who came in from The Voice launch ad
  watches The Voice);
- every number the trainer relies on is planted on purpose and checked by the validator.

## 2. What gets added

### 2a. On every client event of a session (session-scoped, like Mixpanel super properties)

| Property | Example | Notes |
|---|---|---|
| `utm_source` | `google`, `meta`, `tiktok`, `youtube`, `newsletter`, `push`, `prosieben.de` | null when there is no campaign |
| `utm_medium` | `cpc`, `paid_social`, `display`, `video`, `email`, `push`, `referral`, `affiliate` | |
| `utm_campaign` | `2026_09_launch_tvog_s3` | naming: `yyyy_mm_objective_topic` |
| `utm_content` | `video15s_coaches_a` | creative / variant, for A/B creative analysis |
| `utm_term` | `the voice of germany 2026` | search keywords only |
| `$referrer` | `https://www.google.de/`, `$direct` | PostHog and Mixpanel default property name |
| `$referring_domain` | `www.google.de`, `$direct` | |
| `gclid` / `fbclid` / `ttclid` | random click ids | only on Google / Meta / TikTok paid clicks |
| `landing_url` | `https://www.joyn.de/?utm_source=…`, `joyn://…?utm_source=…` | web URL or app deep link |

Server-side billing events get none of these.

### 2b. Per person (first touch)

`acquisition_channel`, `acquisition_date`, `acquisition_platform`, `initial_utm_source`,
`initial_utm_medium`, `initial_utm_campaign`, `initial_referring_domain`, plus `acquired_in_window`.
They are written to `users.csv`, sent to PostHog as `$set_once` (`$initial_utm_source`, …) on each
person's first event, and to Mixpanel as user profiles (`mixpanel_profiles.jsonl` for the Engage API).

### 2c. New tables in `out/marketing/`

| File | Grain | Use |
|---|---|---|
| `campaigns.csv` | campaign | Name, channel, source, medium, objective, dates, budget type |
| `marketing_spend_daily.csv` | day × campaign × source × content | impressions, clicks, cost (paid); sends, opens, clicks (CRM). Joins to events on `utm_campaign` + date → CAC, ROAS, CTR, CPC |
| `channel_mapping.csv` | source × medium → channel | GA4-style default channel grouping, so learners can build it themselves or join it |
| `user_acquisition.csv` | user | First touch per person (as 2b) |
| `daily_channel_aggregates.csv` | day × channel | sessions, new users, registrations, subscriptions, spend: a ready Looker Studio source |

Spend is derived from the attributed sessions (clicks ≥ landing sessions, realistic CTR/CPC/CPM per
channel), so funnels from impression to subscription add up.

## 3. Channels

Channels: organic search, paid search (brand / non-brand), paid social (Meta, TikTok), display,
online video (YouTube), CRM email, CRM push, referral (ProSieben/SAT.1/kicker sites), organic
social, affiliate, TV promo, app store, direct.

Platform realism:

- **web**: referrers and UTM landing URLs.
- **iOS / Android**: app-store acquisition and deep links with UTM. Opening the app is direct.
- **ott (TV)**: no clickable ads. Sessions are direct; acquisition is TV promo, smart-TV store or
  pre-installed, and is visible only as a person property.

## 4. Planted marketing stories (validated)

| # | Story | Check |
|---|---|---|
| M1 | **The Voice launch campaign** (paid social, YouTube, push, email, TV promo) drives the week-3 spike; 2025 had a smaller one | Campaign share of week-3 Voice sessions (2026) ≥ 25%; campaign spend in week 3 ≥ 2× weeks 1–2 |
| M2 | **Channel mix shift YoY**: paid social up (TikTok only in 2026), display down | paid-social share of new users +≥ 5pp; display share down; TikTok absent in 2025 |
| M3 | **First touch ≠ last touch**: subscribers are found by social/video/search, but convert after a CRM email or brand search | last touch of the subscribe session is email or brand search ≥ 50%; same users' first touch on those channels < 30% |
| M4 | **CAC differs by channel**: brand search is cheapest per new user, display the most expensive | CAC(brand search) < CAC(paid social) < CAC(display) |
| M5 | **Payment incident follow-up**: CRM email "update your payment" on 18.09.2026 to affected subscribers | campaign present only after the incident, only for users with a failed payment |
| M6 | **TV attribution gap**: ott is about 40% of users but carries almost no UTMs | 0 UTM events on ott |

Also planted: retargeting (display/social) brings back anonymous users who abandoned sign-up;
Saturday paid search for ran Football; an always-on brand search campaign.

## 5. Implementation steps

1. `config.yaml` → `marketing:` acquisition mix per period, return-visit source mix by platform,
   campaign calendar, and cost assumptions (CTR, CPC, CPM, email open/click rates).
2. `src/marketing.py`:
   - first-touch assignment per user (platform-compatible, seeded);
   - session source assignment (content- and lifecycle-aware rules, seeded);
   - UTM/referrer/click-id/landing-URL builder;
   - spend, campaign and channel-mapping tables.
3. `src/models.py`: optional marketing properties on all events (schema-validated).
4. `src/expand.py`: a marketing pass after timing; the emitter adds session properties to client events.
5. Exporters: new columns in the flat CSV and BigQuery schema (plus a derived `channel` column);
   PostHog `$set_once` initial properties; Mixpanel profiles file and upload support; daily channel aggregates.
6. `src/validate.py`: marketing consistency checks and M1–M6 magnitudes in `validation_report.md`.
7. Tests: determinism, OTT has no UTMs, every `utm_campaign` exists in `campaigns.csv`, clicks ≥ sessions.
8. README section and training exercises (below).

## 6. Training exercises this enables

**PostHog**
- Web analytics / Trends broken down by `utm_source`, `$referring_domain` and channel type.
- Funnel *landing → registration → subscription* by `utm_campaign`.
- Cohort of users whose `$initial_utm_source = tiktok`; their retention against Google.
- First-touch versus last-touch comparison using person versus event properties.

**Mixpanel**
- Attribution report (first touch, last touch, linear) on `subscribed` with UTM properties.
- Funnels by `utm_campaign`; Flows from campaign landings; retention by `acquisition_channel` (profile).
- Creative test: The Voice ad `utm_content` variants and their conversion rates.

**Looker Studio / BigQuery**
- Blend `marketing_spend_daily` with events on `utm_campaign` + date for CPC, CTR, CAC and ROAS
  (ROAS uses `payment_received.amount`).
- Channel-mix YoY dashboard (STLY versus CURRENT), and the Voice campaign flight against watch time.
- Build the channel grouping as a CASE statement and compare it with `channel_mapping.csv`.

## 7. Out of scope (possible later phase)

Ad-platform-native exports (Google Ads / Meta schemas), email send/open events as their own event
types, view-through attribution, and media-mix-model-length time series (needs more than two months).
