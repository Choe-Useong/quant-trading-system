#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.kis.client import KISClient, KISError, load_kis_config, redacted_account
from us_stock_live.strategy.target_weights import (
    _profile_strategy_feature_tail_rows,
    _profile_strategy_defaults,
    _resolve_path,
    build_latest_target_weights,
)


DEFAULT_PROFILE_JSON = ROOT_DIR / "us_stock_live" / "configs" / "us_2x_ugl_tlt_mom126_top3_p975.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate stock-live symbols through KIS quote API without orders.")
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="Stock-live profile JSON path.")
    parser.add_argument(
        "--scope",
        choices=["active", "all"],
        default="active",
        help="active validates latest target symbols; all validates profile data.tickers.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=1.1, help="Delay between KIS quote calls.")
    parser.add_argument("--as-of", default="", help="Optional target date/time for active target selection.")
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _symbols_for_scope(profile_path: Path, profile: dict[str, Any], scope: str, as_of: str) -> list[str]:
    if scope == "all":
        return [str(symbol).upper() for symbol in profile.get("data", {}).get("tickers", [])]
    config_json, source_cache_dir, context = _profile_strategy_defaults(profile_path)
    feature_tail_rows = _profile_strategy_feature_tail_rows(profile_path)
    payload = build_latest_target_weights(
        config_json=config_json,
        context=context,
        source_cache_dir_override=source_cache_dir,
        as_of=as_of,
        feature_tail_rows=feature_tail_rows,
    )
    return [str(item["symbol"]).upper() for item in payload["weights"]]


def _last_price(payload: dict[str, Any]) -> str:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return ""
    return str(output.get("last") or output.get("last_price") or output.get("ovrs_nmix_prpr") or "").strip()


def main() -> int:
    args = build_parser().parse_args()
    profile_path = _resolve_path(args.profile_json)
    profile = _read_json(profile_path)
    kis = profile.get("kis") or {}
    symbol_settings = kis.get("symbols") or {}
    default_exchange = str(kis.get("default_price_exchange") or "AMS")
    symbols = _symbols_for_scope(profile_path, profile, args.scope, args.as_of)
    if not symbols:
        raise ValueError("No symbols to validate")

    config = load_kis_config()
    client = KISClient(config)
    client.issue_access_token()
    results = []
    for index, symbol in enumerate(symbols):
        if index:
            time.sleep(max(args.sleep_seconds, 0.0))
        exchange = str((symbol_settings.get(symbol) or {}).get("price_exchange") or default_exchange)
        try:
            payload = client.overseas_price(symbol, exchange_code=exchange)
            last = _last_price(payload)
            ok = bool(last)
            error = ""
        except KISError as exc:
            last = ""
            ok = False
            error = str(exc)
        results.append(
            {
                "symbol": symbol,
                "price_exchange": exchange,
                "ok": ok,
                "last": last or None,
                "error": error or None,
            }
        )

    output = {
        "env": config.env,
        "account": redacted_account(config),
        "profile_json": str(profile_path.relative_to(ROOT_DIR) if profile_path.is_relative_to(ROOT_DIR) else profile_path),
        "scope": args.scope,
        "results": results,
        "all_ok": all(item["ok"] for item in results),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
