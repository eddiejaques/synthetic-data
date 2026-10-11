# Batch spec reference

A spec adds one batch to the dataset. Run it with `python main.py generate --spec <file>`.
All keys except `run_id` and `periods` are optional; anything omitted falls back to `config.yaml`.

```yaml
run_id: oct_2026                 # new, a-z 0-9 _ ; becomes out/batches/oct_2026/
returning_from: all              # all: any earlier user may return | batch: only this batch's own users

periods:                         # one or more; each at most 93 days
  - name: OCT_2026               # reuse STLY / CURRENT (same dates) to add users to existing months
    start: 2026-10-01
    end: 2026-10-31
    new_users: 300               # brand-new people whose first month is this one
    returning_users: 350         # people active before (last month ended before this start)

targets:                         # apply to this batch's users
  mvu_ratio: 0.6                 # video users / active users, per period (new + returning)
  segments: {anonymous: 0.6, registered: 0.3, subscribed: 0.1}     # final tier of NEW users
  platforms: {ott: 0.4, android: 0.2, ios: 0.2, web: 0.2}           # primary device of NEW users

lifecycle:                       # any config.yaml lifecycle key can be overridden too
  returning_register_rate: 0.06  # anonymous returners who register in the period
  returning_subscribe_rate: 0.04 # registered returners who subscribe in the period

stories:
  premieres:                     # adds a NEW season to an existing VOD series (one Gemini call or offline fallback)
    - title_id: joyn_gntm        # any series id from out/content_catalog.csv
      date: 2026-10-15           # episode 1 release
      episodes: 8
      cadence_days: [0, 3]       # releases per week: day 0 and day 3 (Thu + Sun)
      fan_share: 0.30            # share of the period's video users who follow it
      uplift_min: 1.5            # validation: launch-week watch time vs weekly average before
  payment_incidents:             # gateway timeouts at multiplier x the period's baseline rate
    - {date: 2026-10-21, multiplier: 5.0, tol: 1.0, min_count: 5}
  campaigns:                     # absolute dates; named yyyy_mm_objective_topic unless `name` is set
    - key: launch_gntm
      objective: launch          # launch | acquisition | engagement | retention | conversion | service ...
      topic: gntm
      title_id: joyn_gntm        # optional: sessions in the flight that watch it may come via the campaign
      touch_rate: 0.5            # ... with this probability
      channels: [paid_social, online_video, crm_push, crm_email]
      start: 2026-10-10
      end: 2026-10-31
      contents: [video15s_gntm_a, push_gntm_new_episode, email_gntm_launch]
    - {key: service_payment_update, objective: service, topic: payment, channels: [crm_email],
       start: 2026-10-22, end: 2026-10-31, contents: [email_update_card]}   # payment follow-up email
  ott_pair: [CURRENT, OCT_2026]  # optional: plant the +8pp ott-share tilt between two periods

marketing:
  acquisition_mix: {...}         # first-touch channel mix for new users (default: the latest in config)
  tiktok_share_of_paid_social: 0.45
```

Channels you can use in campaigns: `paid_search_brand`, `paid_search_generic`, `paid_social`,
`display`, `online_video`, `affiliate`, `crm_email`, `crm_push`, `tv_promo`.

Tips:
- **Cost**: about 1.3 Gemini calls per user-month (new + returning) plus one call per premiere.
  Use `--limit 20` for a smoke test.
- **Trying a spec**: `--offline` runs everything with the Python fallbacks in `cache_offline/`
  and `out_offline/`, without touching your real data.
- **Changing a spec**: once registered, a batch's spec can't change. Copy it with a new `run_id`
  instead (to replace a batch entirely, remove its folder in `cache/batches/` and its entry in
  `cache/batches/index.json` before anything is built on top of it).
