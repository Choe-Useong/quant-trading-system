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

from kr_stock_live.kis.client import KRKISClient, load_kr_kis_config
from kr_stock_live.active_profile import resolve_profile_reference
from kr_stock_live.strategy.target_weights import DEFAULT_PROFILE_JSON
from kr_stock_live.trading.state import (
    build_plan_id,
    load_state,
    register_plan,
    short_hash,
    state_path_for_profile,
    write_json,
)
from live_common.kis_base import redacted_account
from live_common.rebalance_policy import (
    CASH_TOPUP_REASON,
    CASH_TOPUP_WAITING_BUY_REASON,
    cash_topup_config,
    evaluate_cash_topup,
)
from live_common.target_weights import (
    _resolve_path,
    _strategy_feature_tail_rows,
    build_latest_target_weights,
    live_target_extension_options,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a KRW-denominated Korean-stock/ETF rebalance plan.")
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="KR stock live profile JSON path.")
    parser.add_argument("--as-of", default="", help="Optional target date/time for target weights.")
    parser.add_argument("--equity-krw", type=float, default=None, help="Override total account equity in KRW.")
    parser.add_argument("--cash-krw", type=float, default=None, help="Override available cash in KRW.")
    parser.add_argument("--min-order-krw", type=float, default=10000.0, help="Skip order lines below this notional.")
    parser.add_argument(
        "--phase",
        choices=["full", "sell", "buy"],
        default="full",
        help="full shows both sides; sell/buy are execution phases.",
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
        help="How to handle a missing state file.",
    )
    parser.add_argument("--state-json", default="", help="Optional state JSON path. Defaults by profile.")
    parser.add_argument("--save-plan", default="", help="Optional path to save the generated plan JSON and register it.")
    parser.add_argument("--sleep-seconds", type=float, default=1.2, help="Delay between KIS quote calls.")
    parser.add_argument("--order-style", choices=["limit", "market"], default="limit")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.0)
    parser.add_argument(
        "--buy-cash-buffer-pct",
        type=float,
        default=None,
        help="Optional cash reserve percent for buy sizing. Defaults to profile trading.buy_cash_buffer_pct.",
    )
    parser.add_argument("--output-json", default="", help="Optional output JSON path.")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format.")
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_float(value: Any) -> float:
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int_floor(value: float) -> int:
    if value <= 0 or not math.isfinite(value):
        return 0
    return int(math.floor(value))


def _profile_strategy(profile: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
    strategy = profile["strategy"]
    return (
        _resolve_path(strategy["config_json"]),
        _resolve_path(strategy["source_cache_dir"]),
        dict(strategy.get("context") or {}),
    )


def _profile_buy_cash_buffer_pct(profile: dict[str, Any]) -> float:
    trading = profile.get("trading") if isinstance(profile.get("trading"), dict) else {}
    value = trading.get("buy_cash_buffer_pct", profile.get("buy_cash_buffer_pct", 0.0))
    return max(min(float(value or 0.0), 100.0), 0.0)


def _collect_holdings(payload: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    holdings: dict[str, float] = {}
    values: dict[str, float] = {}
    output1 = payload.get("output1") or []
    if not isinstance(output1, list):
        return holdings, values
    for item in output1:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("pdno") or "").strip()
        qty = _to_float(item.get("hldg_qty"))
        value = _to_float(item.get("evlu_amt"))
        if symbol and qty > 0:
            holdings[symbol] = qty
            values[symbol] = value
    return holdings, values


def _account_summary(payload: dict[str, Any]) -> dict[str, float]:
    output2 = payload.get("output2") or []
    row = output2[0] if isinstance(output2, list) and output2 else {}
    return {
        "deposit_cash_krw": _to_float(row.get("dnca_tot_amt")),
        "previous_received_cash_krw": _to_float(row.get("prvs_rcdl_excc_amt")),
        "total_eval_krw": _to_float(row.get("tot_evlu_amt")),
        "net_asset_krw": _to_float(row.get("nass_amt")),
    }


def _last_price(payload: dict[str, Any]) -> float:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return 0.0
    return _to_float(output.get("stck_prpr"))


def _price_tick_unit(payload: dict[str, Any]) -> int:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return 1
    return max(_to_int_floor(_to_float(output.get("aspr_unit"))), 1)


def _round_price_to_tick(price: float, tick_unit: int, *, direction: str) -> int:
    tick = max(int(tick_unit), 1)
    if price <= 0:
        return 0
    if direction == "down":
        return max(int(math.floor(price / tick) * tick), tick)
    return max(int(math.ceil(price / tick) * tick), tick)


def _orderable_price(price: float, *, order_style: str, limit_buffer_pct: float, tick_unit: int) -> float:
    if order_style == "market" or price <= 0:
        return price
    return float(
        _round_price_to_tick(
            price * (1.0 + limit_buffer_pct / 100.0),
            tick_unit,
            direction="up",
        )
    )


def _orderable_cash_summary(
    client: KRKISClient,
    *,
    target_symbols: list[str],
    prices: dict[str, float],
    order_division: str,
    sleep_seconds: float,
) -> dict[str, Any]:
    by_symbol: dict[str, dict[str, float]] = {}
    first_symbol = ""
    first_orderable_cash_krw = 0.0
    first_raw_orderable_cash_krw = 0.0
    first_orderable_qty = 0.0
    for index, symbol in enumerate(target_symbols):
        price = prices.get(symbol, 0.0)
        if price <= 0:
            continue
        if index:
            time.sleep(max(sleep_seconds, 0.0))
        payload = client.domestic_orderable_amount(symbol=symbol, price=str(price), order_division=order_division)
        output = payload.get("output") or {}
        if not isinstance(output, dict):
            continue
        item = {
            "orderable_cash_krw": _to_float(output.get("nrcvb_buy_amt")),
            "raw_orderable_cash_krw": _to_float(output.get("ord_psbl_cash")),
            "orderable_qty": _to_float(output.get("nrcvb_buy_qty")),
        }
        by_symbol[symbol] = item
        if not first_symbol:
            first_symbol = symbol
            first_orderable_cash_krw = item["orderable_cash_krw"]
            first_raw_orderable_cash_krw = item["raw_orderable_cash_krw"]
            first_orderable_qty = item["orderable_qty"]
    return {
        "symbol": first_symbol,
        "orderable_cash_krw": first_orderable_cash_krw,
        "raw_orderable_cash_krw": first_raw_orderable_cash_krw,
        "orderable_qty": first_orderable_qty,
        "by_symbol": by_symbol,
    }


def _reduce_buy_orders_to_cash(rows: list[dict[str, Any]], cash_krw: float) -> tuple[bool, float]:
    buy_rows = [row for row in rows if row["raw_action"] == "buy" and row["raw_order_qty"] > 0]
    raw_buy_notional = sum(float(row["raw_order_notional_krw"]) for row in buy_rows)
    if raw_buy_notional <= max(cash_krw, 0.0):
        for row in rows:
            row["cash_limited"] = False
        return False, raw_buy_notional

    cash_limit = max(cash_krw, 0.0)
    for row in rows:
        row["cash_limited"] = False

    while sum(float(row["order_notional_krw"]) for row in buy_rows) > cash_limit:
        reducible = [row for row in buy_rows if int(row["order_qty"]) > 0]
        if not reducible:
            break
        chosen = max(reducible, key=lambda row: float(row["order_notional_krw"]))
        chosen["order_qty"] = int(chosen["order_qty"]) - 1
        chosen["order_notional_krw"] = int(chosen["order_qty"]) * float(chosen["price_krw"])
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
            row["order_notional_krw"] = 0.0
            row["phase_blocked"] = True
        elif phase == "buy" and row["raw_action"] != "buy":
            row["action"] = "hold"
            row["order_qty"] = 0
            row["order_notional_krw"] = 0.0
            row["phase_blocked"] = True
        else:
            row["phase_blocked"] = False


def _allocate_target_quantities(rows: list[dict[str, Any]], *, equity_krw: float) -> None:
    selected_rows = [row for row in rows if float(row["target_weight"]) > 0 and float(row["price_krw"]) > 0]
    if not selected_rows:
        return

    gross_target_weight = sum(float(row["target_weight"]) for row in selected_rows)
    available_cash = max(float(equity_krw) * gross_target_weight, 0.0)
    target_rows = sorted(selected_rows, key=lambda row: (-float(row["target_weight"]), int(row["selection_order"])))

    # Small-account mode: keep the selected basket intact when possible before
    # approximating the ideal fractional target weights.
    for row in target_rows:
        if available_cash >= float(row["price_krw"]):
            row["target_qty"] = max(int(row["target_qty"]), 1)
            available_cash -= float(row["price_krw"])

    while True:
        candidates = []
        for row in target_rows:
            price = float(row["price_krw"])
            current_target_value = int(row["target_qty"]) * price
            desired_value = float(equity_krw) * float(row["target_weight"])
            shortfall = desired_value - current_target_value
            if price <= available_cash:
                candidates.append((shortfall, -price, row))
        if not candidates:
            break
        _, _, chosen = max(candidates, key=lambda item: (item[0], item[1]))
        chosen["target_qty"] = int(chosen["target_qty"]) + 1
        available_cash -= float(chosen["price_krw"])


def build_rebalance_plan(
    *,
    profile_json: Path,
    as_of: str = "",
    equity_krw_override: float | None = None,
    cash_krw_override: float | None = None,
    min_order_krw: float = 10000.0,
    phase: str = "full",
    trade_mode: str = "signal-change-only",
    bootstrap_policy: str = "empty-account",
    state_json: Path | None = None,
    save_plan: Path | None = None,
    sleep_seconds: float = 1.2,
    order_style: str = "limit",
    limit_buffer_pct: float = 0.0,
    buy_cash_buffer_pct_override: float | None = None,
) -> dict[str, Any]:
    profile_json, _active_profile = resolve_profile_reference(profile_json)
    profile = _read_json(profile_json)
    buy_cash_buffer_pct = (
        max(min(float(buy_cash_buffer_pct_override), 100.0), 0.0)
        if buy_cash_buffer_pct_override is not None
        else _profile_buy_cash_buffer_pct(profile)
    )
    config_json, source_cache_dir, context = _profile_strategy(profile)
    strategy = profile.get("strategy") or {}
    feature_tail_rows = _strategy_feature_tail_rows(strategy)
    extension_options = live_target_extension_options(strategy)
    target_payload = build_latest_target_weights(
        config_json=config_json,
        context=context,
        source_cache_dir_override=source_cache_dir,
        as_of=as_of,
        feature_tail_rows=feature_tail_rows,
        **extension_options,
    )
    target_symbols = [str(item["symbol"]) for item in target_payload["weights"]]

    account_profile = str((profile.get("kis") or {}).get("account_profile") or "default")
    config = load_kr_kis_config(account_profile=account_profile)
    client = KRKISClient(config)
    client.issue_access_token()

    balance_payload = client.domestic_balance()
    holdings, holding_values = _collect_holdings(balance_payload)
    account_summary = _account_summary(balance_payload)
    all_symbols = sorted(set(holdings) | set(target_symbols))

    prices: dict[str, float] = {}
    tick_units: dict[str, int] = {}
    for index, symbol in enumerate(all_symbols):
        if index:
            time.sleep(max(sleep_seconds, 0.0))
        price_payload = client.domestic_price(symbol)
        prices[symbol] = _last_price(price_payload)
        tick_units[symbol] = _price_tick_unit(price_payload)

    holding_value_krw = sum(
        holding_values.get(symbol, holdings.get(symbol, 0.0) * prices.get(symbol, 0.0))
        for symbol in holdings
    )
    orderable_prices = {
        symbol: _orderable_price(
            prices.get(symbol, 0.0),
            order_style=order_style,
            limit_buffer_pct=limit_buffer_pct,
            tick_unit=tick_units.get(symbol, 1),
        )
        for symbol in target_symbols
    }
    orderable_summary = _orderable_cash_summary(
        client,
        target_symbols=target_symbols,
        prices=orderable_prices,
        order_division="01" if order_style == "market" else "00",
        sleep_seconds=sleep_seconds,
    )
    cash_krw = (
        float(cash_krw_override)
        if cash_krw_override is not None
        else float(orderable_summary["orderable_cash_krw"])
    )
    equity_krw = (
        float(equity_krw_override)
        if equity_krw_override is not None
        else account_summary["total_eval_krw"] or (cash_krw + holding_value_krw)
    )

    target_weight_by_symbol = {str(item["symbol"]): float(item["target_weight"]) for item in target_payload["weights"]}
    selection_order_by_symbol = {str(item["symbol"]): index for index, item in enumerate(target_payload["weights"])}

    state_path = state_json or state_path_for_profile(profile_json)
    state = load_state(state_path)
    last_executed_change_timestamp = state.get("last_executed_change_timestamp")
    current_change_timestamp = target_payload.get("last_change_timestamp")
    strategy_holdings_present = any(holdings.get(symbol, 0.0) > 0 for symbol in target_symbols)
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
        cash=float(cash_krw),
        equity=float(equity_krw),
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
    for symbol in all_symbols:
        price = prices.get(symbol, 0.0)
        current_qty = holdings.get(symbol, 0.0)
        current_value = holding_values.get(symbol, current_qty * price)
        target_weight = target_weight_by_symbol.get(symbol, 0.0)
        rows.append(
            {
                "symbol": symbol,
                "price_krw": price,
                "price_tick_unit": tick_units.get(symbol, 1),
                "current_qty": current_qty,
                "current_value_krw": current_value,
                "target_weight": target_weight,
                "target_value_krw": equity_krw * target_weight,
                "target_qty": 0,
                "selection_order": selection_order_by_symbol.get(symbol, 10**9),
                "delta_value_krw": 0.0,
                "action": "hold",
                "order_qty": 0,
                "order_notional_krw": 0.0,
                "raw_action": "hold",
                "raw_order_qty": 0,
                "raw_order_notional_krw": 0.0,
                "uncapped_order_qty": 0,
                "uncapped_order_notional_krw": 0.0,
                "cash_limited": False,
                "orderable_limited": False,
                "phase_blocked": False,
                "rebalance_allowed": effective_rebalance_allowed,
                "rebalance_reason": effective_rebalance_reason,
            }
        )

    _allocate_target_quantities(rows, equity_krw=equity_krw)
    for row in rows:
        price = float(row["price_krw"])
        current_qty = float(row["current_qty"])
        target_qty = int(row["target_qty"])
        target_value = target_qty * price
        delta_qty = target_qty - current_qty
        delta_value = target_value - float(row["current_value_krw"])
        if not effective_rebalance_allowed:
            action = "hold"
            order_qty = 0
        elif topup_plan_allowed:
            if topup_buy_allowed and abs(delta_value) >= min_order_krw and price > 0 and delta_qty > 0:
                action = "buy"
                order_qty = _to_int_floor(delta_qty)
            else:
                action = "hold"
                order_qty = 0
        elif abs(delta_value) < min_order_krw or price <= 0:
            action = "hold"
            order_qty = 0
        elif delta_qty > 0:
            action = "buy"
            order_qty = _to_int_floor(delta_qty)
        elif delta_qty < 0:
            action = "sell"
            order_qty = min(_to_int_floor(abs(delta_qty)), _to_int_floor(current_qty))
        else:
            action = "hold"
            order_qty = 0
        uncapped_order_qty = order_qty
        orderable_qty = _to_int_floor(
            float((orderable_summary.get("by_symbol") or {}).get(symbol, {}).get("orderable_qty") or 0.0)
        )
        orderable_limited = False
        if action == "buy" and orderable_qty > 0 and order_qty > orderable_qty:
            order_qty = orderable_qty
            orderable_limited = True
        row["target_qty"] = target_qty
        row["price_tick_unit"] = tick_units.get(symbol, 1)
        row["target_value_krw"] = target_value
        row["delta_value_krw"] = delta_value
        row["action"] = action
        row["order_qty"] = order_qty
        row["order_notional_krw"] = order_qty * price
        row["raw_action"] = action
        row["raw_order_qty"] = order_qty
        row["raw_order_notional_krw"] = order_qty * price
        row["uncapped_order_qty"] = uncapped_order_qty
        row["uncapped_order_notional_krw"] = uncapped_order_qty * price
        row["orderable_qty"] = orderable_qty
        row["orderable_price_krw"] = orderable_prices.get(symbol, price)
        row["orderable_limited"] = orderable_limited

    _apply_phase(rows, phase)
    if phase in {"full", "buy"}:
        buy_cash_buffer_krw = cash_krw * buy_cash_buffer_pct / 100.0
        buy_cash_limit_krw = max(cash_krw - buy_cash_buffer_krw, 0.0)
        cash_limited, raw_buy_notional_krw = _reduce_buy_orders_to_cash(rows, buy_cash_limit_krw)
    else:
        buy_cash_buffer_krw = 0.0
        buy_cash_limit_krw = cash_krw
        raw_buy_notional_krw = sum(float(row["raw_order_notional_krw"]) for row in rows if row["raw_action"] == "buy")
        cash_limited = False

    payload = {
        "type": "kr_stock_live_rebalance_plan_v1",
        "profile_json": str(profile_json.relative_to(ROOT_DIR)),
        "run_name": target_payload["run_name"],
        "account": redacted_account(config),
        "account_profile": account_profile,
        "target_timestamp": target_payload["target_timestamp"],
        "latest_feature_timestamp": target_payload["latest_feature_timestamp"],
        "last_change_timestamp": current_change_timestamp,
        "gross_target_weight": target_payload["gross_target_weight"],
        "cash_krw": cash_krw,
        "buy_cash_buffer_pct": buy_cash_buffer_pct,
        "buy_cash_buffer_krw": buy_cash_buffer_krw,
        "buy_cash_limit_krw": buy_cash_limit_krw,
        "deposit_cash_krw": account_summary["deposit_cash_krw"],
        "raw_orderable_cash_krw": orderable_summary["raw_orderable_cash_krw"],
        "orderable_cash_reference_symbol": orderable_summary["symbol"],
        "orderable_qty_reference": orderable_summary["orderable_qty"],
        "order_style_for_orderable": order_style,
        "limit_buffer_pct_for_orderable": limit_buffer_pct,
        "equity_krw": equity_krw,
        "holding_value_krw": holding_value_krw,
        "min_order_krw": min_order_krw,
        "phase": phase,
        "cash_limited": cash_limited,
        "raw_buy_notional_krw": raw_buy_notional_krw,
        "planned_buy_notional_krw": sum(float(row["order_notional_krw"]) for row in rows if row["action"] == "buy"),
        "planned_sell_notional_krw": sum(float(row["order_notional_krw"]) for row in rows if row["action"] == "sell"),
        "trade_mode": trade_mode,
        "bootstrap_policy": bootstrap_policy,
        "state_json": str(state_path.relative_to(ROOT_DIR) if state_path.is_relative_to(ROOT_DIR) else state_path),
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
    print(f"run_name: {payload['run_name']}")
    print(f"account: {payload['account']} ({payload['account_profile']})")
    print(f"target_timestamp: {payload['target_timestamp']}")
    print(f"last_change_timestamp: {payload['last_change_timestamp']}")
    print(f"last_executed_change_timestamp: {payload['last_executed_change_timestamp']}")
    print(f"equity_krw: {payload['equity_krw']:.0f}")
    print(f"cash_krw: {payload['cash_krw']:.0f}")
    print(f"holding_value_krw: {payload['holding_value_krw']:.0f}")
    print(f"phase: {payload['phase']}")
    print(f"rebalance_allowed: {payload['rebalance_allowed']} ({payload['rebalance_reason']})")
    print()
    print("symbol  price_krw  current_qty  target_w  target_qty  delta_krw  action  order_qty")
    for row in payload["rows"]:
        print(
            f"{row['symbol']:<6} "
            f"{row['price_krw']:>9.0f} "
            f"{row['current_qty']:>11.0f} "
            f"{row['target_weight']:>8.4f} "
            f"{row['target_qty']:>10d} "
            f"{row['delta_value_krw']:>10.0f} "
            f"{row['action']:<6} "
            f"{row['order_qty']:>9d}"
        )


def main() -> int:
    args = build_parser().parse_args()
    profile_json = _resolve_path(args.profile_json)
    payload = build_rebalance_plan(
        profile_json=profile_json,
        as_of=args.as_of,
        equity_krw_override=args.equity_krw,
        cash_krw_override=args.cash_krw,
        min_order_krw=args.min_order_krw,
        phase=args.phase,
        trade_mode=args.trade_mode,
        bootstrap_policy=args.bootstrap_policy,
        state_json=_resolve_path(args.state_json) if args.state_json else None,
        save_plan=_resolve_path(args.save_plan) if args.save_plan else None,
        sleep_seconds=args.sleep_seconds,
        order_style=args.order_style,
        limit_buffer_pct=args.limit_buffer_pct,
        buy_cash_buffer_pct_override=args.buy_cash_buffer_pct,
    )
    if args.output_json:
        output_path = _resolve_path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(output_path, payload)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
