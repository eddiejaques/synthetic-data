"""Config loading, path helpers and deterministic RNG helpers."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    """Thin wrapper around config.yaml with resolved paths."""

    raw: dict[str, Any]
    root: Path

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def seed(self) -> int:
        return int(self.raw["seed"])

    def path(self, name: str) -> Path:
        p = self.root / self.raw["paths"][name]
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache(self) -> Path:
        return self.path("cache")

    @property
    def logs(self) -> Path:
        return self.path("logs")

    @property
    def out(self) -> Path:
        return self.path("out")

    @property
    def period_names(self) -> list[str]:
        """Period names in date order (never rely on YAML key order)."""
        return sorted(self.raw["periods"], key=lambda k: str(self.raw["periods"][k]["start"]))

    def period_days(self, period: str) -> list[date]:
        p = self.raw["periods"][period]
        start, end = _d(p["start"]), _d(p["end"])
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    @property
    def model(self) -> str:
        return os.environ.get("GEMINI_MODEL") or self.raw["gemini"]["model"]

    def view(self, periods: dict[str, dict], overrides: dict | None = None) -> "Settings":
        """A copy restricted to `periods` (ordered by start date) with `overrides` deep-merged in.

        Every stage takes a Settings, so a batch runs on a view that only knows its own periods."""
        raw = copy.deepcopy(self.raw)
        raw["periods"] = dict(sorted(periods.items(), key=lambda kv: str(kv[1]["start"])))
        if overrides:
            deep_merge(raw, overrides)
        return Settings(raw=raw, root=self.root)

    def payment_incidents(self) -> list[dict]:
        """All payment incidents: the legacy single `payment_incident` plus any `payment_incidents` list."""
        st = self.raw["stories"]
        out = [st["payment_incident"]] if st.get("payment_incident") else []
        out += list(st.get("payment_incidents") or [])
        return out


def deep_merge(base: dict, extra: dict) -> dict:
    """Recursively merge `extra` into `base` (dicts merge, everything else replaces)."""
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def _d(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def load_settings(path: str | Path | None = None, root: Path | None = None) -> Settings:
    """Load config.yaml (and .env for keys) relative to the project root."""
    root = root or ROOT
    load_dotenv(root / ".env")
    cfg_path = Path(path) if path else root / "config.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    return Settings(raw=raw, root=root)


def sub_seed(*parts: Any) -> int:
    """Stable 64-bit seed from arbitrary parts, independent of PYTHONHASHSEED."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big")


def rng_for(*parts: Any) -> random.Random:
    """Order-independent RNG: same parts -> same stream, whatever ran before."""
    return random.Random(sub_seed(*parts))


def write_json(path: Path, obj: Any) -> None:
    """Atomic JSON write (tmp + rename) so a crash never leaves a half file in cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())
