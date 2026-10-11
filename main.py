#!/usr/bin/env python3
"""Joyn synthetic dataset CLI.

  python main.py catalog                     # Gemini, ~10-20 calls (cached)
  python main.py personas                    # Python, seeded
  python main.py journeys [--resume] [--refresh u0001,u0002] [--limit N]
  python main.py expand
  python main.py export [--sheets SHEET_ID]
  python main.py validate
  python main.py all [--offline]             # everything; --offline = no Gemini (fallback generators)

Growing the dataset (additive; earlier data never changes):
  python main.py generate --spec specs/oct_2026.yaml [--offline] [--limit N]
  python main.py batches                     # list base + batches

Exit codes: 0 ok, 1 validation failed, 75 all keys exhausted / budget hit (resume later).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from zoneinfo import ZoneInfo

from src.catalog import build_catalog, load_catalog
from src.config import Settings, load_settings
from src.gemini_client import BudgetExceeded, GeminiClient
from src.keypool import AllKeysExhausted, KeyPool, load_keys_from_env

log = logging.getLogger("main")


def make_client(cfg: Settings) -> GeminiClient:
    g = cfg["gemini"]
    pool = KeyPool(load_keys_from_env(), rpm=g["rpm"], rpd=g["rpd"], overrides=g.get("per_key_overrides"),
                   reset_tz=g["daily_reset_tz"], state_path=cfg.logs / "keypool_state.json",
                   backoff_base=g["backoff_base_s"], backoff_max=g["backoff_max_s"], seed=cfg.seed)
    log.info("Gemini: model=%s keys=%s", cfg.model, [k.label for k in pool.keys])
    return GeminiClient(pool, cfg.model, g, cfg.logs / "gemini_usage.json")


def cmd_catalog(cfg: Settings, offline: bool) -> None:
    asyncio.run(build_catalog(cfg, None if offline else make_client(cfg)))


def cmd_personas(cfg: Settings) -> None:
    from src.personas import build_personas, save_personas
    briefs = build_personas(cfg)
    save_personas(cfg, briefs)
    log.info("personas: %d users", len(briefs))


def cmd_journeys(cfg: Settings, offline: bool, refresh: set[str], resume: bool, limit: int | None = None,
                 only: str | None = None) -> None:
    """Journeys for the base and every batch (or only one batch); cached, fresh user-months are skipped."""
    from src.batches import Registry
    from src.journeys import run_journeys
    if resume:
        log.info("resuming: cached user-months are skipped")
    reg = Registry.load(cfg)
    cat = load_catalog(cfg)
    client = None if offline else make_client(cfg)

    async def run_all() -> None:
        left = limit
        for bt in reg.batches:
            if only and bt.run_id != only:
                continue
            cfgv, views = reg.batch_view(bt)
            res = await run_journeys(cfgv, cat, views, client, refresh=refresh, offline=offline, limit=left)
            if left is not None:
                left = max(0, left - res["generated"])
    asyncio.run(run_all())


def cmd_expand(cfg: Settings) -> None:
    from src.batches import Registry
    from src.expand import expand_all
    expand_all(Registry.load(cfg), load_catalog(cfg))


def cmd_generate(cfg: Settings, spec_path: str, offline: bool, limit: int | None, refresh: set[str]) -> int:
    """Register a batch from a spec (idempotent), generate its journeys, then expand/export/validate all."""
    from src.batches import Registry, build_batch, load_spec, register_batch
    from src.catalog import extend_for_premieres
    spec = load_spec(spec_path)
    reg = Registry.load(cfg)
    known = {bt.run_id: bt for bt in reg.batches}
    if spec["run_id"] in known:
        if known[spec["run_id"]].spec != spec:
            sys.exit(f"batch {spec['run_id']!r} already exists with a different spec; use a new run_id")
        log.info("batch %s already registered; resuming", spec["run_id"])
    else:
        new, ext = build_batch(reg, spec)
        register_batch(cfg, spec, new, ext)
        log.info("batch %s: %d new users, %d returning users", spec["run_id"], len(new), len(ext))
    prem = spec["stories"].get("premieres") or []
    if prem:
        asyncio.run(extend_for_premieres(cfg, None if offline else make_client(cfg), load_catalog(cfg), prem))
    cmd_journeys(cfg, offline, refresh, True, limit, only=spec["run_id"])
    if limit is not None:
        print("--limit given: stopped after journeys (smoke test). Rerun without --limit to finish.")
        return 0
    cmd_expand(cfg)
    cmd_export(cfg, None)
    return 0 if cmd_validate(cfg) else 1


def cmd_batches(cfg: Settings) -> None:
    from src.batches import Registry
    from src.journeys import cache_path
    reg = Registry.load(cfg)
    print(f"{'batch':<14}{'periods':<34}{'new users':>10}{'returning':>10}{'user-months':>12}{'journeys':>10}")
    for bt in reg.batches:
        ret = sum(len(ps) for u, ps in bt.owned.items() if u not in set(bt.new_users))
        have = sum(1 for u, ps in bt.owned.items() for p in ps if cache_path(cfg, u, p).exists())
        print(f"{bt.run_id:<14}{', '.join(bt.periods):<34}{len(bt.new_users):>10}{ret:>10}"
              f"{bt.user_months:>12}{have:>6}/{bt.user_months}")


def cmd_export(cfg: Settings, sheets: str | None) -> None:
    from src.batches import Registry
    from src.exporters import bigquery, channels, dirty, flat, mixpanel, posthog, sheets as sh, tables
    from src.identity import account_users
    from src.marketing import load_acquisitions
    out = cfg.out
    ev = out / "events.ndjson"
    if not ev.exists():
        sys.exit("no events.ndjson — run `python main.py expand` first")
    reg = Registry.load(cfg)
    briefs = list(reg.briefs.values())
    acq = load_acquisitions(out)
    person_of = {d.anon_id: b.user_id for b in briefs for d in b.devices} | {b.user_id: b.user_id for b in briefs}
    idents = account_users(briefs)
    mp_mode = cfg.get("exports", {}).get("mixpanel_id_merge", "simplified")
    tables.write_users(briefs, out / "users.csv", acq,
                       periods=sorted(reg.periods, key=lambda p: str(reg.periods[p]["start"])), identities=idents)
    tables.write_catalog(load_catalog(cfg), out / "content_catalog.csv")
    log.info("flat csv: %d rows", flat.write_flat(ev, out / "events_flat.csv"))
    # Upload files per batch: base at the top level (unchanged once uploaded), each later batch in its
    # own sub-folder, so the uploaders only ever send what is new.
    import shutil
    runs = [bt.run_id for bt in reg.batches]
    for top in ("posthog_batch", "mixpanel_import"):
        for d in (out / top).glob("*/"):
            if d.is_dir() and d.name not in runs and not d.name.startswith("."):
                shutil.rmtree(d)
    for bt in reg.batches:
        bev = out / "batches" / bt.run_id / "events.ndjson"
        sub = "" if bt.run_id == "base" else bt.run_id
        n_ph = posthog.write_posthog(bev, out / "posthog_batch" / sub, person_of=person_of,
                                     acquisitions=acq, identities=idents)
        n_mp = mixpanel.write_mixpanel(bev, out / "mixpanel_import" / sub, mode=mp_mode)
        log.info("batch %s: posthog %d files, mixpanel (%s ID merge) %d files", bt.run_id, n_ph, mp_mode, n_mp)
    log.info("mixpanel profiles: %d", mixpanel.write_mixpanel_profiles(out / "mixpanel_profiles.jsonl", briefs, acq,
                                                                       idents, mp_mode))
    log.info("daily channel rows: %d", channels.write_daily_channels(
        ev, out / "marketing" / "marketing_spend_daily.csv", acq,
        out / "marketing" / "daily_channel_aggregates.csv", cfg["timezone"]))
    bigquery.write_bigquery(ev, out / "bigquery")
    n = dirty.write_dirty(ev, out / "events_dirty.ndjson", out / "dirty_manifest.csv",
                          float(cfg["simulation"]["dirty_rate"]), cfg.seed, cfg["timezone"])
    log.info("dirty: %d rows altered", n)
    rows = sh.daily_aggregates(ev, cfg["timezone"])
    sh.write_daily(rows, out / "daily_aggregates.csv")
    if sheets:
        sh.push_to_sheets(rows, sheets)
        log.info("sheets: %d daily rows pushed", len(rows))


def cmd_validate(cfg: Settings) -> bool:
    from src.validate import run_validation
    ok, rep = run_validation(cfg)
    for name, val, exp, status in rep["checks"]:
        print(f"{status:4}  {name}: {val}  (expected {exp})")
    print(f"\nreport: {cfg.out / 'validation_report.md'}  ->  {'PASSED' if ok else 'FAILED'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["catalog", "personas", "journeys", "expand", "export", "validate", "all",
                                        "generate", "batches"])
    ap.add_argument("--spec", default=None, help="generate: batch spec YAML (see specs/README.md)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--offline", action="store_true", help="no Gemini calls; use Python fallbacks")
    ap.add_argument("--resume", action="store_true", help="continue an interrupted journeys run")
    ap.add_argument("--refresh", default="", help="comma-separated user ids to regenerate")
    ap.add_argument("--sheets", default=None, metavar="SHEET_ID")
    ap.add_argument("--limit", type=int, default=None, help="journeys: only generate N user-months (smoke test)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_settings(a.config)
    if a.offline:
        # keep fallback-only output away from the real cache, or a later live run would reuse it
        cfg.raw["paths"] = {k: f"{v}_offline" for k, v in cfg.raw["paths"].items()}
    refresh = {u.strip() for u in a.refresh.split(",") if u.strip()}
    try:
        if a.command == "generate":
            if not a.spec:
                sys.exit("generate needs --spec FILE")
            return cmd_generate(cfg, a.spec, a.offline, a.limit, refresh)
        if a.command == "batches":
            cmd_batches(cfg)
            return 0
        if a.command in ("catalog", "all"):
            cmd_catalog(cfg, a.offline)
        if a.command in ("personas", "all"):
            cmd_personas(cfg)
        if a.command in ("journeys", "all"):
            cmd_journeys(cfg, a.offline, refresh, a.resume, a.limit)
        if a.command in ("expand", "all"):
            cmd_expand(cfg)
        if a.command in ("export", "all"):
            cmd_export(cfg, a.sheets)
        if a.command in ("validate", "all"):
            return 0 if cmd_validate(cfg) else 1
    except ValueError as e:
        from src.batches import SpecError
        if not isinstance(e, SpecError):
            raise
        print(f"spec error: {e}")
        return 2
    except AllKeysExhausted as e:
        local = e.resume_at.astimezone(ZoneInfo(cfg["timezone"]))
        print(f"\nAll Gemini keys are out of daily quota. Progress is cached.\n"
              f"Resume after {local:%Y-%m-%d %H:%M} (Europe/Berlin) with: python main.py journeys --resume")
        return 75
    except BudgetExceeded as e:
        print(f"\nStopped by the cost guard: {e}\nRaise gemini.max_output_tokens_total and rerun with --resume.")
        return 75
    return 0


if __name__ == "__main__":
    sys.exit(main())
