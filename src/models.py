"""Pydantic models for every stage: catalog, briefs, LLM journeys and output events.

ASSUMPTION: the "attached spec" with event properties was not included with the brief, so the
event property set below is my own, in a tracking-plan style. All event models live in this one
file so swapping in the real spec only touches this module and the expander.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------------
Platform = Literal["ott", "android", "ios", "web"]
Segment = Literal["anonymous", "registered", "subscribed"]
Licence = Literal["AVOD", "RVOD", "SVOD"]
LaneType = Literal["hero_lane", "big_teaser", "standard_lane"]
ScreenName = Literal[
    "home", "live_tv", "category", "search", "detail", "player", "account",
    "login", "registration", "checkout", "registration_wall", "paywall",
]
LANE_SCREENS: tuple[str, ...] = ("home", "live_tv", "category", "detail", "search")
PlaybackEvent = Literal["start", "heartbeat", "complete"]
EventName = Literal[
    "screen_viewed", "video_view", "lane_viewed", "asset_clicked", "login", "registration",
    "subscribed", "payment_received", "payment_failed", "$identify",
]


# --------------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------------
class Playable(BaseModel):
    """Something that can be played: an episode, a movie/compilation, or a LIVE stream."""

    content_id: str
    title_id: str
    title: str
    episode_title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    content_type: Literal["VOD", "LIVE"]
    genre: str
    licence_type: Licence
    duration_s: Optional[int] = None          # null for LIVE
    available_from: Optional[date] = None     # date the episode drops (null = always)


class Title(BaseModel):
    """A tile in a lane: a series, a movie/compilation or a LIVE channel."""

    title_id: str
    title: str
    content_type: Literal["VOD", "LIVE"]
    asset_type: Literal["series", "movie", "compilation"]
    genre: str
    licence_type: Licence                      # gate applied at click time ("weakest" playable)
    playable_ids: list[str]


class Lane(BaseModel):
    lane_id: str
    lane_name: str
    lane_type: LaneType
    screens: list[str]
    title_ids: list[str]


class Catalog(BaseModel):
    titles: list[Title]
    playables: list[Playable]
    lanes: list[Lane]

    def title_map(self) -> dict[str, Title]:
        return {t.title_id: t for t in self.titles}

    def playable_map(self) -> dict[str, Playable]:
        return {p.content_id: p for p in self.playables}

    def lane_map(self) -> dict[str, Lane]:
        return {l.lane_id: l for l in self.lanes}


# --------------------------------------------------------------------------------------------
# Personas / briefs (Python-owned hard constraints)
# --------------------------------------------------------------------------------------------
class Device(BaseModel):
    label: str                  # "d1" primary, "d2" second
    device_id: str
    anon_id: str
    platform: Platform
    device_type: Literal["phone", "tablet", "tv", "desktop"]
    brand: str
    model: str
    os: str
    os_version: str
    app_version: str
    viewport_w: int
    viewport_h: int


class BillingEvent(BaseModel):
    """Server-side billing attempts Python schedules (renewals, retries)."""

    when: str                   # ISO local datetime, Europe/Berlin
    kind: Literal["renewal", "retry"]
    outcome: Literal["received", "failed"]
    failure_reason: Optional[str] = None
    attempt_number: int = 1
    plan: Literal["monthly", "annual"]


class PeriodBrief(BaseModel):
    """Everything the LLM may do for one user in one month."""

    period: str
    segment_at_start: Segment
    is_video_user: bool
    sessions_range: tuple[int, int]
    target_watch_hours: float
    must_register_on_day: Optional[int] = None       # registration required on this day
    must_subscribe_on_day: Optional[int] = None      # conversion required on this day
    plan: Optional[Literal["monthly", "annual"]] = None
    svod_until_day: Optional[int] = None             # churn: no SVOD after this day
    voice_fan: bool = False
    premiere_fans: list[str] = Field(default_factory=list)   # title_ids premiering this period (spec stories)
    football_fan: bool = False
    zapper: bool = False
    persona_note: str = ""
    billing: list[BillingEvent] = Field(default_factory=list)


class UserBrief(BaseModel):
    user_id: str
    segment: Segment                     # final state, used for the 60/30/10 split
    primary_platform: Platform
    devices: list[Device]
    periods: dict[str, PeriodBrief]
    registered_before_window: bool = False
    subscribed_before_window: bool = False
    renewal_day: Optional[int] = None
    renewal_month: Optional[int] = None  # annual plans renew in this month
    batch: str = "base"                  # run_id of the batch that created this user


# --------------------------------------------------------------------------------------------
# LLM journey schema (semantic level only: no ids, seconds, heartbeats or device fields)
# --------------------------------------------------------------------------------------------
class JClick(BaseModel):
    lane: str = Field(description="lane_id the tile was clicked in; must be in this step's lanes")
    title: str = Field(description="title_id clicked; must be in that lane")


class JPlay(BaseModel):
    content: str = Field(description="content_id of the episode/movie/live stream")
    watch: Optional[float] = Field(None, description="VOD only: fraction watched 0.01-1.0")
    minutes: Optional[int] = Field(None, description="LIVE only: minutes watched")
    outcome: Literal["completed", "abandoned"]


class JStep(BaseModel):
    screen: ScreenName
    lanes: list[str] = Field(default_factory=list, description="lane_ids seen on this screen")
    click: Optional[JClick] = None
    play: Optional[JPlay] = None
    blocked: Optional[str] = Field(None, description="wall screens only: gated title/content id")
    auth: Optional[Literal["login", "register", "register_abandoned"]] = None
    billing: Optional[Literal["subscribe_monthly", "subscribe_annual", "checkout_abandoned"]] = None


class JSession(BaseModel):
    day: int = Field(description="day of month 1-30")
    start: str = Field(description="local start time HH:MM")
    device: str = Field(description="device label from the brief, e.g. d1")
    steps: list[JStep]

    @field_validator("start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        hh, mm = v.split(":")[:2]
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"bad time {v}")
        return f"{h:02d}:{m:02d}"


class Journey(BaseModel):
    sessions: list[JSession]


# --------------------------------------------------------------------------------------------
# Output events
# --------------------------------------------------------------------------------------------
class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event: EventName
    distinct_id: str
    insert_id: str = Field(alias="$insert_id")
    timestamp: str                       # ISO-8601 UTC with ms, "...Z"
    session_id: Optional[str]            # null only for server-side billing events
    user_id: Optional[str]
    anonymous_id: str
    user_segment: Segment                # segment at event time
    platform: Platform
    device_id: str
    device_type: Literal["phone", "tablet", "tv", "desktop"]
    device_brand: str
    device_model: str
    os: str
    os_version: str
    app_version: str
    viewport_width: int
    viewport_height: int
    country: Literal["DE"] = "DE"
    locale: Literal["de-DE"] = "de-DE"
    event_source: Literal["client", "server"] = "client"
    # marketing (session-scoped on client events; absent on server events) — see MARKETING_ANALYTICS.md
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_content: Optional[str] = None
    utm_term: Optional[str] = None
    referrer: Optional[str] = Field(None, alias="$referrer")
    referring_domain: Optional[str] = Field(None, alias="$referring_domain")
    gclid: Optional[str] = None
    fbclid: Optional[str] = None
    ttclid: Optional[str] = None
    landing_url: Optional[str] = None

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: str) -> str:
        if not v.endswith("Z") or "T" not in v:
            raise ValueError("timestamp must be ISO-8601 UTC ending in Z")
        return v


class ScreenViewed(_Base):
    event: Literal["screen_viewed"]
    screen_name: ScreenName
    previous_screen: Optional[ScreenName]


class AssetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_id: str
    title: str
    asset_position: int
    licence_type: Licence


class LaneViewed(_Base):
    event: Literal["lane_viewed"]
    screen_name: ScreenName
    lane_id: str
    lane_name: str
    lane_type: LaneType
    lane_position: int
    assets_list: list[AssetItem]


class AssetClicked(_Base):
    event: Literal["asset_clicked"]
    screen_name: ScreenName
    lane_id: str
    lane_name: str
    lane_type: LaneType
    lane_position: int
    content_id: str
    title: str
    asset_position: int
    licence_type: Licence


class VideoView(_Base):
    event: Literal["video_view"]
    screen_name: Literal["player"] = "player"
    playback_event: PlaybackEvent
    playback_session_id: str
    content_id: str
    title_id: str
    title: str
    episode_title: Optional[str]
    season: Optional[int]
    episode: Optional[int]
    genre: str
    content_type: Literal["VOD", "LIVE"]
    licence_type: Licence
    duration_seconds: Optional[int]
    watch_time_seconds: int
    completion_rate: Optional[float]


class Login(_Base):
    event: Literal["login"]
    method: Literal["email", "google", "apple", "facebook"]
    success: bool = True


class Registration(_Base):
    event: Literal["registration"]
    method: Literal["email", "google", "apple", "facebook"]


class Subscribed(_Base):
    event: Literal["subscribed"]
    plan: Literal["monthly", "annual"]
    price: float
    currency: Literal["EUR"]


class PaymentReceived(_Base):
    event: Literal["payment_received"]
    plan: Literal["monthly", "annual"]
    amount: float
    currency: Literal["EUR"]
    payment_type: Literal["initial", "renewal", "retry"]
    transaction_id: str
    attempt_number: int


class PaymentFailed(_Base):
    event: Literal["payment_failed"]
    plan: Literal["monthly", "annual"]
    amount: float
    currency: Literal["EUR"]
    payment_type: Literal["initial", "renewal", "retry"]
    failure_reason: Literal["card_declined", "insufficient_funds", "gateway_timeout", "expired_card"]
    attempt_number: int


class Identify(_Base):
    event: Literal["$identify"]
    anon_distinct_id: str = Field(alias="$anon_distinct_id")


EVENT_MODELS: dict[str, type[_Base]] = {
    "screen_viewed": ScreenViewed, "lane_viewed": LaneViewed, "asset_clicked": AssetClicked,
    "video_view": VideoView, "login": Login, "registration": Registration,
    "subscribed": Subscribed, "payment_received": PaymentReceived,
    "payment_failed": PaymentFailed, "$identify": Identify,
}


def validate_event(row: dict) -> None:
    """Raise pydantic.ValidationError if a clean event row breaks its schema."""
    EVENT_MODELS[row["event"]].model_validate(row)

