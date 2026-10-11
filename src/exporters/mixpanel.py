"""Mixpanel /import files: time in epoch ms, $insert_id, <=2,000 events per file (one request each).

Identity (Mixpanel "ID Merge", check Project Settings -> Identity Merge):
- simplified (default; every project created since 2023): each event carries $device_id (the
  device's anonymous id) and, once the user is logged in on that device, $user_id. Mixpanel merges
  the device into the user on the first event that has both. distinct_id follows Mixpanel's own
  convention: the user id when known, else "$device:<device id>". No $identify events are sent.
- original (older projects): distinct_id as in the canonical data, plus $identify events carrying
  $identified_id / $anon_id.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def to_mixpanel(e: dict, mode: str = "simplified") -> dict | None:
    """One canonical event -> one Mixpanel import record (None = not sent in this mode)."""
    ms = int(datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
    props = {k: v for k, v in e.items() if k not in ("event", "timestamp")}
    props["time"] = ms
    if mode == "simplified":
        if e["event"] == "$identify":
            return None                         # the merge happens on events with $device_id + $user_id
        props["$device_id"] = e["anonymous_id"]
        if e.get("user_id"):
            props["$user_id"] = e["user_id"]
            props["distinct_id"] = e["user_id"]
        else:
            props["distinct_id"] = f"$device:{e['anonymous_id']}"
    elif e["event"] == "$identify":
        props["$identified_id"] = e["distinct_id"]   # original ID Merge
        props["$anon_id"] = props.pop("$anon_distinct_id")
    return {"event": e["event"], "properties": props}


def write_mixpanel(events: Path, out_dir: Path, per_file: int = 2000, mode: str = "simplified") -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("batch_*.jsonl"):
        old.unlink()
    n = i = 0
    f = None
    with open(events) as fin:
        for line in fin:
            rec = to_mixpanel(json.loads(line), mode)
            if rec is None:
                continue
            if n % per_file == 0:
                if f:
                    f.close()
                i += 1
                f = open(out_dir / f"batch_{i:05d}.jsonl", "w")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    if f:
        f.close()
    return i


def write_mixpanel_profiles(path: Path, briefs, acquisitions: dict, identities: dict | None = None,
                            mode: str = "simplified") -> int:
    """Engage API rows ($set_once first touch, $set current state + identity). The upload script adds $token.

    Users with an account are keyed by user_id (their merged identity) and get $email, email_sha256,
    crm_id. Anonymous users are keyed by the distinct_id their events use for the chosen ID Merge mode.
    """
    identities = identities or {}
    n = 0
    with open(path, "w") as f:
        for b in briefs:
            a = acquisitions.get(b.user_id, {})
            anon = b.devices[0].anon_id
            did = b.user_id if b.user_id in identities else (f"$device:{anon}" if mode == "simplified" else anon)
            once = {k: v for k, v in {
                "initial_utm_source": a.get("initial_utm_source"), "initial_utm_medium": a.get("initial_utm_medium"),
                "initial_utm_campaign": a.get("initial_utm_campaign"),
                "initial_referring_domain": a.get("initial_referring_domain"),
                "acquisition_channel": a.get("acquisition_channel"), "acquisition_date": a.get("acquisition_date"),
                "acquisition_platform": a.get("acquisition_platform")}.items() if v not in (None, "")}
            props = {"segment": b.segment, "primary_platform": b.primary_platform}
            if b.user_id in identities:
                ident = identities[b.user_id]
                props.update({"$email": ident["email"], "email_sha256": ident["email_sha256"],
                              "crm_id": ident["crm_id"], "user_id": b.user_id})
            f.write(json.dumps({"$distinct_id": did, "$set_once": once, "$set": props}) + "\n")
            n += 1
    return n
