#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.kis.client import KISClient, load_kis_config, redacted_account
from us_stock_live.strategy.target_weights import (
    DEFAULT_PROFILE_JSON,
    _profile_strategy_feature_tail_rows,
    _profile_strategy_live_extension_options,
    _profile_strategy_defaults,
    _resolve_path,
    build_latest_target_weights,
)
from us_stock_live.trading.state import (
    build_plan_id,
    load_state,
    register_plan,
    short_hash,
    state_path_for_profile,
    write_json,
)
from live_common.rebalance_policy import (
    CASH_TOPUP_REASON,
    CASH_TOPUP_WAITING_BUY_REASON,
    cash_topup_config,
    evaluate_cash_topup,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a USD-denominated stock-live rebalance plan without orders.")
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="Stock-live profile JSON path.")
    parser.add_argument("--as-of", default="", help="Optional target date/time for target weights.")
    parser.add_argument("--equity-usd", type=float, default=None, help="Override total account equity in USD.")
    parser.add_argument("--cash-usd", type=float, default=None, help="Override available cash in USD.")
    parser.add_argument("--min-order-usd", type=float, default=50.0, help="Skip order lines below this notional.")
    parser.add_argument(
        "--phase",
        choices=["full", "sell", "buy"],
        default="full",
        help="full shows both sides for review; sell/buy are execution phases. Buy should be run after sell fills and cash refresh.",
    )
    parser.add_argument(
        "--trade-mode",
        choices=["signal-change-only", "always"],
        default="signal-change-only",
        help="signal-change-only suppresses repeat drift rebalancing for an already processed change event.",
    )
    parser.add_argument(
        "--bootstrap-policy",
        choices=["empty-account", "always", "never"],
        default="empty-account",
        help="How to handle a missing state file. Default allows initial alignment only when strategy holdings are absent.",
    )
    parser.add_argument("--state-json", default="", help="Optional state JSON path. Defaults to us_stock_live/.cache by profile.")
    parser.add_argument("--save-plan", default="", help="Optional path to save the generated plan JSON and register it in state.")
    parser.add_argument("--sleep-seconds", type=float, default=1.1, help="Delay between KIS quote/orderable calls.")
    parser.add_argument("--output-json", default="", help="Optional output JSON path.")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format.")
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_float(value: Any) -> float:
    try:
        text = str(value).replace(",", "").strip()
        if not text:
            return 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _to_int_floor(value: float) -> int:
    if value <= 0 or not math.isfinite(value):
        return 0
    return int(math.floor(value + 1e-9))


def _reduce_buy_orders_to_cash(rows: list[dict[str, Any]], cash_usd: float) -> tuple[bool, float]:
    buy_rows = [row for row in rows if row["raw_action"] == "buy" and row["raw_order_qty"] > 0]
    raw_buy_notional = sum(float(row["raw_order_notional_usd"]) for row in buy_rows)
    if raw_buy_notional <= max(cash_usd, 0.0):
        for row in rows:
            row["cash_limited"] = False
        return False, raw_buy_notional

    cash_limit = max(cash_usd, 0.0)
    for row in rows:
        row["cash_limited"] = False

    while sum(float(row["order_notional_usd"]) for row in buy_rows) > cash_limit:
        reducible = [row for row in buy_rows if int(row["order_qty"]) > 0]
        if not reducible:
            break
        excess = sum(float(row["order_notional_usd"]) for row in buy_rows) - cash_limit

        def reduction_key(row: dict[str, Any]) -> tuple[int, float, float]:
            price = float(row["price_usd"])
            after_notional = float(row["order_notional_usd"]) - price
            after_gap = max(float(row["target_value_usd"]) - after_notional, 0.0)
            already_over_target = float(row["order_notional_usd"]) >= float(row["target_value_usd"])
            removes_enough = price >= excess
            return (
                0 if removes_enough else 1,
                0 if already_over_target else after_gap,
                price,
            )

        chosen = min(reducible, key=reduction_key)
        chosen["order_qty"] = int(chosen["order_qty"]) - 1
        chosen["order_notional_usd"] = int(chosen["order_qty"]) * float(chosen["price_usd"])
        chosen["cash_limited"] = True
        if chosen["order_qty"] <= 0:
            chosen["action"] = "hold"

    for row in buy_rows:
        if int(row["order_qty"]) != int(row["raw_order_qty"]):
            row["cash_limited"] = True
    return True, raw_buy_notional


def _apply_phase(rows: list[dict[str, Any]], phase: str) -> None:
    if phase == "full":
        return
    for row in rows:
        if phase == "sell" and row["raw_action"] != "sell":
            row["action"] = "hold"
            row["order_qty"] = 0
            row["order_notional_usd"] = 0.0
            row["phase_blocked"] = True
        elif phase == "buy" and row["raw_action"] != "buy":
            row["action"] = "hold"
            row["order_qty"] = 0
            row["order_notional_usd"] = 0.0
            row["phase_blocked"] = True
        else:
            row["phase_blocked"] = False


def _profile_kis(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    kis = profile.get("kis") or {}
    return kis, kis.get("symbols") or {}


def _symbol_setting(symbol_settings: dict[str, Any], symbol: str) -> dict[str, Any]:
    return symbol_settings.get(symbol.upper()) or {}


def _last_price(payload: dict[str, Any]) -> float:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return 0.0
    return _to_float(output.get("last") or output.get("last_price") or output.get("ovrs_nmix_prpr"))


def _holding_qty(item: dict[str, Any]) -> float:
    return _to_float(item.get("ovrs_cblc_qty") or item.get("ord_psbl_qty"))


def _holding_symbol(item: dict[str, Any]) -> str:
    return str(item.get("ovrs_pdno") or item.get("pdno") or "").upper().strip()


def _collect_holdings(client: KISClient, balance_exchanges: list[str], *, sleep_seconds: float) -> dict[str, float]:
    holdings: dict[str, float] = {}
    for index, exchange in enumerate(balance_exchanges):
        if index:
            time.sleep(max(sleep_seconds, 0.0))
        payload = client.overseas_balance(exchange_code=exchange, currency="USD")
        output1 = payload.get("output1") or []
        if not isinstance(output1, list):
            continue
        for item in output1:
            if not isinstance(item, dict):
                continue
            symbol = _holding_symbol(item)
            qty = _holding_qty(item)
            if symbol and qty:
                # KIS can return the same US holding for multiple exchange filters.
                # Treat duplicate symbols as the same position instead of summing.
                holdings[symbol] = max(holdings.get(symbol, 0.0), qty)
    return holdings


def _orderable_cash(payload: dict[str, Any]) -> float:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return 0.0
    return _to_float(
        output.get("ovrs_ord_psbl_amt")
        or output.get("ord_psbl_frcr_amt")
        or output.get("frcr_ord_psbl_amt")
        or output.get("max_ord_psbl_amt")
    )


def build_rebalance_plan(
    *,
    profile_json: Path,
    as_of: str = "",
    equity_usd_override: float | None = None,
    cash_usd_override: float | None = None,
    min_order_usd: float = 50.0,
    phase: str = "full",
    trade_mode: str = "signal-change-only",
    bootstrap_policy: str = "empty-account",
    state_json: Path | None = None,
    save_plan: Path | None = None,
    sleep_seconds: float = 1.1,
) -> dict[str, Any]:
    profile = _read_json(profile_json)
    kis_profile, symbol_settings = _profile_kis(profile)
    default_price_exchange = str(kis_profile.get("default_price_exchange") or "AMS")
    default_balance_exchange = str(kis_profile.get("default_balance_exchange") or "AMEX")

    config_json, source_cache_dir, context = _profile_strategy_defaults(profile_json)
    feature_tail_rows = _profile_strategy_feature_tail_rows(profile_json)
    extension_options = _profile_strategy_live_extension_options(profile_json)
    target_payload = build_latest_target_weights(
        config_json=config_json,
        context=context,
        source_cache_dir_override=source_cache_dir,
        as_of=as_of,
        feature_tail_rows=feature_tail_rows,
        **extension_options,
    )
    target_symbols = [str(item["symbol"]).upper() for item in target_payload["weights"]]
    profile_symbols = [str(symbol).upper() for symbol in profile.get("data", {}).get("tickers", [])]
    all_symbols = sorted(set(profile_symbols) | set(target_symbols))

    config = load_kis_config()
    client = KISClient(config)
    client.issue_access_token()

    prices: dict[str, float] = {}
    for index, symbol in enumerate(all_symbols):
        if index:
            time.sleep(max(sleep_seconds, 0.0))
        setting = _symbol_setting(symbol_settings, symbol)
        exchange = str(setting.get("price_exchange") or default_price_exchange)
        prices[symbol] = _last_price(client.overseas_price(symbol, exchange_code=exchange))

    balance_exchanges = sorted(
        {
            str((_symbol_setting(symbol_settings, symbol).get("balance_exchange") or default_balance_exchange))
            for symbol in all_symbols
        }
    )
    time.sleep(max(sleep_seconds, 0.0))
    holdings = _collect_holdings(client, balance_exchanges, sleep_seconds=sleep_seconds)
    strategy_holdings_present = any(holdings.get(symbol, 0.0) > 0 for symbol in profile_symbols)
    holding_value_usd = sum(qty * prices.get(symbol, 0.0) for symbol, qty in holdings.items())

    cash_usd = cash_usd_override
    cash_source = "override" if cash_usd is not None else "kis_orderable_amount"
    if cash_usd is None:
        if target_symbols:
            symbol = target_symbols[0]
        elif all_symbols:
            symbol = all_symbols[0]
        else:
            symbol = ""
        if symbol:
            setting = _symbol_setting(symbol_settings, symbol)
            exchange = str(setting.get("balance_exchange") or default_balance_exchange)
            price = prices.get(symbol, 1.0) or 1.0
            time.sleep(max(sleep_seconds, 0.0))
            cash_usd = _orderable_cash(client.overseas_orderable_amount(symbol=symbol, price=price, exchange_code=exchange))
        else:
            cash_usd = 0.0

    equity_source = "override" if equity_usd_override is not None else "cash_plus_holdings"
    equity_usd = float(equity_usd_override) if equity_usd_override is not None else float(cash_usd + holding_value_usd)

    target_weight_by_symbol = {
        str(item["symbol"]).upper(): float(item["target_weight"]) for item in target_payload["weights"]
    }
    state_path = state_json or state_path_for_profile(profile_json)
    state = load_state(state_path)
    last_executed_change_timestamp = state.get("last_executed_change_timestamp")
    current_change_timestamp = target_payload.get("last_change_timestamp")
    rebalance_allowed = True
    rebalance_reason = "always"
    if trade_mode == "signal-change-only":
        if not current_change_timestamp:
            rebalance_allowed = False
            rebalance_reason = "no_signal_change_event"
        elif last_executed_change_timestamp == current_change_timestamp:
            rebalance_allowed = False
            rebalance_reason = "change_event_already_executed"
        elif not last_executed_change_timestamp:
            if bootstrap_policy == "always":
                rebalance_reason = "bootstrap_state_missing"
            elif bootstrap_policy == "empty-account" and not strategy_holdings_present:
                rebalance_reason = "bootstrap_empty_account"
            else:
                rebalance_allowed = False
                rebalance_reason = "state_missing_bootstrap_blocked"
        else:
            rebalance_reason = "new_unexecuted_change_event"

    cash_topup_enabled, cash_topup_min_cash_pct = cash_topup_config(profile)
    cash_topup = evaluate_cash_topup(
        phase=phase,
        rebalance_reason=rebalance_reason,
        cash=float(cash_usd),
        equity=float(equity_usd),
        enabled=cash_topup_enabled,
        min_cash_pct=cash_topup_min_cash_pct,
    )
    topup_plan_allowed = bool(cash_topup["needed"])
    topup_buy_allowed = bool(cash_topup["buy_allowed"])
    effective_rebalance_allowed = rebalance_allowed or topup_plan_allowed
    effective_rebalance_reason = rebalance_reason
    if topup_plan_allowed:
        effective_rebalance_reason = CASH_TOPUP_REASON if topup_buy_allowed else CASH_TOPUP_WAITING_BUY_REASON

    rows = []
    for symbol in sorted(set(holdings) | set(target_weight_by_symbol)):
        price = prices.get(symbol, 0.0)
        current_qty = holdings.get(symbol, 0.0)
        current_value = current_qty * price
        target_weight = target_weight_by_symbol.get(symbol, 0.0)
        target_value = equity_usd * target_weight
        delta_value = target_value - current_value
        abs_delta = abs(delta_value)
        if not effective_rebalance_allowed:
            action = "hold"
            order_qty = 0
        elif topup_plan_allowed:
            if topup_buy_allowed and abs_delta >= min_order_usd and price > 0 and delta_value > 0:
                action = "buy"
                order_qty = _to_int_floor(delta_value / price)
            else:
                action = "hold"
                order_qty = 0
        elif abs_delta < min_order_usd or price <= 0:
            action = "hold"
            order_qty = 0
        elif delta_value > 0:
            action = "buy"
            order_qty = _to_int_floor(delta_value / price)
        else:
            action = "sell"
            if target_weight <= 0.0 or target_value <= 0.0:
                order_qty = _to_int_floor(current_qty)
            else:
                order_qty = min(_to_int_floor(abs_delta / price), _to_int_floor(current_qty))
        rows.append(
            {
                "symbol": symbol,
                "price_usd": price,
                "current_qty": current_qty,
                "current_value_usd": current_value,
                "target_weight": target_weight,
                "target_value_usd": target_value,
                "delta_value_usd": delta_value,
                "action": action,
                "order_qty": order_qty,
                "order_notional_usd": order_qty * price,
                "raw_action": action,
                "raw_order_qty": order_qty,
                "raw_order_notional_usd": order_qty * price,
                "cash_limited": False,
                "phase_blocked": False,
                "rebalance_allowed": effective_rebalance_allowed,
                "rebalance_reason": effective_rebalance_reason,
            }
        )

    _apply_phase(rows, phase)
    if phase in {"full", "buy"}:
        cash_limited, raw_buy_notional_usd = _reduce_buy_orders_to_cash(rows, float(cash_usd))
    else:
        raw_buy_notional_usd = sum(
            float(row["raw_order_notional_usd"]) for row in rows if row["raw_action"] == "buy"
        )
        cash_limited = False
    planned_buy_notional_usd = sum(float(row["order_notional_usd"]) for row in rows if row["action"] == "buy")
    planned_sell_notional_usd = sum(float(row["order_notional_usd"]) for row in rows if row["action"] == "sell")

    payload = {
        "type": "stock_live_rebalance_plan_v1",
        "profile_json": str(profile_json.relative_to(ROOT_DIR) if profile_json.is_relative_to(ROOT_DIR) else profile_json),
        "run_name": target_payload["run_name"],
        "account": redacted_account(config),
        "target_timestamp": target_payload["target_timestamp"],
        "latest_feature_timestamp": target_payload["latest_feature_timestamp"],
        "cash_usd": cash_usd,
        "cash_source": cash_source,
        "holding_value_usd": holding_value_usd,
        "equity_usd": equity_usd,
        "equity_source": equity_source,
        "min_order_usd": min_order_usd,
        "phase": phase,
        "cash_limited": cash_limited,
        "raw_buy_notional_usd": raw_buy_notional_usd,
        "planned_buy_notional_usd": planned_buy_notional_usd,
        "planned_sell_notional_usd": planned_sell_notional_usd,
        "cash_after_planned_buys_usd": float(cash_usd) - planned_buy_notional_usd,
        "trade_mode": trade_mode,
        "bootstrap_policy": bootstrap_policy,
        "state_json": str(state_path.relative_to(ROOT_DIR) if state_path.is_relative_to(ROOT_DIR) else state_path),
        "last_change_timestamp": current_change_timestamp,
        "target_is_signal_change": target_payload.get("target_is_signal_change"),
        "last_executed_change_timestamp": last_executed_change_timestamp,
        "strategy_holdings_present": strategy_holdings_present,
        "cash_topup_enabled": cash_topup_enabled,
        "cash_topup_min_cash_pct": cash_topup_min_cash_pct,
        "cash_topup_cash_pct": cash_topup["cash_pct"],
        "cash_topup_needed": cash_topup["needed"],
        "cash_topup_buy_allowed": cash_topup["buy_allowed"],
        "rebalance_allowed": effective_rebalance_allowed,
        "rebalance_reason": effective_rebalance_reason,
        "orders_enabled": False,
        "rows": rows,
    }
    payload["plan_id"] = build_plan_id(payload)
    payload["plan_hash"] = short_hash(payload)
    if save_plan:
        write_json(save_plan, payload)
        state = register_plan(state, payload)
        write_json(state_path, state)
    return payload


def _print_table(payload: dict[str, Any]) -> None:
    print(f"target_timestamp: {payload['target_timestamp']}")
    print(f"latest_feature_timestamp: {payload['latest_feature_timestamp']}")
    print(f"equity_usd: {payload['equity_usd']:.2f} ({payload['equity_source']})")
    print(f"cash_usd: {payload['cash_usd']:.2f} ({payload['cash_source']})")
    print(f"holding_value_usd: {payload['holding_value_usd']:.2f}")
    print(f"phase: {payload['phase']}")
    print(f"cash_limited: {payload['cash_limited']}")
    print(f"planned_buy_notional_usd: {payload['planned_buy_notional_usd']:.2f}")
    print(f"cash_after_planned_buys_usd: {payload['cash_after_planned_buys_usd']:.2f}")
    print(f"last_change_timestamp: {payload['last_change_timestamp']}")
    print(f"last_executed_change_timestamp: {payload['last_executed_change_timestamp']}")
    print(f"trade_mode: {payload['trade_mode']}")
    print(f"rebalance_allowed: {payload['rebalance_allowed']} ({payload['rebalance_reason']})")
    print(f"orders_enabled: {payload['orders_enabled']}")
    print()
    print("symbol  action  qty  raw_qty  price_usd  target_w  current_usd  target_usd  delta_usd  cash_cap  phase_block")
    for row in payload["rows"]:
        print(
            f"{row['symbol']:<7} {row['action']:<6} {row['order_qty']:>4} {row['raw_order_qty']:>7} "
            f"{row['price_usd']:>9.4f} {row['target_weight']:>8.4f} "
            f"{row['current_value_usd']:>11.2f} {row['target_value_usd']:>10.2f} {row['delta_value_usd']:>10.2f} "
            f"{str(row['cash_limited']):>8} {str(row['phase_blocked']):>12}"
        )


def main() -> int:
    args = build_parser().parse_args()
    payload = build_rebalance_plan(
        profile_json=_resolve_path(args.profile_json),
        as_of=args.as_of,
        equity_usd_override=args.equity_usd,
        cash_usd_override=args.cash_usd,
        min_order_usd=args.min_order_usd,
        phase=args.phase,
        trade_mode=args.trade_mode,
        bootstrap_policy=args.bootstrap_policy,
        state_json=_resolve_path(args.state_json) if args.state_json else None,
        save_plan=_resolve_path(args.save_plan) if args.save_plan else None,
        sleep_seconds=args.sleep_seconds,
    )
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(output_path, payload)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
