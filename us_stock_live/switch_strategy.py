#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.active_profile import ACTIVE_PROFILE_PATH, write_active_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Point stock-live scheduling at a new profile for the next run.")
    parser.add_argument("--profile-json", required=True, help="Concrete stock-live profile JSON to activate.")
    parser.add_argument(
        "--no-pending-switch",
        action="store_true",
        help="Activate without scheduling the one-time bootstrap override.",
    )
    parser.add_argument("--format", choices=["table", "json"], default="table")
    return parser


def _resolve_path(path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def _read_profile(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "strategy", "data", "kis"):
        if key not in payload:
            raise ValueError(f"Invalid stock-live profile, missing {key}: {path}")
    return payload


def main() -> int:
    args = build_parser().parse_args()
    profile_path = _resolve_path(args.profile_json)
    if not profile_path.exists():
        raise FileNotFoundError(profile_path)
    profile = _read_profile(profile_path)
    active_path = write_active_profile(
        profile_json=profile_path,
        pending_switch=not args.no_pending_switch,
        path=ACTIVE_PROFILE_PATH,
    )
    payload = {
        "type": "stock_live_strategy_switch_v1",
        "active_profile_json": _rel(active_path),
        "profile_json": _rel(profile_path),
        "profile_name": profile["name"],
        "pending_switch": not args.no_pending_switch,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"active_profile_json: {payload['active_profile_json']}")
        print(f"profile_json: {payload['profile_json']}")
        print(f"profile_name: {payload['profile_name']}")
        print(f"pending_switch: {payload['pending_switch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
