#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live.check_balance import ENV_PATH, UPBIT_BASE_URL, _authorized_get, _encode_jwt_hs512, _load_dotenv
from live.dry_run_executor import MIN_ORDER_KRW, _build_live_signal_rows, _safe_float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or execute live Upbit signal orders.")
    parser.add_argument(
        "--mode",
        choices=["preview", "live"],
        default="preview",
        help="Execution mode. preview prints the plan only, live sends orders immediately without extra confirmation.",
    )
    parser.add_argument(
        "--ignore-unmanaged",
        action="store_true",
        help="Allow execution even when holdings exist outside the managed asset universe.",
    )
    parser.add_argument(
        "--min-order-krw",
        type=float,
        default=MIN_ORDER_KRW,
        help="Minimum absolute KRW notional required before an order is sent.",
    )
    return parser


def _build_query_string(params: dict[str, object]) -> str:
    normalized: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        normalized.append((key, str(value)))
    return urllib.parse.urlencode(normalized, doseq=True)


def _authorized_post(path: str, body: dict[str, object]) -> dict[str, object]:
    import os

    access_key = os.environ.get("UPBIT_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("UPBIT_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise RuntimeError("Missing UPBIT_ACCESS_KEY or UPBIT_SECRET_KEY")

    query_string = _build_query_string(body)
    token = _encode_jwt_hs512(
        {
            "access_key": access_key,
            "nonce": str(uuid.uuid4()),
            "query_hash": hashlib.sha512(query_string.encode("utf-8")).hexdigest(),
            "query_hash_alg": "SHA512",
        },
        secret_key,
    )
    req = urllib.request.Request(
        f"{UPBIT_BASE_URL}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _build_execution_plan(min_order_krw: float) -> dict[str, object]:
    balances = _authorized_get("/v1/accounts")
    signal_rows, latest_price_by_asset = _build_live_signal_rows()

    supported_rows = [row for row in signal_rows if row.get("supported") is True]
    supported_assets = {str(row["asset"]).upper() for row in supported_rows}
    on_assets = sorted(str(row["asset"]).upper() for row in supported_rows if bool(row.get("signal_on")))

    balance_by_currency = {str(item.get("currency", "")).upper(): item for item in balances}
    available_krw = _safe_float(balance_by_currency.get("KRW", {}).get("balance")) + _safe_float(
        balance_by_currency.get("KRW", {}).get("locked")
    )

    managed_holdings: list[dict[str, object]] = []
    unmanaged_holdings: list[dict[str, object]] = []
    for item in balances:
        currency = str(item.get("currency", "")).upper()
        if currency == "KRW":
            continue
        qty = _safe_float(item.get("balance")) + _safe_float(item.get("locked"))
        if qty <= 0.0:
            continue
        price = latest_price_by_asset.get(currency)
        current_value = qty * price if price is not None else None
        row = {
            "asset": currency,
            "quantity": qty,
            "latest_price": price,
            "current_value_krw": current_value,
        }
        if currency in supported_assets:
            managed_holdings.append(row)
        else:
            unmanaged_holdings.append(row)

    managed_equity_krw = available_krw + sum(
        float(item["current_value_krw"])
        for item in managed_holdings
        if item["current_value_krw"] is not None
    )
    target_weight = (1.0 / len(on_assets)) if on_assets else 0.0

    actions: list[dict[str, object]] = []
    for row in supported_rows:
        asset = str(row["asset"]).upper()
        latest_price = float(row["latest_price"])
        signal_on = bool(row["signal_on"])
        balance_item = balance_by_currency.get(asset, {})
        current_qty = _safe_float(balance_item.get("balance")) + _safe_float(balance_item.get("locked"))
        current_value_krw = current_qty * latest_price
        target_value_krw = managed_equity_krw * target_weight if signal_on else 0.0
        delta_value_krw = target_value_krw - current_value_krw

        if abs(delta_value_krw) < min_order_krw:
            action = "hold"
        elif delta_value_krw > 0:
            action = "buy"
        else:
            action = "sell"

        actions.append(
            {
                "asset": asset,
                "signal_on": signal_on,
                "latest_price": latest_price,
                "current_qty": current_qty,
                "current_value_krw": current_value_krw,
                "target_weight": target_weight if signal_on else 0.0,
                "target_value_krw": target_value_krw,
                "delta_value_krw": delta_value_krw,
                "action": action,
            }
        )

    return {
        "allocation_mode": "equal_weight_on_signals",
        "managed_equity_krw": managed_equity_krw,
        "available_krw": available_krw,
        "min_order_krw": min_order_krw,
        "supported_assets": sorted(supported_assets),
        "on_assets": on_assets,
        "unmanaged_holdings": unmanaged_holdings,
        "actions": actions,
    }


def _place_sell_market_order(asset: str, volume: float) -> dict[str, object]:
    body = {
        "market": f"KRW-{asset}",
        "side": "ask",
        "volume": f"{volume:.16f}".rstrip("0").rstrip("."),
        "ord_type": "market",
        "identifier": f"live-sell-{asset.lower()}-{uuid.uuid4().hex[:12]}",
    }
    return _authorized_post("/v1/orders", body)


def _place_buy_market_order(asset: str, price_krw: float) -> dict[str, object]:
    body = {
        "market": f"KRW-{asset}",
        "side": "bid",
        "price": f"{price_krw:.0f}",
        "ord_type": "price",
        "identifier": f"live-buy-{asset.lower()}-{uuid.uuid4().hex[:12]}",
    }
    return _authorized_post("/v1/orders", body)


def main() -> int:
    args = build_parser().parse_args()
    _load_dotenv(ENV_PATH)

    try:
        plan = _build_execution_plan(args.min_order_krw)
    except Exception as exc:
        print(f"Execution plan failed: {exc}", file=sys.stderr)
        return 1

    if plan["unmanaged_holdings"] and not args.ignore_unmanaged:
        print(json.dumps({**plan, "execution_blocked": True, "reason": "unmanaged_holdings_present"}, ensure_ascii=False, indent=2))
        return 2

    if args.mode == "preview":
        print(json.dumps({**plan, "mode": "preview"}, ensure_ascii=False, indent=2))
        return 0

    execution_results: list[dict[str, object]] = []
    sell_actions = [row for row in plan["actions"] if row["action"] == "sell"]
    buy_actions = [row for row in plan["actions"] if row["action"] == "buy"]

    for action in sell_actions:
        asset = str(action["asset"])
        current_qty = float(action["current_qty"])
        target_qty = float(action["target_value_krw"]) / float(action["latest_price"]) if float(action["latest_price"]) > 0 else 0.0
        sell_qty = max(current_qty - target_qty, 0.0)
        if sell_qty * float(action["latest_price"]) < args.min_order_krw:
            execution_results.append({**action, "submitted": False, "reason": "below_min_order"})
            continue
        try:
            response = _place_sell_market_order(asset, sell_qty)
            execution_results.append({**action, "submitted": True, "order_response": response})
        except Exception as exc:
            execution_results.append({**action, "submitted": False, "error": str(exc)})

    try:
        refreshed_balances = _authorized_get("/v1/accounts")
        available_krw = _safe_float(next((item.get("balance") for item in refreshed_balances if str(item.get("currency", "")).upper() == "KRW"), 0.0))
    except Exception:
        available_krw = float(plan["available_krw"])

    remaining_cash = available_krw
    for action in buy_actions:
        buy_value = min(float(action["delta_value_krw"]), remaining_cash)
        if buy_value < args.min_order_krw:
            execution_results.append({**action, "submitted": False, "reason": "below_min_order_or_no_cash"})
            continue
        try:
            response = _place_buy_market_order(str(action["asset"]), buy_value)
            remaining_cash -= buy_value
            execution_results.append({**action, "submitted": True, "order_response": response})
        except Exception as exc:
            execution_results.append({**action, "submitted": False, "error": str(exc)})

    hold_actions = [row for row in plan["actions"] if row["action"] == "hold"]
    for action in hold_actions:
        execution_results.append({**action, "submitted": False, "reason": "hold"})

    result = {
        **plan,
        "mode": "live",
        "execution_results": execution_results,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
