"""Stage 1 — CATALOG. Gemini expands the seed table into seasons/episodes, extra titles and lanes.

Python owns every id: content_ids are `joyn_<slug>_s<SS>e<EE>` for episodes, `joyn_<slug>` for
movies/compilations/LIVE-only channels and `joyn_<slug>_live` for the LIVE side of a mixed
LIVE+VOD title. Anything the model invents later is rejected against this catalog.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .config import Settings, read_json, rng_for, write_json
from .gemini_client import GeminiClient, parse_json
from .models import LANE_SCREENS, Catalog, Lane, Playable, Title

log = logging.getLogger(__name__)

GENRES = ["Reality TV", "Comedy", "Drama", "News", "Live Sports", "Crime"]
LICENCE_RANK = {"AVOD": 0, "RVOD": 1, "SVOD": 2}

# Seed table, kept exactly as specified. Mixed rows are split into a VOD title and a LIVE title.
SEED = [
    # slug, title, content_type, asset_type, genre, licence (VOD side), live licence
    ("gntm", "Germany's Next Topmodel", "VOD", "series", "Reality TV", "AVOD", None),
    ("the_voice", "The Voice of Germany", "VOD", "series", "Reality TV", "AVOD", None),
    ("jerks", "Jerks", "VOD", "series", "Comedy", "SVOD", None),
    ("sloborn", "Sløborn", "VOD", "series", "Drama", "SVOD", None),
    ("big_brother", "Big Brother", "VOD+LIVE", "series", "Reality TV", "AVOD", "SVOD"),
    ("sat1_live", "SAT.1 Live", "LIVE", "series", "News", None, "AVOD"),
    ("prosieben_live", "ProSieben Live", "LIVE", "series", "Comedy", None, "AVOD"),
    ("k1_magazin", "K1 Magazin", "VOD", "series", "News", "AVOD", None),
    ("ran_football", "ran Football", "VOD+LIVE", "compilation", "Live Sports", "AVOD", "AVOD"),
    ("ncis_hawaii", "NCIS: Hawaii", "VOD", "series", "Crime", "RVOD*", None),  # *latest=RVOD, rest SVOD
]

# Duration ranges in seconds. ASSUMPTION: News VOD and movies were not given in the spec.
DURATIONS = {
    "Reality TV": (4500, 6000), "Comedy": (1500, 1800), "Crime": (2500, 2800),
    "Drama": (2500, 2800), "News": (1500, 2700), "Live Sports": (300, 900),
    "compilation": (300, 900), "movie": (5400, 7800),
}


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("ø", "o").replace("Ø", "O").replace("ß", "ss"))
    s = s.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40]


def ep_id(slug: str, season: int, ep: int) -> str:
    return f"joyn_{slug}_s{season:02d}e{ep:02d}"


# ----------------------------------------------------------------------------- LLM schemas
class _Ep(BaseModel):
    episode: int
    title: str = Field(description="German episode title")
    duration_s: int


class _Season(BaseModel):
    season: int
    episodes: list[_Ep]


class SeriesOut(BaseModel):
    seasons: list[_Season]


class _Extra(BaseModel):
    title: str
    asset_type: Literal["movie", "compilation"]
    genre: Literal["Reality TV", "Comedy", "Drama", "News", "Live Sports", "Crime"]
    licence_type: Literal["AVOD", "RVOD", "SVOD"]
    duration_s: int


class ExtrasOut(BaseModel):
    items: list[_Extra]


class _Lane(BaseModel):
    lane_name: str = Field(description="German lane title")
    lane_type: Literal["hero_lane", "big_teaser", "standard_lane"]
    screens: list[Literal["home", "live_tv", "category", "detail", "search"]]
    title_ids: list[str]


class LanesOut(BaseModel):
    lanes: list[_Lane]


SYSTEM = ("You generate realistic catalog metadata for Joyn, a German streaming service. "
          "Answer with JSON only, matching the schema. Use German titles.")


# ----------------------------------------------------------------------------- builders
def _series_prompt(title: str, genre: str, asset_type: str, lo: int, hi: int, seasons: str) -> str:
    unit = "highlight clips" if asset_type == "compilation" else "episodes"
    return (f"Series: {title} (genre: {genre}). Produce {seasons} seasons, 6-12 {unit} each, "
            f"numbered from 1. Give each a realistic German {unit[:-1]} title (no numbering in the "
            f"title) and duration_s between {lo} and {hi} seconds.")


def _apply_series(slug: str, title: str, genre: str, asset_type: str, licence: str,
                  out: SeriesOut, cfg: Settings) -> tuple[Title, list[Playable]]:
    lo, hi = DURATIONS["compilation" if asset_type == "compilation" else genre]
    seasons = sorted(out.seasons, key=lambda s: s.season)[:3]
    playables: list[Playable] = []
    n_seasons = len(seasons)
    voice = cfg["stories"]["voice_premiere"]
    for si, s in enumerate(seasons, start=1):
        eps = s.episodes[:12]
        for ei, e in enumerate(eps, start=1):
            lic = licence
            if licence == "RVOD*":                      # NCIS: latest season RVOD, rest SVOD
                lic = "RVOD" if si == n_seasons else "SVOD"
            avail = None
            if slug == "the_voice" and si >= n_seasons - 1:
                # Last two seasons are the 2025 and 2026 runs; Thu + Sun drops from premiere.
                prem = date.fromisoformat(voice["CURRENT"] if si == n_seasons else voice["STLY"])
                avail = prem + timedelta(days=(ei - 1) // 2 * 7 + (3 if (ei - 1) % 2 else 0))
            playables.append(Playable(
                content_id=ep_id(slug, si, ei), title_id=f"joyn_{slug}", title=title,
                episode_title=e.title.strip()[:120] or f"Folge {ei}", season=si, episode=ei,
                content_type="VOD", genre=genre, licence_type=lic,
                duration_s=int(min(hi, max(lo, e.duration_s))), available_from=avail))
    title_lic = min({p.licence_type for p in playables}, key=LICENCE_RANK.get)
    t = Title(title_id=f"joyn_{slug}", title=title, content_type="VOD", asset_type=asset_type,
              genre=genre, licence_type=title_lic, playable_ids=[p.content_id for p in playables])
    return t, playables


def _live_title(slug: str, title: str, genre: str, licence: str, mixed: bool) -> tuple[Title, Playable]:
    tid = f"joyn_{slug}_live" if mixed else f"joyn_{slug}"
    name = f"{title} Live 24/7" if slug == "big_brother" else (f"{title} Live" if mixed else title)
    p = Playable(content_id=tid, title_id=tid, title=name, content_type="LIVE", genre=genre,
                 licence_type=licence, duration_s=None)
    return Title(title_id=tid, title=name, content_type="LIVE", asset_type="series", genre=genre,
                 licence_type=licence, playable_ids=[tid]), p


def _apply_extras(out: ExtrasOut, taken: set[str]) -> tuple[list[Title], list[Playable]]:
    titles, plays = [], []
    for it in out.items:
        slug = slugify(it.title)
        tid = f"joyn_{slug}"
        if not slug or tid in taken:
            continue
        taken.add(tid)
        lo, hi = DURATIONS[it.asset_type]
        plays.append(Playable(content_id=tid, title_id=tid, title=it.title, content_type="VOD",
                              genre=it.genre, licence_type=it.licence_type,
                              duration_s=int(min(hi, max(lo, it.duration_s)))))
        titles.append(Title(title_id=tid, title=it.title, content_type="VOD", asset_type=it.asset_type,
                            genre=it.genre, licence_type=it.licence_type, playable_ids=[tid]))
        if len(titles) >= 18:
            break
    return titles, plays


def _lanes_prompt(titles: list[Title]) -> str:
    lines = "\n".join(f"{t.title_id} | {t.title} | {t.genre} | {t.content_type} | {t.licence_type}"
                      for t in titles)
    return ("Design 12-18 home/browse lanes (rows of tiles) for Joyn's German app. Lane names in "
            "German. Exactly 2 hero_lane, 2-3 big_teaser, the rest standard_lane. Each lane lists "
            "10-16 title_ids chosen ONLY from the list below. At least one lane must be eligible for "
            "live_tv and contain the LIVE titles; at least one lane eligible for detail "
            "('Mehr wie das'); several genre lanes for category.\n"
            "title_id | title | genre | type | licence\n" + lines)


def _apply_lanes(out: LanesOut, titles: list[Title], seed: int) -> list[Lane]:
    """Reject invented ids, pad short lanes with same-genre titles, enforce 12-18 lanes."""
    known = {t.title_id: t for t in titles}
    lanes: list[Lane] = []
    used_ids: set[str] = set()
    rejected = 0
    for i, l in enumerate(out.lanes[:18]):
        ids = []
        for tid in l.title_ids:
            if tid in known and tid not in ids:
                ids.append(tid)
            elif tid not in known:
                rejected += 1
        screens = sorted({s for s in l.screens if s in LANE_SCREENS}) or ["home"]
        if len(ids) < 8:
            rng = rng_for(seed, "lane-pad", l.lane_name)
            genres = {known[t].genre for t in ids}
            pool = [t.title_id for t in titles if t.title_id not in ids and (t.genre in genres or not genres)]
            pool += [t.title_id for t in titles if t.title_id not in ids and t.title_id not in pool]
            rng.shuffle(pool)
            ids += pool[: 8 - len(ids)]
        lid = f"lane_{slugify(l.lane_name)}" or f"lane_{i}"
        while lid in used_ids:
            lid += "_2"
        used_ids.add(lid)
        lanes.append(Lane(lane_id=lid, lane_name=l.lane_name, lane_type=l.lane_type,
                          screens=screens, title_ids=ids[:20]))
    if rejected:
        log.warning("lanes: dropped %d invented title ids", rejected)
    _ensure_lane_coverage(lanes, titles)
    if len(lanes) < 12:
        raise ValueError(f"only {len(lanes)} lanes after validation")
    return lanes


def _ensure_lane_coverage(lanes: list[Lane], titles: list[Title]) -> None:
    """Every lane-capable screen needs lanes, home needs a hero lane, live_tv needs LIVE tiles."""
    live_ids = [t.title_id for t in titles if t.content_type == "LIVE"]
    if not any(l.lane_type == "hero_lane" and "home" in l.screens for l in lanes):
        lanes[0].lane_type, lanes[0].screens = "hero_lane", sorted(set(lanes[0].screens) | {"home"})
    if not any("live_tv" in l.screens and set(live_ids) & set(l.title_ids) for l in lanes):
        lanes.append(Lane(lane_id="lane_live_jetzt", lane_name="Jetzt live", lane_type="standard_lane",
                          screens=["home", "live_tv"],
                          title_ids=(live_ids + [t.title_id for t in titles if t.title_id not in live_ids])[:10]))
    for scr in LANE_SCREENS:
        if not any(scr in l.screens for l in lanes):
            lanes[-1].screens = sorted(set(lanes[-1].screens) | {scr})


# ----------------------------------------------------------------------------- offline fallback
_FALLBACK_EP_WORDS = ["Der Anfang", "Neue Wege", "Die Entscheidung", "Unter Druck", "Das Casting",
                      "Showdown", "Alte Freunde", "Die Wahrheit", "Zurück auf Los", "Das Finale",
                      "Grenzen", "Nachtschicht"]
_FALLBACK_EXTRAS = [
    ("Die Bergretter Spezial", "movie", "Drama", "AVOD"), ("Tatort Hamburg: Nachtfahrt", "movie", "Crime", "SVOD"),
    ("Lachen mit Folgen", "movie", "Comedy", "AVOD"), ("Bundesliga: Die besten Tore", "compilation", "Live Sports", "AVOD"),
    ("DFB-Pokal Highlights", "compilation", "Live Sports", "RVOD"), ("Promis unter Palmen: Best of", "compilation", "Reality TV", "AVOD"),
    ("Das große Backen Classics", "compilation", "Reality TV", "RVOD"), ("Kommissar Rex: Der Film", "movie", "Crime", "SVOD"),
    ("Spiegel TV Reportage", "compilation", "News", "AVOD"), ("Galileo Spezial", "compilation", "News", "AVOD"),
    ("Sommer in Bayern", "movie", "Drama", "SVOD"), ("Die Wochenshow Classics", "compilation", "Comedy", "AVOD"),
    ("Der Pass", "movie", "Crime", "SVOD"), ("Heldt: Fall 12", "movie", "Crime", "RVOD"),
    ("Ostfriesenkiller", "movie", "Crime", "AVOD"),
]
_FALLBACK_LANES = [
    ("Top-Highlights für dich", "hero_lane", ["home"]), ("Neu auf Joyn", "hero_lane", ["home", "category"]),
    ("Jetzt live", "big_teaser", ["home", "live_tv"]), ("Reality-Hits", "standard_lane", ["home", "category"]),
    ("Crime & Spannung", "standard_lane", ["home", "category"]), ("Comedy zum Abschalten", "standard_lane", ["home", "category"]),
    ("Sport & Fußball", "big_teaser", ["home", "category", "live_tv"]), ("Weiterschauen", "standard_lane", ["home"]),
    ("Mehr wie das", "standard_lane", ["detail"]), ("Beliebt bei Joyn PLUS+", "big_teaser", ["home", "category"]),
    ("Nachrichten & Magazine", "standard_lane", ["category", "live_tv"]), ("Beliebte Suchen", "standard_lane", ["search"]),
    ("Filme am Abend", "standard_lane", ["home", "category"]), ("Trending in Deutschland", "standard_lane", ["home", "detail"]),
]


def fallback_series(asset_type: str, genre: str, slug: str, seed: int, n_seasons: Optional[int] = None) -> SeriesOut:
    rng = rng_for(seed, "fallback-series", slug)
    lo, hi = DURATIONS["compilation" if asset_type == "compilation" else genre]
    ns = n_seasons or rng.randint(1, 3)
    return SeriesOut(seasons=[_Season(season=s, episodes=[
        _Ep(episode=e, title=f"{rng.choice(_FALLBACK_EP_WORDS)}", duration_s=rng.randint(lo, hi))
        for e in range(1, rng.randint(6, 12) + 1)]) for s in range(1, ns + 1)])


def fallback_extras(seed: int) -> ExtrasOut:
    rng = rng_for(seed, "fallback-extras")
    return ExtrasOut(items=[_Extra(title=t, asset_type=a, genre=g, licence_type=l,
                                   duration_s=rng.randint(*DURATIONS[a])) for t, a, g, l in _FALLBACK_EXTRAS])


def fallback_lanes(titles: list[Title], seed: int) -> LanesOut:
    rng = rng_for(seed, "fallback-lanes")
    out = []
    for name, lt, screens in _FALLBACK_LANES:
        ids = [t.title_id for t in titles]
        if "live" in name.lower():
            ids = [t.title_id for t in titles if t.content_type == "LIVE"] + \
                  [t.title_id for t in titles if t.content_type != "LIVE"]
        else:
            rng.shuffle(ids)
        out.append(_Lane(lane_name=name, lane_type=lt, screens=screens, title_ids=ids[: rng.randint(8, 16)]))
    return LanesOut(lanes=out)


# ----------------------------------------------------------------------------- stage runner
async def _cached_call(client: Optional[GeminiClient], raw_dir: Path, name: str, prompt: str,
                       schema: type[BaseModel], temp: float, fallback) -> BaseModel:
    """Return a cached response if present, else call Gemini (retry once), else fallback."""
    path = raw_dir / f"{name}.json"
    if path.exists():
        return schema.model_validate(read_json(path)["data"])
    if client is None:
        data = fallback()
        write_json(path, {"source": "fallback", "data": data.model_dump()})
        return data
    err = ""
    for attempt in range(2):
        res = await client.generate(prompt + (f"\nPrevious answer was invalid: {err}" if err else ""),
                                    schema, temp, SYSTEM)
        try:
            data = schema.model_validate(parse_json(res.text))
            write_json(path, {"source": "llm", "key": res.key_label, "data": data.model_dump()})
            return data
        except Exception as e:  # noqa: BLE001 — any parse/schema error triggers the one retry
            err = str(e)[:400]
    log.warning("catalog %s: LLM output invalid twice, using fallback", name)
    data = fallback()
    write_json(path, {"source": "fallback", "data": data.model_dump()})
    return data


async def build_catalog(cfg: Settings, client: Optional[GeminiClient]) -> Catalog:
    """Run the catalog stage (idempotent: cached raw responses are reused)."""
    raw_dir = cfg.cache / "catalog"
    raw_dir.mkdir(parents=True, exist_ok=True)
    temp = float(cfg["gemini"]["temperature_catalog"])
    titles: list[Title] = []
    plays: list[Playable] = []

    jobs = []
    for slug, title, ctype, atype, genre, vod_lic, live_lic in SEED:
        if "VOD" in ctype:
            lo, hi = DURATIONS["compilation" if atype == "compilation" else genre]
            seasons = "exactly 3" if slug == "the_voice" else "1 to 3"
            fb = (lambda a=atype, g=genre, s=slug: fallback_series(a, g, s, cfg.seed, 3 if s == "the_voice" else None))
            jobs.append((slug, _cached_call(client, raw_dir, f"series_{slug}",
                                            _series_prompt(title, genre, atype, lo, hi, seasons),
                                            SeriesOut, temp, fb)))
    results = await asyncio.gather(*(j for _, j in jobs))
    by_slug = dict(zip([s for s, _ in jobs], results))

    for slug, title, ctype, atype, genre, vod_lic, live_lic in SEED:
        if "VOD" in ctype:
            out = by_slug[slug]
            if slug == "the_voice" and len(out.seasons) < 3:
                out = fallback_series(atype, genre, slug, cfg.seed, 3)
            t, ps = _apply_series(slug, title, genre, atype, vod_lic, out, cfg)
            titles.append(t)
            plays += ps
        if "LIVE" in ctype:
            t, p = _live_title(slug, title, genre, live_lic, mixed="VOD" in ctype)
            titles.append(t)
            plays.append(p)

    extras_prompt = (f"List 15 extra titles for Joyn Germany: German movies and compilations spread "
                     f"across these genres: {', '.join(GENRES)}. Mix AVOD/RVOD/SVOD licences. Movies "
                     f"5400-7800s, compilations 300-900s. Do not repeat: "
                     + "; ".join(t.title for t in titles))
    extras = await _cached_call(client, raw_dir, "extras", extras_prompt, ExtrasOut, temp,
                                lambda: fallback_extras(cfg.seed))
    et, ep = _apply_extras(extras, {t.title_id for t in titles})
    if len(et) < 12:  # model gave too few usable items: top up from the fallback list
        mt, mp = _apply_extras(fallback_extras(cfg.seed), {t.title_id for t in titles + et})
        et, ep = et + mt[: 15 - len(et)], ep + mp[: 15 - len(et)]
    titles += et
    plays += ep

    lanes_out = await _cached_call(client, raw_dir, "lanes", _lanes_prompt(titles), LanesOut, temp,
                                   lambda: fallback_lanes(titles, cfg.seed))
    try:
        lanes = _apply_lanes(lanes_out, titles, cfg.seed)
    except ValueError as e:
        log.warning("lanes invalid (%s); using fallback lanes", e)
        lanes = _apply_lanes(fallback_lanes(titles, cfg.seed), titles, cfg.seed)

    cat = Catalog(titles=titles, playables=plays, lanes=lanes)
    write_json(cfg.cache / "catalog.json", cat.model_dump(mode="json"))
    log.info("catalog: %d titles, %d playables, %d lanes", len(titles), len(plays), len(lanes))
    return cat


def load_catalog(cfg: Settings) -> Catalog:
    p = cfg.cache / "catalog.json"
    if not p.exists():
        raise SystemExit("no catalog yet — run `python main.py catalog` first")
    return Catalog.model_validate(read_json(p))


# ----------------------------------------------------------------------------- spec premieres
async def extend_for_premieres(cfg: Settings, client: Optional[GeminiClient], cat: Catalog,
                               premieres: list[dict]) -> Catalog:
    """Add a new season for each premiere (title_id, date, episodes, cadence_days). Append-only:
    existing ids and release dates never change, so cached journeys stay valid. Idempotent."""
    raw_dir = cfg.cache / "catalog"
    raw_dir.mkdir(parents=True, exist_ok=True)
    titles = cat.title_map()
    changed = False
    for prem in premieres:
        tid = prem["title_id"]
        d = date.fromisoformat(str(prem["date"]))
        t = titles.get(tid)
        if t is None or t.content_type != "VOD" or t.asset_type == "movie":
            raise ValueError(f"premiere title {tid!r} must be an existing VOD series or compilation")
        own = [p for p in cat.playables if p.title_id == tid]
        if any(p.available_from == d for p in own):
            continue                                       # already added
        new_s = max(p.season or 1 for p in own) + 1
        latest = [p for p in own if p.season == new_s - 1]
        slug = tid.removeprefix("joyn_")
        n_eps = int(prem.get("episodes", 8))
        cadence = [int(x) for x in prem.get("cadence_days", [0, 3])]
        lo, hi = DURATIONS["compilation" if t.asset_type == "compilation" else t.genre]
        prompt = (f"Series: {t.title} (genre: {t.genre}). Produce exactly 1 season: the NEW season premiering "
                  f"{d:%d.%m.%Y}, with {n_eps} episodes numbered from 1. Give each a realistic German episode title "
                  f"(no numbering) and duration_s between {lo} and {hi} seconds.")
        out = await _cached_call(client, raw_dir, f"premiere_{slug}_s{new_s:02d}", prompt, SeriesOut,
                                 float(cfg["gemini"]["temperature_catalog"]),
                                 lambda: fallback_series(t.asset_type, t.genre, f"{slug}_s{new_s}", cfg.seed, 1))
        eps = list(out.seasons[0].episodes) if out.seasons else []
        if len(eps) < n_eps:
            eps += fallback_series(t.asset_type, t.genre, f"{slug}_pad", cfg.seed, 1).seasons[0].episodes
        lic = latest[0].licence_type if latest else t.licence_type
        for i, e in enumerate(eps[:n_eps]):
            avail = d + timedelta(days=(i // len(cadence)) * 7 + cadence[i % len(cadence)])
            p = Playable(content_id=ep_id(slug, new_s, i + 1), title_id=tid, title=t.title,
                         episode_title=e.title.strip()[:120] or f"Folge {i + 1}", season=new_s, episode=i + 1,
                         content_type="VOD", genre=t.genre, licence_type=lic,
                         duration_s=int(min(hi, max(lo, e.duration_s))), available_from=avail)
            cat.playables.append(p)
            t.playable_ids.append(p.content_id)
        changed = True
        log.info("catalog: %s season %d premieres %s (%d episodes)", tid, new_s, d, n_eps)
    if changed:
        write_json(cfg.cache / "catalog.json", cat.model_dump(mode="json"))
    return cat
