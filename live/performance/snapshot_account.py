#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live.check_balance import ENV_PATH, UPBIT_BASE_URL, _authorized_get, _load_dotenv
from live_common.snapshot_io import (
    append_csv as _append_csv,
    append_jsonl as _append_jsonl,
    now_iso as _now_iso,
    read_json as _read_json,
    to_float as _to_float,
)


DEFAULT_EXECUTION_CONFIG = ROOT_DIR / "configs" / "examples" / "live_portfolio_v2.example.json"
DEFAULT_OUT_DIR = ROOT_DIR / "live" / ".cache" / "performance"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Save an Upbit live account performance snapshot.")
    parser.add_argument("--execution-config-json", default=str(DEFAULT_EXECUTION_CONFIG), help="Coin live execution config JSON path.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Snapshot output directory.")
    parser.add_argument("--source", default="", help="Caller label, for example scheduler/live/preview.")
    parser.add_argument("--run-exit-code", type=int, default=None, help="Exit code of the live run that triggered this snapshot.")
    parser.add_argument("--format", choices=["summary", "json"], default="summary")
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _relative_text(path: Path) -> str:
    return str(path.relative_to(ROOT_DIR) if path.is_relative_to(ROOT_DIR) else path)


def _portfolio_name(config: dict[str, Any], config_path: Path) -> str:
    value = str(config.get("portfolio_name") or "").strip()
    return value or config_path.stem


def _nonzero_balances(payload: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cash = {
        "currency": "KRW",
        "balance": 0.0,
        "locked": 0.0,
        "total": 0.0,
    }
    holdings: list[dict[str, Any]] = []
    for item in payload:
        currency = str(item.get("currency") or "").upper().strip()
        if not currency:
            continue
        balance = _to_float(item.get("balance"))
        locked = _to_float(item.get("locked"))
        total = balance + locked
        if currency == "KRW":
            cash = {
                "currency": currency,
                "balance": balance,
                "locked": locked,
                "total": total,
            }
            continue
        if total <= 0.0:
            continue
        unit_currency = str(item.get("unit_currency") or "KRW").upper().strip() or "KRW"
        holdings.append(
            {
                "currency": currency,
                "market": f"{unit_currency}-{currency}",
                "balance": balance,
                "locked": locked,
                "qty": total,
                "avg_buy_price_krw": _to_float(item.get("avg_buy_price")),
                "avg_buy_price_modified": bool(item.get("avg_buy_price_modified", False)),
                "unit_currency": unit_currency,
            }
        )
    return cash, sorted(holdings, key=lambda row: str(row["market"]))


def _public_get_json(path: str, params: dict[str, str]) -> Any:
    query = urllib.parse.urlencode(params)
    url = f"{UPBIT_BASE_URL}{path}?{query}" if query else f"{UPBIT_BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_ticker_prices(markets: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    prices: dict[str, float] = {}
    errors: dict[str, str] = {}
    unique_markets = sorted({market for market in markets if market})
    for start in range(0, len(unique_markets), 100):
        chunk = unique_markets[start : start + 100]
        try:
            payload = _public_get_json("/v1/ticker", {"markets": ",".join(chunk)})
            for item in payload if isinstance(payload, list) else []:
                market = str(item.get("market") or "").upper().strip()
                price = _to_float(item.get("trade_price"))
                if market and price > 0:
                    prices[market] = price
        except Exception as exc:
            for market in chunk:
                errors[market] = str(exc)

    missing = [market for market in unique_markets if market not in prices]
    for market in missing:
        if market in errors:
            continue
        try:
            payload = _public_get_json("/v1/ticker", {"markets": market})
            if isinstance(payload, list) and payload:
                price = _to_float(payload[0].get("trade_price"))
                if price > 0:
                    prices[market] = price
                    continue
            errors[market] = "ticker response missing trade_price"
        except Exception as exc:
            errors[market] = str(exc)
    return prices, errors


def _snapshot_paths(out_dir: Path, portfolio_name: str) -> dict[str, Path]:
    out_dir = out_dir / portfolio_name
    return {
        "summary_csv": out_dir / "account_equity_curve.csv",
        "positions_csv": out_dir / "account_positions.csv",
        "snapshots_jsonl": out_dir / "account_snapshots.jsonl",
        "latest_json": out_dir / "latest_account_snapshot.json",
    }


def build_snapshot(
    *,
    execution_config_json: Path,
    out_dir: Path,
    source: str,
    run_exit_code: int | None,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _load_dotenv(ENV_PATH)
    execution_config = _read_json(execution_config_json)
    portfolio_name = _portfolio_name(execution_config, execution_config_json)

    balances = _authorized_get("/v1/accounts")
    cash, holdings = _nonzero_balances(balances)
    prices, price_errors = _fetch_ticker_prices([str(item["market"]) for item in holdings])

    positions: list[dict[str, Any]] = []
    holding_value_krw = 0.0
    for item in holdings:
        market = str(item["market"])
        price = prices.get(market, 0.0)
        qty = float(item["qty"])
        value_krw = qty * price if price > 0 else 0.0
        purchase_amount_krw = qty * float(item["avg_buy_price_krw"]) if float(item["avg_buy_price_krw"]) > 0 else 0.0
        pnl_krw = value_krw - purchase_amount_krw if purchase_amount_krw > 0 else 0.0
        pnl_pct = (pnl_krw / purchase_amount_krw * 100.0) if purchase_amount_krw > 0 else 0.0
        holding_value_krw += value_krw
        positions.append(
            {
                **item,
                "price_krw": price,
                "value_krw": value_krw,
                "purchase_amount_krw": purchase_amount_krw,
                "pnl_krw": pnl_krw,
                "pnl_pct": pnl_pct,
                "price_error": price_errors.get(market, ""),
            }
        )

    cash_total_krw = float(cash["total"])
    total_eval_krw = cash_total_krw + holding_value_krw
    for item in positions:
        item["weight"] = float(item["value_krw"]) / total_eval_krw if total_eval_krw > 0 else 0.0

    snapshot = {
        "type": "upbit_live_account_snapshot_v1",
        "snapshot_id": f"upbitacct_{int(time.time())}",
        "created_at": _now_iso(),
        "execution_config_json": _relative_text(execution_config_json),
        "portfolio_name": portfolio_name,
        "source": source,
        "run_exit_code": run_exit_code,
        "holdings_count": len(positions),
        "cash_balance_krw": float(cash["balance"]),
        "cash_locked_krw": float(cash["locked"]),
        "cash_total_krw": cash_total_krw,
        "holding_value_krw": holding_value_krw,
        "total_eval_krw": total_eval_krw,
        "pricing_errors_count": sum(1 for item in positions if item.get("price_error")),
        "positions": positions,
    }
    return snapshot, _snapshot_paths(out_dir, portfolio_name)


def save_snapshot(snapshot: dict[str, Any], paths: dict[str, Path]) -> None:
    latest_path = paths["latest_json"]
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_jsonl(paths["snapshots_jsonl"], snapshot)

    summary_fields = [
        "snapshot_id",
        "created_at",
        "portfolio_name",
        "source",
        "run_exit_code",
        "holdings_count",
        "cash_balance_krw",
        "cash_locked_krw",
        "cash_total_krw",
        "holding_value_krw",
        "total_eval_krw",
        "pricing_errors_count",
    ]
    _append_csv(paths["summary_csv"], snapshot, summary_fields)

    position_fields = [
        "snapshot_id",
        "created_at",
        "portfolio_name",
        "market",
        "currency",
        "qty",
        "balance",
        "locked",
        "price_krw",
        "value_krw",
        "weight",
        "avg_buy_price_krw",
        "purchase_amount_krw",
        "pnl_krw",
        "pnl_pct",
        "avg_buy_price_modified",
        "unit_currency",
        "price_error",
    ]
    for item in snapshot["positions"]:
        row = {
            "snapshot_id": snapshot["snapshot_id"],
            "created_at": snapshot["created_at"],
            "portfolio_name": snapshot["portfolio_name"],
            **item,
        }
        _append_csv(paths["positions_csv"], row, position_fields)


def main() -> int:
    args = build_parser().parse_args()
    execution_config_json = _resolve_path(args.execution_config_json)
    out_dir = _resolve_path(args.out_dir)
    snapshot, paths = build_snapshot(
        execution_config_json=execution_config_json,
        out_dir=out_dir,
        source=args.source,
        run_exit_code=args.run_exit_code,
    )
    save_snapshot(snapshot, paths)
    if args.format == "json":
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(
            "snapshot "
            f"id={snapshot['snapshot_id']} "
            f"total_eval={snapshot['total_eval_krw']:.0f}KRW "
            f"holdings={snapshot['holdings_count']} "
            f"cash={snapshot['cash_total_krw']:.0f}KRW "
            f"path={paths['summary_csv'].relative_to(ROOT_DIR)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

