#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.kis.client import KISClient, KISError, load_kis_config, redacted_account
from us_stock_live.strategy.target_weights import _resolve_path
from us_stock_live.trading.state import (
    load_state,
    mark_plan_submitted,
    read_json,
    short_hash,
    state_path_for_profile,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit or dry-run a saved stock-live rebalance plan.")
    parser.add_argument("--plan-json", required=True, help="Saved rebalance plan JSON path.")
    parser.add_argument("--state-json", default="", help="Optional state JSON path. Defaults by profile in plan.")
    parser.add_argument("--output-json", default="", help="Optional execution report path.")
    parser.add_argument("--execute", action="store_true", help="Actually submit orders to KIS. Omit for dry-run.")
    parser.add_argument("--confirm-live", action="store_true", help="Required with --execute when KIS_ENV=live.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Allow --execute outside US regular hours.")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.0, help="Buy raises and sell lowers limit by this percent.")
    parser.add_argument("--sleep-seconds", type=float, default=1.2, help="Delay between order submissions.")
    return parser


def _read_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile_json = _resolve_path(payload["profile_json"])
    return read_json(profile_json)


def _symbol_setting(profile: dict[str, Any], symbol: str) -> dict[str, Any]:
    return ((profile.get("kis") or {}).get("symbols") or {}).get(symbol.upper()) or {}


def _order_exchange(profile: dict[str, Any], symbol: str) -> str:
    kis = profile.get("kis") or {}
    default_exchange = str(kis.get("default_balance_exchange") or "AMEX")
    return str(_symbol_setting(profile, symbol).get("balance_exchange") or default_exchange)


def _regular_us_market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def _limit_price(row: dict[str, Any], *, buffer_pct: float) -> str:
    price = float(row["price_usd"])
    if row["action"] == "buy":
        price *= 1.0 + buffer_pct / 100.0
    elif row["action"] == "sell":
        price *= max(1.0 - buffer_pct / 100.0, 0.0)
    return f"{price:.2f}"


def _order_no(payload: dict[str, Any]) -> str | None:
    output = payload.get("output") or {}
    if isinstance(output, list) and output:
        output = output[0]
    if not isinstance(output, dict):
        return None
    return output.get("ODNO") or output.get("odno") or output.get("order_no")


def _planned_orders(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in plan.get("rows", [])
        if row.get("action") in {"buy", "sell"} and int(row.get("order_qty") or 0) > 0
    ]


def build_execution_report(
    *,
    plan: dict[str, Any],
    execute: bool,
    limit_buffer_pct: float,
) -> dict[str, Any]:
    orders = _planned_orders(plan)
    seed = {
        "plan_id": plan.get("plan_id"),
        "execute": execute,
        "orders": [
            {"symbol": row.get("symbol"), "action": row.get("action"), "qty": row.get("order_qty")}
            for row in orders
        ],
    }
    return {
        "type": "stock_live_execution_report_v1",
        "execution_report_id": f"exec_{short_hash(seed)}",
        "plan_id": plan.get("plan_id"),
        "plan_hash": plan.get("plan_hash"),
        "profile_json": plan.get("profile_json"),
        "phase": plan.get("phase"),
        "execute": execute,
        "limit_buffer_pct": limit_buffer_pct,
        "orders": orders,
        "submitted_orders": [],
        "blocked_reason": None,
        "blocked_reasons": [],
    }


def main() -> int:
    args = build_parser().parse_args()
    plan_path = _resolve_path(args.plan_json)
    plan = read_json(plan_path)
    profile = _read_profile(plan)
    report = build_execution_report(plan=plan, execute=args.execute, limit_buffer_pct=args.limit_buffer_pct)

    blockers: list[str] = []
    if plan.get("phase") == "full":
        blockers.append("phase_full_is_review_only")
    if not plan.get("rebalance_allowed"):
        blockers.append(f"rebalance_not_allowed:{plan.get('rebalance_reason')}")
    if not report["orders"]:
        blockers.append("no_nonzero_orders")

    config = load_kis_config()
    if args.execute and config.env == "live" and not args.confirm_live:
        blockers.append("missing_confirm_live")
    if args.execute and not args.ignore_market_hours and not _regular_us_market_is_open():
        blockers.append("outside_us_regular_market_hours")
    report["blocked_reasons"] = blockers
    report["blocked_reason"] = blockers[0] if blockers else None

    if blockers:
        if args.output_json:
            write_json(_resolve_path(args.output_json), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if args.execute else 0

    client = KISClient(config)
    client.issue_access_token()
    for index, row in enumerate(report["orders"]):
        if index:
            time.sleep(max(args.sleep_seconds, 0.0))
        symbol = str(row["symbol"]).upper()
        side = str(row["action"]).lower()
        qty = int(row["order_qty"])
        exchange = _order_exchange(profile, symbol)
        limit_price = _limit_price(row, buffer_pct=args.limit_buffer_pct)
        submitted: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "exchange_code": exchange,
            "limit_price": limit_price,
            "dry_run": not args.execute,
        }
        if args.execute:
            try:
                response = client.overseas_order(
                    side=side,
                    symbol=symbol,
                    qty=qty,
                    limit_price=limit_price,
                    exchange_code=exchange,
                    order_division="00",
                )
                submitted["status"] = "submitted"
                submitted["order_no"] = _order_no(response)
                submitted["response"] = response
            except KISError as exc:
                submitted["status"] = "error"
                submitted["error"] = str(exc)
        else:
            submitted["status"] = "dry_run"
        report["submitted_orders"].append(submitted)

    if args.execute and any(order.get("status") == "submitted" for order in report["submitted_orders"]):
        profile_json = _resolve_path(plan["profile_json"])
        state_path = _resolve_path(args.state_json) if args.state_json else state_path_for_profile(profile_json)
        state = load_state(state_path)
        state = mark_plan_submitted(state, plan, execution_report=report)
        write_json(state_path, state)

    if args.output_json:
        write_json(_resolve_path(args.output_json), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not any(order.get("status") == "error" for order in report["submitted_orders"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
