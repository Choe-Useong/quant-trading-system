#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pandas as pd

from live.check_balance import ENV_PATH, _authorized_get, _load_dotenv
from live.current_signals import STRATEGY_DB_PATH, _build_signal, _load_live_price_series


MIN_ORDER_KRW = 5000.0


def _load_balances() -> list[dict[str, object]]:
    return _authorized_get("/v1/accounts")


def _build_live_signal_rows() -> tuple[list[dict[str, object]], dict[str, float]]:
    strategies = pd.read_csv(STRATEGY_DB_PATH)
    rows: list[dict[str, object]] = []
    latest_price_by_asset: dict[str, float] = {}

    for row in strategies.to_dict(orient="records"):
        asset = str(row["asset"]).upper()
        strategy_family = str(row["strategy_family"])
        run_name = str(row["run_name"])
        try:
            series = _load_live_price_series(asset)
            latest_price_by_asset[asset] = float(series.iloc[-1])
            signal, error = _build_signal(asset, strategy_family, run_name, series)
            if error is not None or signal is None:
                rows.append(
                    {
                        "asset": asset,
                        "strategy_family": strategy_family,
                        "run_name": run_name,
                        "supported": False,
                        "error": error,
                    }
                )
                continue
            latest_timestamp = signal.index[-1]
            latest_signal = bool(signal.fillna(False).iloc[-1])
            rows.append(
                {
                    "asset": asset,
                    "strategy_family": strategy_family,
                    "run_name": run_name,
                    "supported": True,
                    "latest_timestamp_utc": latest_timestamp.isoformat(),
                    "latest_price": latest_price_by_asset[asset],
                    "signal_on": latest_signal,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "asset": asset,
                    "strategy_family": strategy_family,
                    "run_name": run_name,
                    "supported": False,
                    "error": str(exc),
                }
            )
    return rows, latest_price_by_asset


def _safe_float(value: object) -> float:
    return float(value or 0.0)


def main() -> int:
    _load_dotenv(ENV_PATH)
    try:
        balances = _load_balances()
        signal_rows, latest_price_by_asset = _build_live_signal_rows()
    except Exception as exc:
        print(f"Dry run failed: {exc}", file=sys.stderr)
        return 1

    supported_rows = [row for row in signal_rows if row.get("supported") is True]
    supported_assets = {str(row["asset"]).upper() for row in supported_rows}
    on_assets = sorted(str(row["asset"]).upper() for row in supported_rows if bool(row.get("signal_on")))

    balance_by_currency = {str(item.get("currency", "")).upper(): item for item in balances}
    cash_krw = _safe_float(balance_by_currency.get("KRW", {}).get("balance"))
    locked_krw = _safe_float(balance_by_currency.get("KRW", {}).get("locked"))
    available_krw = cash_krw + locked_krw

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
        if price is None:
            try:
                price = float(_load_live_price_series(currency).iloc[-1])
            except Exception:
                price = None
        current_value = qty * price if price is not None else None
        holding_row = {
            "asset": currency,
            "quantity": qty,
            "latest_price": price,
            "current_value_krw": current_value,
        }
        if currency in supported_assets:
            managed_holdings.append(holding_row)
        else:
            unmanaged_holdings.append(holding_row)

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

        if abs(delta_value_krw) < MIN_ORDER_KRW:
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

    result = {
        "allocation_mode": "equal_weight_on_signals",
        "managed_equity_krw": managed_equity_krw,
        "available_krw": available_krw,
        "min_order_krw": MIN_ORDER_KRW,
        "supported_assets": sorted(supported_assets),
        "on_assets": on_assets,
        "unmanaged_holdings": unmanaged_holdings,
        "actions": actions,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
