"""Identity attributes for users with an account, derived deterministically from user_id.

- email: synthetic, on the reserved `.invalid` TLD (RFC 2606), so it can never reach a real inbox
  or match a real person.
- email_sha256: SHA-256 of the trimmed, lower-cased email, the normalisation Meta's Conversions API
  and Google's Enhanced Conversions expect, so hashed-matching exercises work as in production.
- crm_id: a stable CRM key, as a CRM export or customer-data platform would carry it.

Anonymous users (never registered) have none of these: only their device ids.
Applied at export time, so the canonical events.ndjson stays unchanged.
"""
from __future__ import annotations

import hashlib

EMAIL_DOMAIN = "example.invalid"


def email_for(user_id: str) -> str:
    return f"viewer.{user_id}@{EMAIL_DOMAIN}"


def sha256_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def crm_id_for(user_id: str) -> str:
    return "CRM-" + hashlib.sha256(f"crm|{user_id}".encode()).hexdigest()[:10].upper()


def identity_for(user_id: str) -> dict:
    email = email_for(user_id)
    return {"email": email, "email_sha256": sha256_email(email), "crm_id": crm_id_for(user_id)}


def account_users(briefs) -> dict[str, dict]:
    """user_id -> identity attributes, for every user who registered at some point."""
    return {b.user_id: identity_for(b.user_id) for b in briefs if b.segment != "anonymous"}
