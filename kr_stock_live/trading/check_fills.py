#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from kr_stock_live.kis.client import KRKISClient, load_kr_kis_config
from kr_stock_live.trading.state import read_json, short_hash, write_json
from live_common.target_weights import _resolve_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query KIS domestic order/fill history for a saved execution report.")
    parser.add_argument("--execution-json", default="", help="Execution report JSON path.")
    parser.add_argument("--account-profile", default="", help="Override account profile suffix.")
    parser.add_argument("--start-date", default="", help="YYYYMMDD. Defaults to today.")
    parser.add_argument("--end-date", default="", help="YYYYMMDD. Defaults to start-date.")
    parser.add_argument("--lookback-days", type=int, default=0, help="If start-date is omitted, query from today minus N days.")
    parser.add_argument("--symbol", default="", help="Symbol filter. Default: all.")
    parser.add_argument("--fill-filter", choices=["00", "01", "02"], default="00", help="00 all, 01 filled, 02 open.")
    parser.add_argument("--side-filter", choices=["00", "01", "02"], default="00", help="00 all, 01 sell, 02 buy.")
    parser.add_argument("--sleep-seconds", type=float, default=1.2)
    parser.add_argument("--output-json", default="", help="Optional fill report path.")
    return parser


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _date_yyyymmdd(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=max(days_ago, 0))).strftime("%Y%m%d")


def _history_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = payload.get("output") or payload.get("output1") or []
    if isinstance(output, dict):
        return [output]
    if isinstance(output, list):
        return [row for row in output if isinstance(row, dict)]
    return []


def _order_no_candidates(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key) or "").strip()
        for key in ["ODNO", "odno", "ord_no", "ORD_NO", "orgn_odno", "ORGN_ODNO"]
        if str(row.get(key) or "").strip()
    }


def _symbol_candidates(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key) or "").upper().strip()
        for key in ["pdno", "PDNO", "isu_no", "ISU_NO", "item_cd", "ITEM_CD"]
        if str(row.get(key) or "").strip()
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _first_number(row: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


FILLED_QTY_KEYS = [
    "tot_ccld_qty",
    "TOT_CCLD_QTY",
    "ccld_qty",
    "CCLD_QTY",
    "ord_ccld_qty",
    "ORD_CCLD_QTY",
    "exec_qty",
    "EXEC_QTY",
]

OPEN_QTY_KEYS = [
    "nccs_qty",
    "NCCS_QTY",
    "rmn_qty",
    "RMN_QTY",
    "ord_psbl_qty",
    "ORD_PSBL_QTY",
]


def _submitted_orders(execution: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not execution:
        return []
    return [
        order
        for order in execution.get("submitted_orders", [])
        if str(order.get("status") or "").lower() == "submitted"
    ]


def _order_qty(order: dict[str, Any]) -> int:
    try:
        return int(float(str(order.get("qty") or 0).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _rows_for_order(rows: list[dict[str, Any]], order: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    order_no = str(order.get("order_no") or "").strip()
    symbol = str(order.get("symbol") or "").upper().strip()
    if order_no:
        matched = [row for row in rows if order_no in _order_no_candidates(row)]
        if matched:
            return matched, False
    if symbol:
        return [row for row in rows if symbol in _symbol_candidates(row)], True
    return [], True


def _summarize_order_fill(rows: list[dict[str, Any]], order: dict[str, Any], *, fill_filter: str) -> dict[str, Any]:
    matched_rows, weak_match = _rows_for_order(rows, order)
    expected_qty = _order_qty(order)
    filled_values = [
        value
        for row in matched_rows
        for value in [_first_number(row, FILLED_QTY_KEYS)]
        if value is not None
    ]
    open_values = [
        value
        for row in matched_rows
        for value in [_first_number(row, OPEN_QTY_KEYS)]
        if value is not None
    ]
    filled_qty = sum(filled_values) if filled_values else None
    open_qty = min(open_values) if open_values else None
    qty_known = filled_qty is not None or open_qty is not None
    if filled_qty is not None:
        fully_filled = filled_qty >= expected_qty > 0
    elif open_qty is not None:
        fully_filled = open_qty <= 0 and bool(matched_rows)
    else:
        fully_filled = bool(matched_rows) and fill_filter == "01"
    return {
        "symbol": str(order.get("symbol") or "").upper(),
        "order_no": str(order.get("order_no") or ""),
        "side": order.get("side"),
        "expected_qty": expected_qty,
        "matched_rows": len(matched_rows),
        "filled_qty": filled_qty,
        "open_qty": open_qty,
        "qty_known": qty_known,
        "weak_symbol_match": weak_match,
        "fully_filled": fully_filled,
    }


def _account_profile_from_execution(execution: dict[str, Any] | None, explicit: str) -> str:
    if explicit:
        return explicit
    if not execution:
        return "default"
    profile_json = execution.get("profile_json")
    if not profile_json:
        return "default"
    profile = read_json(_resolve_path(profile_json))
    return str((profile.get("kis") or {}).get("account_profile") or "default")


def main() -> int:
    args = build_parser().parse_args()
    execution = read_json(_resolve_path(args.execution_json)) if args.execution_json else None
    if args.start_date:
        start_date = args.start_date
        end_date = args.end_date or start_date
    else:
        start_date = _date_yyyymmdd(args.lookback_days)
        end_date = args.end_date or _today_yyyymmdd()

    account_profile = _account_profile_from_execution(execution, args.account_profile)
    client = KRKISClient(load_kr_kis_config(account_profile=account_profile))
    client.issue_access_token()
    time.sleep(max(args.sleep_seconds, 0.0))
    payload = client.domestic_order_history(
        start_date=start_date,
        end_date=end_date,
        symbol=args.symbol,
        side_filter=args.side_filter,
        fill_filter=args.fill_filter,
    )
    rows = _history_rows(payload)
    submitted = _submitted_orders(execution)
    order_fill_status = [_summarize_order_fill(rows, order, fill_filter=args.fill_filter) for order in submitted]
    fully_filled_count = sum(1 for item in order_fill_status if item["fully_filled"])
    weak_match_used = any(item["weak_symbol_match"] for item in order_fill_status)
    matched_rows = [row for order in submitted for row in _rows_for_order(rows, order)[0]]
    report = {
        "type": "kr_stock_live_fill_report_v1",
        "fill_report_id": f"fills_{short_hash({'start': start_date, 'end': end_date, 'rows': matched_rows})}",
        "execution_report_id": execution.get("execution_report_id") if execution else None,
        "account_profile": account_profile,
        "start_date": start_date,
        "end_date": end_date,
        "symbol": args.symbol,
        "fill_filter": args.fill_filter,
        "row_count": len(rows),
        "matched_count": len(matched_rows),
        "submitted_order_count": len(submitted),
        "matched_submitted_count": sum(1 for item in order_fill_status if item["matched_rows"] > 0),
        "fully_filled_count": fully_filled_count,
        "all_submitted_orders_fully_filled": bool(submitted) and fully_filled_count == len(submitted),
        "weak_match_used": weak_match_used,
        "order_fill_status": order_fill_status,
        "matched_rows": matched_rows,
    }
    if args.output_json:
        write_json(_resolve_path(args.output_json), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
