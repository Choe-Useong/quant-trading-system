#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GRID_ROOT = ROOT_DIR / "data" / "grid"
DEFAULT_BACKTEST_ROOT = ROOT_DIR / "data" / "backtest"
DEFAULT_OUTPUT_CSV = ROOT_DIR / "data" / "strategy_db" / "strategy_results_master.csv"

KNOWN_ASSET_TOKENS = {
    "btc",
    "eth",
    "sol",
    "xrp",
    "ada",
    "doge",
    "avax",
    "xlm",
    "link",
}

STANDARD_COLUMNS = [
    "record_type",
    "source_dir",
    "summary_path",
    "run_name",
    "asset_scope",
    "strategy_family",
    "strategy_label",
    "timeframe",
    "benchmark_market",
    "status",
    "start",
    "end",
    "total_return_pct",
    "cagr_pct",
    "annualized_information_ratio",
    "recent_1y_air",
    "benchmark_cagr_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "win_rate_pct",
    "total_trades",
    "rolling_ir_126d_median",
    "rolling_ir_126d_q25",
    "rolling_ir_126d_positive_ratio",
    "rolling_ir_252d_median",
    "rolling_ir_252d_q25",
    "rolling_ir_252d_positive_ratio",
    "start_value",
    "end_value",
]


def _parameter_keys_from_row(row: dict[str, str]) -> list[str]:
    return sorted(key for key in row.keys() if str(key).startswith("parameter_"))


def _dynamic_parameter_columns(rows: list[dict[str, str]]) -> list[str]:
    columns: set[str] = set()
    for row in rows:
        for key in row.keys():
            if str(key).startswith("parameter_"):
                columns.add(str(key))
    return sorted(columns)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a master CSV of strategy backtest results from grid/backtest summaries."
    )
    parser.add_argument("--grid-root", default=str(DEFAULT_GRID_ROOT), help="Root directory for grid outputs")
    parser.add_argument(
        "--backtest-root",
        default=str(DEFAULT_BACKTEST_ROOT),
        help="Root directory for single backtest outputs",
    )
    parser.add_argument(
        "--output-csv",
        default=str(DEFAULT_OUTPUT_CSV),
        help="Output master CSV path",
    )
    parser.add_argument(
        "--include-tmp",
        action="store_true",
        help="Include directories whose name starts with _tmp",
    )
    return parser


def _read_grid_summary(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_backtest_summary(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            key = row[0].strip()
            value = row[1].strip()
            if key:
                result[key] = value
    return result


def _tokenize(name: str) -> list[str]:
    return [token for token in name.lower().split("_") if token]


def _infer_asset_scope(name: str) -> str:
    tokens = _tokenize(name)
    assets: list[str] = []
    for token in tokens:
        if token in KNOWN_ASSET_TOKENS:
            assets.append(token.upper())
            continue
        if assets:
            break
    return ",".join(assets) if assets else ""


def _infer_strategy_family(name: str) -> str:
    lowered = name.lower()
    if "score_exposure" in lowered:
        return "score_exposure"
    if "hierarchical_alt" in lowered:
        return "hierarchical_alt"
    if "hierarchical_stack" in lowered:
        return "hierarchical_stack"
    if "hierarchical" in lowered:
        return "hierarchical"
    if "vwma" in lowered and "cross" in lowered:
        return "vwma_cross"
    if "ma_stack4" in lowered:
        return "ma_stack4"
    if "ma_stack" in lowered:
        return "ma_stack"
    if "ma_cross" in lowered:
        return "ma_cross"
    if "momentum" in lowered or "mom" in lowered:
        return "momentum"
    if "adx" in lowered:
        return "adx_filter"
    return "other"


def _get_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _normalize_grid_row(row: dict[str, str], source_dir: str, summary_path: Path) -> dict[str, str]:
    run_name = _get_value(row, "run_name")
    normalized = {
        "record_type": "grid",
        "source_dir": source_dir,
        "summary_path": str(summary_path.relative_to(ROOT_DIR)),
        "run_name": run_name,
        "asset_scope": _get_value(row, "asset_scope", "Asset Scope") or _infer_asset_scope(source_dir or run_name),
        "strategy_family": _get_value(row, "strategy_family", "Strategy Family") or _infer_strategy_family(source_dir or run_name),
        "strategy_label": _get_value(row, "strategy_label", "Strategy Label"),
        "timeframe": _get_value(row, "Timeframe"),
        "benchmark_market": _get_value(row, "Benchmark Market"),
        "status": _get_value(row, "status"),
        "start": "",
        "end": "",
        "total_return_pct": _get_value(row, "Total Return [%]"),
        "cagr_pct": _get_value(row, "CAGR [%]"),
        "annualized_information_ratio": _get_value(row, "Annualized Information Ratio"),
        "recent_1y_air": _get_value(row, "Recent 1Y AIR"),
        "benchmark_cagr_pct": _get_value(row, "Benchmark CAGR [%]"),
        "max_drawdown_pct": _get_value(row, "Max Drawdown [%]"),
        "sharpe_ratio": _get_value(row, "Sharpe Ratio"),
        "sortino_ratio": _get_value(row, "Sortino Ratio"),
        "calmar_ratio": _get_value(row, "Calmar Ratio"),
        "win_rate_pct": _get_value(row, "Win Rate [%]"),
        "total_trades": _get_value(row, "Total Trades"),
        "rolling_ir_126d_median": _get_value(row, "Rolling IR 126d Median"),
        "rolling_ir_126d_q25": _get_value(row, "Rolling IR 126d Q25"),
        "rolling_ir_126d_positive_ratio": _get_value(row, "Rolling IR 126d Positive Ratio"),
        "rolling_ir_252d_median": _get_value(row, "Rolling IR 252d Median"),
        "rolling_ir_252d_q25": _get_value(row, "Rolling IR 252d Q25"),
        "rolling_ir_252d_positive_ratio": _get_value(row, "Rolling IR 252d Positive Ratio"),
        "start_value": _get_value(row, "Start Value"),
        "end_value": _get_value(row, "End Value"),
    }
    for key in _parameter_keys_from_row(row):
        normalized[key] = _get_value(row, key)
    return normalized


def _normalize_backtest_row(row: dict[str, str], source_dir: str, summary_path: Path) -> dict[str, str]:
    run_name = source_dir
    normalized = {
        "record_type": "backtest",
        "source_dir": source_dir,
        "summary_path": str(summary_path.relative_to(ROOT_DIR)),
        "run_name": run_name,
        "asset_scope": _get_value(row, "Asset Scope") or _infer_asset_scope(source_dir),
        "strategy_family": _get_value(row, "Strategy Family") or _infer_strategy_family(source_dir),
        "strategy_label": _get_value(row, "Strategy Label"),
        "timeframe": _get_value(row, "Timeframe"),
        "benchmark_market": _get_value(row, "Benchmark Market"),
        "status": "ok",
        "start": _get_value(row, "Start"),
        "end": _get_value(row, "End"),
        "total_return_pct": _get_value(row, "Total Return [%]"),
        "cagr_pct": _get_value(row, "CAGR [%]"),
        "annualized_information_ratio": _get_value(row, "Annualized Information Ratio"),
        "recent_1y_air": _get_value(row, "Recent 1Y AIR"),
        "benchmark_cagr_pct": _get_value(row, "Benchmark CAGR [%]"),
        "max_drawdown_pct": _get_value(row, "Max Drawdown [%]"),
        "sharpe_ratio": _get_value(row, "Sharpe Ratio"),
        "sortino_ratio": _get_value(row, "Sortino Ratio"),
        "calmar_ratio": _get_value(row, "Calmar Ratio"),
        "win_rate_pct": _get_value(row, "Win Rate [%]"),
        "total_trades": _get_value(row, "Total Trades"),
        "rolling_ir_126d_median": _get_value(row, "Rolling IR 126d Median"),
        "rolling_ir_126d_q25": _get_value(row, "Rolling IR 126d Q25"),
        "rolling_ir_126d_positive_ratio": _get_value(row, "Rolling IR 126d Positive Ratio"),
        "rolling_ir_252d_median": _get_value(row, "Rolling IR 252d Median"),
        "rolling_ir_252d_q25": _get_value(row, "Rolling IR 252d Q25"),
        "rolling_ir_252d_positive_ratio": _get_value(row, "Rolling IR 252d Positive Ratio"),
        "start_value": _get_value(row, "Start Value"),
        "end_value": _get_value(row, "End Value"),
    }
    for key in _parameter_keys_from_row(row):
        normalized[key] = _get_value(row, key)
    return normalized


def _collect_grid_records(grid_root: Path, include_tmp: bool) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    if not grid_root.exists():
        return records

    for directory in sorted(path for path in grid_root.iterdir() if path.is_dir()):
        if not include_tmp and directory.name.startswith("_tmp"):
            continue
        summary_path = directory / "summary_results.csv"
        if not summary_path.exists():
            continue
        for row in _read_grid_summary(summary_path):
            records.append(_normalize_grid_row(row, directory.name, summary_path))
    return records


def _collect_backtest_records(backtest_root: Path, include_tmp: bool) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    if not backtest_root.exists():
        return records

    for directory in sorted(path for path in backtest_root.iterdir() if path.is_dir()):
        if not include_tmp and directory.name.startswith("_tmp"):
            continue
        summary_path = directory / "summary.csv"
        if not summary_path.exists():
            continue
        row = _read_backtest_summary(summary_path)
        if row:
            records.append(_normalize_backtest_row(row, directory.name, summary_path))
    return records


def _sort_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row["record_type"], row["asset_scope"], row["run_name"])


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = STANDARD_COLUMNS + [column for column in _dynamic_parameter_columns(rows) if column not in STANDARD_COLUMNS]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in fieldnames})


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    grid_root = Path(args.grid_root)
    backtest_root = Path(args.backtest_root)
    output_csv = Path(args.output_csv)

    rows = _collect_grid_records(grid_root, args.include_tmp)
    rows.extend(_collect_backtest_records(backtest_root, args.include_tmp))
    rows.sort(key=_sort_key)
    _write_csv(output_csv, rows)
    print(f"Wrote {len(rows)} strategy result rows to {output_csv}")


if __name__ == "__main__":
    main()
