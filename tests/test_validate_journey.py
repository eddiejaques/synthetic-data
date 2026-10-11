"""Journey validator on hand-written good and bad journeys."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.models import Catalog, Device, Journey, Lane, PeriodBrief, Playable, Title, UserBrief
from src.validate_journey import Ctx, check_journey

DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]


def _cat() -> Catalog:
    eps = [Playable(content_id=f"joyn_free_s01e0{i}", title_id="joyn_free", title="Free", season=1, episode=i,
                    content_type="VOD", genre="Comedy", licence_type="AVOD", duration_s=1800) for i in (1, 2)]
    plus = [Playable(content_id="joyn_plus_s01e01", title_id="joyn_plus", title="Plus", season=1, episode=1,
                     content_type="VOD", genre="Drama", licence_type="SVOD", duration_s=2600)]
    reg = [Playable(content_id="joyn_reg", title_id="joyn_reg", title="Reg", content_type="VOD", genre="Crime",
                    licence_type="RVOD", duration_s=6000)]
    live = [Playable(content_id="joyn_live", title_id="joyn_live", title="Live", content_type="LIVE",
                     genre="News", licence_type="AVOD")]
    new = [Playable(content_id="joyn_new_s01e01", title_id="joyn_new", title="New", season=1, episode=1,
                    content_type="VOD", genre="Reality TV", licence_type="AVOD", duration_s=5000,
                    available_from=date(2026, 9, 17))]
    T = lambda tid, ct, at, lic, ps: Title(title_id=tid, title=tid, content_type=ct, asset_type=at, genre="x",  # noqa: E731
                                           licence_type=lic, playable_ids=[p.content_id for p in ps])
    return Catalog(
        titles=[T("joyn_free", "VOD", "series", "AVOD", eps), T("joyn_plus", "VOD", "series", "SVOD", plus),
                T("joyn_reg", "VOD", "movie", "RVOD", reg), T("joyn_live", "LIVE", "series", "AVOD", live),
                T("joyn_new", "VOD", "series", "AVOD", new)],
        playables=eps + plus + reg + live + new,
        lanes=[Lane(lane_id="lane_hero", lane_name="Hero", lane_type="hero_lane", screens=["home"],
                    title_ids=["joyn_free", "joyn_plus", "joyn_reg", "joyn_live", "joyn_new"]),
               Lane(lane_id="lane_more", lane_name="Mehr", lane_type="standard_lane", screens=["detail"],
                    title_ids=["joyn_free", "joyn_plus"])])


def _ctx(seg="anonymous", video=True, hours=1.0, **pb) -> Ctx:
    dev = Device(label="d1", device_id="dev1", anon_id="anon_1", platform="ott", device_type="tv", brand="LG",
                 model="OLED55", os="webOS", os_version="24", app_version="6.12.0", viewport_w=1920, viewport_h=1080)
    period = PeriodBrief(period="CURRENT", segment_at_start=seg, is_video_user=video, sessions_range=(1, 5),
                         target_watch_hours=hours, **pb)
    brief = UserBrief(user_id="u1", segment=seg, primary_platform="ott", devices=[dev], periods={"CURRENT": period})
    return Ctx(brief=brief, pb=period, catalog=_cat(), days=DAYS)


def _j(*sessions) -> Journey:
    return Journey.model_validate({"sessions": [{"day": d, "start": "20:00", "device": "d1", "steps": st}
                                                for d, st in sessions]})


CLICK_FREE = {"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_free"}}


def play(cid, watch=1.0, outcome="completed", minutes=None):
    return {"screen": "player", "play": {"content": cid, "watch": watch, "minutes": minutes, "outcome": outcome}}


def test_good_journey_passes():
    j = _j((2, [CLICK_FREE, {"screen": "detail"}, play("joyn_free_s01e01"), play("joyn_free_s01e02", 0.5, "abandoned")]))
    _, v = check_journey(j, _ctx(hours=0.9))
    assert v == []


def test_anonymous_cannot_play_svod_or_log_in():
    j = _j((2, [{"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_free"}},
                {"screen": "detail"}, play("joyn_plus_s01e01"), {"screen": "login", "auth": "login"}]))
    _, v = check_journey(j, _ctx())
    assert any("is SVOD; a anonymous user cannot play it" in x for x in v)
    assert any("anonymous user cannot log in" in x for x in v)


def test_gated_click_must_lead_to_correct_wall():
    j = _j((2, [{"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_reg"}},
                {"screen": "paywall", "blocked": "joyn_reg"}, CLICK_FREE, {"screen": "detail"}, play("joyn_free_s01e01")]))
    _, v = check_journey(j, _ctx(hours=0.5))
    assert any("must show registration_wall, not paywall" in x for x in v)


def test_gated_click_to_detail_is_repaired_into_wall():
    j = _j((2, [{"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_plus"}},
                {"screen": "detail"}, CLICK_FREE, {"screen": "detail"}, play("joyn_free_s01e01")]))
    fixed, v = check_journey(j, _ctx(hours=0.5))
    assert v == []
    assert fixed.sessions[0].steps[1].screen == "paywall" and fixed.sessions[0].steps[1].blocked == "joyn_plus"


def test_invented_ids_rejected():
    j = _j((2, [{"screen": "home", "lanes": ["lane_fake"], "click": {"lane": "lane_hero", "title": "joyn_madeup"}},
                {"screen": "detail"}, play("joyn_free_s09e99")]))
    _, v = check_journey(j, _ctx())
    assert any("unknown lane 'lane_fake'" in x for x in v)
    assert any("'joyn_madeup' is not in lane" in x for x in v)
    assert any("unknown content_id 'joyn_free_s09e99'" in x for x in v)


def test_unreleased_episode_rejected():
    j = _j((10, [{"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_new"}},
                 {"screen": "detail"}, play("joyn_new_s01e01")]))
    _, v = check_journey(j, _ctx(hours=1.4))
    assert any("not released until 2026-09-17" in x for x in v)


def test_non_video_user_cannot_play_and_video_user_must():
    j = _j((2, [CLICK_FREE, {"screen": "detail"}, play("joyn_free_s01e01")]))
    _, v = check_journey(j, _ctx(video=False, hours=0))
    assert any("NOT a video user" in x for x in v)
    _, v2 = check_journey(_j((2, [CLICK_FREE, {"screen": "detail"}])), _ctx(video=True))
    assert any("include playback" in x for x in v2)


def test_required_registration_and_subscription():
    j = _j((2, [CLICK_FREE, {"screen": "detail"}, play("joyn_free_s01e01")]))
    _, v = check_journey(j, _ctx(hours=0.5, must_register_on_day=5))
    assert any("registration is REQUIRED exactly once, on day 5" in x for x in v)
    # registered user subscribing on the wrong day, twice
    j2 = _j((3, [{"screen": "checkout", "billing": "subscribe_monthly"}, CLICK_FREE, {"screen": "detail"},
                 play("joyn_free_s01e01")]),
            (4, [{"screen": "checkout", "billing": "subscribe_monthly"}]))
    _, v2 = check_journey(j2, _ctx(seg="registered", hours=0.5, must_subscribe_on_day=3, plan="monthly"))
    assert any("subscription only allowed once" in x for x in v2)


def test_svod_after_churn_rejected():
    steps = [{"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_plus"}},
             {"screen": "detail"}, play("joyn_plus_s01e01")]
    _, ok = check_journey(_j((5, steps)), _ctx(seg="subscribed", hours=0.7, svod_until_day=10, plan="monthly"))
    assert ok == []
    _, v = check_journey(_j((12, steps)), _ctx(seg="subscribed", hours=0.7, svod_until_day=10, plan="monthly"))
    assert any("cannot play it on day 12" in x for x in v)


def test_mechanical_repairs():
    # completed with low watch, LIVE given a fraction, click missing its lane, player without detail,
    # registered user without a login, click that ends the session
    j = _j((2, [{"screen": "home", "lanes": [], "click": {"lane": "lane_hero", "title": "joyn_free"}},
                play("joyn_free_s01e01", 0.5, "completed"),
                {"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_live"}},
                {"screen": "player", "play": {"content": "joyn_live", "watch": 0.5, "minutes": None, "outcome": "completed"}},
                {"screen": "home", "lanes": ["lane_hero"], "click": {"lane": "lane_hero", "title": "joyn_free"}}]))
    fixed, v = check_journey(j, _ctx(seg="registered", hours=0.5))
    assert v == []
    st = fixed.sessions[0].steps
    assert st[0].screen == "login" and st[0].auth == "login"
    assert st[1].lanes == ["lane_hero"] and st[2].screen == "detail"
    assert st[3].play.watch >= 0.9
    assert st[5].play.minutes == 30 and st[5].play.outcome == "abandoned"
    assert st[-1].click is None


@pytest.mark.parametrize("hours,ok", [(0.5, True), (10.0, False)])
def test_watch_time_far_from_target(hours, ok):
    j = _j((2, [CLICK_FREE, {"screen": "detail"}, play("joyn_free_s01e01")]))
    _, v = check_journey(j, _ctx(hours=hours))
    assert (not any("far from" in x for x in v)) == ok
