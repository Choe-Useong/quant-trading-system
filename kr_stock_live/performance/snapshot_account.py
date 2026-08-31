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

from kr_stock_live.kis.client import KRKISClient, load_kr_kis_config
from kr_stock_live.active_profile import resolve_profile_reference
from live_common.snapshot_io import (
    append_csv as _append_csv,
    append_jsonl as _append_jsonl,
    now_iso as _now_iso,
    read_json as _read_json,
    to_float as _to_float,
)
from live_common.kis_base import KISError, redacted_account


DEFAULT_PROFILE_JSON = ROOT_DIR / "kr_stock_live" / "configs" / "kr_etf_cat24_rank9_top2_w8020_breadth45_isa.json"
DEFAULT_OUT_DIR = ROOT_DIR / "kr_stock_live" / ".cache" / "performance"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Save a Korean stock/ETF live account performance snapshot.")
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="KR live profile JSON path.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Snapshot output directory.")
    parser.add_argument("--source", default="", help="Caller label, for example scheduler/live/preview.")
    parser.add_argument("--run-exit-code", type=int, default=None, help="Exit code of the live run that triggered this snapshot.")
    parser.add_argument("--orderable-symbol", default="", help="Optional domestic symbol for orderable-cash reference.")
    parser.add_argument("--format", choices=["summary", "json"], default="summary")
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _account_profile(profile: dict[str, Any]) -> str:
    return str((profile.get("kis") or {}).get("account_profile") or "default").strip().lower()


def _holdings_from_payload(balance_payload: dict[str, Any]) -> list[dict[str, Any]]:
    output1 = balance_payload.get("output1") or []
    if not isinstance(output1, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in output1:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("pdno") or "").strip()
        if not symbol:
            continue
        qty = _to_float(item.get("hldg_qty"))
        price = _to_float(item.get("prpr"))
        value = _to_float(item.get("evlu_amt"))
        rows.append(
            {
                "symbol": symbol,
                "name": str(item.get("prdt_name") or "").strip(),
                "qty": qty,
                "orderable_qty": _to_float(item.get("ord_psbl_qty")),
                "price_krw": price,
                "value_krw": value,
                "avg_price_krw": _to_float(item.get("pchs_avg_pric")),
                "purchase_amount_krw": _to_float(item.get("pchs_amt")),
                "pnl_krw": _to_float(item.get("evlu_pfls_amt")),
                "pnl_pct": _to_float(item.get("evlu_pfls_rt")),
            }
        )
    return rows


def _account_row(balance_payload: dict[str, Any]) -> dict[str, Any]:
    output2 = balance_payload.get("output2") or []
    if isinstance(output2, list) and output2 and isinstance(output2[0], dict):
        return output2[0]
    return {}


def _reference_symbol(holdings: list[dict[str, Any]], requested: str) -> str:
    requested = requested.strip()
    if requested:
        return requested
    for item in holdings:
        symbol = str(item.get("symbol") or "").strip()
        if symbol:
            return symbol
    return "005930"


def _last_price(payload: dict[str, Any]) -> float:
    output = payload.get("output") or {}
    return _to_float(output.get("stck_prpr")) if isinstance(output, dict) else 0.0


def _orderable_snapshot(client: KRKISClient, symbol: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "orderable_reference_symbol": symbol,
        "orderable_reference_price_krw": 0.0,
        "orderable_cash_krw": 0.0,
        "raw_orderable_cash_krw": 0.0,
        "orderable_qty": 0.0,
        "orderable_error": "",
    }
    try:
        price = _last_price(client.domestic_price(symbol))
        result["orderable_reference_price_krw"] = price
        payload = client.domestic_orderable_amount(symbol=symbol, price=str(price or 1), order_division="01")
        output = payload.get("output") or {}
        if isinstance(output, dict):
            result["orderable_cash_krw"] = _to_float(output.get("nrcvb_buy_amt"))
            result["raw_orderable_cash_krw"] = _to_float(output.get("ord_psbl_cash"))
            result["orderable_qty"] = _to_float(output.get("nrcvb_buy_qty"))
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
    profile_json: Path,
    out_dir: Path,
    source: str,
    run_exit_code: int | None,
    orderable_symbol: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    profile_json, _active_profile = resolve_profile_reference(profile_json)
    profile = _read_json(profile_json)
    account_profile = _account_profile(profile)
    config = load_kr_kis_config(account_profile=account_profile)
    client = KRKISClient(config)
    client.issue_access_token()

    balance = client.domestic_balance()
    holdings = _holdings_from_payload(balance)
    account = _account_row(balance)
    holding_value_krw = sum(float(item.get("value_krw") or 0.0) for item in holdings)
    orderable = _orderable_snapshot(client, _reference_symbol(holdings, orderable_symbol))

    total_eval_krw = _to_float(account.get("tot_evlu_amt"))
    snapshot_id = f"kracct_{int(time.time())}"
    total_value_for_weights = total_eval_krw if total_eval_krw > 0 else holding_value_krw + float(orderable["orderable_cash_krw"])
    positions = []
    for item in holdings:
        value_krw = float(item.get("value_krw") or 0.0)
        enriched = dict(item)
        enriched["weight"] = value_krw / total_value_for_weights if total_value_for_weights > 0 else 0.0
        positions.append(enriched)

    snapshot = {
        "type": "kr_stock_live_account_snapshot_v1",
        "snapshot_id": snapshot_id,
        "created_at": _now_iso(),
        "profile_json": str(profile_json.relative_to(ROOT_DIR) if profile_json.is_relative_to(ROOT_DIR) else profile_json),
        "profile_name": str(profile.get("name") or profile_json.stem),
        "source": source,
        "run_exit_code": run_exit_code,
        "account": redacted_account(config),
        "account_profile": account_profile,
        "holdings_count": len(positions),
        "total_eval_krw": total_eval_krw,
        "net_asset_krw": _to_float(account.get("nass_amt")),
        "deposit_cash_krw": _to_float(account.get("dnca_tot_amt")),
        "previous_received_cash_krw": _to_float(account.get("prvs_rcdl_excc_amt")),
        "holding_value_krw": holding_value_krw,
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
        "account_profile",
        "holdings_count",
        "total_eval_krw",
        "net_asset_krw",
        "deposit_cash_krw",
        "previous_received_cash_krw",
        "holding_value_krw",
        "orderable_cash_krw",
        "raw_orderable_cash_krw",
        "orderable_reference_symbol",
        "orderable_reference_price_krw",
        "orderable_qty",
        "orderable_error",
    ]
    _append_csv(paths["summary_csv"], snapshot, summary_fields)

    position_fields = [
        "snapshot_id",
        "created_at",
        "profile_name",
        "account_profile",
        "symbol",
        "name",
        "qty",
        "orderable_qty",
        "price_krw",
        "value_krw",
        "weight",
        "avg_price_krw",
        "purchase_amount_krw",
        "pnl_krw",
        "pnl_pct",
    ]
    for item in snapshot["positions"]:
        row = {
            "snapshot_id": snapshot["snapshot_id"],
            "created_at": snapshot["created_at"],
            "profile_name": snapshot["profile_name"],
            "account_profile": snapshot["account_profile"],
            **item,
        }
        _append_csv(paths["positions_csv"], row, position_fields)


def main() -> int:
    args = build_parser().parse_args()
    profile_json = _resolve_path(args.profile_json)
    out_dir = _resolve_path(args.out_dir)
    snapshot, paths = build_snapshot(
        profile_json=profile_json,
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
            f"total_eval={snapshot['total_eval_krw']:.0f}KRW "
            f"holdings={snapshot['holdings_count']} "
            f"orderable={snapshot['orderable_cash_krw']:.0f}KRW "
            f"path={paths['summary_csv'].relative_to(ROOT_DIR)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
