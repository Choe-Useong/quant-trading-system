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

from us_stock_live.active_profile import resolve_profile_reference
from live_common.snapshot_io import (
    append_csv as _append_csv,
    append_jsonl as _append_jsonl,
    now_iso as _now_iso,
    read_json as _read_json,
    to_float as _to_float,
)
from us_stock_live.kis.client import KISClient, KISError, load_kis_config, redacted_account
from us_stock_live.strategy.target_weights import DEFAULT_PROFILE_JSON, _resolve_path


DEFAULT_OUT_DIR = ROOT_DIR / "us_stock_live" / ".cache" / "performance"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Save an overseas stock-live account performance snapshot.")
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="US Stock Live profile JSON path.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Snapshot output directory.")
    parser.add_argument("--source", default="", help="Caller label, for example scheduler/live/preview.")
    parser.add_argument("--run-exit-code", type=int, default=None, help="Exit code of the live run that triggered this snapshot.")
    parser.add_argument("--orderable-symbol", default="", help="Optional US ticker for orderable-cash reference.")
    parser.add_argument("--format", choices=["summary", "json"], default="summary")
    return parser


def _profile_and_reference(path: Path) -> tuple[Path, dict[str, Any]]:
    profile_path, _active = resolve_profile_reference(path)
    return profile_path, _read_json(profile_path)


def _symbol_settings(profile: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    kis = profile.get("kis") or {}
    return (
        str(kis.get("default_price_exchange") or "AMS"),
        str(kis.get("default_balance_exchange") or "AMEX"),
        kis.get("symbols") or {},
    )


def _symbol_setting(symbol_settings: dict[str, Any], symbol: str) -> dict[str, Any]:
    return symbol_settings.get(symbol.upper()) or {}


def _balance_exchanges(profile: dict[str, Any]) -> list[str]:
    _default_price_exchange, default_balance_exchange, symbol_settings = _symbol_settings(profile)
    tickers = [str(item).upper() for item in (profile.get("data") or {}).get("tickers", [])]
    exchanges = {
        str(_symbol_setting(symbol_settings, symbol).get("balance_exchange") or default_balance_exchange)
        for symbol in tickers
    }
    return sorted(exchange for exchange in exchanges if exchange)


def _holdings_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output1 = payload.get("output1") or []
    if not isinstance(output1, list):
        return []
    holdings: list[dict[str, Any]] = []
    for item in output1:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("ovrs_pdno") or item.get("pdno") or "").upper().strip()
        if not symbol:
            continue
        holdings.append(
            {
                "symbol": symbol,
                "name": str(item.get("ovrs_item_name") or "").strip(),
                "qty": _to_float(item.get("ovrs_cblc_qty") or item.get("ord_psbl_qty")),
                "orderable_qty": _to_float(item.get("ord_psbl_qty")),
                "price_usd": _to_float(item.get("now_pric2")),
                "value_usd": _to_float(item.get("ovrs_stck_evlu_amt")),
                "avg_price_usd": _to_float(item.get("pchs_avg_pric")),
                "purchase_amount_usd": _to_float(item.get("frcr_pchs_amt1")),
                "pnl_usd": _to_float(item.get("frcr_evlu_pfls_amt")),
                "pnl_pct": _to_float(item.get("evlu_pfls_rt")),
                "currency": str(item.get("tr_crcy_cd") or "USD"),
                "exchange": str(item.get("ovrs_excg_cd") or ""),
            }
        )
    return holdings


def _merge_holdings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row["symbol"])
        existing = by_symbol.get(symbol)
        if not existing or float(row.get("value_usd") or 0.0) > float(existing.get("value_usd") or 0.0):
            by_symbol[symbol] = row
    return sorted(by_symbol.values(), key=lambda item: str(item["symbol"]))


def _fetch_holdings(client: KISClient, exchanges: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_holdings: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    for index, exchange in enumerate(exchanges or ["AMEX"]):
        if index:
            time.sleep(0.2)
        payload = client.overseas_balance(exchange_code=exchange, currency="USD")
        all_holdings.extend(_holdings_from_payload(payload))
        output2 = payload.get("output2") or {}
        if isinstance(output2, dict):
            summaries[exchange] = output2
    return _merge_holdings(all_holdings), summaries


def _last_price(payload: dict[str, Any]) -> float:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return 0.0
    return _to_float(output.get("last") or output.get("last_price") or output.get("ovrs_nmix_prpr"))


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


def _reference_symbol(profile: dict[str, Any], holdings: list[dict[str, Any]], requested: str) -> str:
    requested = requested.upper().strip()
    if requested:
        return requested
    for item in holdings:
        symbol = str(item.get("symbol") or "").upper().strip()
        if symbol:
            return symbol
    tickers = [str(item).upper() for item in (profile.get("data") or {}).get("tickers", [])]
    return tickers[0] if tickers else "QQQ"


def _orderable_snapshot(client: KISClient, profile: dict[str, Any], holdings: list[dict[str, Any]], requested_symbol: str) -> dict[str, Any]:
    default_price_exchange, default_balance_exchange, symbol_settings = _symbol_settings(profile)
    symbol = _reference_symbol(profile, holdings, requested_symbol)
    setting = _symbol_setting(symbol_settings, symbol)
    price_exchange = str(setting.get("price_exchange") or default_price_exchange)
    balance_exchange = str(setting.get("balance_exchange") or default_balance_exchange)
    result: dict[str, Any] = {
        "orderable_reference_symbol": symbol,
        "orderable_reference_exchange": balance_exchange,
        "orderable_reference_price_usd": 0.0,
        "orderable_cash_usd": 0.0,
        "orderable_error": "",
    }
    try:
        price = _last_price(client.overseas_price(symbol, exchange_code=price_exchange))
        result["orderable_reference_price_usd"] = price
        payload = client.overseas_orderable_amount(symbol=symbol, price=price or 1.0, exchange_code=balance_exchange)
        result["orderable_cash_usd"] = _orderable_cash(payload)
    except KISError as exc:
        result["orderable_error"] = str(exc)
    return result


def _snapshot_paths(out_dir: Path, profile_json: Path) -> dict[str, Path]:
    out_dir = out_dir / profile_json.stem
    return {
        "summary_csv": out_dir / "account_equity_curve.csv",
        "positions_csv": out_dir / "account_positions.csv",
        "snapshots_jsonl": out_dir / "account_snapshots.jsonl",
        "latest_json": out_dir / "latest_account_snapshot.json",
    }


def build_snapshot(
    *,
    profile_reference: Path,
    out_dir: Path,
    source: str,
    run_exit_code: int | None,
    orderable_symbol: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    profile_json, profile = _profile_and_reference(profile_reference)
    config = load_kis_config()
    client = KISClient(config)
    client.issue_access_token()

    holdings, balance_summaries = _fetch_holdings(client, _balance_exchanges(profile))
    holding_value_usd = sum(float(item.get("value_usd") or 0.0) for item in holdings)
    orderable = _orderable_snapshot(client, profile, holdings, orderable_symbol)
    estimated_total_usd = holding_value_usd + float(orderable["orderable_cash_usd"])

    positions = []
    for item in holdings:
        value_usd = float(item.get("value_usd") or 0.0)
        enriched = dict(item)
        enriched["weight"] = value_usd / estimated_total_usd if estimated_total_usd > 0 else 0.0
        positions.append(enriched)

    snapshot = {
        "type": "stock_live_account_snapshot_v1",
        "snapshot_id": f"usacct_{int(time.time())}",
        "created_at": _now_iso(),
        "profile_reference": str(profile_reference.relative_to(ROOT_DIR) if profile_reference.is_relative_to(ROOT_DIR) else profile_reference),
        "profile_json": str(profile_json.relative_to(ROOT_DIR) if profile_json.is_relative_to(ROOT_DIR) else profile_json),
        "profile_name": str(profile.get("name") or profile_json.stem),
        "source": source,
        "run_exit_code": run_exit_code,
        "account": redacted_account(config),
        "holdings_count": len(positions),
        "holding_value_usd": holding_value_usd,
        "estimated_total_usd": estimated_total_usd,
        "balance_summaries": balance_summaries,
        **orderable,
        "positions": positions,
    }
    return snapshot, _snapshot_paths(out_dir, profile_json)


def save_snapshot(snapshot: dict[str, Any], paths: dict[str, Path]) -> None:
    latest_path = paths["latest_json"]
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_jsonl(paths["snapshots_jsonl"], snapshot)

    summary_fields = [
        "snapshot_id",
        "created_at",
        "profile_name",
        "source",
        "run_exit_code",
        "account",
        "holdings_count",
        "holding_value_usd",
        "orderable_cash_usd",
        "estimated_total_usd",
        "orderable_reference_symbol",
        "orderable_reference_exchange",
        "orderable_reference_price_usd",
        "orderable_error",
    ]
    _append_csv(paths["summary_csv"], snapshot, summary_fields)

    position_fields = [
        "snapshot_id",
        "created_at",
        "profile_name",
        "symbol",
        "name",
        "qty",
        "orderable_qty",
        "price_usd",
        "value_usd",
        "weight",
        "avg_price_usd",
        "purchase_amount_usd",
        "pnl_usd",
        "pnl_pct",
        "currency",
        "exchange",
    ]
    for item in snapshot["positions"]:
        row = {
            "snapshot_id": snapshot["snapshot_id"],
            "created_at": snapshot["created_at"],
            "profile_name": snapshot["profile_name"],
            **item,
        }
        _append_csv(paths["positions_csv"], row, position_fields)


def main() -> int:
    args = build_parser().parse_args()
    profile_reference = _resolve_path(args.profile_json)
    out_dir = _resolve_path(args.out_dir)
    snapshot, paths = build_snapshot(
        profile_reference=profile_reference,
        out_dir=out_dir,
        source=args.source,
        run_exit_code=args.run_exit_code,
        orderable_symbol=args.orderable_symbol,
    )
    save_snapshot(snapshot, paths)
    if args.format == "json":
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(
            "snapshot "
            f"id={snapshot['snapshot_id']} "
            f"estimated_total={snapshot['estimated_total_usd']:.2f}USD "
            f"holdings={snapshot['holdings_count']} "
            f"orderable={snapshot['orderable_cash_usd']:.2f}USD "
            f"path={paths['summary_csv'].relative_to(ROOT_DIR)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
