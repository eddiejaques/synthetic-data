"""Business-rule validator for one LLM journey (one user, one month).

`check_journey` returns a *repaired* copy plus a list of violations:
- repairs are mechanical and safe (sort sessions, add a missing lane for a click, align outcome
  with watch fraction, insert a login where a registered user needs one, ...);
- violations are semantic (wrong gating, invented ids, missing registration, ...). Any violation
  sends the journey back to the model once, with these exact messages appended to the prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import (LANE_SCREENS, Catalog, JClick, JPlay, Journey, JSession, JStep, PeriodBrief,
                     Playable, Title, UserBrief)  # noqa: F401

WALLS = ("registration_wall", "paywall")
RANK = {"anonymous": 0, "registered": 1, "subscribed": 2}


@dataclass
class Ctx:
    brief: UserBrief
    pb: PeriodBrief
    catalog: Catalog
    days: list[date]
    titles: dict[str, Title] = field(default_factory=dict)
    plays: dict[str, Playable] = field(default_factory=dict)
    lanes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.titles = self.catalog.title_map()
        self.plays = self.catalog.playable_map()
        self.lanes = self.catalog.lane_map()


@dataclass
class State:
    """Account state as the journey unfolds."""

    seg: str
    churn_day: Optional[int]

    def can(self, licence: str, day: int) -> bool:
        if licence == "AVOD":
            return True
        if licence == "RVOD":
            return RANK[self.seg] >= 1
        return self.seg == "subscribed" and (self.churn_day is None or day <= self.churn_day)

    def wall_for(self, licence: str) -> str:
        return "registration_wall" if licence == "RVOD" else "paywall"


def watch_seconds(play: JPlay, p: Playable) -> float:
    if p.content_type == "LIVE":
        return float((play.minutes or 0) * 60)
    return float((play.watch or 0.0) * (p.duration_s or 0))


def _day_of_svod_end(pb: PeriodBrief) -> Optional[int]:
    return pb.svod_until_day


def check_journey(j: Journey, ctx: Ctx) -> tuple[Journey, list[str]]:
    """Validate and mechanically repair. Returns (repaired_journey, violations)."""
    v: list[str] = []
    pb, brief = ctx.pb, ctx.brief
    n_days = len(ctx.days)
    dev_labels = {d.label for d in brief.devices}
    j = j.model_copy(deep=True)

    # ---- session-level repairs
    sessions = [s for s in j.sessions if s.steps]
    for s in sessions:
        if not 1 <= s.day <= n_days:
            v.append(f"session on day {s.day}: day must be 1..{n_days}")
        if s.device not in dev_labels:
            v.append(f"session day {s.day} {s.start}: device '{s.device}' not in brief ({sorted(dev_labels)})")
    sessions = [s for s in sessions if 1 <= s.day <= n_days and s.device in dev_labels]
    sessions.sort(key=lambda s: (s.day, s.start))
    if sessions and sessions[0].device != "d1":
        sessions[0].device = "d1"          # primary device opens the month (platform split)
    j.sessions = sessions

    lo, hi = pb.sessions_range
    if not lo <= len(sessions) <= int(hi * 1.5) + 1:
        v.append(f"{len(sessions)} sessions; the brief asks for {lo}-{hi}")

    state = State(seg=pb.segment_at_start, churn_day=_day_of_svod_end(pb))
    registered_n = subscribed_n = 0
    authed_devices: set[str] = set()
    total_watch = 0.0
    plays_n = 0

    for s in sessions:
        _repair_click_targets(s, ctx)

    for si, s in enumerate(sessions, start=1):
        where = f"session {si} (day {s.day} {s.start})"
        # an anonymous user only has one device; a second device belongs to the account, so it
        # appears only once the user is registered (otherwise it could never be stitched)
        if state.seg == "anonymous" and s.device != "d1":
            s.device = "d1"
        # a registered user on a not-yet-authenticated device logs in first
        needs_login = RANK[state.seg] >= 1 and s.device not in authed_devices
        if needs_login and not any(st.auth == "login" for st in s.steps):
            s.steps.insert(0, JStep(screen="login", auth="login"))
        current_title: Optional[str] = None
        prev: Optional[JStep] = None
        for ti, st in enumerate(s.steps, start=1):
            at = f"{where} step {ti} [{st.screen}]"
            # lanes
            if st.screen not in LANE_SCREENS and st.lanes:
                st.lanes = []
            good_lanes = []
            for lid in st.lanes:
                lane = ctx.lanes.get(lid)
                if lane is None:
                    v.append(f"{at}: unknown lane '{lid}'")
                elif st.screen not in lane.screens:
                    v.append(f"{at}: lane '{lid}' is not shown on {st.screen} (only {lane.screens})")
                elif lid not in good_lanes:
                    good_lanes.append(lid)
            st.lanes = good_lanes

            # walls must explain themselves
            if st.screen in WALLS:
                if not st.blocked and prev and prev.click:
                    st.blocked = prev.click.title
                gated = _gate_target(st.blocked, ctx)
                if gated is None:
                    v.append(f"{at}: wall screen needs 'blocked' set to the gated title/content id")
                else:
                    lic, tid = gated
                    if state.can(lic, s.day):
                        v.append(f"{at}: '{st.blocked}' ({lic}) is not gated for a {state.seg} user")
                    elif st.screen != state.wall_for(lic):
                        v.append(f"{at}: {lic} content for a {state.seg} user must show "
                                 f"{state.wall_for(lic)}, not {st.screen}")
                    came_from_click = prev is not None and prev.click is not None and prev.click.title == tid
                    came_from_detail = prev is not None and prev.screen in ("detail", "player") and current_title == tid
                    if not (came_from_click or came_from_detail):
                        v.append(f"{at}: wall for '{st.blocked}' must follow a click on it or its detail page")
            elif st.blocked:
                st.blocked = None

            # what the previous click implies for this screen
            if prev and prev.click:
                t = ctx.titles.get(prev.click.title)
                if t is not None:
                    gated_title = not state.can(t.licence_type, s.day)
                    if gated_title and st.screen == "detail":
                        # repair: the spec routes gated clicks straight to the wall
                        st.screen, st.blocked, st.lanes = state.wall_for(t.licence_type), t.title_id, []
                    if gated_title and st.screen not in WALLS:
                        v.append(f"{at}: click on gated '{t.title_id}' ({t.licence_type}) must lead to "
                                 f"{state.wall_for(t.licence_type)}")
                    elif not gated_title:
                        want = "player" if t.content_type == "LIVE" else "detail"
                        if st.screen != want:
                            v.append(f"{at}: click on '{t.title_id}' must open {want}, not {st.screen}")
                current_title = prev.click.title

            # click
            if st.click:
                _check_click(st, at, ctx, v)

            # play
            if st.play:
                if not pb.is_video_user:
                    v.append(f"{at}: this user is NOT a video user — no playback at all")
                p = ctx.plays.get(st.play.content)
                if st.screen != "player":
                    v.append(f"{at}: playback only happens on the player screen")
                if p is None:
                    v.append(f"{at}: unknown content_id '{st.play.content}'")
                else:
                    if not state.can(p.licence_type, s.day):
                        v.append(f"{at}: '{p.content_id}' is {p.licence_type}; a {state.seg} user "
                                 f"cannot play it on day {s.day} (show {state.wall_for(p.licence_type)} instead)")
                    if p.available_from and ctx.days[s.day - 1] < p.available_from:
                        v.append(f"{at}: '{p.content_id}' is not released until {p.available_from}")
                    ok_prev = prev is not None and (
                        prev.screen in ("detail", "player", "live_tv")
                        or (prev.click is not None and prev.click.title == p.title_id))
                    if not ok_prev:
                        v.append(f"{at}: playback must follow detail, live_tv, player or a click on the title")
                    if prev is not None and prev.screen == "detail" and current_title and current_title != p.title_id:
                        v.append(f"{at}: detail page was for '{current_title}' but '{p.content_id}' was played")
                    _repair_play(st.play, p, at, v)
                    total_watch += watch_seconds(st.play, p)
                    plays_n += 1
                    current_title = p.title_id

            # auth
            if st.auth == "login":
                if st.screen != "login":
                    st.screen = "login"
                if RANK[state.seg] < 1:
                    v.append(f"{at}: anonymous user cannot log in")
                authed_devices.add(s.device)
            elif st.auth == "register":
                if st.screen != "registration":
                    v.append(f"{at}: registration happens on the registration screen")
                if pb.must_register_on_day != s.day or registered_n:
                    v.append(f"{at}: registration only allowed once, on day {pb.must_register_on_day}")
                if state.seg != "anonymous":
                    v.append(f"{at}: user is already registered")
                registered_n += 1
                state.seg = "registered"
                authed_devices.add(s.device)
            elif st.auth == "register_abandoned":
                if state.seg != "anonymous":
                    v.append(f"{at}: only anonymous users abandon a sign-up")
                if st.screen not in ("registration", "registration_wall"):
                    v.append(f"{at}: abandoned sign-up belongs on registration or registration_wall")

            # billing
            if st.billing in ("subscribe_monthly", "subscribe_annual"):
                if st.screen != "checkout":
                    v.append(f"{at}: subscription happens on the checkout screen")
                if pb.must_subscribe_on_day != s.day or subscribed_n:
                    v.append(f"{at}: subscription only allowed once, on day {pb.must_subscribe_on_day}")
                if state.seg != "registered":
                    v.append(f"{at}: only a registered (not anonymous, not subscribed) user can subscribe")
                want = f"subscribe_{pb.plan or 'monthly'}"
                if st.billing != want:
                    st.billing = want                       # plan is Python's call, not the model's
                subscribed_n += 1
                if state.seg == "registered":
                    state.seg = "subscribed"
            elif st.billing == "checkout_abandoned":
                if state.seg != "registered":
                    v.append(f"{at}: only registered non-subscribers can abandon checkout")
            if st.screen == "checkout" and not st.billing:
                st.billing = "checkout_abandoned" if state.seg == "registered" else None
                if state.seg != "registered":
                    v.append(f"{at}: {state.seg} user cannot reach checkout")
            prev = st

        # a session must not end on a click (the click has to land somewhere)
        if s.steps and s.steps[-1].click:
            v.append(f"{where}: last step is a click with no destination screen")

    if pb.must_register_on_day and registered_n != 1:
        v.append(f"registration is REQUIRED exactly once, on day {pb.must_register_on_day}")
    if pb.must_subscribe_on_day and subscribed_n != 1:
        v.append(f"subscription ({pb.plan}) is REQUIRED exactly once, on day {pb.must_subscribe_on_day}")
    if pb.is_video_user and plays_n == 0:
        v.append("this is a video user: include playback")
    if pb.is_video_user and pb.target_watch_hours > 0:
        hrs = total_watch / 3600
        if not 0.3 * pb.target_watch_hours <= hrs <= 3.0 * pb.target_watch_hours:
            v.append(f"total watch time {hrs:.1f} h is far from the {pb.target_watch_hours} h target")
    return j, v


def _repair_click_targets(s: JSession, ctx: Ctx) -> None:
    """Structural repairs around clicks (gating is checked later, with account state):
    - a VOD click followed straight by the player gets the missing detail screen;
    - a click whose next screen cannot be its destination (or that ends the session) is dropped,
      i.e. the tile was seen but not clicked."""
    out: list[JStep] = []
    steps = s.steps
    for i, st in enumerate(steps):
        out.append(st)
        if not st.click:
            continue
        t = ctx.titles.get(st.click.title)
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if t is None or nxt is None:
            if nxt is None:
                st.click = None
            continue
        if t.content_type == "LIVE":
            if nxt.screen not in ("player", "detail") + WALLS:
                st.click = None
        elif nxt.screen == "player":
            out.append(JStep(screen="detail"))
        elif nxt.screen not in ("detail",) + WALLS:
            st.click = None
    s.steps = out


def _gate_target(blocked: Optional[str], ctx: Ctx) -> Optional[tuple[str, str]]:
    """(licence, title_id) for a blocked title or content id."""
    if not blocked:
        return None
    if blocked in ctx.plays:
        p = ctx.plays[blocked]
        return p.licence_type, p.title_id
    if blocked in ctx.titles:
        t = ctx.titles[blocked]
        return t.licence_type, t.title_id
    return None


def _check_click(st: JStep, at: str, ctx: Ctx, v: list[str]) -> None:
    c: JClick = st.click  # type: ignore[assignment]
    lane = ctx.lanes.get(c.lane)
    if lane is None:
        v.append(f"{at}: click in unknown lane '{c.lane}'")
        return
    if c.title not in lane.title_ids:
        v.append(f"{at}: '{c.title}' is not in lane '{c.lane}' (pick one of its title_ids)")
        return
    if c.lane not in st.lanes:
        if st.screen in lane.screens:
            st.lanes.append(c.lane)                 # repair: the lane was obviously seen
        else:
            v.append(f"{at}: lane '{c.lane}' is not shown on {st.screen}")


def _repair_play(play: JPlay, p: Playable, at: str, v: list[str]) -> None:
    if p.content_type == "LIVE":
        if play.minutes is None:
            if play.watch is not None:
                play.minutes = max(1, int(round(play.watch * 60)))
            else:
                v.append(f"{at}: LIVE playback needs 'minutes'")
                return
        play.minutes = max(1, min(600, int(play.minutes)))
        play.watch = None
        play.outcome = "abandoned"                 # LIVE never "completes"
        return
    if play.watch is None:
        if play.minutes is not None and p.duration_s:
            play.watch = play.minutes * 60 / p.duration_s
        else:
            v.append(f"{at}: VOD playback needs 'watch' (fraction 0.01-1.0)")
            return
    play.minutes = None
    play.watch = round(max(0.01, min(1.0, float(play.watch))), 3)
    if play.outcome == "completed" and play.watch < 0.9:
        play.watch = max(play.watch, 0.92)
    if play.outcome == "abandoned" and play.watch >= 0.9:
        play.outcome = "completed"
