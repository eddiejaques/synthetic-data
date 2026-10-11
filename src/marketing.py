"""Marketing analytics layer: first touch per user, a source per session, UTMs, spend tables.

Pure Python and deterministic. It runs inside expansion after session timing, so every source
fits what the session actually did (Voice launch ads lead to Voice sessions, the plus_trial email
leads to the subscribe session, ...). See MARKETING_ANALYTICS.md.
"""
from __future__ import annotations

import csv
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from .config import Settings, rng_for
from .models import Catalog, UserBrief

ALL = {"ott", "ios", "android", "web"}
MOBILE = {"ios", "android"}
# channel -> platforms where a *session* can carry it (person-level first touch is wider, below)
SESSION_ELIGIBLE = {
    "direct": ALL, "organic_search": {"web"}, "paid_search_brand": {"web"} | MOBILE,
    "paid_search_generic": {"web"} | MOBILE, "paid_social": {"web"} | MOBILE, "display": {"web"} | MOBILE,
    "online_video": {"web"} | MOBILE, "referral": {"web"}, "organic_social": {"web"}, "affiliate": {"web"},
    "crm_email": {"web"} | MOBILE, "crm_push": MOBILE, "tv_promo": {"web"}, "app_store": set(),
}
# first-touch compatibility weight by platform (TV: no clickable ads)
FIRST_TOUCH_WEIGHT = {
    "web": {c: 1.0 for c in ("organic_search", "direct", "paid_search_brand", "paid_search_generic", "paid_social",
                             "display", "online_video", "referral", "organic_social", "affiliate", "tv_promo")},
    "ios": {"direct": 0.4, "paid_search_brand": 1.0, "paid_search_generic": 0.6, "paid_social": 1.4, "display": 0.6,
            "online_video": 1.0, "tv_promo": 1.0, "app_store": 3.0, "organic_social": 0.0},
    "ott": {"direct": 1.5, "tv_promo": 3.0, "app_store": 2.0},
}
FIRST_TOUCH_WEIGHT["android"] = FIRST_TOUCH_WEIGHT["ios"]
PAID = {"paid_search_brand", "paid_search_generic", "paid_social", "display", "online_video", "affiliate"}
CRM = {"crm_email", "crm_push"}
CONTENT_PREFIX = {"paid_search_brand": ("rsa",), "paid_search_generic": ("rsa",), "paid_social": ("static", "carousel", "video15s", "video6s"),
                  "display": ("banner",), "online_video": ("video",), "affiliate": ("affiliate",),
                  "crm_email": ("email", "newsletter"), "crm_push": ("push",), "tv_promo": ("onair", "qr")}
PUBLISHERS = ["www.spiegel.de", "www.bild.de", "www.chip.de", "www.t-online.de", "www.focus.de"]
REFERRAL_SITES = ["www.prosieben.de", "www.sat1.de", "www.kicker.de", "de.wikipedia.org", "www.ran.de"]
SOCIAL_SITES = ["www.instagram.com", "t.co", "www.facebook.com", "www.reddit.com"]
LANDING_PATH = {"tvog": "/serien/the-voice-of-germany", "ranfootball": "/live-tv/ran", "joynplus": "/plus",
                "registration": "/signup", "payment": "/account/payment"}
SESSION_PROPS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "$referrer",
                 "$referring_domain", "gclid", "fbclid", "ttclid", "landing_url"]


# ----------------------------------------------------------------------------- channel grouping
CHANNEL_LABEL = {
    "direct": "Direct", "organic_search": "Organic Search", "paid_search_brand": "Paid Search",
    "paid_search_generic": "Paid Search", "paid_social": "Paid Social", "display": "Display",
    "online_video": "Paid Video", "referral": "Referral", "organic_social": "Organic Social",
    "affiliate": "Affiliates", "crm_email": "Email", "crm_push": "Push", "tv_promo": "Offline / TV",
    "app_store": "App Store",
}
MAPPING_ROWS = [  # (utm_source, utm_medium, referring_domain pattern, channel) — first match wins
    ("google", "cpc", "*", "Paid Search"), ("apple_search_ads", "cpc", "*", "Paid Search"),
    ("facebook", "paid_social", "*", "Paid Social"), ("instagram", "paid_social", "*", "Paid Social"),
    ("tiktok", "paid_social", "*", "Paid Social"), ("google", "display", "*", "Display"),
    ("youtube", "video", "*", "Paid Video"), ("*", "affiliate", "*", "Affiliates"),
    ("newsletter", "email", "*", "Email"), ("joyn_app", "push", "*", "Push"),
    ("*", "tv_promo", "*", "Offline / TV"),
    ("(none)", "(none)", "www.google.de|www.bing.com|www.ecosia.org", "Organic Search"),
    ("(none)", "(none)", "|".join(SOCIAL_SITES), "Organic Social"),
    ("(none)", "(none)", "$direct", "Direct"),
    ("(none)", "(none)", "*", "Referral"),
]


def channel_for(e: dict) -> Optional[str]:
    """GA4-style default channel grouping from raw event properties (reference implementation)."""
    if e.get("event_source") == "server":
        return None
    src, med = e.get("utm_source") or "(none)", e.get("utm_medium") or "(none)"
    dom = e.get("$referring_domain") or e.get("referring_domain") or "$direct"
    for s, m, d, ch in MAPPING_ROWS:
        if (s in ("*", src)) and (m in ("*", med)) and (d == "*" or dom in d.split("|")):
            return ch
    return "Unassigned"


# ----------------------------------------------------------------------------- data classes
@dataclass
class Touch:
    channel: str
    campaign_key: Optional[str] = None
    source: Optional[str] = None
    medium: Optional[str] = None
    content: Optional[str] = None
    term: Optional[str] = None
    referring_domain: str = "$direct"
    click_id: Optional[tuple[str, str]] = None
    path: str = "/"


@dataclass
class MarketingResult:
    session_props: dict[tuple[str, str, int], dict] = field(default_factory=dict)
    session_channel: dict[tuple[str, str, int], str] = field(default_factory=dict)
    acquisitions: list[dict] = field(default_factory=list)
    campaigns: list[dict] = field(default_factory=list)
    spend: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def campaign_name(cfg: Settings, key: str, period: str) -> str:
    """`yyyy_mm_objective_topic[suffix]` from the period's month, unless the campaign sets `name`."""
    c = _campaigns(cfg)[key]
    if c.get("name"):
        return str(c["name"])
    first = cfg.period_days(period)[0]
    return f"{first.year}_{first.month:02d}_{c['objective']}_{c['topic']}" + (c.get("name_suffix") or {}).get(period, "")


def _campaigns(cfg: Settings) -> dict[str, dict]:
    return {c["key"]: c for c in cfg["marketing"]["campaigns"]}


def _in_window(cfg: Settings, key: str, period: str, d: date) -> bool:
    c = _campaigns(cfg)[key]
    if "start" in c:                                   # spec campaigns use absolute dates
        return date.fromisoformat(str(c["start"])) <= d <= date.fromisoformat(str(c["end"]))
    w = c.get("window")
    if not w:
        return True
    if period not in w:
        return False
    a, b = (date.fromisoformat(x) for x in w[period])
    return a <= d <= b


def _pick(rng: random.Random, weights: dict[str, float]) -> Optional[str]:
    items = [(k, v) for k, v in weights.items() if v > 0]
    if not items:
        return None
    return rng.choices([k for k, _ in items], weights=[v for _, v in items])[0]


# ----------------------------------------------------------------------------- touch builder
def build_touch(cfg: Settings, rng: random.Random, channel: str, platform: str, period: str,
                campaign_key: Optional[str], titles_played: set[str]) -> Touch:
    """Turn a channel (+ campaign) into concrete utm / referrer / click-id values for a platform."""
    t = Touch(channel=channel, campaign_key=campaign_key)
    camp = _campaigns(cfg).get(campaign_key or "", {})
    if camp:
        t.path = LANDING_PATH.get(camp["topic"], "/")
        prefixes = CONTENT_PREFIX.get(channel, ())
        options = [c for c in camp.get("contents", []) if c.startswith(prefixes)] or camp.get("contents", []) or [None]
        t.content = rng.choice(options)
        terms = camp.get("terms") or []
        if channel.startswith("paid_search") and terms:
            hits = [x for x in terms if any(w in x for tid in titles_played for w in tid.replace("joyn_", "").split("_") if len(w) > 3)]
            t.term = rng.choice(hits or terms)
    web = platform == "web"
    if channel == "organic_search":
        t.referring_domain = rng.choices(["www.google.de", "www.bing.com", "www.ecosia.org"], [0.88, 0.07, 0.05])[0]
    elif channel == "referral":
        t.referring_domain = rng.choice(REFERRAL_SITES)
    elif channel == "organic_social":
        t.referring_domain = rng.choice(SOCIAL_SITES)
    elif channel.startswith("paid_search"):
        if platform == "ios":
            t.source, t.medium = "apple_search_ads", "cpc"
        else:
            t.source, t.medium, t.click_id = "google", "cpc", ("gclid", _cid(rng, 40))
            t.referring_domain = "www.google.de" if web else "$direct"
    elif channel == "paid_social":
        tiktok = rng.random() < float(cfg["marketing"]["tiktok_share_of_paid_social"][period])
        if tiktok:
            t.source, t.click_id = "tiktok", ("ttclid", _cid(rng, 32))
            t.referring_domain = "www.tiktok.com" if web else "$direct"
        else:
            t.source = rng.choice(["facebook", "instagram"])
            t.click_id = ("fbclid", "IwAR" + _cid(rng, 40))
            t.referring_domain = ("l.facebook.com" if t.source == "facebook" else "l.instagram.com") if web else "$direct"
        t.medium = "paid_social"
    elif channel == "display":
        t.source, t.medium, t.click_id = "google", "display", ("gclid", _cid(rng, 40))
        t.referring_domain = rng.choice(PUBLISHERS) if web else "$direct"
    elif channel == "online_video":
        t.source, t.medium, t.click_id = "youtube", "video", ("gclid", _cid(rng, 40))
        t.referring_domain = "www.youtube.com" if web else "$direct"
    elif channel == "affiliate":
        t.source, t.medium = rng.choice(["mydealz", "chip"]), "affiliate"
        t.referring_domain = "www.mydealz.de" if t.source == "mydealz" else "www.chip.de"
    elif channel == "crm_email":
        t.source, t.medium = "newsletter", "email"
        t.referring_domain = "mail.google.com" if web and rng.random() < 0.3 else "$direct"
    elif channel == "crm_push":
        t.source, t.medium = "joyn_app", "push"
    elif channel == "tv_promo":
        t.source, t.medium = rng.choice(["prosieben", "sat1"]), "tv_promo"
    return t


def _cid(rng: random.Random, n: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(rng.choice(alphabet) for _ in range(n))


def touch_props(cfg: Settings, t: Touch, platform: str, period: str) -> dict:
    utm = t.source is not None
    props = {k: None for k in SESSION_PROPS}
    if utm:
        props.update(utm_source=t.source, utm_medium=t.medium,
                     utm_campaign=campaign_name(cfg, t.campaign_key, period) if t.campaign_key else None,
                     utm_content=t.content, utm_term=t.term)
    if t.click_id:
        props[t.click_id[0]] = t.click_id[1]
    props["$referring_domain"] = t.referring_domain
    props["$referrer"] = "$direct" if t.referring_domain == "$direct" else f"https://{t.referring_domain}/"
    query = {k: v for k, v in (("utm_source", props["utm_source"]), ("utm_medium", props["utm_medium"]),
                               ("utm_campaign", props["utm_campaign"]), ("utm_content", props["utm_content"]),
                               ("utm_term", props["utm_term"])) if v}
    if t.click_id:
        query[t.click_id[0]] = t.click_id[1]
    q = ("?" + urlencode(query)) if query else ""
    if platform == "web":
        props["landing_url"] = f"https://www.joyn.de{t.path}{q}"
    elif platform in MOBILE and query:
        props["landing_url"] = f"joyn://open{t.path}{q}"
    return props


# ----------------------------------------------------------------------------- assignment
def assign_marketing(cfg: Settings, cat: Catalog, briefs: list[UserBrief], sessions: list, pay: dict,
                     new_users: Optional[set] = None) -> MarketingResult:
    """new_users: users first seen in this batch (None = all). Returning users get no first touch."""
    M = cfg["marketing"]
    R = M["rules"]
    plays = cat.playable_map()
    res = MarketingResult()
    by_user: dict[str, list] = defaultdict(list)
    for s in sessions:
        by_user[s.user].append(s)
    failed_at: dict[str, list[datetime]] = defaultdict(list)
    for g in pay.values():
        if g.outcome == "failed" or g.pre_timeouts:
            failed_at[g.user].append(g.when)
    incidents = sorted(date.fromisoformat(str(i["date"])) for i in cfg.payment_incidents())
    periods = cfg.period_names
    period_days = {p: set(cfg.period_days(p)) for p in periods}
    is_new = (lambda u: True) if new_users is None else (lambda u: u in new_users)  # noqa: E731
    first_touch = assign_first_touch(cfg, [b for b in briefs if is_new(b.user_id)])
    dated = [c for c in M["campaigns"] if "start" in c and c.get("title_id")]   # spec campaigns for a title

    for b in briefs:
        ss = sorted(by_user.get(b.user_id, []), key=lambda s: (periods.index(s.period), s.start_dt))
        devs = {d.label: d.platform for d in b.devices}
        urng = rng_for(cfg.seed, "mkt-user", b.user_id)
        # ---- first touch
        new = is_new(b.user_id)
        pre_existing = b.registered_before_window or b.subscribed_before_window or not new
        first_period = next(iter(b.periods))
        acq_platform = b.primary_platform
        acq_channel = first_touch.get(b.user_id, "direct")
        if pre_existing:
            acq_date = date(2019, 6, 1) + timedelta(days=urng.randint(0, (date(2025, 8, 15) - date(2019, 6, 1)).days))
            acq_touch = build_touch(cfg, urng, acq_channel, acq_platform, "STLY", None, set())
        else:
            acq_date = ss[0].start_dt.date() if ss else cfg.period_days(first_period)[0]
            acq_touch = None                       # built on the first session below
        last_session_flags: dict = {}
        acq_row = {"user_id": b.user_id, "acquisition_channel": acq_channel, "acquisition_date": acq_date.isoformat(),
                   "acquisition_platform": acq_platform, "acquired_in_window": not pre_existing,
                   "acquisition_period": None if pre_existing else first_period}

        for i, s in enumerate(ss):
            rng = rng_for(cfg.seed, "mkt", b.user_id, s.period, s.idx)
            plat = devs[s.device]
            d = s.start_dt.date()
            s_days = period_days[s.period]
            titles = {plays[st.play.content].title_id for st in s.steps if st.play}
            pb = b.periods[s.period]
            registered = pb.segment_at_start != "anonymous" or (pb.must_register_on_day is not None and d.day > pb.must_register_on_day)
            subscribe = any(st.billing in ("subscribe_monthly", "subscribe_annual") for st in s.steps)
            channel, key = "direct", None
            if i == 0 and not pre_existing:
                channel = acq_channel if plat in SESSION_ELIGIBLE[acq_channel] or acq_channel in ("app_store", "tv_promo", "direct") else "direct"
                if channel in ("app_store",) or (channel == "tv_promo" and plat != "web"):
                    channel = "direct"             # visible only as a person property
                key = _acq_campaign(cfg, channel, s.period, d, titles)
            elif subscribe and plat != "ott":
                ch = _pick(rng, {**{k: float(v) for k, v in R["subscribe_last_touch"].items()},
                                 "direct": 1 - sum(R["subscribe_last_touch"].values())})
                if ch == "crm_email" and not registered:
                    ch = "paid_search_brand"
                channel, key = ch, ("plus_trial" if ch != "direct" else None)
            elif (any(i < d and i in s_days for i in incidents) and _in_window(cfg, "service_payment_update", s.period, d)
                  and any(w < s.start_dt for w in failed_at[b.user_id]) and plat != "ott"
                  and not last_session_flags.get("got_payment_mail") and rng.random() < float(R["payment_followup_touch"])):
                channel, key = "crm_email", "service_payment_update"
                last_session_flags["got_payment_mail"] = True
            elif "joyn_the_voice" in titles and plat != "ott" and _in_window(cfg, "launch_tvog", s.period, d) \
                    and rng.random() < float(R["voice_campaign_touch"]):
                opts = {"paid_social": 1.0, "online_video": 0.7}
                if plat in MOBILE:
                    opts["crm_push"] = 0.8
                if registered:
                    opts["crm_email"] = 0.6
                channel, key = _pick(rng, opts), "launch_tvog"
            elif d.weekday() == 5 and titles & {"joyn_ran_football", "joyn_ran_football_live"} and plat != "ott" \
                    and rng.random() < float(R["football_touch"]):
                channel, key = ("crm_push" if plat in MOBILE else "paid_search_generic"), "ran_bundesliga"
            elif plat != "ott" and (hit := _dated_hit(cfg, dated, s.period, d, titles, plat, registered, rng)):
                channel, key = hit
            elif last_session_flags.get("abandoned") and not registered and plat != "ott" \
                    and rng.random() < float(R["retargeting_touch"]):
                channel, key = rng.choice(["display", "paid_social"]), "retargeting_signup"
            elif rng.random() >= float(M["return_direct_share"][plat]):
                opts = {c: float(w) for c, w in M["return_mix"].items() if plat in SESSION_ELIGIBLE[c]}
                if not registered:
                    opts.pop("crm_email", None)
                ch = _pick(rng, opts) or "direct"
                channel = ch
                key = {"crm_email": "crm_weekly", "crm_push": "crm_weekly", "paid_search_brand": "always_on_brand",
                       "display": "acquisition_broad"}.get(ch)
            touch = build_touch(cfg, rng, channel, plat, s.period, key, titles)
            if i == 0 and not pre_existing:
                acq_touch = touch if touch.source or touch.referring_domain != "$direct" else \
                    build_touch(cfg, urng, acq_channel, acq_platform, s.period, _acq_campaign(cfg, acq_channel, s.period, d, titles), titles)
                acq_row["acquisition_campaign"] = campaign_name(cfg, acq_touch.campaign_key, s.period) if acq_touch.campaign_key else None
            res.session_props[(b.user_id, s.period, s.idx)] = touch_props(cfg, touch, plat, s.period)
            res.session_channel[(b.user_id, s.period, s.idx)] = channel
            last_session_flags["abandoned"] = any(st.auth == "register_abandoned" or st.screen == "registration_wall"
                                                  for st in s.steps) and not registered
        if not new:
            continue                               # returning user: first touch belongs to an earlier batch
        if acq_touch is None:
            acq_touch = build_touch(cfg, urng, acq_channel, acq_platform, first_period, None, set())
        acq_row.setdefault("acquisition_campaign", None)
        acq_row.update(initial_utm_source=acq_touch.source, initial_utm_medium=acq_touch.medium,
                       initial_utm_campaign=acq_row["acquisition_campaign"],
                       initial_referring_domain=acq_touch.referring_domain)
        if acq_channel in ("app_store", "tv_promo") and acq_touch.source is None:
            acq_row["initial_utm_source"] = {"ios": "app_store", "android": "google_play", "ott": "smart_tv_store",
                                             "web": None}[acq_platform] if acq_channel == "app_store" else "prosieben"
            acq_row["initial_utm_medium"] = "app_store" if acq_channel == "app_store" else "tv_promo"
        res.acquisitions.append(acq_row)

    res.campaigns = campaign_table(cfg)
    res.spend = spend_table(cfg, sessions, res)
    res.meta = {"session_channels": dict(Counter(res.session_channel.values())),
                "acquisition_channels": dict(Counter(a["acquisition_channel"] for a in res.acquisitions))}
    return res


def assign_first_touch(cfg: Settings, briefs: list[UserBrief]) -> dict[str, str]:
    """Exact first-touch quotas per (acquisition cohort, platform), so the channel mix is planted by
    construction: shares = acquisition_mix x platform compatibility, largest-remainder rounded."""
    from .personas import _quota
    M = cfg["marketing"]
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for b in briefs:
        pre = b.registered_before_window or b.subscribed_before_window
        cohort = "pre" if pre else next(iter(b.periods))
        groups[(cohort, b.primary_platform)].append(b.user_id)
    out: dict[str, str] = {}
    for (cohort, plat), users in sorted(groups.items()):
        mix = M["acquisition_mix"]["STLY" if cohort == "pre" else cohort]
        w = {c: float(mix.get(c, 0)) * FIRST_TOUCH_WEIGHT[plat].get(c, 0.0) for c in sorted(set(mix) | {"app_store"})}
        w = {c: v for c, v in w.items() if v > 0}
        tot = sum(w.values())
        labels = [c for c, n in _quota(len(users), {c: v / tot for c, v in w.items()}).items() for _ in range(n)]
        rng = rng_for(cfg.seed, "first-touch", cohort, plat)
        rng.shuffle(labels)
        out.update(zip(sorted(users), labels))
    return out


def _dated_hit(cfg: Settings, dated: list[dict], period: str, d: date, titles: set[str], plat: str,
               registered: bool, rng: random.Random) -> Optional[tuple[str, str]]:
    """Spec campaigns promoting a title: sessions in the flight that watch it may come via the campaign."""
    for c in dated:
        if c["title_id"] in titles and _in_window(cfg, c["key"], period, d) \
                and rng.random() < float(c.get("touch_rate", 0.5)):
            opts = [ch for ch in c["channels"] if plat in SESSION_ELIGIBLE.get(ch, set())
                    and (ch != "crm_email" or registered)]
            if opts:
                return rng.choice(opts), c["key"]
    return None


def _acq_campaign(cfg: Settings, channel: str, period: str, d: date, titles: set[str]) -> Optional[str]:
    if channel in ("paid_social", "online_video", "tv_promo") and "joyn_the_voice" in titles \
            and _in_window(cfg, "launch_tvog", period, d):
        return "launch_tvog"
    for c in cfg["marketing"]["campaigns"]:          # spec acquisition/launch flights win in their window
        if "start" in c and c["objective"] in ("acquisition", "launch") and channel in c["channels"] \
                and _in_window(cfg, c["key"], period, d) and (not c.get("title_id") or c["title_id"] in titles):
            return c["key"]
    return {"paid_search_brand": "always_on_brand", "paid_search_generic": "generic_shows",
            "paid_social": "acquisition_broad", "display": "acquisition_broad", "online_video": "acquisition_broad",
            "affiliate": "acquisition_broad", "tv_promo": "tv_crosspromo"}.get(channel)


# ----------------------------------------------------------------------------- tables
def campaign_table(cfg: Settings) -> list[dict]:
    rows = []
    for p in cfg.period_names:
        for c in cfg["marketing"]["campaigns"]:
            days = cfg.period_days(p)
            w = c.get("window")
            if "start" in c:                        # absolute dates: keep periods it overlaps, clipped
                start, end = max(str(c["start"]), days[0].isoformat()), min(str(c["end"]), days[-1].isoformat())
                if start > end:
                    continue
            elif w and p not in w:
                continue
            else:
                start, end = (w[p] if w else (days[0].isoformat(), days[-1].isoformat()))
            rows.append({"utm_campaign": campaign_name(cfg, c["key"], p), "campaign_key": c["key"], "period": p,
                         "objective": c["objective"], "topic": c["topic"], "channels": "|".join(c["channels"]),
                         "start_date": start, "end_date": end,
                         "budget_type": "owned" if set(c["channels"]) <= CRM else "paid"})
    return rows


def spend_table(cfg: Settings, sessions: list, res: MarketingResult) -> list[dict]:
    """Daily spend / delivery per campaign × source × content × term, derived from landing sessions."""
    costs = cfg["marketing"]["costs"]
    agg: dict[tuple, int] = Counter()
    for s in sessions:
        key = (s.user, s.period, s.idx)
        p = res.session_props.get(key)
        if not p or not p.get("utm_campaign"):
            continue
        ch = res.session_channel[key]
        agg[(s.start_dt.date().isoformat(), p["utm_campaign"], ch, p["utm_source"], p["utm_medium"],
             p["utm_content"] or "", p["utm_term"] or "")] += 1
    rows = []
    for (d, camp, ch, src, med, content, term), n in sorted(agg.items()):
        c = costs.get(ch)
        rng = rng_for(cfg.seed, "spend", d, camp, src, content, term)
        row = {"date": d, "utm_campaign": camp, "channel": CHANNEL_LABEL[ch], "utm_source": src, "utm_medium": med,
               "utm_content": content, "utm_term": term, "landing_sessions": n, "impressions": 0, "clicks": 0,
               "sends": 0, "opens": 0, "cost_eur": 0.0}
        if c is None:                      # tv_promo: no media cost tracked at click level
            rows.append(row)
            continue
        clicks = math.ceil(n / float(c["landing_rate"]) * rng.uniform(1.0, 1.12))
        row["clicks"] = clicks
        if ch == "crm_email":
            opens = math.ceil(clicks / rng.uniform(*c["click_to_open"]))
            row.update(opens=opens, sends=math.ceil(opens / rng.uniform(*c["open_rate"])))
        elif ch == "crm_push":
            row.update(opens=clicks, sends=math.ceil(clicks / rng.uniform(*c["open_rate"])))
        else:
            imps = math.ceil(clicks / rng.uniform(*c["ctr"]))
            row["impressions"] = imps
            cost = clicks * rng.uniform(*c["cpc"]) if "cpc" in c else imps / 1000 * rng.uniform(*c["cpm"])
            row["cost_eur"] = round(cost, 2)
        rows.append(row)
    return rows


def write_marketing(out: Path, res: MarketingResult) -> None:
    d = out / "marketing"
    d.mkdir(parents=True, exist_ok=True)
    _csv(d / "campaigns.csv", res.campaigns)
    _csv(d / "marketing_spend_daily.csv", res.spend)
    cols = ["user_id", "acquisition_channel", "acquisition_date", "acquisition_platform", "acquired_in_window",
            "acquisition_period", "acquisition_campaign", "initial_utm_source", "initial_utm_medium",
            "initial_utm_campaign", "initial_referring_domain"]
    _csv(d / "user_acquisition.csv", res.acquisitions, cols)
    _csv(d / "channel_mapping.csv", [{"utm_source": s, "utm_medium": m, "referring_domain": r, "channel": c,
                                      "rule_order": i} for i, (s, m, r, c) in enumerate(MAPPING_ROWS, start=1)])


def _csv(path: Path, rows: list[dict], cols: Optional[list[str]] = None) -> None:
    cols = cols or (list(rows[0]) if rows else [])
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_acquisitions(out: Path) -> dict[str, dict]:
    p = out / "marketing" / "user_acquisition.csv"
    if not p.exists():
        return {}
    with open(p) as f:
        return {r["user_id"]: r for r in csv.DictReader(f)}

