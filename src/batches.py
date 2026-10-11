"""Batches: grow the dataset with more users and/or new time frames, additively.

The dataset is a list of batches. Batch "base" is config.yaml + cache/personas.json (the original
1,000 users, STLY + CURRENT). Every further batch comes from a spec file:

    run_id: oct26
    periods:
      - {name: OCT_2026, start: 2026-10-01, end: 2026-10-31, new_users: 300, returning_users: 350}
    returning_from: all          # all | batch  (who may return: anyone earlier, or only this batch's users)
    targets: {mvu_ratio: 0.6, segments: {...}, platforms: {...}}      # optional, new users
    lifecycle: {returning_register_rate: 0.06, returning_subscribe_rate: 0.04}
    stories:
      premieres: [{title_id: joyn_gntm, date: 2026-10-08, fan_share: 0.3, episodes: 8, cadence_days: [0, 3]}]
      payment_incidents: [{date: 2026-10-14, multiplier: 5.0, tol: 1.0, min_count: 5}]
      campaigns: [{key: launch_gntm, objective: launch, topic: gntm, channels: [paid_social, crm_push],
                   start: 2026-10-01, end: 2026-10-20, contents: [video15s_gntm_a]}]
      ott_pair: [CURRENT, OCT_2026]     # optional: apply the ott-share tilt between two periods
    marketing: {acquisition_mix: {...}, tiktok_share_of_paid_social: 0.45}

Rules that keep it safe:
- a batch never changes earlier batches: their personas, journeys and events stay byte-identical;
- new users get fresh ids (u1001, ...); returning users keep their id, devices and account state
  (registered / subscribed / churned, plan, renewal day) carried forward from their last period;
- a user is active at most once per period, and only returns to a period after their last one;
- a batch's stories (incidents, premieres, campaigns) apply to it and to later batches, never to earlier ones.
"""
from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from .config import Settings, read_json, rng_for, write_json
from .models import PeriodBrief, UserBrief

log = logging.getLogger(__name__)
RANK = {"anonymous": 0, "registered": 1, "subscribed": 2}


class SpecError(ValueError):
    pass


# ----------------------------------------------------------------------------- spec
def load_spec(path: str | Path) -> dict:
    """Read and check a batch spec file."""
    spec = yaml.safe_load(Path(path).read_text())
    if not isinstance(spec, dict):
        raise SpecError("spec must be a YAML mapping")
    rid = str(spec.get("run_id", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{1,40}", rid) or rid == "base":
        raise SpecError("run_id must be 2-41 chars of a-z, 0-9, _ (and not 'base')")
    periods = spec.get("periods") or []
    if not periods:
        raise SpecError("spec needs at least one period")
    names = set()
    for p in periods:
        for k in ("name", "start", "end"):
            if k not in p:
                raise SpecError(f"period needs '{k}': {p}")
        if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", str(p["name"])):
            raise SpecError(f"bad period name {p['name']!r}")
        if p["name"] in names:
            raise SpecError(f"period {p['name']} listed twice")
        names.add(p["name"])
        p["start"], p["end"] = str(p["start"]), str(p["end"])
        a, b = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        if b < a or (b - a).days > 92:
            raise SpecError(f"period {p['name']}: end must be after start and within 93 days")
        p["new_users"] = int(p.get("new_users", 0))
        p["returning_users"] = int(p.get("returning_users", 0))
        if p["new_users"] < 0 or p["returning_users"] < 0:
            raise SpecError("user counts must be >= 0")
    if sum(p["new_users"] + p["returning_users"] for p in periods) == 0:
        raise SpecError("spec adds no users")
    if spec.get("returning_from", "all") not in ("all", "batch"):
        raise SpecError("returning_from must be 'all' or 'batch'")
    spec.setdefault("returning_from", "all")
    for k in ("targets", "lifecycle", "stories", "marketing"):
        spec.setdefault(k, {})
    for prem in spec["stories"].get("premieres", []) or []:
        prem["date"] = str(prem["date"])
    for inc in spec["stories"].get("payment_incidents", []) or []:
        inc["date"] = str(inc["date"])
        inc.setdefault("multiplier", 5.0)
        inc.setdefault("tol", 1.0)
        inc.setdefault("min_count", 5)
    for c in spec["stories"].get("campaigns", []) or []:
        for k in ("key", "objective", "topic", "channels", "start", "end"):
            if k not in c:
                raise SpecError(f"campaign needs '{k}': {c}")
        c["start"], c["end"] = str(c["start"]), str(c["end"])
    return spec


# ----------------------------------------------------------------------------- registry
@dataclass
class Batch:
    run_id: str
    spec: Optional[dict]
    periods: dict[str, dict]                     # name -> {start, end}
    new_users: list[str] = field(default_factory=list)
    owned: dict[str, list[str]] = field(default_factory=dict)   # user -> periods this batch generates

    @property
    def user_months(self) -> int:
        return sum(len(v) for v in self.owned.values())


class Registry:
    """Base + all batches, merged. Load with Registry.load(cfg)."""

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.batches: list[Batch] = []
        self.briefs: dict[str, UserBrief] = {}
        self.periods: dict[str, dict] = {}
        self._new_briefs: dict[str, list[UserBrief]] = {}
        self._ext: dict[str, dict[str, dict[str, PeriodBrief]]] = {}

    # ---- paths
    @staticmethod
    def batch_dir(cfg: Settings, run_id: str) -> Path:
        return cfg.cache / "batches" / run_id

    @staticmethod
    def index_path(cfg: Settings) -> Path:
        return cfg.cache / "batches" / "index.json"

    @classmethod
    def run_ids(cls, cfg: Settings) -> list[str]:
        p = cls.index_path(cfg)
        return read_json(p) if p.exists() else []

    # ---- load
    @classmethod
    def load(cls, cfg: Settings, base_briefs: Optional[list[UserBrief]] = None) -> "Registry":
        reg = cls(cfg)
        if base_briefs is None:
            from .personas import load_personas
            base_briefs = load_personas(cfg)
        base_periods = {k: {"start": str(v["start"]), "end": str(v["end"])} for k, v in cfg.raw["periods"].items()}
        base = Batch("base", None, base_periods, [b.user_id for b in base_briefs],
                     {b.user_id: list(b.periods) for b in base_briefs})
        reg.batches.append(base)
        reg.periods.update(base_periods)
        reg._new_briefs["base"] = base_briefs
        reg._ext["base"] = {}
        for rid in cls.run_ids(cfg):
            d = cls.batch_dir(cfg, rid)
            spec = read_json(d / "spec.json")
            new = [UserBrief.model_validate(x) for x in read_json(d / "personas.json")]
            ext = {u: {p: PeriodBrief.model_validate(pb) for p, pb in per.items()}
                   for u, per in read_json(d / "extensions.json").items()}
            periods = {p["name"]: {"start": p["start"], "end": p["end"]} for p in spec["periods"]}
            owned = {b.user_id: list(b.periods) for b in new}
            for u, per in ext.items():
                owned.setdefault(u, []).extend(per)
            reg.batches.append(Batch(rid, spec, periods, [b.user_id for b in new], owned))
            reg.periods.update(periods)
            reg._new_briefs[rid] = new
            reg._ext[rid] = ext
        reg._merge()
        return reg

    def _merge(self) -> None:
        order = {p: str(v["start"]) for p, v in self.periods.items()}
        self.briefs = {}
        for bt in self.batches:
            for b in self._new_briefs[bt.run_id]:
                self.briefs[b.user_id] = b.model_copy(deep=True)
            for u, per in self._ext[bt.run_id].items():
                ub = self.briefs[u]
                ub.periods.update({p: pb.model_copy(deep=True) for p, pb in per.items()})
                ub.periods = dict(sorted(ub.periods.items(), key=lambda kv: order[kv[0]]))
                tier = max([RANK[ub.segment]] + [RANK[end_state(pb)[0]] for pb in per.values()]
                           + [RANK["subscribed"] if pb.must_subscribe_on_day else 0 for pb in per.values()])
                ub.segment = [k for k, v in RANK.items() if v == tier][0]

    # ---- views
    def stories_upto(self, run_id: str) -> dict:
        """Spec stories of every batch up to and including run_id (never later ones)."""
        out = {"premieres": [], "payment_incidents": [], "campaigns": []}
        for bt in self.batches:
            if bt.spec:
                for k in out:
                    out[k] += list(bt.spec["stories"].get(k) or [])
            if bt.run_id == run_id:
                break
        return out

    def batch_view(self, bt: Batch) -> tuple[Settings, list[UserBrief]]:
        """Settings restricted to the batch's periods + brief views holding only its own periods."""
        if bt.spec is None:
            cfgv = self.cfg.view(bt.periods)
        else:
            cfgv = self.cfg.view(bt.periods, spec_overrides(self.cfg, bt.spec, bt.user_months,
                                                            self.stories_upto(bt.run_id), self.anchors(bt)))
        views = []
        for u, periods in bt.owned.items():
            b = self.briefs[u].model_copy(deep=True)
            b.periods = {p: b.periods[p] for p in b.periods if p in periods}
            views.append(b)
        views.sort(key=lambda b: b.user_id)
        return cfgv, views

    def anchors(self, bt: Batch) -> dict[str, float]:
        """Ott share of watch time already realised by earlier batches for periods this batch shares
        (watch-time weighted). New users in an existing month are steered to it so they don't dilute it."""
        num: dict[str, float] = defaultdict(float)
        den: dict[str, float] = defaultdict(float)
        for earlier in self.batches:
            if earlier.run_id == bt.run_id:
                break
            meta_p = self.cfg.out / "batches" / earlier.run_id / "expand_meta.json"
            if not meta_p.exists():
                continue
            share = read_json(meta_p).get("watch_calibration", {}).get("ott_share", {})
            for p in bt.periods:
                if p in share:
                    n = sum(1 for ps in earlier.owned.values() if p in ps)
                    num[p] += share[p] * n
                    den[p] += n
        return {p: round(num[p] / den[p], 4) for p in num if den[p]}

    def combined_cfg(self) -> Settings:
        """All periods; stories of all batches. Used by validation and merged exports."""
        last = self.batches[-1]
        st = self.stories_upto(last.run_id)
        ov: dict = {"stories": {"payment_incidents": st["payment_incidents"], "premieres": st["premieres"]},
                    "marketing": {"campaigns": list(self.cfg["marketing"]["campaigns"]) + st["campaigns"]}}
        for bt in self.batches:
            if bt.spec:
                deep = spec_overrides(self.cfg, bt.spec, bt.user_months, st)
                ov["marketing"].setdefault("acquisition_mix", {}).update(deep["marketing"]["acquisition_mix"])
                ov["marketing"].setdefault("tiktok_share_of_paid_social", {}).update(
                    deep["marketing"]["tiktok_share_of_paid_social"])
        return self.cfg.view(self.periods, ov)

    def owner(self) -> dict[tuple[str, str], str]:
        return {(u, p): bt.run_id for bt in self.batches for u, ps in bt.owned.items() for p in ps}


def spec_overrides(cfg: Settings, spec: dict, user_months: int, stories: dict,
                   anchors: Optional[dict] = None) -> dict:
    """Config overrides for a spec batch: row bounds scaled to its size, targets, stories, marketing."""
    S = cfg["simulation"]["rows_per_user_month"]
    um = max(1, user_months)
    names = [p["name"] for p in spec["periods"]]
    pair = spec["stories"].get("ott_pair") or (["STLY", "CURRENT"] if {"STLY", "CURRENT"} <= set(names) else None)
    M = cfg["marketing"]
    last_mix = list(M["acquisition_mix"].values())[-1]
    last_tt = list(M["tiktok_share_of_paid_social"].values())[-1]
    mix = spec["marketing"].get("acquisition_mix")
    tt = spec["marketing"].get("tiktok_share_of_paid_social")
    ov = {
        "simulation": {"calibrate_rows_to": round(S["calibrate"] * um), "max_row_cap": round(S["cap"] * um),
                       "target_rows": [round(S["target"][0] * um), round(S["target"][1] * um)]},
        "stories": {"ott_watch_share_pp": {"pair": pair, "mode": "targets", "anchors": anchors or {}},
                    "payment_incidents": stories["payment_incidents"], "premieres": stories["premieres"]},
        "marketing": {
            "acquisition_mix": {n: _per_period(mix, n, names) or M["acquisition_mix"].get(n) or last_mix for n in names},
            "tiktok_share_of_paid_social": {n: _per_period(tt, n, names) if _per_period(tt, n, names) is not None
                                            else M["tiktok_share_of_paid_social"].get(n, last_tt) for n in names},
            "campaigns": list(M["campaigns"]) + stories["campaigns"],
        },
    }
    t = spec.get("targets") or {}
    if "mvu_ratio" in t:
        ov["users"] = {"mvu_ratio": float(t["mvu_ratio"])}
    if "segments" in t:
        ov["segments"] = dict(t["segments"])
    if "platforms" in t:
        ov["platforms"] = dict(t["platforms"])
    if spec.get("lifecycle"):
        ov["lifecycle"] = dict(spec["lifecycle"])
    return ov


def _per_period(value, period: str, names: list[str]):
    """Spec marketing values may be one value for all periods, or a mapping keyed by period name."""
    if isinstance(value, dict) and set(value) & set(names):
        return value.get(period)
    return value


def end_state(pb: PeriodBrief) -> tuple[str, bool]:
    """(segment at the end of a period, churned?) from its brief."""
    s = pb.segment_at_start
    if pb.must_register_on_day and s == "anonymous":
        s = "registered"
    if pb.must_subscribe_on_day:
        s = "subscribed"
    churned = pb.svod_until_day is not None
    if churned:
        s = "registered"
    return s, churned


# ----------------------------------------------------------------------------- build a batch
def build_batch(reg: Registry, spec: dict) -> tuple[list[UserBrief], dict[str, dict[str, PeriodBrief]]]:
    """Personas for a new batch: new users plus period briefs for returning users."""
    from .personas import (_device, _lognormal_hours, _pick, _plant_incident_renewals, _quota,
                           ARCHETYPES, renewal_attempts)
    cfg = reg.cfg
    run = spec["run_id"]
    periods = sorted(spec["periods"], key=lambda p: p["start"])
    _check_periods(reg, periods)
    um = sum(p["new_users"] + p["returning_users"] for p in periods)
    upto = reg.stories_upto(reg.batches[-1].run_id)           # every existing batch ...
    stories = {k: upto[k] + list(spec["stories"].get(k) or []) for k in upto}   # ... plus this spec
    cfgv = cfg.view({p["name"]: {"start": p["start"], "end": p["end"]} for p in periods},
                    spec_overrides(cfg, spec, um, stories))
    seed = cfg.seed
    rng = rng_for(seed, "batch", run)
    L = cfgv["lifecycle"]
    ret_reg = float(L.get("returning_register_rate", 0.06))
    ret_sub = float(L.get("returning_subscribe_rate", 0.04))

    start = max(int(u[1:]) for u in reg.briefs) + 1
    n_new = sum(p["new_users"] for p in periods)
    new_ids = [f"u{i:04d}" for i in range(start, start + n_new)]
    seg_labels = [s for s, n in _quota(n_new, cfgv["segments"]).items() for _ in range(n)]
    plat_labels = [s for s, n in _quota(n_new, cfgv["platforms"]).items() for _ in range(n)]
    rng.shuffle(seg_labels)
    rng.shuffle(plat_labels)
    segment = dict(zip(new_ids, seg_labels))
    platform = dict(zip(new_ids, plat_labels))

    # history (start, end) of every existing user, and their state at the end of it
    hist: dict[str, list[str]] = {u: [str(reg.periods[p]["end"]) for p in b.periods] for u, b in reg.briefs.items()}
    in_period: dict[str, set] = {u: set(b.periods) for u, b in reg.briefs.items()}
    state: dict[str, dict] = {}
    for u, b in reg.briefs.items():
        last = max(b.periods.values(), key=lambda pb: str(reg.periods[pb.period]["start"]))
        seg, churned = end_state(last)
        by_date = sorted(b.periods.values(), key=lambda pb: str(reg.periods[pb.period]["start"]))
        state[u] = {"seg": seg, "churned": churned, "plan": next((pb.plan for pb in reversed(by_date) if pb.plan), None),
                    "renewal_day": b.renewal_day, "renewal_month": b.renewal_month, "video": last.is_video_user}

    new_briefs: dict[str, UserBrief] = {}
    ext: dict[str, dict[str, PeriodBrief]] = defaultdict(dict)
    id_iter = iter(new_ids)
    days_of = {p["name"]: cfgv.period_days(p["name"]) for p in periods}

    for P in periods:
        name, pstart = P["name"], P["start"]
        days = days_of[name]
        news = [next(id_iter) for _ in range(P["new_users"])]
        # returning users: last active period ended before this one starts, not already active in it
        pool = [u for u in (sorted(reg.briefs) + sorted(new_briefs)) if u not in news
                and hist.get(u) and max(hist[u]) < pstart and name not in in_period.get(u, set())]
        if spec["returning_from"] == "batch":
            pool = [u for u in pool if u in new_briefs]
        if len(pool) < P["returning_users"]:
            raise SpecError(f"{name}: asked for {P['returning_users']} returning users but only {len(pool)} "
                            f"earlier users are eligible (last active before {pstart}, not already in {name})")
        prng = rng_for(seed, "returning", run, name)
        def score(u: str) -> float:
            st = state[u]
            gap = (date.fromisoformat(pstart) - date.fromisoformat(max(hist[u]))).days
            return RANK[st["seg"]] * 0.8 + (0.8 if st["video"] else 0) - gap / 400 + prng.random() * 1.5
        returning = sorted(sorted(pool, key=score, reverse=True)[: P["returning_users"]])

        # new users: devices, lifecycle in their first period
        for u in news:
            urng = rng_for(seed, "user", u)
            seg, prim = segment[u], platform[u]
            devices = [_device(urng, prim, "d1", name)]
            if seg != "anonymous" and urng.random() < float(cfgv["second_device_rate"]):
                devices.append(_device(urng, _pick(urng, {k: v for k, v in cfgv["platforms"].items() if k != prim}),
                                       "d2", name))
            st = {"seg": "anonymous", "churned": False, "plan": None, "renewal_day": None,
                  "renewal_month": None, "video": False, "reg_day": None, "sub_day": None}
            reg_before = sub_before = False
            if seg == "subscribed":
                sub_before = urng.random() >= float(L["subscribed_in_window_share"])
                st["plan"] = _pick(urng, L["plan_split"])
                if sub_before:
                    reg_before = True
                    st.update(seg="subscribed", renewal_day=urng.randint(1, 28), renewal_month=urng.randint(1, 12))
                else:
                    sday = urng.randint(3, len(days) - 2)
                    st.update(sub_day=sday, renewal_day=sday, renewal_month=days[0].month)
                    reg_before = urng.random() < 0.8
                    st["seg"] = "registered" if reg_before else "anonymous"
                    if not reg_before:
                        st["reg_day"] = urng.randint(1, sday)
            elif seg == "registered":
                reg_before = urng.random() >= float(L["registered_in_window_share"])
                st["seg"] = "registered" if reg_before else "anonymous"
                if not reg_before:
                    st["reg_day"] = urng.randint(1, len(days) - 1)
            state[u] = st
            new_briefs[u] = UserBrief(user_id=u, segment=seg, primary_platform=prim, devices=devices, periods={},
                                      registered_before_window=reg_before, subscribed_before_window=sub_before,
                                      renewal_day=st["renewal_day"], renewal_month=st["renewal_month"], batch=run)

        active = news + returning
        # video users: exact count for the period, engaged users first
        n_mvu = round(len(active) * float(cfgv["users"]["mvu_ratio"]))
        vrng = rng_for(seed, "video", run, name)
        bias = {"subscribed": 3.0, "registered": 0.9, "anonymous": 0.0}
        tier = lambda u: segment[u] if u in news else state[u]["seg"]  # noqa: E731
        chosen = set(sorted(active, key=lambda u: bias[tier(u)] + vrng.random() * 1.6, reverse=True)[:n_mvu])
        prems = [x for x in (cfgv["stories"].get("premieres") or []) if days[0].isoformat() <= x["date"] <= days[-1].isoformat()]

        for u in active:
            prng = rng_for(seed, "user-period", u, name)
            st = state[u]
            s0 = st["seg"]
            reg_day = st.pop("reg_day", None) if u in news else None
            sub_day = st.pop("sub_day", None) if u in news else None
            if u not in news:                               # returning: maybe move up a tier
                if s0 == "anonymous" and prng.random() < ret_reg:
                    reg_day = prng.randint(1, len(days) - 1)
                elif s0 == "registered" and prng.random() < ret_sub:
                    sub_day = prng.randint(3, len(days) - 2)
                    st.update(plan=_pick(prng, L["plan_split"]), renewal_day=sub_day, renewal_month=days[0].month)
            seg_for_sessions = "subscribed" if (s0 == "subscribed" or sub_day) else ("registered" if s0 == "registered" or reg_day else "anonymous")
            vid = u in chosen
            lo, hi = cfgv["simulation"]["sessions_per_month"][seg_for_sessions]
            if not vid:
                lo, hi = 1, max(2, hi // 3)
            hours = 0.0
            if vid:
                hours = _lognormal_hours(prng, float(cfgv["simulation"]["watch_hours_mean"]),
                                         float(cfgv["simulation"]["watch_hours_sigma"]))
                lo = max(lo, min(30, math.ceil(hours / 8)))
                hi = max(hi, min(45, math.ceil(hours / 3)), lo)
            fans = [x["title_id"] for x in prems if vid and prng.random() < float(x.get("fan_share", 0.3))]
            pb = PeriodBrief(period=name, segment_at_start=s0, is_video_user=vid, sessions_range=(lo, hi),
                             target_watch_hours=round(hours, 1), must_register_on_day=reg_day,
                             must_subscribe_on_day=sub_day,
                             plan=st["plan"] if (s0 == "subscribed" or sub_day) else None,
                             premiere_fans=fans, football_fan=prng.random() < 0.2, zapper=prng.random() < 0.15,
                             persona_note=prng.choice(ARCHETYPES))
            if fans and pb.target_watch_hours < 8:
                pb.target_watch_hours = 8.0
                pb.sessions_range = (max(pb.sessions_range[0], 4), max(pb.sessions_range[1], 6))
            # subscription renewals this period (not in the month they convert)
            if s0 == "subscribed" and not sub_day:
                plan = st["plan"] or "monthly"
                pb.plan = plan
                rday = st["renewal_day"] or rng_for(seed, "renewal-day", u).randint(1, 28)
                rmonth = st["renewal_month"] or rng_for(seed, "renewal-month", u).randint(1, 12)
                st["renewal_day"], st["renewal_month"] = rday, rmonth
                if plan == "monthly" or rmonth == days[0].month:
                    if renewal_attempts(cfgv, pb, name, plan, min(rday, len(days)), True, prng):
                        st["churned"] = True
            # state at the end of this period
            st["seg"], churned = end_state(pb)
            st["churned"] = st["churned"] or churned
            st["video"] = vid
            hist.setdefault(u, []).append(days[-1].isoformat())
            in_period.setdefault(u, set()).add(name)
            if u in new_briefs:
                new_briefs[u].periods[name] = pb
                new_briefs[u].renewal_day, new_briefs[u].renewal_month = st["renewal_day"], st["renewal_month"]
            else:
                ext[u][name] = pb

    # incidents in this batch's periods: make sure a few renewals land on the day
    views = list(new_briefs.values()) + [UserBrief(**{**reg.briefs[u].model_dump(), "periods": per})
                                         for u, per in ext.items()]
    for inc in cfgv["stories"].get("payment_incidents") or []:
        d = date.fromisoformat(inc["date"])
        for P in periods:
            if P["start"] <= inc["date"] <= P["end"]:
                _plant_incident_renewals(cfgv, views, P["name"], d.day, 6)
    for v in views:                                         # copy planted changes back
        if v.user_id in ext:
            ext[v.user_id] = dict(v.periods)
    return list(new_briefs.values()), dict(ext)


def _check_periods(reg: Registry, periods: list[dict]) -> None:
    for p in periods:
        old = reg.periods.get(p["name"])
        if old and (str(old["start"]), str(old["end"])) != (p["start"], p["end"]):
            raise SpecError(f"period {p['name']} already exists with dates {old['start']}..{old['end']}")
        if not old:
            for n, o in reg.periods.items():
                if not (p["end"] < str(o["start"]) or p["start"] > str(o["end"])):
                    raise SpecError(f"period {p['name']} overlaps existing period {n}")
    ps = sorted(periods, key=lambda p: p["start"])
    for a, b in zip(ps, ps[1:]):
        if b["start"] <= a["end"]:
            raise SpecError(f"periods {a['name']} and {b['name']} overlap")


def register_batch(cfg: Settings, spec: dict, new: list[UserBrief], ext: dict) -> None:
    d = Registry.batch_dir(cfg, spec["run_id"])
    write_json(d / "spec.json", spec)
    write_json(d / "personas.json", [b.model_dump(mode="json") for b in new])
    write_json(d / "extensions.json", {u: {p: pb.model_dump(mode="json") for p, pb in per.items()}
                                        for u, per in ext.items()})
    ids = Registry.run_ids(cfg)
    if spec["run_id"] not in ids:
        write_json(Registry.index_path(cfg), ids + [spec["run_id"]])
