"""Stage 2 — PERSONAS. 1,000 seeded user briefs that carry every hard constraint.

Targets (MAU, MVU, segment split, platform split, lifecycle dates, billing outcomes) are met here
by construction, with exact quota assignment rather than per-user coin flips.
"""
from __future__ import annotations

import logging
import math
import random
import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Optional

from .config import Settings, read_json, rng_for, write_json
from .models import BillingEvent, Device, PeriodBrief, UserBrief

log = logging.getLogger(__name__)

# platform -> list of (weight, device_type, brand, model, os, os_versions, viewport)
DEVICE_CATALOG = {
    "ios": [(0.8, "phone", "Apple", "iPhone 15 Pro", "iOS", ["17.6", "18.1", "18.4"], (393, 852)),
            (0.2, "tablet", "Apple", "iPad Air", "iPadOS", ["17.6", "18.1"], (820, 1180))],
    "android": [(0.6, "phone", "Samsung", "Galaxy S24", "Android", ["14", "15"], (384, 854)),
                (0.4, "phone", "Google", "Pixel 8", "Android", ["14", "15"], (412, 915))],
    "ott": [(0.30, "tv", "LG", "OLED55", "webOS", ["23", "24"], (1920, 1080)),
            (0.25, "tv", "Samsung", "QN90", "Tizen", ["7.0", "8.0"], (1920, 1080)),
            (0.30, "tv", "Amazon", "Fire TV Stick 4K", "Fire OS", ["7.6", "8.1"], (1920, 1080)),
            (0.15, "tv", "Sony", "Bravia XR", "Google TV", ["12", "14"], (1920, 1080))],
    "web": [(0.55, "desktop", "Apple", "MacBook Pro M3", "macOS", ["14.6", "15.1"], (1512, 982)),
            (0.45, "desktop", "Microsoft", "Surface Laptop", "Windows", ["11"], (1536, 1024))],
}
APP_VERSIONS = {"STLY": ["5.31.0", "5.32.1", "5.33.0"], "CURRENT": ["6.12.0", "6.13.2", "6.14.0"]}
def app_versions(period: str) -> list[str]:
    """App versions live in a period. Base periods are fixed; others follow `<year-2020>.<month+10>`."""
    if period in APP_VERSIONS:
        return APP_VERSIONS[period]
    m = re.search(r"(20\d\d)", period)
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9,
              "OCT": 10, "NOV": 11, "DEC": 12}
    year = int(m.group(1)) if m else 2026
    month = next((v for k, v in months.items() if k in period.upper()), 1)
    # Anchored to the base timeline (Sep 2025 = 5.31, Sep 2026 = 6.12) so versions only ever move forward.
    if year == 2025:
        major, minor = 5, max(0, 31 + 2 * (month - 9))
    elif year == 2026:
        major, minor = 6, max(0, round(12 + 1.5 * (month - 9)))
    else:
        major, minor = year - 2020, 2 * month if year > 2026 else 3 * month
    return [f"{major}.{minor}.0", f"{major}.{minor}.1", f"{major}.{minor + 1}.0"]


ARCHETYPES = [
    "evening reality fan who watches new episodes the day they drop",
    "weekend binger: few sessions, several episodes back to back",
    "one-off visitor: short visits, mostly browsing",
    "background Live-TV viewer, zaps between channels",
    "crime and drama completist, watches episodes in order",
    "lunch-break phone viewer, short clips and highlights",
    "family TV evening viewer, long sessions after 20:00",
    "sports-first viewer, Saturday football and highlights",
]


def _quota(n: int, shares: dict[str, float]) -> dict[str, int]:
    """Largest-remainder rounding: integer counts that sum exactly to n."""
    raw = {k: n * v for k, v in shares.items()}
    out = {k: int(math.floor(v)) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: n - sum(out.values())]:
        out[k] += 1
    return out


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _device(rng: random.Random, platform: str, label: str, period_hint: str) -> Device:
    opts = DEVICE_CATALOG[platform]
    w, dtype, brand, model, os_, vers, (vw, vh) = rng.choices(opts, weights=[o[0] for o in opts])[0]
    did = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    return Device(label=label, device_id=did, anon_id="anon_" + did.replace("-", "")[:20],
                  platform=platform, device_type=dtype, brand=brand, model=model, os=os_,
                  os_version=rng.choice(vers), app_version=rng.choice(app_versions(period_hint)),
                  viewport_w=vw, viewport_h=vh)


def _assign_platforms(cfg: Settings, cohorts: dict[str, list[str]], rng: random.Random) -> dict[str, str]:
    """Cohort-skewed platform quotas, then nudged so overall counts match the global split exactly."""
    total = sum(len(v) for v in cohorts.values())
    target = _quota(total, cfg["platforms"])
    assign: dict[str, str] = {}
    for cohort, users in cohorts.items():
        q = _quota(len(users), cfg["platform_by_cohort"][cohort])
        labels = [p for p, n in q.items() for _ in range(n)]
        rng.shuffle(labels)
        assign.update(zip(users, labels))
    # fix-up: move users from surplus platforms to deficit ones, preferring the 'both' cohort
    order = cohorts["both"] + cohorts["STLY_only"] + cohorts["CURRENT_only"]
    for _ in range(total):
        counts = {p: 0 for p in target}
        for p in assign.values():
            counts[p] += 1
        surplus = [p for p in target if counts[p] > target[p]]
        deficit = [p for p in target if counts[p] < target[p]]
        if not surplus:
            break
        u = next(u for u in order if assign[u] == surplus[0])
        assign[u] = deficit[0]
        order.remove(u)
    return assign


def _lognormal_hours(rng: random.Random, mean: float, sigma: float) -> float:
    mu = math.log(mean) - sigma ** 2 / 2
    return round(min(140.0, max(0.5, rng.lognormvariate(mu, sigma))), 1)


def build_personas(cfg: Settings) -> list[UserBrief]:
    """Create all user briefs deterministically from SEED."""
    rng = rng_for(cfg.seed, "personas")
    U = cfg["users"]
    n_total, n_both = int(U["total"]), int(U["both"])
    mau = {p: int(U["mau"][p]) for p in cfg.period_names}
    stly, cur = cfg.period_names                       # ["STLY", "CURRENT"]
    if mau[stly] + mau[cur] - n_both != n_total:
        raise ValueError("MAU_STLY + MAU_CURRENT - both must equal total users")

    users = [f"u{i:04d}" for i in range(1, n_total + 1)]
    shuffled = users[:]
    rng.shuffle(shuffled)
    cohorts = {"both": shuffled[:n_both],
               "STLY_only": shuffled[n_both: mau[stly]],
               "CURRENT_only": shuffled[mau[stly]:]}
    active: dict[str, list[str]] = {}
    for u in cohorts["both"]:
        active[u] = [stly, cur]
    for u in cohorts["STLY_only"]:
        active[u] = [stly]
    for u in cohorts["CURRENT_only"]:
        active[u] = [cur]

    # segments: exact quotas
    seg_q = _quota(n_total, cfg["segments"])
    seg_labels = [s for s, n in seg_q.items() for _ in range(n)]
    rng.shuffle(seg_labels)
    segment = dict(zip(users, seg_labels))

    platform = _assign_platforms(cfg, cohorts, rng)

    # video users per period: exact count, subscribers first, then registered, then anonymous
    is_video: dict[tuple[str, str], bool] = {}
    for p in cfg.period_names:
        act = [u for u in users if p in active[u]]
        n_mvu = round(mau[p] * float(U["mvu_ratio"]))
        bias = {"subscribed": 3.0, "registered": 0.9, "anonymous": 0.0}
        scored = sorted(act, key=lambda u: bias[segment[u]] + rng.random() * 1.6, reverse=True)
        chosen = set(scored[:n_mvu])
        for u in act:
            is_video[(u, p)] = u in chosen

    L = cfg["lifecycle"]
    days = {p: len(cfg.period_days(p)) for p in cfg.period_names}
    briefs: list[UserBrief] = []
    incident = cfg["stories"]["payment_incident"]
    incident_day = date.fromisoformat(incident["date"]).day
    renew_17_quota = 6       # guarantee enough attempts on the incident day for a measurable rate

    for u in users:
        urng = rng_for(cfg.seed, "user", u)
        seg = segment[u]
        acts = active[u]
        prim = platform[u]
        devices = [_device(urng, prim, "d1", acts[-1])]
        if seg != "anonymous" and urng.random() < float(cfg["second_device_rate"]):
            others = {k: v for k, v in cfg["platforms"].items() if k != prim}
            devices.append(_device(urng, _pick(urng, others), "d2", acts[-1]))

        reg_before = sub_before = False
        reg_at: Optional[tuple[str, int]] = None
        sub_at: Optional[tuple[str, int]] = None
        plan = None
        renewal_day = None
        if seg in ("registered", "subscribed"):
            if seg == "subscribed":
                sub_before = urng.random() >= float(L["subscribed_in_window_share"])
                plan = _pick(urng, L["plan_split"])
                if sub_before:
                    reg_before = True
                    renewal_day = urng.randint(1, 28)
                else:
                    p = urng.choice(acts)
                    sub_day = urng.randint(3, days[p] - 2)
                    sub_at = (p, sub_day)
                    renewal_day = sub_day
                    reg_before = urng.random() < 0.8
                    if not reg_before:
                        reg_at = (p, urng.randint(1, sub_day))
            else:
                reg_before = urng.random() >= float(L["registered_in_window_share"])
                if not reg_before:
                    p = urng.choice(acts)
                    reg_at = (p, urng.randint(1, days[p] - 1))

        periods: dict[str, PeriodBrief] = {}
        churned = False
        for p in acts:
            prng = rng_for(cfg.seed, "user-period", u, p)
            # segment at the start of this period
            if seg == "anonymous":
                s0 = "anonymous"
            elif sub_before:
                s0 = "subscribed"
            elif sub_at and cfg.period_names.index(sub_at[0]) < cfg.period_names.index(p):
                s0 = "subscribed"
            elif reg_before or (reg_at and cfg.period_names.index(reg_at[0]) < cfg.period_names.index(p)):
                s0 = "registered"
            else:
                s0 = "anonymous"
            if churned:
                s0 = "registered"
            vid = is_video[(u, p)]
            lo, hi = cfg["simulation"]["sessions_per_month"][seg]
            if not vid:
                lo, hi = 1, max(2, hi // 3)
            hours = 0.0
            if vid:
                hours = _lognormal_hours(prng, float(cfg["simulation"]["watch_hours_mean"]),
                                         float(cfg["simulation"]["watch_hours_sigma"]))
                if p == cur:                                    # story 1: ott up, web down
                    hours *= {"ott": 1.1, "web": 0.9}.get(prim, 1.0)
                hours = round(hours, 1)
                # heavy viewers need enough sessions to hold their hours (3-8 h per session)
                lo = max(lo, min(30, math.ceil(hours / 8)))
                hi = max(hi, min(45, math.ceil(hours / 3)), lo)
            pb = PeriodBrief(
                period=p, segment_at_start=s0, is_video_user=vid, sessions_range=(lo, hi),
                target_watch_hours=hours,
                must_register_on_day=reg_at[1] if reg_at and reg_at[0] == p else None,
                must_subscribe_on_day=sub_at[1] if sub_at and sub_at[0] == p else None,
                plan=plan if seg == "subscribed" else None,
                voice_fan=vid and prng.random() < float(cfg["stories"]["voice_premiere"]["fan_share"][p]),
                football_fan=prng.random() < 0.2,
                zapper=prng.random() < 0.15,
                persona_note=prng.choice(ARCHETYPES),
            )
            if pb.voice_fan and pb.target_watch_hours < 8:
                pb.target_watch_hours = 8.0       # room for the new Voice episodes (~90 min each)
                pb.sessions_range = (max(pb.sessions_range[0], 4), max(pb.sessions_range[1], 6))
            periods[p] = pb
            if seg == "subscribed" and not churned:
                churned = _schedule_renewals(cfg, pb, p, u, plan, renewal_day, sub_before, sub_at,
                                             is_last=(p == acts[-1]), rng=prng)
        briefs.append(UserBrief(user_id=u, segment=seg, primary_platform=prim, devices=devices,
                                periods=periods, registered_before_window=reg_before,
                                subscribed_before_window=sub_before, renewal_day=renewal_day))

    _plant_incident_renewals(cfg, briefs, cur, incident_day, renew_17_quota)
    return briefs


def _billing_time(cfg: Settings, p: str, day: int, rng: random.Random) -> datetime:
    d = cfg.period_days(p)[day - 1]
    return datetime.combine(d, time(rng.randint(6, 21), rng.randint(0, 59)))


def _schedule_renewals(cfg: Settings, pb: PeriodBrief, p: str, u: str, plan: str, renewal_day: int,
                       sub_before: bool, sub_at, is_last: bool, rng: random.Random) -> bool:
    """Add server-side renewal (and retry) attempts for this period. Returns True if user churned."""
    if sub_at and sub_at[0] == p:
        return False                     # converted this month: next renewal is next month
    if not sub_before and not sub_at:
        return False
    if sub_at and cfg.period_names.index(sub_at[0]) > cfg.period_names.index(p):
        return False                     # not yet subscribed in this period
    if plan == "annual":
        # annual renews in this month only if the anniversary month is September
        if sub_at:                       # converted last September -> renews this September
            pass
        elif rng.random() >= 1 / 12:
            return False
    n_days = len(cfg.period_days(p))
    return renewal_attempts(cfg, pb, p, plan, min(renewal_day, n_days), is_last, rng)


def renewal_attempts(cfg: Settings, pb: PeriodBrief, p: str, plan: str, day: int, is_last: bool,
                     rng: random.Random) -> bool:
    """Renewal on `day` (+ retry on failure). Returns True if the retry also failed (churn)."""
    L = cfg["lifecycle"]
    n_days = len(cfg.period_days(p))
    when = _billing_time(cfg, p, day, rng)
    if rng.random() >= float(L["renewal_failure_rate"]):
        pb.billing.append(BillingEvent(when=when.isoformat(), kind="renewal", outcome="received", plan=plan))
        return False
    reason = _pick(rng, L["failure_reasons"])
    pb.billing.append(BillingEvent(when=when.isoformat(), kind="renewal", outcome="failed",
                                   failure_reason=reason, plan=plan))
    lo, hi = L["retry_delay_days"]
    rday = min(n_days, day + rng.randint(lo, hi))
    rwhen = _billing_time(cfg, p, rday, rng)
    if rwhen <= when:
        rwhen = when + timedelta(hours=6)
    # ASSUMPTION: a both-period user only churns in their last active period, so the
    # CURRENT month never shows renewals for someone who churned a year earlier.
    ok = rng.random() < float(L["retry_success_rate"]) or not is_last
    pb.billing.append(BillingEvent(when=rwhen.isoformat(), kind="retry", attempt_number=2,
                                   outcome="received" if ok else "failed",
                                   failure_reason=None if ok else _pick(rng, L["failure_reasons"]),
                                   plan=plan))
    if not ok:
        pb.svod_until_day = rwhen.day
        return True
    return False


def _plant_incident_renewals(cfg: Settings, briefs: list[UserBrief], period: str, day: int, quota: int) -> None:
    """Move a few CURRENT monthly renewals onto the incident day so the rate there is measurable."""
    have = sum(1 for b in briefs if period in b.periods
               for e in b.periods[period].billing if datetime.fromisoformat(e.when).day == day)
    cands = [b for b in briefs if period in b.periods and b.subscribed_before_window
             and b.periods[period].plan == "monthly" and b.periods[period].billing
             and all(e.outcome == "received" for e in b.periods[period].billing)
             and datetime.fromisoformat(b.periods[period].billing[0].when).day != day]
    rng = rng_for(cfg.seed, "incident-renewals")
    rng.shuffle(cands)
    for b in cands[: max(0, quota - have)]:
        e = b.periods[period].billing[0]
        w = datetime.fromisoformat(e.when)
        e.when = w.replace(day=day).isoformat()
        b.renewal_day = day


def save_personas(cfg: Settings, briefs: list[UserBrief]) -> None:
    write_json(cfg.cache / "personas.json", [b.model_dump(mode="json") for b in briefs])


def load_personas(cfg: Settings) -> list[UserBrief]:
    p = cfg.cache / "personas.json"
    if not p.exists():
        raise SystemExit("no personas yet — run `python main.py personas` first")
    return [UserBrief.model_validate(x) for x in read_json(p)]
