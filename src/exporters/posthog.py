"""PostHog /batch payload files (<=1,000 events each) for historical_migration import.

Identity: events before login use the device's anonymous id as distinct_id. At registration or the
first login on a device, a `$identify` event (distinct_id = user id, $anon_distinct_id = device id)
merges that device's anonymous person into the user, past events included. A second device is
merged into the same person the same way. The $identify event also sets the account's person
properties: email, email_sha256, crm_id.
"""
from __future__ import annotations

import json
from pathlib import Path

TOP = {"event", "distinct_id", "timestamp", "$insert_id"}


def to_posthog(e: dict) -> dict:
    props = {k: v for k, v in e.items() if k not in TOP}
    if e["event"] == "$identify":
        props["$anon_distinct_id"] = e["$anon_distinct_id"]
    props["$insert_id"] = e["$insert_id"]
    if e.get("session_id"):
        props["$session_id"] = e["session_id"]
    # $insert_id is a UUID5, so it doubles as PostHog's event uuid -> re-uploads dedupe
    return {"event": e["event"], "distinct_id": e["distinct_id"], "timestamp": e["timestamp"],
            "uuid": e["$insert_id"], "properties": props}


def person_set_once(acq: dict) -> dict:
    """PostHog initial-touch person properties (normally set by posthog-js on the first pageview)."""
    return {k: v for k, v in {
        "$initial_utm_source": acq.get("initial_utm_source") or None,
        "$initial_utm_medium": acq.get("initial_utm_medium") or None,
        "$initial_utm_campaign": acq.get("initial_utm_campaign") or None,
        "$initial_referring_domain": acq.get("initial_referring_domain") or None,
        "acquisition_channel": acq.get("acquisition_channel"),
        "acquisition_date": acq.get("acquisition_date"),
        "acquisition_platform": acq.get("acquisition_platform"),
    }.items() if v not in (None, "")}


def write_posthog(events: Path, out_dir: Path, per_file: int = 1000,
                  person_of: dict | None = None, acquisitions: dict | None = None,
                  identities: dict | None = None) -> int:
    """person_of maps anonymous_id/user_id -> user_id; acquisitions maps user_id -> first-touch row."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("batch_*.jsonl"):
        old.unlink()
    n = i = 0
    f = None
    seen: set[str] = set()
    with open(events) as fin:
        for line in fin:
            if n % per_file == 0:
                if f:
                    f.close()
                i += 1
                f = open(out_dir / f"batch_{i:05d}.jsonl", "w")
            e = json.loads(line)
            ev = to_posthog(e)
            user = (person_of or {}).get(e.get("user_id") or e["anonymous_id"])
            if user and user not in seen and acquisitions and user in acquisitions:
                ev["properties"]["$set_once"] = person_set_once(acquisitions[user])
                seen.add(user)
            if e["event"] == "$identify" and identities and e["distinct_id"] in identities:
                ident = identities[e["distinct_id"]]
                ev["properties"]["$set"] = {"email": ident["email"], "email_sha256": ident["email_sha256"],
                                            "crm_id": ident["crm_id"], "user_id": e["distinct_id"]}
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            n += 1
    if f:
        f.close()
    return i
