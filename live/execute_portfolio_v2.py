#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live.check_balance import ENV_PATH, UPBIT_BASE_URL, _authorized_get, _encode_jwt_hs512, _load_dotenv
from live.live_weight_builders_v2 import build_latest_pipeline_weights_v2
from live.live_weight_postprocess import postprocess_latest_weight_rows


DEFAULT_EXECUTION_CONFIG = ROOT_DIR / "configs" / "examples" / "live_portfolio_v2.example.json"
MIN_ORDER_KRW = 5000.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or execute a live Upbit v2 portfolio from an execution config.")
    parser.add_argument("--mode", choices=["preview", "live"], default="preview")
    parser.add_argument("--execution-config-json", default=str(DEFAULT_EXECUTION_CONFIG))
    parser.add_argument("--ignore-unmanaged", action="store_true")
    parser.add_argument("--min-order-krw", type=float, default=None)
    parser.add_argument("--refresh-candles", type=int, default=None)
    return parser


def _load_execution_config(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    strategy_type = str(payload.get("strategy_type", ""))
    if strategy_type != "portfolio_pipeline_v2":
        raise ValueError(f"execute_portfolio_v2 only supports strategy_type=portfolio_pipeline_v2, got: {strategy_type}")
    market_scores_raw = str(payload.get("market_scores_spec_json", "")).strip()
    return {
        "strategy_type": "portfolio_pipeline_v2",
        "portfolio_name": str(payload.get("portfolio_name", "live_portfolio_v2")),
        "candle_dir": (ROOT_DIR / str(payload.get("candle_dir", "data/upbit_research/minutes/240"))).resolve(),
        "source_cache_dir": ((ROOT_DIR / str(payload["source_cache_dir"])).resolve() if payload.get("source_cache_dir") else None),
        "candle_unit": (None if payload.get("candle_unit") is None else int(payload.get("candle_unit"))),
        "refresh_candles": int(payload.get("refresh_candles", 240)),
        "min_order_krw": float(payload.get("min_order_krw", MIN_ORDER_KRW)),
        "only_markets": [str(item).upper() for item in payload.get("only_markets", [])],
        "exclude_markets": [str(item).upper() for item in payload.get("exclude_markets", [])],
        "market_caps": {str(key).upper(): float(value) for key, value in payload.get("market_caps", {}).items()},
        "cap_overflow_mode": str(payload.get("cap_overflow_mode", "keep_cash")),
        "buy_cash_buffer_pct": float(payload.get("buy_cash_buffer_pct", 0.0)),
        "live_rebalance_policy": str(payload.get("live_rebalance_policy", "target_weight")),
        "no_change_rebalance": payload.get("no_change_rebalance", {}),
        "features_spec_json": (ROOT_DIR / str(payload["features_spec_json"])).resolve(),
        "market_scores_spec_json": (ROOT_DIR / market_scores_raw).resolve() if market_scores_raw else None,
        "universe_spec_json": (ROOT_DIR / str(payload["universe_spec_json"])).resolve(),
        "weights_spec_json": (ROOT_DIR / str(payload["weights_spec_json"])).resolve(),
    }


def _build_query_string(params: dict[str, object]) -> str:
    return urllib.parse.urlencode([(key, str(value)) for key, value in params.items() if value is not None], doseq=True)


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
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc


def _safe_float(value: object) -> float:
    return float(value or 0.0)


def _no_change_rebalance_config(execution_config: dict[str, object]) -> dict[str, object]:
    raw = execution_config.get("no_change_rebalance", {})
    if not isinstance(raw, dict):
        return {"enabled": False}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "min_abs_delta_krw": float(raw.get("min_abs_delta_krw", 0.0) or 0.0),
        "min_portfolio_drift_pct": float(raw.get("min_portfolio_drift_pct", 0.0) or 0.0),
        "min_target_drift_pct": float(raw.get("min_target_drift_pct", 0.0) or 0.0),
    }


def _allow_no_change_rebalance(
    action: dict[str, object],
    *,
    managed_equity_krw: float,
    min_order_krw: float,
    config: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    if not bool(config.get("enabled", False)):
        return False, {"enabled": False}
    if str(action.get("action")) not in {"buy", "sell"}:
        return False, {"enabled": True, "reason": "not_trade_action"}

    delta = abs(_safe_float(action.get("delta_value_krw")))
    target_value = abs(_safe_float(action.get("target_value_krw")))
    current_value = abs(_safe_float(action.get("current_value_krw")))
    min_abs_delta = max(min_order_krw, float(config.get("min_abs_delta_krw", 0.0) or 0.0))
    portfolio_drift = delta / managed_equity_krw if managed_equity_krw > 0.0 else 0.0
    target_reference = max(target_value, current_value, 1.0)
    target_drift = delta / target_reference

    allowed = (
        delta >= min_abs_delta
        and portfolio_drift >= float(config.get("min_portfolio_drift_pct", 0.0) or 0.0)
        and target_drift >= float(config.get("min_target_drift_pct", 0.0) or 0.0)
    )
    return allowed, {
        "enabled": True,
        "allowed": allowed,
        "abs_delta_krw": delta,
        "min_abs_delta_krw": min_abs_delta,
        "portfolio_drift_pct": portfolio_drift,
        "min_portfolio_drift_pct": float(config.get("min_portfolio_drift_pct", 0.0) or 0.0),
        "target_drift_pct": target_drift,
        "min_target_drift_pct": float(config.get("min_target_drift_pct", 0.0) or 0.0),
    }


def _place_sell_market_order(market: str, volume: float) -> dict[str, object]:
    body = {
        "market": market,
        "side": "ask",
        "volume": f"{volume:.16f}".rstrip("0").rstrip("."),
        "ord_type": "market",
        "identifier": f"live-sell-{market.lower()}-{uuid.uuid4().hex[:10]}",
    }
    return _authorized_post("/v1/orders", body)


def _place_buy_market_order(market: str, price_krw: float) -> dict[str, object]:
    body = {
        "market": market,
        "side": "bid",
        "price": f"{price_krw:.0f}",
        "ord_type": "price",
        "identifier": f"live-buy-{market.lower()}-{uuid.uuid4().hex[:10]}",
    }
    return _authorized_post("/v1/orders", body)


def _build_plan(execution_config: dict[str, object], refresh_candles: int, min_order_krw: float) -> dict[str, object]:
    latest_weight_rows, latest_price_by_market = build_latest_pipeline_weights_v2(execution_config, refresh_candles)
    latest_weight_rows = postprocess_latest_weight_rows(execution_config, latest_weight_rows)
    balances = _authorized_get("/v1/accounts")

    target_weight_by_market = {row["market"]: float(row["target_weight"]) for row in latest_weight_rows}
    managed_markets = sorted(set(list(target_weight_by_market.keys()) + list(latest_price_by_market.keys())))
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
        market = f"KRW-{currency}"
        latest_price = latest_price_by_market.get(market)
        current_value_krw = qty * latest_price if latest_price is not None else None
        row = {
            "market": market,
            "asset": currency,
            "quantity": qty,
            "latest_price": latest_price,
            "current_value_krw": current_value_krw,
        }
        if market in managed_markets:
            managed_holdings.append(row)
        else:
            unmanaged_holdings.append(row)

    managed_equity_krw = available_krw + sum(
        float(item["current_value_krw"]) for item in managed_holdings if item["current_value_krw"] is not None
    )

    actions: list[dict[str, object]] = []
    latest_weight_date = max((row["date_utc"] for row in latest_weight_rows), default=None)
    for market in managed_markets:
        asset = market.replace("KRW-", "")
        latest_price = float(latest_price_by_market[market])
        balance_item = balance_by_currency.get(asset, {})
        current_qty = _safe_float(balance_item.get("balance")) + _safe_float(balance_item.get("locked"))
        current_value_krw = current_qty * latest_price
        target_weight = target_weight_by_market.get(market, 0.0)
        target_value_krw = managed_equity_krw * target_weight
        delta_value_krw = target_value_krw - current_value_krw

        if abs(delta_value_krw) < min_order_krw:
            action = "hold"
        elif delta_value_krw > 0:
            action = "buy"
        else:
            action = "sell"

        actions.append(
            {
                "market": market,
                "asset": asset,
                "latest_weight_date_utc": latest_weight_date,
                "latest_price": latest_price,
                "current_qty": current_qty,
                "current_value_krw": current_value_krw,
                "target_weight": target_weight,
                "target_value_krw": target_value_krw,
                "delta_value_krw": delta_value_krw,
                "action": action,
            }
        )

    live_rebalance_policy = str(execution_config.get("live_rebalance_policy", "target_weight"))
    latest_target_changed = bool(execution_config.get("_latest_target_changed", True))
    no_change_rebalance = _no_change_rebalance_config(execution_config)
    if live_rebalance_policy == "latest_target_change_full_rebalance" and not latest_target_changed:
        next_actions: list[dict[str, object]] = []
        for action in actions:
            allowed, evaluation = _allow_no_change_rebalance(
                action,
                managed_equity_krw=managed_equity_krw,
                min_order_krw=min_order_krw,
                config=no_change_rebalance,
            )
            if allowed:
                next_actions.append(
                    {
                        **action,
                        "no_change_rebalance": evaluation,
                        "rebalance_reason": "target_drift_retry",
                    }
                )
            else:
                next_actions.append(
                    {
                        **action,
                        "would_action": action["action"],
                        "action": "hold",
                        "skip_reason": "no_target_change",
                        "no_change_rebalance": evaluation,
                    }
                )
        actions = next_actions
    elif live_rebalance_policy not in {"target_weight", "latest_target_change_full_rebalance"}:
        raise ValueError(f"Unsupported live_rebalance_policy: {live_rebalance_policy}")

    return {
        "portfolio_name": str(execution_config["portfolio_name"]),
        "strategy_type": str(execution_config["strategy_type"]),
        "execution_config_json": str(execution_config.get("_path", "")),
        "live_rebalance_policy": live_rebalance_policy,
        "buy_cash_buffer_pct": float(execution_config.get("buy_cash_buffer_pct", 0.0)),
        "latest_target_changed": latest_target_changed,
        "no_change_rebalance": no_change_rebalance,
        "managed_equity_krw": managed_equity_krw,
        "available_krw": available_krw,
        "min_order_krw": min_order_krw,
        "managed_markets": managed_markets,
        "latest_weight_date_utc": latest_weight_date,
        "live_data_diagnostics": execution_config.get("_live_data_diagnostics", []),
        "latest_weight_rows": latest_weight_rows,
        "unmanaged_holdings": unmanaged_holdings,
        "actions": actions,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(ENV_PATH)
    execution_config_path = Path(args.execution_config_json)
    execution_config = _load_execution_config(execution_config_path)
    execution_config["_path"] = str(execution_config_path)
    refresh_candles = args.refresh_candles if args.refresh_candles is not None else int(execution_config["refresh_candles"])
    min_order_krw = args.min_order_krw if args.min_order_krw is not None else float(execution_config["min_order_krw"])

    try:
        plan = _build_plan(execution_config, refresh_candles, min_order_krw)
    except Exception as exc:
        print(f"Portfolio execution plan failed: {exc}", file=sys.stderr)
        return 1

    if args.ignore_unmanaged:
        plan["unmanaged_holdings"] = []
    elif plan["unmanaged_holdings"]:
        print(json.dumps({**plan, "execution_blocked": True, "reason": "unmanaged_holdings_present"}, ensure_ascii=False, indent=2))
        return 2

    if args.mode == "preview":
        print(json.dumps({**plan, "mode": "preview"}, ensure_ascii=False, indent=2))
        return 0

    execution_results: list[dict[str, object]] = []
    sell_actions = [row for row in plan["actions"] if row["action"] == "sell"]
    buy_actions = [row for row in plan["actions"] if row["action"] == "buy"]
    hold_actions = [row for row in plan["actions"] if row["action"] == "hold"]

    for action in sell_actions:
        latest_price = float(action["latest_price"])
        current_qty = float(action["current_qty"])
        target_qty = float(action["target_value_krw"]) / latest_price if latest_price > 0.0 else 0.0
        sell_qty = max(current_qty - target_qty, 0.0)
        if sell_qty * latest_price < min_order_krw:
            execution_results.append({**action, "submitted": False, "reason": "below_min_order"})
            continue
        try:
            response = _place_sell_market_order(str(action["market"]), sell_qty)
            execution_results.append({**action, "submitted": True, "order_response": response})
        except Exception as exc:
            execution_results.append({**action, "submitted": False, "error": str(exc)})

    try:
        refreshed_balances = _authorized_get("/v1/accounts")
        refreshed_available_krw = _safe_float(
            next((item.get("balance") for item in refreshed_balances if str(item.get("currency", "")).upper() == "KRW"), 0.0)
        )
    except Exception:
        refreshed_available_krw = float(plan["available_krw"])

    buy_cash_buffer_pct = max(float(plan.get("buy_cash_buffer_pct") or 0.0), 0.0)
    buy_cash_buffer_krw = refreshed_available_krw * min(buy_cash_buffer_pct, 100.0) / 100.0
    remaining_cash = max(refreshed_available_krw - buy_cash_buffer_krw, 0.0)
    for action in buy_actions:
        buy_value = min(float(action["delta_value_krw"]), remaining_cash)
        if buy_value < min_order_krw:
            execution_results.append(
                {
                    **action,
                    "submitted": False,
                    "reason": "below_min_order_or_no_cash",
                    "buy_cash_buffer_pct": buy_cash_buffer_pct,
                    "buy_cash_buffer_krw": buy_cash_buffer_krw,
                    "remaining_cash_krw": remaining_cash,
                }
            )
            continue
        try:
            response = _place_buy_market_order(str(action["market"]), buy_value)
            remaining_cash -= buy_value
            execution_results.append(
                {
                    **action,
                    "submitted": True,
                    "order_response": response,
                    "buy_cash_buffer_pct": buy_cash_buffer_pct,
                    "buy_cash_buffer_krw": buy_cash_buffer_krw,
                    "submitted_value_krw": buy_value,
                    "remaining_cash_krw": remaining_cash,
                }
            )
        except Exception as exc:
            execution_results.append(
                {
                    **action,
                    "submitted": False,
                    "error": str(exc),
                    "buy_cash_buffer_pct": buy_cash_buffer_pct,
                    "buy_cash_buffer_krw": buy_cash_buffer_krw,
                    "attempted_value_krw": buy_value,
                    "remaining_cash_krw": remaining_cash,
                }
            )

    for action in hold_actions:
        execution_results.append({**action, "submitted": False, "reason": "hold"})

    print(json.dumps({**plan, "mode": "live", "execution_results": execution_results}, ensure_ascii=False, indent=2))
    return 1 if any("error" in row for row in execution_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
