"""Shared fixtures: a small offline world (60 users) built in a temp directory."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.catalog import build_catalog  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.personas import build_personas, save_personas  # noqa: E402


def small_config(tmp: Path) -> Path:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["users"] = {"total": 60, "mau": {"STLY": 30, "CURRENT": 36}, "both": 6, "mvu_ratio": 0.6}
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


@pytest.fixture
def world(tmp_path):
    cfg = load_settings(small_config(tmp_path), root=tmp_path)
    cat = asyncio.run(build_catalog(cfg, None))
    briefs = build_personas(cfg)
    save_personas(cfg, briefs)
    return cfg, cat, briefs
