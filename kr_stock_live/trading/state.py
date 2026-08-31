#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from live_common.state import (
    build_plan_id,
    canonical_json,
    load_state,
    mark_plan_executed,
    mark_plan_submitted,
    now_unix,
    read_json,
    register_plan,
    short_hash,
    write_json,
)


ROOT_DIR = Path(__file__).resolve().parents[2]


def state_path_for_profile(profile_json: Path) -> Path:
    return ROOT_DIR / "kr_stock_live" / ".cache" / f"rebalance_state_{profile_json.stem}.json"


__all__ = [
    "build_plan_id",
    "canonical_json",
    "load_state",
    "mark_plan_executed",
    "mark_plan_submitted",
    "now_unix",
    "read_json",
    "register_plan",
    "short_hash",
    "state_path_for_profile",
    "write_json",
]

