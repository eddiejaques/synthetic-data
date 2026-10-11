"""Stage 3 — JOURNEYS. One Gemini call per user per active month; Python fallback when it fails.

The cache (`cache/journeys/<user_id>_<period>.json`) is the reproducibility mechanism: a rerun never
re-calls for a cached user-month unless `--refresh` names it. The cache stores the model's raw
journey; repairs are re-applied deterministically at expansion time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .config import Settings, read_json, rng_for, write_json
from .gemini_client import BudgetExceeded, GeminiClient, JobFailed, parse_json
from .keypool import AllKeysExhausted
from .models import Catalog, JClick, JPlay, Journey, JSession, JStep, PeriodBrief, UserBrief
from .validate_journey import RANK, Ctx, State, check_journey

log = logging.getLogger(__name__)

WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

SYSTEM = (
    "You simulate one real person's month of using Joyn, a German streaming app (Live-TV, "
    "Mediathek, Joyn PLUS+). Write believable, varied human behaviour: binges, one-off visits, "
    "weekend sports, abandoned sign-ups, zapping through Live-TV, browsing without watching. "
    "Use ONLY ids given in the prompt. Output JSON matching the schema, nothing else."
)


# ----------------------------------------------------------------------------- prompt
def hourly_weights(cfg: Settings, d: date) -> list[float]:
    w = list(map(float, cfg["diurnal"]["weekday"]))
    if d.weekday() >= 5:
        up = float(cfg["diurnal"]["weekend_daytime_uplift"])
        w = [x * up if 10 <= h <= 17 else x for h, x in enumerate(w)]
    if d.weekday() == 5:                          # Saturday Bundesliga kickoffs
        for k in cfg["diurnal"]["bundesliga_kickoffs"]:
            h = int(k.split(":")[0])
            w[h] *= 1.5
            w[(h + 1) % 24] *= 1.3
    return w


def _availability(pb: PeriodBrief, brief: UserBrief, licence: str) -> Optional[str]:
    """Human text for when a licence is playable this month, or None if never."""
    seg0 = pb.segment_at_start
    if licence == "AVOD":
        return "always"
    if licence == "RVOD":
        if RANK[seg0] >= 1:
            return "always"
        if pb.must_register_on_day:
            return f"from day {pb.must_register_on_day} (after registering)"
        return None
    if seg0 == "subscribed":
        return f"until day {pb.svod_until_day}" if pb.svod_until_day else "always"
    if pb.must_subscribe_on_day:
        return f"from day {pb.must_subscribe_on_day} (after subscribing)"
    return None


def build_prompt(cfg: Settings, brief: UserBrief, pb: PeriodBrief, cat: Catalog) -> str:
    """Compact prompt: brief, the playable catalog subset, lanes, diurnal weights, rules."""
    days = cfg.period_days(pb.period)
    first = days[0]
    lines: list[str] = []
    a = lines.append
    a(f"MONTH: {first:%B %Y}, days 1-{len(days)}; day 1 is {WEEKDAYS_DE[first.weekday()]}. Times are local (Europe/Berlin).")
    a(f"PERSON: {pb.persona_note}.")
    a(f"Account at start of month: {pb.segment_at_start}. Video user: {'YES' if pb.is_video_user else 'NO - never plays video, only browses'}.")
    devs = ", ".join(f"{d.label}={d.platform}/{d.device_type}" for d in brief.devices)
    a(f"Devices: {devs} (d1 is the main device; open the month on d1).")
    a(f"Sessions this month: {pb.sessions_range[0]}-{pb.sessions_range[1]}.")
    if pb.is_video_user:
        a(f"Total watch time this month: about {pb.target_watch_hours} hours (sum of VOD duration x watch + LIVE minutes).")
    if pb.must_register_on_day:
        a(f"REQUIRED: register exactly once on day {pb.must_register_on_day} (step screen=registration, auth=register), "
          "typically after hitting a registration_wall.")
    if pb.must_subscribe_on_day:
        a(f"REQUIRED: subscribe exactly once on day {pb.must_subscribe_on_day} (screen=checkout, billing=subscribe_{pb.plan}), "
          "typically after a paywall.")
    if pb.svod_until_day:
        a(f"Subscription lapses after day {pb.svod_until_day}: SVOD after that shows paywall.")
    if pb.segment_at_start == "anonymous" and not pb.must_register_on_day:
        a("Never logs in, registers or pays. May abandon a sign-up (auth=register_abandoned).")
    if pb.segment_at_start == "registered" and not pb.must_subscribe_on_day:
        a("Does not subscribe; may abandon checkout (screen=checkout, billing=checkout_abandoned).")
    if pb.voice_fan:
        prem = cfg["stories"]["voice_premiere"][pb.period]
        a(f"Big fan of The Voice of Germany: watches new episodes on/just after release (season premiere {prem}).")
    for tid in pb.premiere_fans:
        t = cat.title_map().get(tid)
        drops = sorted(p.available_from for p in cat.playables
                       if p.title_id == tid and p.available_from and days[0] <= p.available_from <= days[-1])
        if t and drops:
            a(f"Big fan of {t.title}: watches new episodes on/just after release (new season from {drops[0]}).")
    if pb.football_fan:
        a("Football fan: Saturday sessions around 15:30 and 18:30 kickoffs (ran Football live + highlights).")
    if pb.zapper:
        a("Zaps through Live-TV: several short LIVE plays in a row on live_tv.")

    w_wd = hourly_weights(cfg, next(d for d in days if d.weekday() < 5))
    w_sa = hourly_weights(cfg, next(d for d in days if d.weekday() == 5))
    a("Session start-time weights by hour 0-23 (place sessions accordingly):")
    a("  weekday: " + ",".join(f"{x:g}" for x in w_wd))
    a("  saturday: " + ",".join(f"{x:.3g}" for x in w_sa) + " (sunday same without kickoffs)")

    a("\nTITLES (title_id | title | genre | type | licence | playable for this user):")
    for t in cat.titles:
        avail = _availability(pb, brief, t.licence_type)
        a(f"  {t.title_id} | {t.title} | {t.genre} | {t.content_type} | {t.licence_type} | {avail or 'NO -> wall'}")
    if pb.is_video_user:
        a("\nPLAYABLE CONTENT for this user (content ids; build episode ids exactly as shown):")
        plays = cat.playable_map()
        for t in cat.titles:
            ps = [plays[i] for i in t.playable_ids]
            ok = [p for p in ps if _availability(pb, brief, p.licence_type)]
            if not ok:
                continue
            if t.content_type == "LIVE" or len(ok) == 1 and ok[0].season is None:
                p = ok[0]
                kind = "LIVE, use minutes" if p.content_type == "LIVE" else f"{(p.duration_s or 0) // 60} min"
                a(f"  {p.content_id} ({kind}; {_availability(pb, brief, p.licence_type)})")
                continue
            by_season: dict[int, list] = {}
            for p in ok:
                by_season.setdefault(p.season or 1, []).append(p)
            parts = []
            for s, eps in sorted(by_season.items()):
                lic = eps[0].licence_type
                drops = [p for p in eps if p.available_from]
                rng_txt = f"{eps[0].content_id}..{eps[-1].content_id}"
                extra = ""
                if drops:
                    extra = " released: " + ", ".join(f"e{p.episode:02d} {p.available_from:%d.%m.%Y}" for p in drops)
                parts.append(f"{rng_txt} [{lic}, ~{(eps[0].duration_s or 0) // 60} min]{extra}")
            a(f"  {t.title_id}: " + "; ".join(parts))
    a("\nLANES (lane_id | type | screens | title_ids):")
    for l in cat.lanes:
        a(f"  {l.lane_id} | {l.lane_type} | {','.join(l.screens)} | {' '.join(l.title_ids)}")
    a(_example(cat, pb.is_video_user))
    a("""
RULES:
- Each session = ordered steps; each step is one screen. Lanes only on home/live_tv/category/detail/search and only lanes listed for that screen.
- A click names a lane from the same step and a title_id inside that lane. The next step is detail (VOD) or player (LIVE), or the wall if the title is not playable.
- Gated content: RVOD for anonymous -> registration_wall; SVOD without subscription -> paywall. Wall steps set blocked=<title or content id>.
- Non-video users never have a play field anywhere.
- Playback only on screen=player, after detail, live_tv, another player step or a click on that title. VOD: watch fraction 0.01-1.0; LIVE: minutes. outcome=completed only if watch >= 0.9.
- Never invent ids. Do not write seconds, ids or device details - only the fields in the schema.
- Vary it: different days, quiet days, binge nights, quick checks, browsing that ends without playing.""")
    return "\n".join(lines)


def _example(cat: Catalog, video: bool) -> str:
    """A worked session built from real ids, so the model sees exactly where clicks and plays go."""
    titles = cat.title_map()
    home = [l for l in cat.lanes if "home" in l.screens]
    lane = next((l for l in home if any(titles[t].licence_type == "AVOD" and titles[t].asset_type == "series"
                                        for t in l.title_ids)), home[0])
    free = next((t for t in lane.title_ids if titles[t].licence_type == "AVOD" and titles[t].asset_type == "series"),
                lane.title_ids[0])
    gated_lane = next((l for l in home if any(titles[t].licence_type == "SVOD" for t in l.title_ids)), lane)
    gated = next((t for t in gated_lane.title_ids if titles[t].licence_type == "SVOD"), gated_lane.title_ids[-1])
    eps = titles[free].playable_ids[:2]
    steps = [{"screen": "home", "lanes": sorted({lane.lane_id, gated_lane.lane_id}),
              "click": {"lane": lane.lane_id, "title": free}},
             {"screen": "detail"}]
    if video and len(eps) == 2:
        steps += [{"screen": "player", "play": {"content": eps[0], "watch": 1.0, "outcome": "completed"}},
                  {"screen": "player", "play": {"content": eps[1], "watch": 0.35, "outcome": "abandoned"}}]
    steps += [{"screen": "home", "lanes": [gated_lane.lane_id], "click": {"lane": gated_lane.lane_id, "title": gated}},
              {"screen": "paywall", "blocked": gated}]
    ex = json.dumps({"day": 3, "start": "20:15", "device": "d1", "steps": steps}, ensure_ascii=False)
    return ("\nEXAMPLE SESSION (shape only - a click sits on the screen WHERE it happens, the next step is "
            "the screen it LEADS to; detail pages show no lanes unless listed for detail; the paywall step "
            "applies when the user is not subscribed):\n" + ex)


# ----------------------------------------------------------------------------- fallback
def fallback_journey(cfg: Settings, brief: UserBrief, pb: PeriodBrief, cat: Catalog) -> Journey:
    """Simple seeded Markov journey that satisfies every rule in validate_journey by construction."""
    rng = rng_for(cfg.seed, "fallback", brief.user_id, pb.period)
    days = cfg.period_days(pb.period)
    titles = cat.title_map()
    plays = cat.playable_map()
    lo, hi = pb.sessions_range
    n = rng.randint(lo, hi)
    required = [d for d in (pb.must_register_on_day, pb.must_subscribe_on_day) if d]
    # Fans come back on each release day of a premiering season (story 2 and spec premieres)
    fan_titles = (["joyn_the_voice"] if pb.voice_fan else []) + list(pb.premiere_fans)
    voice_eps: dict = {}
    for p in cat.playables:
        if p.title_id in fan_titles and p.available_from and days[0] <= p.available_from <= days[-1]:
            voice_eps.setdefault(p.available_from, p)
    voice_days = {(d - days[0]).days + 1: ep for d, ep in voice_eps.items()}
    required += list(voice_days)
    n = max(n, len(set(required)))
    wts = [1.6 if d.weekday() >= 5 else 1.0 for d in days]
    chosen = sorted(set(required)) + rng.choices(range(1, len(days) + 1), weights=wts, k=max(0, n - len(set(required))))
    chosen.sort()
    state = State(seg=pb.segment_at_start, churn_day=pb.svod_until_day)
    devices = [d.label for d in brief.devices]
    budget_s = pb.target_watch_hours * 3600
    video_sessions = max(1, len(chosen))
    sessions: list[JSession] = []
    used_starts: set[tuple[int, str]] = set()

    def lanes_for(screen: str) -> list:
        return [l for l in cat.lanes if screen in l.screens]

    for idx, day in enumerate(chosen):
        hw = hourly_weights(cfg, days[day - 1])
        hour = rng.choices(range(24), weights=hw)[0]
        start = f"{hour:02d}:{rng.randrange(0, 60):02d}"
        while (day, start) in used_starts:
            start = f"{hour:02d}:{rng.randrange(0, 60):02d}"
        # same-day sessions must start in list order, or the validator replays state out of order
        prev_same = [x.start for x in sessions if x.day == day]
        if prev_same and start <= max(prev_same):
            h, m = map(int, max(prev_same).split(":"))
            t = min(23 * 60 + 59, h * 60 + m + rng.randint(20, 90))
            start = f"{t // 60:02d}:{t % 60:02d}"
        used_starts.add((day, start))
        dev = "d1" if idx == 0 or len(devices) == 1 or rng.random() < 0.8 else devices[1]
        steps: list[JStep] = []
        if RANK[state.seg] >= 1 and idx == 0:
            steps.append(JStep(screen="login", auth="login"))
        entry = "live_tv" if (pb.zapper or pb.football_fan) and rng.random() < 0.4 else "home"
        home_lanes = lanes_for(entry)
        shown = rng.sample(home_lanes, k=min(len(home_lanes), rng.randint(2, 4)))
        steps.append(JStep(screen=entry, lanes=[l.lane_id for l in shown]))

        must_reg = pb.must_register_on_day == day and state.seg == "anonymous"
        must_sub = pb.must_subscribe_on_day == day and state.seg in ("registered", "anonymous")
        if must_reg:
            _wall_then(steps, shown, titles, state, day, "RVOD", rng, lanes_for)
            steps.append(JStep(screen="registration", auth="register"))
            state.seg = "registered"
            steps.append(JStep(screen=entry, lanes=[l.lane_id for l in shown]))
        if must_sub and state.seg == "registered":
            _wall_then(steps, shown, titles, state, day, "SVOD", rng, lanes_for)
            steps.append(JStep(screen="checkout", billing=f"subscribe_{pb.plan or 'monthly'}"))
            state.seg = "subscribed"
            steps.append(JStep(screen=entry, lanes=[l.lane_id for l in shown]))
        elif not must_reg and rng.random() < 0.12:
            lic = "RVOD" if state.seg == "anonymous" else "SVOD"
            if not state.can(lic, day) and _wall_then(steps, shown, titles, state, day, lic, rng, lanes_for):
                if state.seg == "anonymous" and rng.random() < 0.4:
                    steps.append(JStep(screen="registration", auth="register_abandoned"))
                elif state.seg == "registered" and rng.random() < 0.3:
                    steps.append(JStep(screen="checkout", billing="checkout_abandoned"))
                steps.append(JStep(screen=entry, lanes=[l.lane_id for l in shown]))

        if day in voice_days and _voice_block(steps, cat, voice_days.pop(day), state, day, rng):
            budget_s -= voice_eps[days[day - 1]].duration_s or 0
            steps.append(JStep(screen=entry, lanes=[l.lane_id for l in shown]))
        if pb.is_video_user and budget_s > 0:
            share = budget_s / max(1, video_sessions - idx) * rng.uniform(0.6, 1.4)
            share = min(share, budget_s)
            used = 0.0
            for block in range(6):                      # several titles per session if budget allows
                if block:
                    steps.append(JStep(screen=entry, lanes=[l.lane_id for l in shown]))
                used += _watch_block(steps, shown, cat, titles, plays, state, day, days, share - used, rng, lanes_for)
                if used >= 0.8 * share:
                    break
            budget_s -= used
        else:
            # browse: open a detail page and leave
            lane = rng.choice(shown)
            ok = [t for t in lane.title_ids if state.can(titles[t].licence_type, day) and titles[t].content_type == "VOD"]
            if ok:
                steps[-1].click = JClick(lane=lane.lane_id, title=rng.choice(ok))
                steps.append(JStep(screen="detail"))
        sessions.append(JSession(day=day, start=start, device=dev, steps=steps))
    if pb.is_video_user and not any(st.play for s in sessions for st in s.steps):
        s = sessions[-1]
        home = lanes_for("home")
        s.steps.append(JStep(screen="home", lanes=[l.lane_id for l in home]))
        _watch_block(s.steps, home, cat, titles, plays, state, s.day, days, 1200, rng, lanes_for)
    return Journey(sessions=sessions)


def _voice_block(steps, cat, ep, state, day, rng) -> bool:
    """Open a lane that carries the premiering title, click it, watch the new episode in full."""
    for lane in cat.lanes:
        if ep.title_id in lane.title_ids:
            scr = steps[-1].screen if steps[-1].screen in lane.screens else lane.screens[0]
            if scr != steps[-1].screen:
                steps.append(JStep(screen=scr, lanes=[lane.lane_id]))
            elif lane.lane_id not in steps[-1].lanes:
                steps[-1].lanes.append(lane.lane_id)
            steps[-1].click = JClick(lane=lane.lane_id, title=ep.title_id)
            steps.append(JStep(screen="detail"))
            steps.append(JStep(screen="player", play=JPlay(content=ep.content_id, watch=1.0, outcome="completed")))
            return True
    return False


def _wall_then(steps, shown, titles, state, day, lic, rng, lanes_for) -> bool:
    """Click a title gated at `lic` from a shown lane and land on its wall."""
    for lane in rng.sample(shown, len(shown)):
        cands = [t for t in lane.title_ids if titles[t].licence_type == lic and not state.can(lic, day)]
        if cands:
            tid = rng.choice(cands)
            steps[-1].click = JClick(lane=lane.lane_id, title=tid)
            steps.append(JStep(screen=state.wall_for(lic), blocked=tid))
            return True
    # no shown lane has one: open a lane that does
    for lane in [l for l in lanes_for(steps[-1].screen)]:
        cands = [t for t in lane.title_ids if titles[t].licence_type == lic]
        if cands and not state.can(lic, day):
            if lane.lane_id not in steps[-1].lanes:
                steps[-1].lanes.append(lane.lane_id)
            tid = cands[0]
            steps[-1].click = JClick(lane=lane.lane_id, title=tid)
            steps.append(JStep(screen=state.wall_for(lic), blocked=tid))
            return True
    return False


def _watch_block(steps, shown, cat, titles, plays, state, day, days, budget_s, rng, lanes_for) -> float:
    """Click a playable title and watch up to budget_s seconds of it. Returns seconds used."""
    d = days[day - 1]
    options = []
    for lane in shown:
        for tid in lane.title_ids:
            t = titles[tid]
            if not state.can(t.licence_type, day):
                continue
            ok = [plays[p] for p in t.playable_ids if state.can(plays[p].licence_type, day)
                  and (plays[p].available_from is None or plays[p].available_from <= d)]
            if ok:
                options.append((lane, t, ok))
    if not options:
        return 0.0
    lane, t, ok = rng.choice(options)
    steps[-1].click = JClick(lane=lane.lane_id, title=t.title_id)
    used = 0.0
    if t.content_type == "LIVE":
        mins = max(5, int(min(budget_s / 60, rng.uniform(30, 240))))
        steps.append(JStep(screen="player", play=JPlay(content=ok[0].content_id, minutes=mins, outcome="abandoned")))
        return mins * 60.0
    steps.append(JStep(screen="detail"))
    start = rng.randrange(len(ok))
    for p in ok[start:start + 12]:
        frac = 1.0 if rng.random() < 0.6 else round(rng.uniform(0.05, 0.85), 2)
        steps.append(JStep(screen="player", play=JPlay(content=p.content_id, watch=frac,
                                                       outcome="completed" if frac >= 0.9 else "abandoned")))
        used += frac * (p.duration_s or 0)
        if used >= budget_s or (frac < 0.9 and rng.random() < 0.5):
            break
    return used


# ----------------------------------------------------------------------------- orchestration
HASH_FIELDS = ("segment_at_start", "is_video_user", "sessions_range", "target_watch_hours", "must_register_on_day",
               "must_subscribe_on_day", "plan", "svod_until_day", "voice_fan", "premiere_fans", "football_fan",
               "zapper", "persona_note")


def brief_hash(brief: UserBrief, period: str) -> str:
    """Fingerprint of everything the journey prompt depends on for one user-month.

    Stored in each cache record; a different brief means the cached journey is stale."""
    pb = brief.periods[period].model_dump(mode="json")
    payload = {"period": period, "brief": {k: pb.get(k) for k in HASH_FIELDS},
               "devices": [(d.label, d.platform, d.device_type) for d in brief.devices]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def is_fresh(rec: dict, brief: UserBrief, period: str) -> bool:
    """Records from before brief hashing (no stamp) are trusted; stamped ones must match."""
    return "brief_hash" not in rec or rec["brief_hash"] == brief_hash(brief, period)


def cache_path(cfg: Settings, user_id: str, period: str) -> Path:
    return cfg.cache / "journeys" / f"{user_id}_{period}.json"


def make_ctx(cfg: Settings, brief: UserBrief, period: str, cat: Catalog) -> Ctx:
    return Ctx(brief=brief, pb=brief.periods[period], catalog=cat, days=cfg.period_days(period))


async def _one_job(cfg: Settings, client: GeminiClient, brief: UserBrief, period: str, cat: Catalog,
                   failed_log: Path) -> dict:
    pb = brief.periods[period]
    ctx = make_ctx(cfg, brief, period, cat)
    prompt = build_prompt(cfg, brief, pb, cat)
    temp = float(cfg["gemini"]["temperature_journeys"])
    violations: list[str] = []
    attempts = 0
    for attempt in range(2):
        attempts += 1
        p = prompt
        if violations:
            p += "\n\nYOUR PREVIOUS ANSWER BROKE THESE RULES - fix every one:\n- " + "\n- ".join(violations[:25])
        try:
            res = await client.generate(p, Journey, temp, SYSTEM)
        except JobFailed as e:
            violations = [f"transport failure: {e}"]
            break
        try:
            j = Journey.model_validate(parse_json(res.text))
        except Exception as e:  # noqa: BLE001 — schema/JSON errors are retried like rule breaks
            violations = [f"schema error: {str(e)[:300]}"]
            continue
        _, violations = check_journey(j, ctx)
        if not violations:
            return {"user_id": brief.user_id, "period": period, "source": "llm", "attempts": attempts,
                    "key": res.key_label, "journey": j.model_dump()}
    with open(failed_log, "a") as f:
        f.write(json.dumps({"user_id": brief.user_id, "period": period, "attempts": attempts,
                            "violations": violations[:40], "at": datetime.now().isoformat()}) + "\n")
    fj = fallback_journey(cfg, brief, pb, cat)
    return {"user_id": brief.user_id, "period": period, "source": "fallback", "attempts": attempts,
            "violations": violations[:40], "journey": fj.model_dump()}


async def run_journeys(cfg: Settings, cat: Catalog, briefs: list[UserBrief], client: Optional[GeminiClient],
                       refresh: Optional[set[str]] = None, offline: bool = False,
                       limit: Optional[int] = None) -> dict:
    """Generate every missing user-month. Raises AllKeysExhausted / BudgetExceeded after checkpointing."""
    refresh = refresh or set()
    (cfg.cache / "journeys").mkdir(parents=True, exist_ok=True)
    failed_log = cfg.logs / "failed_journeys.jsonl"
    todo = []
    for b in briefs:
        for p in b.periods:
            cp = cache_path(cfg, b.user_id, p)
            if cp.exists() and b.user_id not in refresh:
                if is_fresh(read_json(cp), b, p):
                    continue
                log.warning("journey %s_%s is stale (brief changed); regenerating", b.user_id, p)
            todo.append((b, p))
    cached = sum(len(b.periods) for b in briefs) - len(todo)
    if limit is not None:
        todo = todo[:limit]                     # smoke tests: only the first N missing user-months
    log.info("journeys: %d to generate, %d cached", len(todo), cached)
    if offline or client is None:
        for b, p in todo:
            fj = fallback_journey(cfg, b, b.periods[p], cat)
            write_json(cache_path(cfg, b.user_id, p), {"user_id": b.user_id, "period": p, "source": "fallback",
                                                       "attempts": 0, "brief_hash": brief_hash(b, p),
                                                       "journey": fj.model_dump()})
        return {"generated": len(todo), "remaining": 0}

    queue: asyncio.Queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    done = 0
    stop: list[BaseException] = []

    async def worker() -> None:
        nonlocal done
        while not queue.empty() and not stop:
            b, p = queue.get_nowait()
            try:
                rec = await _one_job(cfg, client, b, p, cat, failed_log)
            except (AllKeysExhausted, BudgetExceeded) as e:
                stop.append(e)
                return
            rec["brief_hash"] = brief_hash(b, p)
            write_json(cache_path(cfg, b.user_id, p), rec)
            done += 1
            if done % 25 == 0:
                log.info("journeys: %d/%d done", done, len(todo))

    n_workers = int(cfg["gemini"]["concurrency"]) * 2
    await asyncio.gather(*(worker() for _ in range(n_workers)))
    client.write_usage()
    if stop:
        raise stop[0]
    return {"generated": done, "remaining": 0}


def load_journeys(cfg: Settings, briefs: list[UserBrief]) -> dict[tuple[str, str], dict]:
    out = {}
    missing, stale = [], []
    for b in briefs:
        for p in b.periods:
            cp = cache_path(cfg, b.user_id, p)
            if not cp.exists():
                missing.append(f"{b.user_id}_{p}")
                continue
            rec = read_json(cp)
            if not is_fresh(rec, b, p):
                stale.append(f"{b.user_id}_{p}")
            out[(b.user_id, p)] = rec
    if missing or stale:
        raise SystemExit(f"{len(missing)} journeys missing and {len(stale)} stale (e.g. {(missing + stale)[:3]}); "
                         "run `main.py journeys --resume` (or `main.py generate --spec ...` for a batch)")
    return out
