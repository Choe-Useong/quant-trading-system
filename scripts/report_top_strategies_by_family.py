#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import query_strategy_results as query


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_CSV = ROOT_DIR / "data" / "strategy_db" / "strategy_results_master.csv"
DEFAULT_OUTPUT_MD = ROOT_DIR / "data" / "strategy_db" / "reports" / "top_strategies_by_family.md"

DEFAULT_COLUMNS = [
    "strategy_family",
    "asset_scope",
    "strategy_label",
    "run_name",
    "timeframe",
    "annualized_information_ratio",
    "recent_1y_air",
    "max_drawdown_pct",
    "sharpe_ratio",
    "rolling_ir_252d_median",
    "total_return_pct",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a strategy-family top strategy report from strategy_results_master.csv."
    )
    parser.add_argument(
        "--input-csv",
        default=str(DEFAULT_INPUT_CSV),
        help="Input strategy results master CSV",
    )
    parser.add_argument("--top-n", type=int, default=5, help="Top N rows per strategy family")
    parser.add_argument(
        "--sort-by",
        default="annualized_information_ratio",
        help="Column used for ranking",
    )
    parser.add_argument(
        "--ascending",
        action="store_true",
        help="Sort ascending instead of descending",
    )
    parser.add_argument("--record-type", default="", help="Filter by record_type")
    parser.add_argument("--asset-scope", default="", help="Optional asset_scope filter")
    parser.add_argument("--strategy-family", default="", help="Optional strategy_family filter")
    parser.add_argument("--strategy-label", default="", help="Optional strategy_label filter")
    parser.add_argument("--timeframe", default="", help="Optional timeframe filter")
    parser.add_argument("--status", default="ok", help="Filter by status; empty disables")
    parser.add_argument("--contains", default="", help="Substring filter on run_name and source_dir")
    parser.add_argument("--min-air", type=float, default=None, help="Minimum AIR")
    parser.add_argument("--min-recent-1y-air", type=float, default=None, help="Minimum recent 1Y AIR")
    parser.add_argument("--max-mdd", type=float, default=None, help="Maximum MDD")
    parser.add_argument(
        "--columns",
        default=",".join(DEFAULT_COLUMNS),
        help="Comma-separated columns to display and save",
    )
    parser.add_argument(
        "--output-md",
        default=str(DEFAULT_OUTPUT_MD),
        help="Optional markdown report output path; empty disables",
    )
    parser.add_argument(
        "--output-csv",
        default="",
        help="Optional CSV output path",
    )
    return parser


def _load_and_filter(args: argparse.Namespace) -> pd.DataFrame:
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
    frame = query._coerce_numeric(frame, numeric_columns)

    frame = query._apply_string_filter(frame, "record_type", args.record_type)
    frame = query._apply_string_filter(frame, "asset_scope", args.asset_scope)
    frame = query._apply_string_filter(frame, "strategy_family", args.strategy_family)
    frame = query._apply_string_filter(frame, "strategy_label", args.strategy_label)
    frame = query._apply_string_filter(frame, "timeframe", args.timeframe)
    if args.status:
        frame = query._apply_string_filter(frame, "status", args.status)
    frame = query._apply_contains_filter(frame, args.contains)

    if args.min_air is not None and "annualized_information_ratio" in frame.columns:
        frame = frame[frame["annualized_information_ratio"] >= args.min_air]
    if args.min_recent_1y_air is not None and "recent_1y_air" in frame.columns:
        frame = frame[frame["recent_1y_air"] >= args.min_recent_1y_air]
    if args.max_mdd is not None and "max_drawdown_pct" in frame.columns:
        frame = frame[frame["max_drawdown_pct"] <= args.max_mdd]

    if frame.empty:
        raise SystemExit("No matching rows.")
    if args.sort_by not in frame.columns:
        raise SystemExit(f"sort-by column not found: {args.sort_by}")
    if "strategy_family" not in frame.columns:
        raise SystemExit("strategy_family column not found.")
    return frame


def _select_top_by_family(frame: pd.DataFrame, sort_by: str, ascending: bool, top_n: int) -> pd.DataFrame:
    filtered = frame[frame["strategy_family"].fillna("").astype(str) != ""].copy()
    ordered = filtered.sort_values(by=["strategy_family", sort_by], ascending=[True, ascending], na_position="last")
    return ordered.groupby("strategy_family", dropna=False, group_keys=False).head(top_n).reset_index(drop=True)


def _frame_to_markdown(frame: pd.DataFrame) -> str:
    header = "| " + " | ".join(frame.columns) + " |"
    separator = "| " + " | ".join(["---"] * len(frame.columns)) + " |"
    rows = [header, separator]
    for _, row in frame.iterrows():
        values: list[str] = []
        for column in frame.columns:
            value = row[column]
            if pd.isna(value):
                values.append("")
            else:
                values.append(str(value).replace("\n", " ").replace("|", "\\|"))
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join(rows)


def _write_markdown_report(frame: pd.DataFrame, output_md: Path, columns: list[str], top_n: int, sort_by: str) -> None:
    output_md.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Top Strategies By Family")
    lines.append("")
    lines.append(f"- Top N: `{top_n}`")
    lines.append(f"- Sort By: `{sort_by}`")
    lines.append("")
    for strategy_family, family_frame in frame.groupby("strategy_family", dropna=False):
        family_label = str(strategy_family) if pd.notna(strategy_family) and str(strategy_family) else "UNKNOWN"
        lines.append(f"## {family_label}")
        lines.append("")
        lines.append(_frame_to_markdown(family_frame[columns]))
        lines.append("")
    output_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    frame = _load_and_filter(args)
    result = _select_top_by_family(frame, args.sort_by, args.ascending, args.top_n)
    columns = [column.strip() for column in args.columns.split(",") if column.strip()]
    columns = query._ensure_columns(result, columns)
    if not columns:
        raise SystemExit("No valid display columns selected.")

    if args.output_csv:
        output_csv = Path(args.output_csv)
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"Wrote {len(result)} rows to {output_csv}")

    if args.output_md:
        output_md = Path(args.output_md)
        _write_markdown_report(result, output_md, columns, args.top_n, args.sort_by)
        print(f"Wrote markdown report to {output_md}")

    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(result[columns].to_string(index=False))


if __name__ == "__main__":
    main()
