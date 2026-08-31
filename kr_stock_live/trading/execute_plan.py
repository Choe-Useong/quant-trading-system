#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from kr_stock_live.kis.client import KRKISClient, load_kr_kis_config
from kr_stock_live.trading.state import load_state, mark_plan_submitted, read_json, short_hash, state_path_for_profile, write_json
from live_common.kis_base import KISError
from live_common.target_weights import _resolve_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit or dry-run a saved Korean-stock/ETF rebalance plan.")
    parser.add_argument("--plan-json", required=True, help="Saved rebalance plan JSON path.")
    parser.add_argument("--state-json", default="", help="Optional state JSON path. Defaults by profile in plan.")
    parser.add_argument("--output-json", default="", help="Optional execution report path.")
    parser.add_argument("--execute", action="store_true", help="Actually submit orders to KIS. Omit for dry-run.")
    parser.add_argument("--confirm-live", action="store_true", help="Required with --execute when KIS_ENV=live.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Allow --execute outside KRX regular hours.")
    parser.add_argument("--order-style", choices=["limit", "market"], default="limit")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.0, help="Buy raises and sell lowers limit by this percent.")
    parser.add_argument("--sleep-seconds", type=float, default=1.2, help="Delay between order submissions.")
    return parser


def _read_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile_json = _resolve_path(payload["profile_json"])
    return read_json(profile_json)


def _regular_krx_market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(15, 30)


def _to_int(value: object, default: int = 1) -> int:
    try:
        text = str(value).replace(",", "").strip()
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


def _round_price_to_tick(price: float, tick_unit: int, *, direction: str) -> int:
    tick = max(int(tick_unit), 1)
    if price <= 0:
        return tick
    if direction == "down":
        return max(int(math.floor(price / tick) * tick), tick)
    return max(int(math.ceil(price / tick) * tick), tick)


def _order_price(row: dict[str, Any], *, order_style: str, buffer_pct: float) -> tuple[str, str]:
    if order_style == "market":
        return "01", "0"
    price = float(row["price_krw"])
    tick_unit = _to_int(row.get("price_tick_unit"), 1)
    if row["action"] == "buy":
        price *= 1.0 + buffer_pct / 100.0
        return "00", str(_round_price_to_tick(price, tick_unit, direction="up"))
    elif row["action"] == "sell":
        price *= max(1.0 - buffer_pct / 100.0, 0.0)
        return "00", str(_round_price_to_tick(price, tick_unit, direction="down"))
    return "00", str(_round_price_to_tick(price, tick_unit, direction="up"))


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
    order_style: str,
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
        "type": "kr_stock_live_execution_report_v1",
        "execution_report_id": f"exec_{short_hash(seed)}",
        "plan_id": plan.get("plan_id"),
        "plan_hash": plan.get("plan_hash"),
        "profile_json": plan.get("profile_json"),
        "phase": plan.get("phase"),
        "execute": execute,
        "order_style": order_style,
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
    account_profile = str((profile.get("kis") or {}).get("account_profile") or "default")
    report = build_execution_report(
        plan=plan,
        execute=args.execute,
        order_style=args.order_style,
        limit_buffer_pct=args.limit_buffer_pct,
    )

    blockers: list[str] = []
    if plan.get("phase") == "full":
        blockers.append("phase_full_is_review_only")
    if not plan.get("rebalance_allowed"):
        blockers.append(f"rebalance_not_allowed:{plan.get('rebalance_reason')}")
    if not report["orders"]:
        blockers.append("no_nonzero_orders")

    config = load_kr_kis_config(account_profile=account_profile)
    if args.execute and config.env == "live" and not args.confirm_live:
        blockers.append("missing_confirm_live")
    if args.execute and not args.ignore_market_hours and not _regular_krx_market_is_open():
        blockers.append("outside_krx_regular_market_hours")
    report["blocked_reasons"] = blockers
    report["blocked_reason"] = blockers[0] if blockers else None

    if blockers:
        if args.output_json:
            write_json(_resolve_path(args.output_json), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if args.execute else 0

    client = KRKISClient(config)
    client.issue_access_token()
    for index, row in enumerate(report["orders"]):
        if index:
            time.sleep(max(args.sleep_seconds, 0.0))
        symbol = str(row["symbol"]).strip()
        side = str(row["action"]).lower()
        qty = int(row["order_qty"])
        order_division, order_price = _order_price(row, order_style=args.order_style, buffer_pct=args.limit_buffer_pct)
        submitted: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "order_division": order_division,
            "order_price": order_price,
            "dry_run": not args.execute,
        }
        if args.execute:
            try:
                response = client.domestic_order(
                    side=side,
                    symbol=symbol,
                    qty=qty,
                    price=order_price,
                    order_division=order_division,
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
