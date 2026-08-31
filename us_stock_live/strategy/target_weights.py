#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.target_weights import (
    _read_json,
    _resolve_path,
    _strategy_feature_tail_rows,
    build_latest_target_weights,
    live_target_extension_options,
    print_target_weights_table,
)
from us_stock_live.active_profile import resolve_profile_reference


DEFAULT_CONFIG_JSON = (
    ROOT_DIR
    / "configs"
    / "stocks"
    / "grid_us_leveraged2x_ugl_tlt_plain_vs_absmom126_vol10pct_top3_8_99_daily_commonstart_v2.json"
)
DEFAULT_PROFILE_JSON = ROOT_DIR / "us_stock_live" / "configs" / "us_2x_ugl_tlt_mom126_top3_p975.json"


def _relative_to_root(path: Path) -> str:
    return str(path.relative_to(ROOT_DIR) if path.is_relative_to(ROOT_DIR) else path)


def _resolve_profile_json(profile_json: Path) -> Path:
    if not profile_json:
        return profile_json
    resolved_profile_json, _ = resolve_profile_reference(profile_json)
    return resolved_profile_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the latest US stock live target weights from an existing cross-section grid config."
    )
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="Stock-live profile JSON path.")
    parser.add_argument("--config-json", default="", help="Override grid config JSON path.")
    parser.add_argument("--source-cache-dir", default="", help="Override source cache dir.")
    parser.add_argument("--mom-label", default="", help="Override grid value for mom_label.")
    parser.add_argument("--mom-threshold", type=float, default=None, help="Override grid value for mom_threshold.")
    parser.add_argument("--top-n", type=int, default=None, help="Override grid value for top_n.")
    parser.add_argument("--cutoff", type=float, default=None, help="Override grid value for cutoff.")
    parser.add_argument("--cutoff-label", default="", help="Override grid value for cutoff_label.")
    parser.add_argument("--as-of", default="", help="Optional target date/time. Defaults to latest cache timestamp.")
    parser.add_argument("--output-json", default="", help="Optional output JSON path.")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format.")
    return parser


def _profile_strategy_defaults(profile_json: Path) -> tuple[Path, Path | None, dict[str, Any]]:
    if not profile_json:
        return DEFAULT_CONFIG_JSON, None, {
            "mom_label": "mom126",
            "mom_threshold": 0.0,
            "top_n": 3,
            "cutoff": 0.975,
            "cutoff_label": "p975",
        }
    profile = _read_json(profile_json)
    strategy = profile.get("strategy") or {}
    config_json = _resolve_path(strategy.get("config_json") or DEFAULT_CONFIG_JSON)
    source_cache_dir = strategy.get("source_cache_dir")
    context = strategy.get("context") or {}
    return config_json, _resolve_path(source_cache_dir) if source_cache_dir else None, dict(context)


def _profile_strategy_feature_tail_rows(profile_json: Path) -> int | None:
    if not profile_json:
        return None
    profile = _read_json(profile_json)
    strategy = profile.get("strategy") or {}
    return _strategy_feature_tail_rows(strategy)


def _profile_strategy_live_extension_options(profile_json: Path) -> dict[str, Any]:
    if not profile_json:
        return live_target_extension_options({})
    profile = _read_json(profile_json)
    return live_target_extension_options(profile.get("strategy") or {})


def main() -> int:
    args = build_parser().parse_args()
    profile_json = _resolve_path(args.profile_json) if args.profile_json else Path()
    resolved_profile_json = _resolve_profile_json(profile_json)
    config_json, source_cache_dir, context = _profile_strategy_defaults(resolved_profile_json)
    feature_tail_rows = _profile_strategy_feature_tail_rows(resolved_profile_json)
    extension_options = _profile_strategy_live_extension_options(resolved_profile_json)
    if args.config_json:
        config_json = _resolve_path(args.config_json)
    if args.source_cache_dir:
        source_cache_dir = _resolve_path(args.source_cache_dir)
    if args.mom_label:
        context["mom_label"] = args.mom_label
    if args.mom_threshold is not None:
        context["mom_threshold"] = args.mom_threshold
    if args.top_n is not None:
        context["top_n"] = args.top_n
    if args.cutoff is not None:
        context["cutoff"] = args.cutoff
    if args.cutoff_label:
        context["cutoff_label"] = args.cutoff_label
    payload = build_latest_target_weights(
        config_json=config_json,
        context=context,
        source_cache_dir_override=source_cache_dir,
        as_of=args.as_of,
        feature_tail_rows=feature_tail_rows,
        **extension_options,
    )
    payload["strategy"] = "stock_live_target_weights_v1"
    if profile_json:
        payload["profile_json"] = _relative_to_root(profile_json)
        if resolved_profile_json != profile_json:
            payload["resolved_profile_json"] = _relative_to_root(resolved_profile_json)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_target_weights_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
