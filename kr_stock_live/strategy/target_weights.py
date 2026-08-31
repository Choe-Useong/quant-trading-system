#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.target_weights import (
    _strategy_feature_tail_rows,
    build_latest_target_weights,
    live_target_extension_options,
)

from kr_stock_live.active_profile import resolve_profile_reference


DEFAULT_PROFILE_JSON = (
    ROOT_DIR
    / "kr_stock_live"
    / "configs"
    / "kr_kospi_rank12_skip3_top2_breadth200_gt0p5.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the latest Korean stock live target weights from a v2 strategy profile."
    )
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="KR stock live profile JSON path.")
    parser.add_argument("--as-of", default="", help="Optional target date/time. Defaults to latest cache timestamp.")
    parser.add_argument("--output-json", default="", help="Optional output JSON path.")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format.")
    return parser


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT_DIR / path


def _print_table(payload: dict) -> None:
    print(f"run_name: {payload['run_name']}")
    print(f"target_timestamp: {payload['target_timestamp']}")
    print(f"latest_feature_timestamp: {payload['latest_feature_timestamp']}")
    print(f"last_change_timestamp: {payload['last_change_timestamp']}")
    print(f"gross_target_weight: {payload['gross_target_weight']:.6f}")
    print()
    if not payload["weights"]:
        print("No active target weights.")
        return
    print("market      symbol  target_weight")
    for item in payload["weights"]:
        print(f"{item['market']:<11} {item['symbol']:<7} {item['target_weight']:.8f}")


def main() -> int:
    args = build_parser().parse_args()
    profile_reference_json = _resolve_path(args.profile_json)
    profile_json, active_profile = resolve_profile_reference(profile_reference_json)
    profile = _read_json(profile_json)
    strategy = profile["strategy"]
    config_json = _resolve_path(strategy["config_json"])
    source_cache_dir = _resolve_path(strategy["source_cache_dir"])
    context = dict(strategy.get("context") or {})
    feature_tail_rows = _strategy_feature_tail_rows(strategy)
    extension_options = live_target_extension_options(strategy)

    payload = build_latest_target_weights(
        config_json=config_json,
        context=context,
        source_cache_dir_override=source_cache_dir,
        as_of=args.as_of,
        feature_tail_rows=feature_tail_rows,
        **extension_options,
    )
    payload["strategy"] = "kr_stock_live_target_weights_v1"
    payload["profile_json"] = str(profile_json.relative_to(ROOT_DIR))
    if active_profile is not None:
        payload["profile_reference_json"] = str(profile_reference_json.relative_to(ROOT_DIR))

    if args.output_json:
        output_path = _resolve_path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
