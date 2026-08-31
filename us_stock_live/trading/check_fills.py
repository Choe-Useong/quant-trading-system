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

from us_stock_live.kis.client import KISClient, load_kis_config
from us_stock_live.strategy.target_weights import _resolve_path
from us_stock_live.trading.state import read_json, short_hash, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query KIS overseas order/fill history for a saved execution report.")
    parser.add_argument("--execution-json", default="", help="Execution report JSON path.")
    parser.add_argument("--start-date", default="", help="YYYYMMDD. Defaults to today.")
    parser.add_argument("--end-date", default="", help="YYYYMMDD. Defaults to start-date.")
    parser.add_argument("--lookback-days", type=int, default=0, help="If start-date is omitted, query from today minus N days.")
    parser.add_argument(
        "--exchange-code",
        default="",
        help="Optional exchange code override. Defaults to submitted order exchange codes, or NASD if unavailable.",
    )
    parser.add_argument("--symbol", default="%", help="Symbol filter. Default: all.")
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
    output = payload.get("output") or []
    if isinstance(output, dict):
        return [output]
    if isinstance(output, list):
        return [row for row in output if isinstance(row, dict)]
    return []


def _order_no_candidates(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key) or "").strip()
        for key in [
            "ODNO",
            "odno",
            "ord_no",
            "ORD_NO",
            "orgn_odno",
            "ORGN_ODNO",
            "ft_ord_no",
            "FT_ORD_NO",
            "odno_orgn",
            "ODNO_ORGN",
        ]
        if str(row.get(key) or "").strip()
    }


def _symbol_candidates(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key) or "").upper().strip()
        for key in ["pdno", "PDNO", "ovrs_pdno", "OVRS_PDNO", "symb", "SYMB", "ITEM_CD"]
        if str(row.get(key) or "").strip()
    }


def _match_rows(rows: list[dict[str, Any]], execution: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not execution:
        return rows
    order_nos = {
        str(order.get("order_no") or "").strip()
        for order in execution.get("submitted_orders", [])
        if str(order.get("order_no") or "").strip()
    }
    symbols = {
        str(order.get("symbol") or "").upper()
        for order in execution.get("submitted_orders", [])
        if str(order.get("symbol") or "").strip()
    }
    if order_nos:
        return [row for row in rows if _order_no_candidates(row) & order_nos]
    if symbols:
        return [
            row
            for row in rows
            if _symbol_candidates(row) & symbols
        ]
    return rows


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
    "ft_ccld_qty",
    "FT_CCLD_QTY",
    "ovrs_ccld_qty",
    "OVRS_CCLD_QTY",
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


def _exchange_codes_for_query(execution: dict[str, Any] | None, override: str) -> list[str]:
    override = str(override or "").strip().upper()
    if override:
        return [override]
    codes = sorted(
        {
            str(order.get("exchange_code") or "").strip().upper()
            for order in _submitted_orders(execution)
            if str(order.get("exchange_code") or "").strip()
        }
    )
    return codes or ["NASD"]


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
        # Querying filled-only rows is enough to infer presence, but not partial quantity.
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


def main() -> int:
    args = build_parser().parse_args()
    execution = read_json(_resolve_path(args.execution_json)) if args.execution_json else None
    if args.start_date:
        start_date = args.start_date
        end_date = args.end_date or start_date
    else:
        start_date = _date_yyyymmdd(args.lookback_days)
        end_date = args.end_date or _today_yyyymmdd()

    client = KISClient(load_kis_config())
    client.issue_access_token()
    exchange_codes = _exchange_codes_for_query(execution, args.exchange_code)
    rows: list[dict[str, Any]] = []
    query_results: list[dict[str, Any]] = []
    for index, exchange_code in enumerate(exchange_codes):
        if index:
            time.sleep(max(args.sleep_seconds, 0.0))
        payload = client.overseas_order_history(
            start_date=start_date,
            end_date=end_date,
            symbol=args.symbol,
            exchange_code=exchange_code,
            side_filter=args.side_filter,
            fill_filter=args.fill_filter,
            sort_order="DS",
        )
        next_rows = _history_rows(payload)
        rows.extend(next_rows)
        query_results.append({"exchange_code": exchange_code, "row_count": len(next_rows)})
    matched = _match_rows(rows, execution)
    submitted = _submitted_orders(execution)
    order_fill_status = [
        _summarize_order_fill(rows, order, fill_filter=args.fill_filter)
        for order in submitted
    ]
    fully_filled_count = sum(1 for item in order_fill_status if item["fully_filled"])
    weak_match_used = any(item["weak_symbol_match"] for item in order_fill_status)
    report = {
        "type": "stock_live_fill_report_v1",
        "fill_report_id": f"fills_{short_hash({'start': start_date, 'end': end_date, 'rows': matched})}",
        "execution_report_id": execution.get("execution_report_id") if execution else None,
        "start_date": start_date,
        "end_date": end_date,
        "exchange_code": exchange_codes[0] if len(exchange_codes) == 1 else "MULTI",
        "exchange_codes": exchange_codes,
        "query_results": query_results,
        "symbol": args.symbol,
        "fill_filter": args.fill_filter,
        "row_count": len(rows),
        "matched_count": len(matched),
        "submitted_order_count": len(submitted),
        "matched_submitted_count": sum(1 for item in order_fill_status if item["matched_rows"] > 0),
        "fully_filled_count": fully_filled_count,
        "all_submitted_orders_fully_filled": bool(submitted) and fully_filled_count == len(submitted),
        "weak_match_used": weak_match_used,
        "order_fill_status": order_fill_status,
        "matched_rows": matched,
    }
    if args.output_json:
        write_json(_resolve_path(args.output_json), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
