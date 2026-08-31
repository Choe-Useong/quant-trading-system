#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_CSV = ROOT_DIR / "data" / "strategy_db" / "strategy_results_master.csv"


DEFAULT_DISPLAY_COLUMNS = [
    "asset_scope",
    "strategy_family",
    "strategy_label",
    "run_name",
    "timeframe",
    "annualized_information_ratio",
    "recent_1y_air",
    "max_drawdown_pct",
    "sharpe_ratio",
    "rolling_ir_252d_median",
    "total_return_pct",
    "summary_path",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query strategy_results_master.csv with grouping, filtering, and ranking."
    )
    parser.add_argument(
        "--input-csv",
        default=str(DEFAULT_INPUT_CSV),
        help="Input strategy results master CSV",
    )
    parser.add_argument(
        "--group-by",
        default="",
        help="Optional group column such as asset_scope or strategy_family",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Top N rows overall or per group",
    )
    parser.add_argument(
        "--sort-by",
        default="annualized_information_ratio",
        help="Column to rank by",
    )
    parser.add_argument(
        "--ascending",
        action="store_true",
        help="Sort ascending instead of descending",
    )
    parser.add_argument("--record-type", default="", help="Filter by record_type")
    parser.add_argument("--asset-scope", default="", help="Filter by asset_scope")
    parser.add_argument("--strategy-family", default="", help="Filter by strategy_family")
    parser.add_argument("--strategy-label", default="", help="Filter by strategy_label")
    parser.add_argument("--timeframe", default="", help="Filter by timeframe")
    parser.add_argument("--status", default="ok", help="Filter by status; empty disables")
    parser.add_argument(
        "--contains",
        default="",
        help="Substring filter applied to run_name and source_dir",
    )
    parser.add_argument(
        "--min-air",
        type=float,
        default=None,
        help="Minimum annualized_information_ratio",
    )
    parser.add_argument(
        "--min-recent-1y-air",
        type=float,
        default=None,
        help="Minimum recent_1y_air",
    )
    parser.add_argument(
        "--max-mdd",
        type=float,
        default=None,
        help="Maximum max_drawdown_pct",
    )
    parser.add_argument(
        "--columns",
        default=",".join(DEFAULT_DISPLAY_COLUMNS),
        help="Comma-separated columns to print",
    )
    parser.add_argument(
        "--output-csv",
        default="",
        help="Optional CSV path to write the filtered result",
    )
    return parser


def _ensure_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]


def _apply_string_filter(frame: pd.DataFrame, column: str, value: str) -> pd.DataFrame:
    if not value:
        return frame
    if column not in frame.columns:
        return frame.iloc[0:0]
    return frame[frame[column].fillna("").astype(str) == value]


def _apply_contains_filter(frame: pd.DataFrame, needle: str) -> pd.DataFrame:
    if not needle:
        return frame
    lowered = needle.lower()
    run_name = frame["run_name"].fillna("").astype(str).str.lower() if "run_name" in frame.columns else ""
    source_dir = frame["source_dir"].fillna("").astype(str).str.lower() if "source_dir" in frame.columns else ""
    mask = run_name.str.contains(lowered, regex=False) | source_dir.str.contains(lowered, regex=False)
    return frame[mask]


def _coerce_numeric(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def _select_top(frame: pd.DataFrame, group_by: str, sort_by: str, ascending: bool, top_n: int) -> pd.DataFrame:
    ordered = frame.sort_values(by=sort_by, ascending=ascending, na_position="last")
    if not group_by:
        return ordered.head(top_n)
    if group_by not in ordered.columns:
        raise ValueError(f"group-by column not found: {group_by}")
    return ordered.groupby(group_by, dropna=False, group_keys=False).head(top_n)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        raise SystemExit(f"Input CSV not found: {input_csv}")

    frame = pd.read_csv(input_csv, encoding="utf-8-sig")
    if frame.empty:
        raise SystemExit(f"No rows found in {input_csv}")

    numeric_columns = [
        "annualized_information_ratio",
        "recent_1y_air",
        "max_drawdown_pct",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "rolling_ir_126d_median",
        "rolling_ir_252d_median",
        "total_return_pct",
        "win_rate_pct",
        "total_trades",
    ]
    frame = _coerce_numeric(frame, numeric_columns)

    frame = _apply_string_filter(frame, "record_type", args.record_type)
    frame = _apply_string_filter(frame, "asset_scope", args.asset_scope)
    frame = _apply_string_filter(frame, "strategy_family", args.strategy_family)
    frame = _apply_string_filter(frame, "strategy_label", args.strategy_label)
    frame = _apply_string_filter(frame, "timeframe", args.timeframe)
    if args.status:
        frame = _apply_string_filter(frame, "status", args.status)
    frame = _apply_contains_filter(frame, args.contains)

    if args.min_air is not None and "annualized_information_ratio" in frame.columns:
        frame = frame[frame["annualized_information_ratio"] >= args.min_air]
    if args.min_recent_1y_air is not None and "recent_1y_air" in frame.columns:
        frame = frame[frame["recent_1y_air"] >= args.min_recent_1y_air]
    if args.max_mdd is not None and "max_drawdown_pct" in frame.columns:
        frame = frame[frame["max_drawdown_pct"] <= args.max_mdd]

    if frame.empty:
        print("No matching rows.")
        return

    if args.sort_by not in frame.columns:
        raise SystemExit(f"sort-by column not found: {args.sort_by}")

    result = _select_top(frame, args.group_by, args.sort_by, args.ascending, args.top_n)
    display_columns = [column.strip() for column in args.columns.split(",") if column.strip()]
    display_columns = _ensure_columns(result, display_columns)
    if not display_columns:
        raise SystemExit("No valid display columns selected.")

    if args.output_csv:
        output_csv = Path(args.output_csv)
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"Wrote {len(result)} rows to {output_csv}")

    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(result[display_columns].to_string(index=False))


if __name__ == "__main__":
    main()
