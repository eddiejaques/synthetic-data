"""Run validation: everything is recomputed from out/events.ndjson, never from internal counters.

Writes out/validation_report.md and returns (passed, report_dict). Checks marked WARN are reported
but do not fail the run (story 2 is steered through the LLM, so Python cannot force it exactly).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Settings, read_json
from .expand import target_hour_share

RANK = {"anonymous": 0, "registered": 1, "subscribed": 2}


def _local(ts: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []   # name, value, expected, status

    def add(self, name: str, ok: Optional[bool], value, expected: str, warn_only: bool = False) -> None:
        status = "PASS" if ok else ("WARN" if warn_only or ok is None else "FAIL")
        self.rows.append((name, str(value), expected, status))

    @property
    def failed(self) -> list[str]:
        return [r[0] for r in self.rows if r[3] == "FAIL"]


def run_validation(cfg: Settings, events_path: Optional[Path] = None, reg=None) -> tuple[bool, dict]:
    """Validate the merged dataset against the registry (base + every batch)."""
    from .batches import Registry
    reg = reg or Registry.load(cfg)
    root_cfg = cfg
    cfg = reg.combined_cfg()
    tz = ZoneInfo(cfg["timezone"])
    events_path = events_path or root_cfg.out / "events.ndjson"
    owner = reg.owner()
    uid_of = {d.anon_id: u for u, b in reg.briefs.items() for d in b.devices}
    expected_active = {p: {u for u, b in reg.briefs.items() if p in b.periods} for p in reg.periods}
    periods = {p: (cfg.period_days(p)[0], cfg.period_days(p)[-1]) for p in cfg.period_names}

    def period_of(d: date) -> Optional[str]:
        for p, (a, b) in periods.items():
            if a <= d <= b:
                return p
        return None

    # pass 1: identity map from $identify
    anon_to_user: dict[str, str] = {}
    with open(events_path) as f:
        for line in f:
            if '"$identify"' in line:
                e = json.loads(line)
                if e["event"] == "$identify":
                    anon_to_user[e["$anon_distinct_id"]] = e["distinct_id"]

    rows = 0
    insert_ids: set[str] = set()
    dup_ids = 0
    outside = 0
    active: dict[str, set] = defaultdict(set)
    video: dict[str, set] = defaultdict(set)
    max_seg: dict[str, int] = {}
    first_seen: dict[str, tuple[str, str]] = {}
    violations: Counter = Counter()
    examples: dict[str, str] = {}
    sess_last_ts: dict[str, str] = {}
    sess_prev_screen: dict[str, Optional[str]] = {}
    sess_lanes: dict[str, dict] = {}
    session_start_hours: Counter = Counter()
    pb_state: dict[str, tuple[int, str, str, str]] = {}   # psid -> (watch, platform, period, title_id)
    pb_week: dict[str, int] = {}
    pb_date: dict[str, date] = {}
    reg_count: Counter = Counter()
    sub_count: Counter = Counter()
    pay_day = defaultdict(lambda: [0, 0])                 # local date -> [timeouts, attempts]
    lane_imp: Counter = Counter()
    lane_clk: Counter = Counter()
    pos_imp: Counter = Counter()
    pos_clk: Counter = Counter()
    hourly_events: Counter = Counter()
    seg_by_batch: dict[tuple[str, str], int] = {}                  # (user, batch) -> highest tier
    first_by_batch: dict[tuple[str, str], tuple[str, str]] = {}    # (user, batch) -> (ts, platform)

    def bad(kind: str, e: dict) -> None:
        violations[kind] += 1
        examples.setdefault(kind, e.get("$insert_id", "?"))

    with open(events_path) as f:
        for line in f:
            e = json.loads(line)
            rows += 1
            iid = e["$insert_id"]
            if iid in insert_ids:
                dup_ids += 1
            insert_ids.add(iid)
            person = e["user_id"] or anon_to_user.get(e["anonymous_id"], e["anonymous_id"])
            lt = _local(e["timestamp"], tz)
            p = period_of(lt.date())
            if p is None:
                outside += 1
                continue
            hourly_events[lt.hour] += 1
            active[p].add(person)
            seg = e["user_segment"]
            max_seg[person] = max(max_seg.get(person, 0), RANK[seg])
            if e["event_source"] == "client" and (person not in first_seen or e["timestamp"] < first_seen[person][0]):
                first_seen[person] = (e["timestamp"], e["platform"])
            u_real = e["user_id"] or uid_of.get(e["anonymous_id"])
            ob = owner.get((u_real, p))
            if ob:
                k = (u_real, ob)
                seg_by_batch[k] = max(seg_by_batch.get(k, 0), RANK[seg])
                if e["event_source"] == "client" and (k not in first_by_batch or e["timestamp"] < first_by_batch[k][0]):
                    first_by_batch[k] = (e["timestamp"], e["platform"])
            ev = e["event"]

            # session ordering + navigation
            sid = e["session_id"]
            if sid:
                if sid not in sess_last_ts:
                    session_start_hours[lt.hour] += 1
                elif e["timestamp"] <= sess_last_ts[sid]:
                    bad("timestamps not strictly increasing within session", e)
                sess_last_ts[sid] = e["timestamp"]
                if ev == "screen_viewed":
                    if e["previous_screen"] != sess_prev_screen.get(sid):
                        bad("previous_screen != actual prior screen", e)
                    sess_prev_screen[sid] = e["screen_name"]
                    sess_lanes[sid] = {}
                elif ev == "lane_viewed":
                    sess_lanes.setdefault(sid, {})[e["lane_id"]] = e
                    lane_imp[e["lane_type"]] += 1
                    for a in e["assets_list"]:
                        if a["asset_position"] <= 10:
                            pos_imp[a["asset_position"]] += 1
                elif ev == "asset_clicked":
                    lv = sess_lanes.get(sid, {}).get(e["lane_id"])
                    if lv is None or lv["lane_position"] != e["lane_position"]:
                        bad("asset_clicked without matching lane_viewed", e)
                    elif not any(a["content_id"] == e["content_id"] and a["asset_position"] == e["asset_position"]
                                 for a in lv["assets_list"]):
                        bad("clicked asset not in lane assets_list at that position", e)
                    lane_clk[e["lane_type"]] += 1
                    if e["asset_position"] <= 10:
                        pos_clk[e["asset_position"]] += 1

            # rules
            if seg == "anonymous" and ev in ("login", "subscribed", "payment_received", "payment_failed"):
                bad(f"anonymous user fired {ev}", e)
            if ev == "registration":
                reg_count[person] += 1
            if ev == "subscribed":
                sub_count[person] += 1
            if ev in ("payment_received", "payment_failed"):
                d = pay_day[lt.date()]
                d[1] += 1
                if ev == "payment_failed" and e["failure_reason"] == "gateway_timeout":
                    d[0] += 1
            if ev == "video_view":
                video[p].add(person)
                lic = e["licence_type"]
                if lic == "RVOD" and seg == "anonymous":
                    bad("RVOD played by anonymous user", e)
                if lic == "SVOD" and seg != "subscribed":
                    bad("SVOD played without subscription", e)
                if e["content_type"] == "LIVE" and e["completion_rate"] is not None:
                    bad("completion_rate set for LIVE", e)
                if e["playback_event"] == "complete" and (e["completion_rate"] is None or e["completion_rate"] < 0.9):
                    bad("complete with completion_rate < 0.9", e)
                psid = e["playback_session_id"]
                prev = pb_state.get(psid)
                if prev is None and e["playback_event"] != "start":
                    bad("playback does not begin with start", e)
                if prev is not None and e["watch_time_seconds"] < prev[0]:
                    bad("watch_time_seconds decreased", e)
                pb_state[psid] = (e["watch_time_seconds"], e["platform"], p, e["title_id"])
                pb_week[psid] = min(4, (lt.day - 1) // 7 + 1)
                pb_date[psid] = lt.date()

    for person, n in reg_count.items():
        if n > 1:
            violations["registration more than once"] += 1
    for person, n in sub_count.items():
        if n > 1:
            violations["subscribed more than once"] += 1

    C = Check()
    V = cfg["validation"]
    names = list(reg.periods)
    users = set().union(*active.values()) if active else set()
    C.add("distinct users", len(users) == len(reg.briefs), len(users), f"= {len(reg.briefs)}")
    mau = {p: len(active[p]) for p in names}
    mvu = {p: len(video[p]) for p in names}

    # expected MVU per period: each batch picks round(active x its ratio) of the users it owns there
    exp_mvu: Counter = Counter()
    views = {bt.run_id: reg.batch_view(bt) for bt in reg.batches}
    for bt in reg.batches:
        cfg_b, _ = views[bt.run_id]
        for p in bt.periods:
            n = sum(1 for ps in bt.owned.values() if p in ps)
            if n:
                exp_mvu[p] += round(n * float(cfg_b["users"]["mvu_ratio"]))
    seen_before: set = set()
    seen_exp: set = set()
    active_u = {p: {uid_of.get(x, x) for x in active[p]} for p in names}     # persons -> user ids
    for p in sorted(names, key=lambda x: str(reg.periods[x]["start"])):
        exp = len(expected_active[p])
        C.add(f"MAU {p}", mau[p] == exp, mau[p], f"= {exp}")
        r = mvu[p] / mau[p] if mau[p] else 0
        er = exp_mvu[p] / exp if exp else 0
        C.add(f"MVU {p} (MVU/MAU)", abs(r - er) <= V["mvu_ratio_tol"], f"{mvu[p]} ({r:.3f})",
              f"{er:.2f} ± {V['mvu_ratio_tol']}")
        ret_exp = len(expected_active[p] & seen_exp)
        ret = len(active_u[p] & seen_before)
        C.add(f"returning users {p}", ret == ret_exp, ret, f"= {ret_exp} (active in an earlier period)")
        seen_before |= active_u[p]
        seen_exp |= expected_active[p]
    if {"STLY", "CURRENT"} <= set(names):
        yoy = mvu["CURRENT"] / mvu["STLY"] - 1 if mvu["STLY"] else 0
        exp_yoy = exp_mvu["CURRENT"] / exp_mvu["STLY"] - 1 if exp_mvu["STLY"] else 0
        C.add("MVU YoY (STLY -> CURRENT)", abs(yoy - exp_yoy) <= V["mvu_yoy"]["tol"], f"{yoy:+.1%}",
              f"{exp_yoy:+.0%} ± {V['mvu_yoy']['tol'] * 100:.0f}pp")
    tol = float(V["split_tol_pp"])
    seg_names = list(RANK)
    for bt in reg.batches:
        cfg_b, _ = views[bt.run_id]
        cohort = set(bt.new_users)
        label = "" if bt.run_id == "base" else f" [{bt.run_id} new users]"
        if not cohort:
            continue
        segs = Counter(seg_names[v] for (u, b), v in seg_by_batch.items() if b == bt.run_id and u in cohort)
        plats = Counter(v[1] for (u, b), v in first_by_batch.items() if b == bt.run_id and u in cohort)
        for sname, share in cfg_b["segments"].items():
            got = segs[sname] / len(cohort) * 100
            C.add(f"segment {sname}{label}", abs(got - share * 100) <= tol, f"{got:.1f}%", f"{share * 100:.0f}% ± {tol}pp")
        for pl, share in cfg_b["platforms"].items():
            got = plats[pl] / len(cohort) * 100
            C.add(f"platform {pl}{label}", abs(got - share * 100) <= tol, f"{got:.1f}%", f"{share * 100:.0f}% ± {tol}pp")
    lo = sum(int(views[bt.run_id][0]["simulation"]["target_rows"][0]) for bt in reg.batches)
    hi = sum(int(views[bt.run_id][0]["simulation"]["target_rows"][1]) for bt in reg.batches)
    cap = sum(int(views[bt.run_id][0]["simulation"]["max_row_cap"]) for bt in reg.batches)
    C.add("row count", lo <= rows <= hi and rows <= cap, f"{rows:,}", f"{lo:,}-{hi:,}")
    C.add("duplicate $insert_id", dup_ids == 0, dup_ids, "0")
    C.add("events outside periods", outside == 0, outside, "0")
    C.add("rule violations", sum(violations.values()) == 0, sum(violations.values()), "0")

    # hourly curve (session starts)
    target = target_hour_share(cfg)
    n_s = sum(session_start_hours.values()) or 1
    l1 = sum(abs(session_start_hours[h] / n_s - target[h]) for h in range(24))
    C.add("hourly curve L1 (session starts)", l1 <= cfg["diurnal"]["tolerance_l1"], f"{l1:.3f}",
          f"<= {cfg['diurnal']['tolerance_l1']}")

    # story 1: ott share of watch time between configured period pairs
    watch = defaultdict(float)
    for psid, (w, plat, p, tid) in pb_state.items():
        watch[(p, plat)] += w
        watch[(p, "all")] += w
    share = {p: watch[(p, "ott")] / watch[(p, "all")] if watch[(p, "all")] else 0 for p in names}
    web = {p: watch[(p, "web")] / watch[(p, "all")] if watch[(p, "all")] else 0 for p in names}
    s1 = cfg["stories"]["ott_watch_share_pp"]
    pairs = [("STLY", "CURRENT")] if {"STLY", "CURRENT"} <= set(names) else []
    pairs += [tuple(bt.spec["stories"]["ott_pair"]) for bt in reg.batches
              if bt.spec and bt.spec["stories"].get("ott_pair")]
    for a, b in dict.fromkeys(pairs):
        d_pp = (share[b] - share[a]) * 100
        C.add(f"story 1: ott share of watch time {a} -> {b}", abs(d_pp - s1["target"]) <= s1["tol"],
              f"{share[a]:.1%} -> {share[b]:.1%} ({d_pp:+.1f}pp); web {web[a]:.1%} -> {web[b]:.1%}",
              f"+{s1['target']} ± {s1['tol']}pp, web declining")
        C.add(f"story 1: web share declining {a} -> {b}", web[b] < web[a], f"{(web[b] - web[a]) * 100:+.1f}pp", "< 0")

    # story 2: The Voice (base) and spec premieres
    if {"STLY", "CURRENT"} <= set(names):
        vw = defaultdict(float)
        for psid, (w, plat, p, tid) in pb_state.items():
            if tid == "joyn_the_voice":
                vw[(p, pb_week[psid])] += w
        s2 = cfg["stories"]["voice_premiere"]["week3_uplift"]
        for p in ("STLY", "CURRENT"):
            base = (vw[(p, 1)] + vw[(p, 2)]) / 2
            up = vw[(p, 3)] / base if base else float("inf")
            lo2, hi2 = s2[p].get("min", 0), s2[p].get("max", float("inf"))
            C.add(f"story 2: The Voice week-3 uplift {p}", lo2 <= up <= hi2,
                  f"{up:.2f}x (wk1-4 hrs: " + ", ".join(f"{vw[(p, w)] / 3600:.0f}" for w in range(1, 5)) + ")",
                  f">= {lo2}" + (f", <= {hi2}" if hi2 != float("inf") else ""), warn_only=True)
    for prem in cfg["stories"].get("premieres") or []:
        d0 = date.fromisoformat(str(prem["date"]))
        p = period_of(d0)
        if p is None:
            continue
        pstart = cfg.period_days(p)[0]
        weeks_before = (d0 - pstart).days // 7
        launch = sum(w for psid, (w, _, pp, tid) in pb_state.items()
                     if tid == prem["title_id"] and 0 <= (pb_date[psid] - d0).days < 7)
        before = sum(w for psid, (w, _, pp, tid) in pb_state.items()
                     if tid == prem["title_id"] and pp == p and pb_date[psid] < d0
                     and (d0 - pb_date[psid]).days <= weeks_before * 7)
        want = float(prem.get("uplift_min", 1.5))
        if weeks_before < 1:
            C.add(f"story: {prem['title_id']} premiere {d0}", None, f"launch week {launch / 3600:.0f} h",
                  "needs >= 1 full week of the period before the premiere to measure", warn_only=True)
            continue
        up = launch / (before / weeks_before) if before else float("inf")
        C.add(f"story: {prem['title_id']} premiere week uplift {d0}", up >= want,
              f"{up:.2f}x ({launch / 3600:.0f} h vs {before / weeks_before / 3600:.0f} h/week before)",
              f">= {want}", warn_only=True)

    # story 3: every payment incident
    for inc in cfg.payment_incidents():
        inc_day = date.fromisoformat(str(inc["date"]))
        p_inc = period_of(inc_day)
        if p_inc is None:
            continue
        inc_days = {date.fromisoformat(str(i["date"])) for i in cfg.payment_incidents()}
        base_t = sum(v[0] for d, v in pay_day.items() if period_of(d) == p_inc and d not in inc_days)
        base_a = sum(v[1] for d, v in pay_day.items() if period_of(d) == p_inc and d not in inc_days)
        it, ia = pay_day[inc_day]
        r0 = base_t / base_a if base_a else 0
        r1 = it / ia if ia else 0
        mult = r1 / r0 if r0 else float("inf")
        days_n = max(1, len(cfg.period_days(p_inc)) - 1)
        C.add(f"story 3: gateway_timeout rate on {inc_day} vs baseline",
              abs(mult - float(inc["multiplier"])) <= float(inc["tol"]) and it >= int(inc["min_count"]),
              f"{mult:.2f}x (rate {r1:.0%} vs {r0:.1%}; count {it} vs {base_t / days_n:.2f}/day)",
              f"{inc['multiplier']} ± {inc['tol']}, >= {inc['min_count']} events")

    # story 4
    ctr = {k: lane_clk[k] / lane_imp[k] if lane_imp[k] else 0 for k in ("hero_lane", "big_teaser", "standard_lane")}
    s4 = cfg["stories"]["lane_ctr"]
    ratio = ctr["hero_lane"] / ctr["standard_lane"] if ctr["standard_lane"] else 0
    C.add("story 4: hero CTR / standard CTR", abs(ratio - s4["hero_vs_standard"]) <= s4["tol"],
          f"{ratio:.2f} (hero {ctr['hero_lane']:.1%}, teaser {ctr['big_teaser']:.1%}, standard {ctr['standard_lane']:.1%})",
          f"{s4['hero_vs_standard']} ± {s4['tol']}")
    pos_ctr = [pos_clk[k] / pos_imp[k] if pos_imp[k] else 0 for k in range(1, 11)]
    decays = all(pos_ctr[i] >= pos_ctr[i + 1] * 0.9 for i in range(5)) and pos_ctr[0] > pos_ctr[4]
    C.add("story 4: CTR decays with asset_position", decays,
          " ".join(f"{x:.1%}" for x in pos_ctr[:8]), "monotone-ish decline")

    # LLM stats
    usage = read_json(root_cfg.logs / "gemini_usage.json") if (root_cfg.logs / "gemini_usage.json").exists() else {}
    sources = Counter()
    retries = 0
    jdir = root_cfg.cache / "journeys"
    for fp in jdir.glob("*.json"):
        r = read_json(fp)
        sources[r.get("source", "?")] += 1
        retries += max(0, int(r.get("attempts", 1)) - 1)
    meta = read_json(root_cfg.out / "expand_meta.json") if (root_cfg.out / "expand_meta.json").exists() else {}
    metas = list(meta.get("batches", {}).values()) or [meta]
    fb_exp = sum(m.get("journey_sources", {}).get("fallback_at_expand", 0) for m in metas)
    total_j = sum(sources.values()) or 1
    fb_share = (sources["fallback"] + fb_exp) / total_j
    C.add("journeys from fallback", fb_share <= V["fallback_share_flag"], f"{fb_share:.1%}",
          f"<= {V['fallback_share_flag']:.0%} (else data is mostly Markov, not LLM-shaped)", warn_only=True)

    mkt = marketing_checks(cfg, C, events_path, root_cfg.out)
    batches = [{"run_id": bt.run_id, "periods": ", ".join(bt.periods), "new_users": len(bt.new_users),
                "returning_user_months": sum(len(ps) for u, ps in bt.owned.items() if u not in set(bt.new_users)),
                "user_months": bt.user_months, "rows": (metas_by.get(bt.run_id) or {}).get("rows")}
               for bt in reg.batches for metas_by in [meta.get("batches", {})]]

    report = {"checks": C.rows, "batches": batches, "violations": dict(violations), "violation_examples": examples, "marketing": mkt,
              "llm": {"sources": dict(sources), "retries": retries, "usage": usage},
              "hourly": {"session_start_share": {h: round(session_start_hours[h] / n_s, 4) for h in range(24)},
                         "target": [round(x, 4) for x in target]}}
    _write_md(root_cfg.out / "validation_report.md", C, report, rows)
    return not C.failed, report


def _write_md(path: Path, C: Check, rep: dict, rows: int) -> None:
    L = ["# Validation report", "", f"Recomputed from `events.ndjson` ({rows:,} rows).", "",
         "| Check | Value | Expected | Status |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {'✅' if d == 'PASS' else ('⚠️ WARN' if d == 'WARN' else '❌ FAIL')} |" for a, b, c, d in C.rows]
    L += ["", f"**Result: {'PASSED' if not C.failed else 'FAILED: ' + ', '.join(C.failed)}**", ""]
    if rep.get("batches"):
        L += ["## Batches", "", "| Batch | Periods | New users | Returning user-months | User-months | Rows |",
              "|---|---|---|---|---|---|"]
        L += [f"| {b['run_id']} | {b['periods']} | {b['new_users']:,} | {b['returning_user_months']:,} | "
              f"{b['user_months']:,} | {b['rows'] if b['rows'] is None else format(b['rows'], ',')} |" for b in rep["batches"]]
        L.append("")
    if rep["violations"]:
        L += ["## Rule violations", "", "| Rule | Count | Example $insert_id |", "|---|---|---|"]
        L += [f"| {k} | {v} | `{rep['violation_examples'].get(k, '')}` |" for k, v in rep["violations"].items()]
        L.append("")
    if rep.get("marketing"):
        mk = rep["marketing"]
        L += ["## Marketing", "", "| Session channel | Sessions |", "|---|---|"]
        L += [f"| {k} | {v:,} |" for k, v in sorted(mk["session_channels"].items(), key=lambda x: -x[1])]
        L += ["", "| First-touch channel | " + " | ".join(mk["new_user_mix"]) + " |",
              "|---|" + "---|" * len(mk["new_user_mix"])]
        chans = sorted({c for v in mk["new_user_mix"].values() for c in v})
        L += [f"| {c} | " + " | ".join(str(v.get(c, 0)) for v in mk["new_user_mix"].values()) + " |" for c in chans]
        L.append("")
    L += ["## Hourly curve (local time, share of session starts)", "", "| Hour | Actual | Target |", "|---|---|---|"]
    L += [f"| {h:02d} | {rep['hourly']['session_start_share'][h]:.1%} | {rep['hourly']['target'][h]:.1%} |" for h in range(24)]
    llm = rep["llm"]
    L += ["", "## LLM stats", "", f"- Journey sources: {llm['sources']}", f"- Journeys that needed a retry: {llm['retries']}"]
    u = llm.get("usage") or {}
    if u:
        t = u.get("totals", {})
        L += [f"- Model: `{u.get('model')}`; calls {t.get('calls', 0):,}; input tokens {t.get('input_tokens', 0):,}; "
              f"output tokens {t.get('output_tokens', 0):,}; transport retries {t.get('transport_retries', 0)}", "",
              "| Key | Calls | OK | 429 | Daily exhausted | Errors | In tokens | Out tokens |", "|---|---|---|---|---|---|---|---|"]
        for k, s in u.get("per_key", {}).items():
            L.append(f"| {k} | {s['calls']} | {s['ok']} | {s['rate_limited']} | {s['daily_exhausted']} | {s['errors']} | "
                     f"{s['input_tokens']:,} | {s['output_tokens']:,} |")
    else:
        L.append("- No Gemini usage recorded (offline run).")
    path.write_text("\n".join(L) + "\n")


# ----------------------------------------------------------------------------- marketing
def marketing_checks(cfg: Settings, C: Check, events_path: Path, out_root: Optional[Path] = None) -> dict:
    """Consistency of UTMs/click ids/spend and the planted marketing stories M1-M6."""
    import csv
    from .marketing import channel_for

    mdir = (out_root or cfg.out) / "marketing"
    if not (mdir / "campaigns.csv").exists():
        C.add("marketing tables present", False, "missing", "out/marketing/*.csv")
        return {}
    rd = lambda n: list(csv.DictReader(open(mdir / n)))  # noqa: E731
    campaigns = {r["utm_campaign"]: r for r in rd("campaigns.csv")}
    spend = rd("marketing_spend_daily.csv")
    acq = rd("user_acquisition.csv")
    tz = ZoneInfo(cfg["timezone"])
    base_pair = {"STLY", "CURRENT"} <= set(cfg.period_names)
    stly, cur = ("STLY", "CURRENT") if base_pair else (None, None)
    cur_days = cfg.period_days(cur) if base_pair else []

    bad = Counter()
    sess: dict[str, dict] = {}
    failed_before: dict[str, str] = {}
    tiktok = Counter()
    with open(events_path) as f:
        for line in f:
            e = json.loads(line)
            if e["event"] == "payment_failed" and e["user_id"]:
                failed_before.setdefault(e["user_id"], e["timestamp"])
            if e["event_source"] == "server":
                continue
            sid = e["session_id"]
            s = sess.get(sid)
            if s is None:
                lt = _local(e["timestamp"], tz)
                s = sess[sid] = {"date": lt.date(), "platform": e["platform"], "camp": e.get("utm_campaign"),
                                 "src": e.get("utm_source"), "med": e.get("utm_medium"),
                                 "content": e.get("utm_content") or "", "term": e.get("utm_term") or "",
                                 "channel": channel_for(e), "voice": False, "subscribed": False,
                                 "user": e["user_id"], "ts": e["timestamp"]}
                camp = e.get("utm_campaign")
                if camp and camp not in campaigns:
                    bad["utm_campaign not in campaigns.csv"] += 1
                if e["platform"] == "ott" and e.get("utm_source"):
                    bad["UTM on an ott (TV) session"] += 1
                src = e.get("utm_source")
                want = {"gclid": src in ("google", "youtube"), "fbclid": src in ("facebook", "instagram"),
                        "ttclid": src == "tiktok"}
                for k, need in want.items():
                    if bool(e.get(k)) != need:
                        bad[f"{k} inconsistent with utm_source"] += 1
                if src == "tiktok":
                    tiktok[period_of_date(cfg, lt.date())] += 1
            s["user"] = s["user"] or e["user_id"]          # sessions can start logged out
            if e["event"] == "video_view" and e["title_id"] == "joyn_the_voice":
                s["voice"] = True
            if e["event"] == "subscribed":
                s["subscribed"] = True
                s["user"] = e["user_id"]

    # spend covers every landing session
    landed = Counter((s["date"].isoformat(), s["camp"], s["src"], s["med"], s["content"], s["term"])
                     for s in sess.values() if s["camp"])
    clicks = Counter()
    for r in spend:
        clicks[(r["date"], r["utm_campaign"], r["utm_source"], r["utm_medium"], r["utm_content"], r["utm_term"])] += int(r["clicks"])
    # TV promo (vanity URL / QR) has no click-level media cost, so it has no spend rows
    short = sum(1 for k, n in landed.items() if k[3] != "tv_promo" and clicks[k] < n)
    if short:
        bad["campaign sessions without enough clicks in spend table"] += short
    C.add("marketing consistency (campaign names, click ids, OTT, spend >= sessions)", sum(bad.values()) == 0,
          sum(bad.values()) if not bad else dict(bad), "0")

    S = cfg["marketing"]["stories"]
    new = defaultdict(Counter)
    for a in acq:
        if a["acquired_in_window"] == "True":
            new[a["acquisition_period"]][a["acquisition_channel"]] += 1
    if base_pair:                      # M1/M2 are planted between STLY and CURRENT
        # M1 Voice launch campaign
        wk3 = [s for s in sess.values() if s["voice"] and s["date"] in cur_days[14:21]]
        share = sum(1 for s in wk3 if s["camp"] and "launch_tvog" in s["camp"]) / len(wk3) if wk3 else 0
        tvog_spend = defaultdict(float)
        for r in spend:
            if "launch_tvog" in r["utm_campaign"] and r["utm_campaign"].startswith(str(cur_days[0].year)):
                day = date.fromisoformat(r["date"]).day
                tvog_spend[3 if 15 <= day <= 21 else (12 if day <= 14 else 4)] += float(r["cost_eur"])
        ratio = tvog_spend[3] / (tvog_spend[12] / 2) if tvog_spend[12] else float("inf")
        C.add("M1: Voice launch campaign share of week-3 Voice sessions", share >= S["voice_campaign_share_wk3"]["min"],
              f"{share:.0%} of {len(wk3)} sessions", f">= {S['voice_campaign_share_wk3']['min']:.0%}")
        C.add("M1: Voice campaign spend week 3 vs weekly avg of weeks 1-2", ratio >= S["voice_spend_wk3_vs_wk12"]["min"],
              f"{ratio:.1f}x (€{tvog_spend[3]:.0f} vs €{tvog_spend[12]:.0f} in wk 1-2)", f">= {S['voice_spend_wk3_vs_wk12']['min']}x")

        # M2 channel mix of new users
        sh = lambda p, c: new[p][c] / sum(new[p].values()) if new[p] else 0  # noqa: E731
        d_ps = (sh(cur, "paid_social") - sh(stly, "paid_social")) * 100
        d_disp = (sh(cur, "display") - sh(stly, "display")) * 100
        C.add("M2: paid social share of new users YoY", d_ps >= S["paid_social_new_user_pp"]["min"],
              f"{sh(stly, 'paid_social'):.0%} -> {sh(cur, 'paid_social'):.0%} ({d_ps:+.1f}pp)",
              f">= +{S['paid_social_new_user_pp']['min']}pp")
        C.add("M2: display share of new users falls; TikTok only in 2026", d_disp < 0 and tiktok[stly] == 0 and tiktok[cur] > 0,
              f"display {d_disp:+.1f}pp; TikTok sessions {tiktok[stly]} -> {tiktok[cur]}", "display < 0, TikTok 0 -> >0")

    # M3 first touch vs last touch for subscribers (non-TV subscribe sessions)
    subs = [s for s in sess.values() if s["subscribed"] and s["platform"] != "ott"]
    last = sum(1 for s in subs if s["med"] == "email" or (s["med"] == "cpc" and s["camp"]
                                                          and ("brand" in s["camp"] or "joynplus" in s["camp"])))
    acq_by = {a["user_id"]: a for a in acq}
    first = sum(1 for s in subs if acq_by.get(s["user"], {}).get("acquisition_channel") in ("crm_email", "paid_search_brand"))
    lt_share = last / len(subs) if subs else 0
    ft_share = first / len(subs) if subs else 0
    C.add("M3: last touch = email/brand search vs first touch", lt_share >= S["last_touch_email_brand"]["min"]
          and ft_share <= S["first_touch_email_brand"]["max"],
          f"last {lt_share:.0%} vs first {ft_share:.0%} ({len(subs)} subscribers)",
          f"last >= {S['last_touch_email_brand']['min']:.0%}, first <= {S['first_touch_email_brand']['max']:.0%}")

    # M4 CAC by channel (both periods)
    cost = defaultdict(float)
    for r in spend:
        c = r["utm_campaign"]
        if r["utm_medium"] == "cpc" and ("brand" in c or "joynplus" in c):
            cost["paid_search_brand"] += float(r["cost_eur"])
        elif r["channel"] == "Paid Social":
            cost["paid_social"] += float(r["cost_eur"])
        elif r["channel"] == "Display":
            cost["display"] += float(r["cost_eur"])
    n_new = Counter(a["acquisition_channel"] for a in acq if a["acquired_in_window"] == "True")
    cac = {k: cost[k] / n_new[k] if n_new[k] else float("inf") for k in ("paid_search_brand", "paid_social", "display")}
    C.add("M4: CAC brand search < paid social < display",
          cac["paid_search_brand"] < cac["paid_social"] < cac["display"],
          ", ".join(f"{k} €{v:.2f}" for k, v in cac.items()), "ascending")

    # M5 payment follow-up email
    pay_sess = [s for s in sess.values() if s["camp"] and "service_payment" in s["camp"]]
    incs = sorted(date.fromisoformat(str(i["date"])) for i in cfg.payment_incidents())
    if incs:
        ok5 = bool(pay_sess) and all(any(i < s["date"] for i in incs) and s["user"] in failed_before
                                     and failed_before[s["user"]] < s["ts"] for s in pay_sess)
        C.add("M5: payment-update email only after incident, only to affected users", ok5,
              f"{len(pay_sess)} sessions", "all after an incident and after a failed payment")

    # M6 TV attribution gap
    ott = [s for s in sess.values() if s["platform"] == "ott"]
    ott_utm = sum(1 for s in ott if s["src"])
    C.add("M6: ott sessions carry no UTMs", ott_utm == 0, f"{ott_utm} of {len(ott)} ott sessions", "0")

    return {"session_channels": dict(Counter(s["channel"] for s in sess.values())), "cac": cac,
            "new_user_mix": {p: dict(v) for p, v in new.items()}}


def period_of_date(cfg: Settings, d: date) -> Optional[str]:
    for p in cfg.period_names:
        days = cfg.period_days(p)
        if days[0] <= d <= days[-1]:
            return p
    return None
