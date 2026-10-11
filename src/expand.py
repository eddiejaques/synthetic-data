"""Stage 4 — EXPANSION + ENFORCEMENT. Pure Python, deterministic given cached journeys.

Order of passes (each one global, deterministic, documented in out/expand_meta.json):
1. re-validate + repair every cached journey (fallback if a cached journey no longer passes)
2. lane CTR padding           (story 4: hero CTR ~2x standard)
3. watch-time calibration      (story 1: ott share +8pp YoY; total rows near calibrate_rows_to)
4. session timing, de-overlap, diurnal nudge (+-45 min)
5. payment plan               (baseline transient timeouts; story 3 incident rate)
6. emission: ids, timestamps, heartbeats, device fields, identity stitching, schema validation
"""
from __future__ import annotations

import csv
import json
import logging
import math
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Settings, rng_for, write_json
from .journeys import fallback_journey, hourly_weights, make_ctx
from .marketing import assign_marketing, write_marketing
from .models import LANE_SCREENS, Catalog, Device, JPlay, Journey, JStep, UserBrief, validate_event
from .validate_journey import check_journey

log = logging.getLogger(__name__)
NS = uuid.UUID("6f1c2b1e-5d3a-4c8e-9a51-6a0b6f2d7e10")
HB = 300


class RowCapExceeded(Exception):
    pass


@dataclass
class Sess:
    user: str
    period: str
    idx: int
    device: str
    day: int
    start_min: int                       # local minutes since midnight
    steps: list[JStep]
    dwell: list[float] = field(default_factory=list)
    dur_s: float = 0.0
    start_dt: Optional[datetime] = None  # local, tz-aware
    calib: dict[int, tuple[float, bool]] = field(default_factory=dict)  # step -> (watch_s, completed)
    dropped: bool = False                # pushed past the period end by de-overlap


# ----------------------------------------------------------------------------- helpers
def iso(dt: datetime) -> str:
    u = dt.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def uid(*parts) -> str:
    return str(uuid.uuid5(NS, "|".join(map(str, parts))))


def scaled_play(play: JPlay, duration: Optional[int], s: float) -> tuple[float, bool]:
    """(watch_seconds, completed) after applying calibration factor s."""
    if duration is None:                                   # LIVE
        return float(max(1, round((play.minutes or 1) * s)) * 60), False
    f = min(1.0, (play.watch or 0.01) * s)
    if play.outcome == "completed":
        f = max(f, 0.9)
    f = max(0.01, f)
    return f * duration, f >= 0.9


def play_rows(w: float, completed: bool) -> int:
    return 2 + int((max(w, 1) - 1) // HB)                  # start + heartbeats + final/complete


def step_rows(st: JStep) -> int:
    n = 1 + len(st.lanes) + (1 if st.click else 0)
    if st.auth in ("login", "register"):
        n += 2                                             # event + (at most) one $identify
    if st.billing in ("subscribe_monthly", "subscribe_annual"):
        n += 2
    return n


# ----------------------------------------------------------------------------- pass 1
def prepare_sessions(cfg: Settings, cat: Catalog, briefs: list[UserBrief],
                     journeys: dict[tuple[str, str], dict]) -> tuple[list[Sess], dict]:
    sessions: list[Sess] = []
    src = {"llm": 0, "fallback": 0, "fallback_at_expand": 0}
    for b in briefs:
        for p in b.periods:
            rec = journeys[(b.user_id, p)]
            ctx = make_ctx(cfg, b, p, cat)
            j, v = check_journey(Journey.model_validate(rec["journey"]), ctx)
            if v:
                j, v2 = check_journey(fallback_journey(cfg, b, b.periods[p], cat), ctx)
                if v2:
                    raise RuntimeError(f"fallback journey invalid for {b.user_id} {p}: {v2[:3]}")
                src["fallback_at_expand"] += 1
            else:
                src[rec.get("source", "llm")] = src.get(rec.get("source", "llm"), 0) + 1
            for i, s in enumerate(j.sessions):
                h, m = map(int, s.start.split(":"))
                sessions.append(Sess(b.user_id, p, i, s.device, s.day, h * 60 + m, s.steps))
    return sessions, src


# ----------------------------------------------------------------------------- pass 2: CTR
def pad_lane_ctr(cfg: Settings, cat: Catalog, sessions: list[Sess]) -> dict:
    lanes = cat.lane_map()
    imp = {"hero_lane": 0, "standard_lane": 0}
    clk = {"hero_lane": 0, "standard_lane": 0}
    for s in sessions:
        for st in s.steps:
            for lid in st.lanes:
                t = lanes[lid].lane_type
                if t in imp:
                    imp[t] += 1
            if st.click and lanes[st.click.lane].lane_type in clk:
                clk[lanes[st.click.lane].lane_type] += 1
    target = float(cfg["stories"]["lane_ctr"]["hero_vs_standard"])
    meta = {"before": {"impressions": dict(imp), "clicks": dict(clk)}, "added": {}}
    if min(imp.values()) == 0 or min(clk.values()) == 0:
        meta["note"] = "no clicks or impressions for a lane type; padding skipped"
        return meta
    ratio = (clk["hero_lane"] / imp["hero_lane"]) / (clk["standard_lane"] / imp["standard_lane"])
    if ratio < target:
        kind = "standard_lane"
        need = math.ceil(target * clk["standard_lane"] * imp["hero_lane"] / clk["hero_lane"]) - imp["standard_lane"]
    else:
        kind = "hero_lane"
        need = math.ceil(clk["hero_lane"] * imp["standard_lane"] / (target * clk["standard_lane"])) - imp["hero_lane"]
    rng = rng_for(cfg.seed, "ctr-pad")
    slots = [(si, ti) for si, s in enumerate(sessions) for ti, st in enumerate(s.steps) if st.screen in LANE_SCREENS]
    rng.shuffle(slots)
    by_screen = {scr: [l.lane_id for l in cat.lanes if l.lane_type == kind and scr in l.screens] for scr in LANE_SCREENS}
    added = 0
    for _ in range(4):                                       # up to 4 passes over all slots
        for si, ti in slots:
            if added >= need:
                break
            st = sessions[si].steps[ti]
            free = [l for l in by_screen[st.screen] if l not in st.lanes]
            if free:
                st.lanes.append(rng.choice(free))           # scrolled-past lane, lower on the screen
                added += 1
        if added >= need:
            break
    meta["added"] = {kind: added}
    meta["needed"] = max(0, need)
    return meta


# ----------------------------------------------------------------------------- pass 3: watch
def calibrate_watch(cfg: Settings, cat: Catalog, briefs: list[UserBrief], sessions: list[Sess]) -> dict:
    """Per (period, platform) factors so ott share moves +target pp and rows land near target."""
    plays = cat.playable_map()
    dev_plat = {(b.user_id, d.label): d.platform for b in briefs for d in b.devices}
    names = cfg.period_names
    s1 = cfg["stories"]["ott_watch_share_pp"]
    if s1.get("mode") == "targets":
        return _calibrate_targets(cfg, cat, briefs, sessions, s1)
    pair = s1["pair"] if "pair" in s1 else (names[:2] if len(names) == 2 else None)
    tilt = bool(pair) and all(x in names for x in pair)
    stly, cur = pair if tilt else (None, None)        # the story-1 pair, if this batch has both periods
    lo_b, hi_b = map(float, cfg["simulation"]["watch_scale_bounds"])
    target_pp = float(cfg["stories"]["ott_watch_share_pp"]["target"]) / 100
    target_rows = float(cfg["simulation"]["calibrate_rows_to"])

    base_rows = sum(step_rows(st) for s in sessions for st in s.steps)
    items = [(s.period, dev_plat[(s.user, s.device)], st.play, plays[st.play.content].duration_s)
             for s in sessions for st in s.steps if st.play]
    ott_f, glob = 1.0, 1.0

    def factor(p: str, plat: str) -> float:
        # global factor within config bounds; the story-1 ott tilt is a separate, smaller lever
        return glob * (ott_f if (p == cur and plat == "ott") else 1.0)

    def measure():
        w = {(p, k): 0.0 for p in cfg.period_names for k in ("ott", "all")}
        rows = base_rows
        for p, plat, play, dur in items:
            sec, comp = scaled_play(play, dur, factor(p, plat))
            w[(p, "all")] += sec
            if plat == "ott":
                w[(p, "ott")] += sec
            rows += play_rows(sec, comp)
        share = {p: w[(p, "ott")] / w[(p, "all")] if w[(p, "all")] else 0 for p in cfg.period_names}
        return share, rows

    for _ in range(12):
        share, rows = measure()
        if tilt:
            err = target_pp - (share[cur] - share[stly])
            ott_f = min(1.5, max(0.67, ott_f * math.exp(4.0 * err)))
            share, rows = measure()
        play_part = rows - base_rows
        if play_part > 0:
            glob = min(hi_b, max(lo_b, glob * max(0.5, min(2.0, (target_rows - base_rows) / play_part))))
    share, rows = measure()
    factors = {f"{p}/{k}": round(factor(p, k), 4) for p in cfg.period_names for k in ("ott", "android", "ios", "web")}
    # store the scaled watch per step so emission uses exactly what was measured
    for s in sessions:
        plat = dev_plat[(s.user, s.device)]
        for ti, st in enumerate(s.steps):
            if st.play:
                s.calib[ti] = scaled_play(st.play, plays[st.play.content].duration_s, factor(s.period, plat))
    return {"factors": factors, "ott_share": {k: round(v, 4) for k, v in share.items()},
            "estimated_rows": rows}


def _calibrate_targets(cfg: Settings, cat: Catalog, briefs: list[UserBrief], sessions: list[Sess], s1: dict) -> dict:
    """Batch calibration (spec batches only; the base keeps the original method above).

    Per period p a two-sided tilt t_p (ott x t_p, other platforms / t_p) steers the ott share of watch
    time to a target: `anchors` (share already realised for that period by earlier batches, so new
    users don't dilute it) and, for the story pair (a, b), share(b) = share(a) + target pp.
    A global factor still lands the row count. Two-sided so it works even when VOD is capped at 100%."""
    plays = cat.playable_map()
    dev_plat = {(b.user_id, d.label): d.platform for b in briefs for d in b.devices}
    names = cfg.period_names
    lo_b, hi_b = map(float, cfg["simulation"]["watch_scale_bounds"])
    target_rows = float(cfg["simulation"]["calibrate_rows_to"])
    pp = float(s1["target"]) / 100
    anchors = {p: float(v) for p, v in (s1.get("anchors") or {}).items() if p in names}
    pair = s1.get("pair") if s1.get("pair") and all(x in names for x in s1["pair"]) else None
    base_rows = sum(step_rows(st) for s in sessions for st in s.steps)
    items = [(s.period, dev_plat[(s.user, s.device)], st.play, plays[st.play.content].duration_s)
             for s in sessions for st in s.steps if st.play]
    tilt = {p: 1.0 for p in names}
    glob = 1.0

    def factor(p: str, plat: str) -> float:
        return min(hi_b * 1.6, max(lo_b / 1.6, glob * (tilt[p] if plat == "ott" else 1 / tilt[p])))

    def measure():
        w = {(p, k): 0.0 for p in names for k in ("ott", "all")}
        rows = base_rows
        for p, plat, play, dur in items:
            sec, comp = scaled_play(play, dur, factor(p, plat))
            w[(p, "all")] += sec
            if plat == "ott":
                w[(p, "ott")] += sec
            rows += play_rows(sec, comp)
        return {p: w[(p, "ott")] / w[(p, "all")] if w[(p, "all")] else 0 for p in names}, rows

    for _ in range(25):
        share, rows = measure()
        targets = dict(anchors)
        if pair and pair[1] not in anchors:          # story pair: b = a + pp (a anchored or as realised)
            targets[pair[1]] = anchors.get(pair[0], share[pair[0]]) + pp
        for p, tgt in targets.items():
            tilt[p] = min(2.5, max(0.4, tilt[p] * math.exp(2.5 * (tgt - share[p]))))
        share, rows = measure()
        play_part = rows - base_rows
        if play_part > 0:
            glob = min(hi_b, max(lo_b, glob * max(0.5, min(2.0, (target_rows - base_rows) / play_part))))
    share, rows = measure()
    for s in sessions:
        plat = dev_plat[(s.user, s.device)]
        for ti, st in enumerate(s.steps):
            if st.play:
                s.calib[ti] = scaled_play(st.play, plays[st.play.content].duration_s, factor(s.period, plat))
    return {"mode": "targets", "tilt": {p: round(v, 3) for p, v in tilt.items()}, "global": round(glob, 3),
            "anchors": anchors, "ott_share": {k: round(v, 4) for k, v in share.items()}, "estimated_rows": rows}


# ----------------------------------------------------------------------------- pass 4: timing
def time_sessions(cfg: Settings, briefs: list[UserBrief], sessions: list[Sess]) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    period_end = {p: datetime.combine(cfg.period_days(p)[-1], datetime.max.time(), tz) - timedelta(minutes=1)
                  for p in cfg.period_names}
    by_user: dict[tuple[str, str], list[Sess]] = {}
    for s in sessions:
        rng = rng_for(cfg.seed, "dwell", s.user, s.period, s.idx)
        s.dwell = [rng.uniform(4, 45) if st.screen not in ("checkout", "registration") else rng.uniform(40, 150)
                   for st in s.steps]
        # conservative upper bound of what emission will use (ticks are drawn from these ranges)
        s.dur_s = (60 + sum(max(1.5, d) for d in s.dwell) + sum(w + 5 for w, _ in s.calib.values())
                   + 3 * sum(len(st.lanes) for st in s.steps)
                   + sum(6 + (25.3 if st.auth in ("login", "register") else 0)
                         + (90 + 40 * 3 + 2 if st.billing else 0) for st in s.steps))
        by_user.setdefault((s.user, s.period), []).append(s)
    shifted = trimmed = dropped = 0
    for (u, p), ss in by_user.items():
        days = cfg.period_days(p)
        end_of_period = datetime.combine(days[-1], datetime.max.time(), tz) - timedelta(minutes=1)
        ss.sort(key=lambda s: (s.day, s.start_min, s.idx))
        prev_end: Optional[datetime] = None
        rng = rng_for(cfg.seed, "deoverlap", u, p)
        for s in ss:
            st = datetime.combine(days[s.day - 1], datetime.min.time(), tz) + timedelta(minutes=s.start_min)
            if prev_end and st < prev_end + timedelta(minutes=2):
                st = prev_end + timedelta(minutes=rng.randint(3, 40))
                shifted += 1
            critical = any(x.auth or x.billing for x in s.steps)
            if st > end_of_period - timedelta(minutes=10) and not critical:
                s.dropped = True                 # no room left in the month for a plain session
                dropped += 1
                continue
            if st + timedelta(seconds=s.dur_s) > end_of_period:
                # pull the start earlier if there is room, otherwise shorten its plays
                earliest = prev_end + timedelta(minutes=2) if prev_end else st - timedelta(hours=6)
                st = max(earliest, min(st, end_of_period - timedelta(seconds=s.dur_s)))
                if st > end_of_period - timedelta(minutes=10):
                    if not critical:             # the previous session runs to the end: no room left
                        s.dropped = True
                        dropped += 1
                        continue
                    st = end_of_period - timedelta(minutes=10)
                if st + timedelta(seconds=s.dur_s) > end_of_period:
                    trimmed += _trim(s, (end_of_period - st).total_seconds())
            s.start_dt = st
            s.day = (st.date() - days[0]).days + 1
            s.start_min = st.hour * 60 + st.minute
            prev_end = st + timedelta(seconds=s.dur_s)
    by_user = {k: [s for s in v if not s.dropped] for k, v in by_user.items()}
    nudge = _diurnal_nudge(cfg, by_user, period_end)
    return {"deoverlap_shifted": shifted, "trimmed_plays": trimmed, "dropped_sessions": dropped, **nudge}


def _trim(s: Sess, budget: float) -> int:
    """Shorten plays at the end of a session that would cross the period end."""
    over = s.dur_s - max(60.0, budget)
    n = 0
    for ti in sorted(s.calib, reverse=True):
        if over <= 0:
            break
        sec, _ = s.calib[ti]
        cut = min(over, sec - 60)
        if cut > 0:
            s.calib[ti] = (sec - cut, False)           # cut short -> not completed
            over -= cut
            s.dur_s -= cut
            n += 1
    return n


def target_hour_share(cfg: Settings) -> list[float]:
    acc = [0.0] * 24
    days = [d for p in cfg.period_names for d in cfg.period_days(p)]
    for d in days:
        w = hourly_weights(cfg, d)
        t = sum(w)
        for h in range(24):
            acc[h] += w[h] / t / len(days)
    return acc


def _diurnal_nudge(cfg: Settings, by_user: dict, period_end: dict) -> dict:
    target = target_hour_share(cfg)
    all_s = [s for ss in by_user.values() for s in ss]
    n = len(all_s)
    counts = [0] * 24
    for s in all_s:
        counts[s.start_dt.hour] += 1
    l1 = lambda: sum(abs(counts[h] / n - target[h]) for h in range(24))  # noqa: E731
    before = l1()
    tol = float(cfg["diurnal"]["tolerance_l1"])
    max_shift = int(cfg["diurnal"]["nudge_minutes"])
    rng = rng_for(cfg.seed, "nudge")
    order = sorted(all_s, key=lambda s: (s.user, s.period, s.idx))
    rng.shuffle(order)
    moved = 0
    neighbours = {(s.user, s.period): sorted(ss, key=lambda x: x.start_dt) for (s_u, s_p), ss in by_user.items() for s in ss[:1]}
    for _ in range(3):
        if l1() <= tol * 0.8:
            break
        for s in order:
            h = s.start_dt.hour
            excess = counts[h] / n - target[h]
            if excess <= 0:
                continue
            m = s.start_dt.minute
            for nh, shift in ((h + 1, 60 - m + rng.randint(0, 14)), (h - 1, -(m + 1 + rng.randint(0, 14)))):
                if not 0 <= nh <= 23 or abs(shift) > max_shift:
                    continue
                if counts[nh] / n - target[nh] >= 0:
                    continue
                new = s.start_dt + timedelta(minutes=shift)
                if new.date() != s.start_dt.date() or new + timedelta(seconds=s.dur_s) > period_end[s.period]:
                    continue
                ss = neighbours[(s.user, s.period)]
                i = ss.index(s)
                prev_ok = i == 0 or ss[i - 1].start_dt + timedelta(seconds=ss[i - 1].dur_s + 120) <= new
                next_ok = i == len(ss) - 1 or new + timedelta(seconds=s.dur_s + 120) <= ss[i + 1].start_dt
                if prev_ok and next_ok:
                    counts[h] -= 1
                    counts[nh] += 1
                    s.start_dt = new
                    s.start_min = new.hour * 60 + new.minute
                    moved += 1
                    break
            if l1() <= tol * 0.8:
                break
    return {"diurnal_l1_before": round(before, 4), "diurnal_l1_after": round(l1(), 4), "nudged_sessions": moved}


# ----------------------------------------------------------------------------- pass 5: payments
@dataclass
class PayGroup:
    key: tuple
    user: str
    period: str
    when: datetime
    kind: str                     # initial | renewal | retry
    outcome: str
    reason: Optional[str]
    plan: str
    attempt: int
    pre_timeouts: int = 0


def plan_payments(cfg: Settings, briefs: list[UserBrief], sessions: list[Sess]) -> tuple[dict, dict]:
    tz = ZoneInfo(cfg["timezone"])
    groups: list[PayGroup] = []
    for b in briefs:
        for p, pb in b.periods.items():
            for i, e in enumerate(pb.billing):
                groups.append(PayGroup(("srv", b.user_id, p, i), b.user_id, p,
                                       datetime.fromisoformat(e.when).replace(tzinfo=tz), e.kind, e.outcome,
                                       e.failure_reason, e.plan, e.attempt_number))
    plan_of = {(b.user_id, p): pb.plan for b in briefs for p, pb in b.periods.items()}
    for s in sessions:
        for ti, st in enumerate(s.steps):
            if st.billing in ("subscribe_monthly", "subscribe_annual"):
                groups.append(PayGroup(("ses", s.user, s.period, s.idx, ti), s.user, s.period, s.start_dt,
                                       "initial", "received", None, plan_of[(s.user, s.period)] or "monthly", 1))
    r0 = float(cfg["lifecycle"]["transient_timeout_rate"])
    incidents = {date.fromisoformat(str(i["date"])): i for i in cfg.payment_incidents()}
    rng = rng_for(cfg.seed, "payments")
    groups.sort(key=lambda g: g.key)
    meta = {}
    for p in cfg.period_names:
        pdays = set(cfg.period_days(p))
        inc_days = sorted(d for d in incidents if d in pdays)
        normal = [g for g in groups if g.period == p and g.when.date() not in inc_days]
        T = sum(1 for g in normal if g.reason == "gateway_timeout")
        x = max(0, round((r0 * len(normal) - T) / (1 - r0)))
        ok = [g for g in normal if g.outcome == "received"]
        for g in rng.sample(ok, min(x, len(ok))):
            g.pre_timeouts = 1
        attempts = len(normal) + x
        r_real = (T + x) / attempts if attempts else r0
        meta[p] = {"groups": len(normal), "baseline_timeout_rate": round(r_real, 4), "incidents": {}}
        for inc_day in inc_days:
            inc = incidents[inc_day]
            incident = [g for g in groups if g.period == p and g.when.date() == inc_day]
            if not incident:
                continue
            r1 = min(0.85, float(inc["multiplier"]) * r_real)
            T1 = sum(1 for g in incident if g.reason == "gateway_timeout")
            x1 = max(0, round((r1 * len(incident) - T1) / (1 - r1)))
            ok1 = [g for g in incident if g.outcome == "received"] or incident
            for k in range(x1):
                g = ok1[k % len(ok1)]
                g.pre_timeouts = min(3, g.pre_timeouts + 1)
            t1 = T1 + sum(g.pre_timeouts for g in incident)
            a1 = len(incident) + sum(g.pre_timeouts for g in incident)
            meta[p]["incidents"][inc_day.isoformat()] = {
                "groups": len(incident), "timeout_rate": round(t1 / a1, 4),
                "multiplier": round((t1 / a1) / r_real, 2) if r_real else None}
    return {g.key: g for g in groups}, meta


# ----------------------------------------------------------------------------- pass 6: emission
class Emitter:
    def __init__(self, cfg: Settings, cat: Catalog, out_path: Path):
        self.cfg, self.cat = cfg, cat
        self.titles, self.plays, self.lanes = cat.title_map(), cat.playable_map(), cat.lane_map()
        self.f = open(out_path, "w")
        self.rows = 0
        self.cap = int(cfg["simulation"]["max_row_cap"])
        self.prices = cfg["prices"]
        self.identity: list[dict] = []

    def close(self) -> None:
        self.f.close()

    def write(self, ev: dict) -> None:
        validate_event(ev)
        self.f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.rows += 1
        if self.rows > self.cap:
            raise RowCapExceeded(f"row count passed MAX_ROW_CAP={self.cap}")


def _base(ev: str, t: datetime, ins: str, sid: Optional[str], dev: Device, user_id: str, logged_in: bool,
          seg: str, source: str = "client") -> dict:
    return {"event": ev, "distinct_id": user_id if logged_in else dev.anon_id, "$insert_id": ins,
            "timestamp": iso(t), "session_id": sid, "user_id": user_id if logged_in else None,
            "anonymous_id": dev.anon_id, "user_segment": seg, "platform": dev.platform,
            "device_id": dev.device_id, "device_type": dev.device_type, "device_brand": dev.brand,
            "device_model": dev.model, "os": dev.os, "os_version": dev.os_version,
            "app_version": dev.app_version, "viewport_width": dev.viewport_w, "viewport_height": dev.viewport_h,
            "country": "DE", "locale": "de-DE", "event_source": source}


def emit_user(em: Emitter, cfg: Settings, b: UserBrief, sessions: list[Sess], pay: dict,
              mkt: Optional[dict] = None, prior_identified: Optional[set] = None) -> None:
    devs = {d.label: d for d in b.devices}
    method = rng_for(cfg.seed, "method", b.user_id).choices(
        list(cfg["lifecycle"]["login_methods"]), weights=list(cfg["lifecycle"]["login_methods"].values()))[0]
    p_decay = float(cfg["stories"]["lane_ctr"]["position_decay_p"])
    # devices already stitched in an earlier batch don't fire $identify again
    identified: set[str] = {d.label for d in b.devices if d.anon_id in (prior_identified or set())}
    logged_in_devices: set[str] = set()
    timeline: list[tuple[datetime, int, list[dict]]] = []

    for p in b.periods:
        pb = b.periods[p]
        seg = pb.segment_at_start
        if seg == "anonymous":
            logged_in_devices.clear()
        churn_at = None
        srv = [g for k, g in pay.items() if k[0] == "srv" and g.user == b.user_id and g.period == p]
        for g in srv:
            if g.outcome == "failed" and g.kind == "retry":
                churn_at = g.when
        ss = sorted([s for s in sessions if s.user == b.user_id and s.period == p], key=lambda s: s.start_dt)
        for s in ss:
            rng = rng_for(cfg.seed, "emit", b.user_id, p, s.idx)
            dev = devs[s.device]
            sid = uid(cfg.seed, "session", b.user_id, p, s.idx)
            n = 0
            to_midnight = (datetime.combine(s.start_dt.date() + timedelta(days=1), datetime.min.time(),
                                            s.start_dt.tzinfo) - s.start_dt).total_seconds()
            t = s.start_dt + timedelta(seconds=rng.uniform(0, min(59.0, to_midnight - 1)))  # stay on the same day
            evs: list[dict] = []

            def ins() -> str:
                nonlocal n
                n += 1
                return uid(cfg.seed, b.user_id, p, s.idx, n)

            def cur_seg() -> str:
                # a lapsed subscription ends at midnight of the lapse day (journeys allow SVOD that day)
                if seg == "subscribed" and churn_at and t.date() > churn_at.date():
                    return "registered"
                return seg

            touch = (mkt or {}).get((b.user_id, p, s.idx), {})

            def base(ev: str) -> dict:
                e = _base(ev, t, ins(), sid, dev, b.user_id, dev.label in logged_in_devices, cur_seg())
                e.update(touch)                  # session-scoped marketing properties
                return e

            def tick(lo: float, hi: float) -> None:
                nonlocal t
                t = t + timedelta(seconds=rng.uniform(lo, hi))

            prev_screen = None
            for ti, st in enumerate(s.steps):
                click_info = None
                e = base("screen_viewed")
                e.update(screen_name=st.screen, previous_screen=prev_screen)
                evs.append(e)
                if st.auth in ("login", "register"):
                    tick(5, 25)
                    if st.auth == "register":
                        seg = "registered"
                    logged_in_devices.add(dev.label)
                    e = base("registration" if st.auth == "register" else "login")
                    e["method"] = method
                    if st.auth == "login":
                        e["success"] = True
                    evs.append(e)
                    if dev.label not in identified:
                        tick(0.05, 0.3)
                        e = base("$identify")
                        e["$anon_distinct_id"] = dev.anon_id
                        evs.append(e)
                        identified.add(dev.label)
                        em.identity.append({"user_id": b.user_id, "anon_distinct_id": dev.anon_id,
                                            "device_id": dev.device_id, "platform": dev.platform,
                                            "first_identified_at": iso(t)})
                for li, lid in enumerate(st.lanes, start=1):
                    tick(0.4, 3.0)
                    lane = em.lanes[lid]
                    order = lane.title_ids[:]
                    rng.shuffle(order)
                    if st.click and st.click.lane == lid:
                        k = 1
                        while k < len(order) and rng.random() > p_decay:
                            k += 1
                        order.remove(st.click.title)
                        order.insert(k - 1, st.click.title)
                    e = base("lane_viewed")
                    e.update(screen_name=st.screen, lane_id=lid, lane_name=lane.lane_name, lane_type=lane.lane_type,
                             lane_position=li, assets_list=[
                                 {"content_id": tid, "title": em.titles[tid].title, "asset_position": i,
                                  "licence_type": em.titles[tid].licence_type} for i, tid in enumerate(order, start=1)])
                    evs.append(e)
                    if st.click and st.click.lane == lid:
                        click_info = (lane, li, order.index(st.click.title) + 1)
                if st.billing in ("subscribe_monthly", "subscribe_annual"):
                    g = pay[("ses", b.user_id, p, s.idx, ti)]
                    plan = st.billing.split("_")[1]
                    price = float(em.prices[plan])
                    tick(20, 90)
                    for a in range(1, g.pre_timeouts + 1):
                        e = base("payment_failed")
                        e.update(plan=plan, amount=price, currency="EUR", payment_type="initial",
                                 failure_reason="gateway_timeout", attempt_number=a)
                        evs.append(e)
                        tick(8, 40)
                    e = base("payment_received")
                    e.update(plan=plan, amount=price, currency="EUR", payment_type="initial",
                             transaction_id=uid("txn", b.user_id, p, s.idx, ti), attempt_number=g.pre_timeouts + 1)
                    evs.append(e)
                    tick(0.3, 2)
                    seg = "subscribed"
                    e = base("subscribed")
                    e.update(plan=plan, price=price, currency="EUR")
                    evs.append(e)
                if st.play:
                    tick(1, 4)
                    pl = em.plays[st.play.content]
                    sec, completed = s.calib[ti]
                    sec = max(1, int(round(sec)))
                    psid = uid(cfg.seed, "playback", b.user_id, p, s.idx, ti)
                    t0 = t

                    def vv(kind: str, watched: int) -> dict:
                        e = base("video_view")
                        cr = None if pl.duration_s is None else round(min(1.0, watched / pl.duration_s), 4)
                        e.update(screen_name="player", playback_event=kind, playback_session_id=psid,
                                 content_id=pl.content_id, title_id=pl.title_id, title=pl.title,
                                 episode_title=pl.episode_title, season=pl.season, episode=pl.episode,
                                 genre=pl.genre, content_type=pl.content_type, licence_type=pl.licence_type,
                                 duration_seconds=pl.duration_s, watch_time_seconds=watched, completion_rate=cr)
                        return e

                    evs.append(vv("start", 0))
                    k = 1
                    while k * HB < sec:
                        t = t0 + timedelta(seconds=k * HB, milliseconds=rng.randint(0, 900))
                        evs.append(vv("heartbeat", k * HB))
                        k += 1
                    t = t0 + timedelta(seconds=sec, milliseconds=rng.randint(901, 999))
                    final = "complete" if completed and pl.duration_s and sec / pl.duration_s >= 0.9 else "heartbeat"
                    evs.append(vv(final, sec))
                if st.click:
                    tick(1, 6)
                    lane, li, pos = click_info
                    tt = em.titles[st.click.title]
                    e = base("asset_clicked")
                    e.update(screen_name=st.screen, lane_id=lane.lane_id, lane_name=lane.lane_name,
                             lane_type=lane.lane_type, lane_position=li, content_id=tt.title_id, title=tt.title,
                             asset_position=pos, licence_type=tt.licence_type)
                    evs.append(e)
                tick(*sorted((1.0, max(1.5, s.dwell[ti]))))
                prev_screen = st.screen
            timeline.append((s.start_dt, 0, evs))

        # server-side billing for this period
        d1 = devs["d1"]
        for g in sorted(srv, key=lambda g: g.when):
            price = float(em.prices[g.plan])
            ptype = "retry" if g.kind == "retry" else "renewal"
            evs = []
            t = g.when - timedelta(minutes=25 * g.pre_timeouts)
            seg_srv = "subscribed"
            for a in range(1, g.pre_timeouts + 1):
                e = _base("payment_failed", t, uid(cfg.seed, "srv", g.key, a), None, d1, b.user_id, True, seg_srv, "server")
                e.update(plan=g.plan, amount=price, currency="EUR", payment_type=ptype,
                         failure_reason="gateway_timeout", attempt_number=a)
                evs.append(e)
                t += timedelta(minutes=25)
            att = g.attempt + g.pre_timeouts
            if g.outcome == "received":
                e = _base("payment_received", t, uid(cfg.seed, "srv", g.key, "ok"), None, d1, b.user_id, True, seg_srv, "server")
                e.update(plan=g.plan, amount=price, currency="EUR", payment_type=ptype,
                         transaction_id=uid("txn", *g.key), attempt_number=att)
            else:
                e = _base("payment_failed", t, uid(cfg.seed, "srv", g.key, "fail"), None, d1, b.user_id, True, seg_srv, "server")
                e.update(plan=g.plan, amount=price, currency="EUR", payment_type=ptype,
                         failure_reason=g.reason, attempt_number=att)
            evs.append(e)
            timeline.append((g.when - timedelta(minutes=25 * g.pre_timeouts), 1, evs))

    for _, _, evs in sorted(timeline, key=lambda x: (x[0], x[1])):
        for e in evs:
            em.write(e)


# ----------------------------------------------------------------------------- runner
def expand(cfg: Settings, cat: Catalog, briefs: list[UserBrief], journeys: dict, out_dir: Optional[Path] = None,
           prior_identified: Optional[set] = None, new_users: Optional[set] = None) -> dict:
    """Expand one batch (all periods in `cfg`) into `out_dir` (default: cfg.out)."""
    out = out_dir or cfg.out
    out.mkdir(parents=True, exist_ok=True)
    sessions, src = prepare_sessions(cfg, cat, briefs, journeys)
    meta: dict = {"journey_sources": src}
    meta["lane_ctr_padding"] = pad_lane_ctr(cfg, cat, sessions)
    meta["watch_calibration"] = calibrate_watch(cfg, cat, briefs, sessions)
    meta["timing"] = time_sessions(cfg, briefs, sessions)
    sessions = [s for s in sessions if not s.dropped]
    pay, meta["payments"] = plan_payments(cfg, briefs, sessions)
    mkt = assign_marketing(cfg, cat, briefs, sessions, pay, new_users=new_users)
    write_marketing(out, mkt)
    meta["marketing"] = mkt.meta

    by_user: dict[str, list[Sess]] = {}
    for s in sessions:
        by_user.setdefault(s.user, []).append(s)
    pay_by_user: dict[str, dict] = {}
    for k, g in pay.items():
        pay_by_user.setdefault(g.user, {})[k] = g

    tmp = out / "events.ndjson.tmp"
    em = Emitter(cfg, cat, tmp)
    try:
        for b in briefs:
            emit_user(em, cfg, b, by_user.get(b.user_id, []), pay_by_user.get(b.user_id, {}), mkt.session_props,
                      prior_identified)
    except RowCapExceeded:
        em.close()
        tmp.unlink(missing_ok=True)
        raise
    em.close()
    tmp.replace(out / "events.ndjson")
    with open(out / "identity_map.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=IDENTITY_COLS)
        w.writeheader()
        w.writerows(em.identity)
    meta["rows"] = em.rows
    write_json(out / "expand_meta.json", meta)
    log.info("expand: %s rows written to %s", f"{em.rows:,}", out)
    return meta


IDENTITY_COLS = ["user_id", "anon_distinct_id", "device_id", "platform", "first_identified_at"]


def expand_all(reg, cat: Catalog) -> dict:
    """Expand every batch in order into out/batches/<run_id>/, then merge into out/.

    Each batch only sees its own periods and users, so adding a batch never changes earlier ones."""
    from .journeys import load_journeys
    root = reg.cfg.out
    prior: set[str] = set()
    metas = {}
    for bt in reg.batches:
        cfgv, views = reg.batch_view(bt)
        journeys = load_journeys(cfgv, views)
        bdir = root / "batches" / bt.run_id
        metas[bt.run_id] = expand(cfgv, cat, views, journeys, bdir, prior, set(bt.new_users))
        with open(bdir / "identity_map.csv") as f:
            prior |= {r["anon_distinct_id"] for r in csv.DictReader(f)}
    merge_batches(reg, root, metas)
    return metas


def merge_batches(reg, root: Path, metas: dict) -> None:
    """Concatenate batch outputs into out/events.ndjson, identity_map.csv and out/marketing/."""
    dirs = [root / "batches" / bt.run_id for bt in reg.batches]
    tmp = root / "events.ndjson.tmp"
    with open(tmp, "wb") as fout:
        for d in dirs:
            with open(d / "events.ndjson", "rb") as fin:
                shutil.copyfileobj(fin, fout)
    tmp.replace(root / "events.ndjson")
    _concat_csv([d / "identity_map.csv" for d in dirs], root / "identity_map.csv")
    (root / "marketing").mkdir(exist_ok=True)
    for name in ("marketing_spend_daily.csv", "user_acquisition.csv"):
        _concat_csv([d / "marketing" / name for d in dirs], root / "marketing" / name)
    _concat_csv([d / "marketing" / "campaigns.csv" for d in dirs], root / "marketing" / "campaigns.csv",
                unique_key="utm_campaign")
    shutil.copyfile(dirs[0] / "marketing" / "channel_mapping.csv", root / "marketing" / "channel_mapping.csv")
    rows = sum(m["rows"] for m in metas.values())
    write_json(root / "expand_meta.json", {"rows": rows, "batches": metas})
    log.info("merged %d batch(es): %s rows", len(dirs), f"{rows:,}")


def _concat_csv(paths: list[Path], out: Path, unique_key: Optional[str] = None) -> None:
    seen: set = set()
    header = None
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            r = csv.DictReader(f)
            header = header or r.fieldnames
            for row in r:
                if unique_key:
                    if row[unique_key] in seen:
                        continue
                    seen.add(row[unique_key])
                rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header or [])
        w.writeheader()
        w.writerows(rows)
