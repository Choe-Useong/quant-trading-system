#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.feature_graph_v2 import referenced_markets_for_feature_specs
from lib.features_v2 import build_feature_frames_from_cache
from lib.market_scores_v2 import build_market_score_frame, required_markets_for_market_score_spec
from lib.spec_io import load_feature_specs, load_market_score_spec, load_universe_spec, load_weight_spec
from lib.universe_v2 import build_universe_mask_v2
from lib.weights_v2 import build_weight_frame_v2, is_change_only_weight_output, to_change_only_target_frame
from lib.vectorbt_adapter import VectorBTSpec, run_portfolio_from_target_weights
from scripts.run_vectorbt import (
    benchmark_comparison_period_summary,
    benchmark_summary,
    build_benchmark_comparison_curves,
    compute_benchmark_regression_stats,
    compute_drawdown_recovery_stats,
    compute_excess_curves,
    compute_information_ratio,
    compute_recent_1y_stats,
    compute_recent_2y_stats,
    compute_return_series,
    compute_rolling_information_ratio,
    infer_timeframe,
    load_price_frame,
    periods_per_day_for_timeframe,
    print_summary,
    resolve_periods_per_year,
    resolve_benchmark_curve,
    summarize_rolling_information_ratio,
    strategy_performance_summary,
    timeframe_to_pandas_freq,
    trim_frames_to_first_weight,
    write_equity_csv,
    write_summary_csv,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run frame-native v2 cross-sectional backtest from wide source cache."
    )
    parser.add_argument("--candle-dir", required=True, help="Raw candle dir for fallback/timeframe metadata")
    parser.add_argument("--source-cache-dir", required=True, help="Wide source cache directory")
    parser.add_argument(
        "--benchmark-source-cache-dir",
        default="",
        help="Optional wide source cache used only for a single-market benchmark",
    )
    parser.add_argument("--feature-spec-json", required=True, help="Feature spec JSON path")
    parser.add_argument("--market-scores-spec-json", default="", help="Optional market score spec JSON path")
    parser.add_argument("--universe-spec-json", required=True, help="Universe spec JSON path")
    parser.add_argument("--weight-spec-json", required=True, help="Weight spec JSON path")
    parser.add_argument("--portfolio-dir", required=True, help="Output directory for v2 weight files")
    parser.add_argument("--backtest-out-dir", required=True, help="Output directory for vectorbt backtest files")
    parser.add_argument("--fees", type=float, default=0.0, help="Per-order proportional fees")
    parser.add_argument("--slippage", type=float, default=0.0, help="Per-order proportional slippage")
    parser.add_argument(
        "--periods-per-year",
        type=int,
        default=None,
        help="Annualization periods per year; omitted means infer from timeframe",
    )
    parser.add_argument("--benchmark-market", default="KRW-BTC", help="Benchmark market for vectorbt summary")
    parser.add_argument(
        "--benchmark-price-column",
        default="",
        help="Optional source column for single_market benchmark prices. Defaults to --execution-price-column.",
    )
    parser.add_argument(
        "--benchmark-mode",
        choices=["single_market", "portfolio_group", "portfolio_group_rebalance"],
        default="single_market",
        help="single_market uses benchmark_market buy-and-hold, portfolio_group uses vectorbt grouped buy-and-hold, portfolio_group_rebalance uses a rebalanced portfolio baseline",
    )
    parser.add_argument(
        "--benchmark-fixed-weights-json",
        default="",
        help="Optional JSON object of fixed weights for portfolio_group_rebalance benchmark",
    )
    parser.add_argument(
        "--benchmark-rebalance-frequency",
        choices=["every_bar", "daily", "weekly", "monthly", "quarterly", "semiannual"],
        default="every_bar",
        help="Rebalance frequency for portfolio_group_rebalance benchmark",
    )
    parser.add_argument(
        "--benchmark-listed-normalize",
        action="store_true",
        help="For portfolio_group_rebalance, normalize benchmark weights across markets with a valid price at each rebalance timestamp",
    )
    parser.add_argument(
        "--execution-price-column",
        default="trade_price",
        help="Execution price column for portfolio fills (e.g. trade_price, opening_price)",
    )
    parser.add_argument(
        "--trim-start-mode",
        choices=["first_weight", "none"],
        default="none",
        help="Trim the simulation start to the first timestamp with non-zero target weights; default is none",
    )
    parser.add_argument("--max-markets", type=int, default=None, help="Optional market limit for smoke runs")
    parser.add_argument("--tail-hours", type=int, default=None, help="Optional tail length for smoke runs")
    parser.add_argument(
        "--save-weights-parquet",
        action="store_true",
        help="Save weights_v2.parquet for inspection; disabled by default to reduce I/O",
    )
    parser.add_argument(
        "--save-target-weights-full",
        action="store_true",
        help="Write target_weights_full.csv; disabled by default to reduce output size",
    )
    parser.add_argument(
        "--save-excess-returns-csv",
        action="store_true",
        help="Write excess_returns.csv; disabled by default to reduce output size",
    )
    parser.add_argument(
        "--save-rolling-ir-csv",
        action="store_true",
        help="Write rolling_information_ratio.csv; disabled by default to reduce output size",
    )
    return parser


def _write_wide_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.copy()
    output.index = pd.to_datetime(output.index, utc=False)
    output = output.sort_index().sort_index(axis=1)
    output = output.reset_index(names="date_utc")
    output.to_parquet(path, index=False)


def _resolve_required_feature_markets(feature_specs, universe_spec, market_score_spec=None) -> list[str] | None:
    explicit_markets: set[str] = set()
    if universe_spec.allowed_markets:
        explicit_markets.update(str(market).upper() for market in universe_spec.allowed_markets)
    referenced_markets: set[str] = set()
    if market_score_spec is not None:
        referenced_markets.update(required_markets_for_market_score_spec(market_score_spec))
    referenced_markets.update(referenced_markets_for_feature_specs(feature_specs))
    if explicit_markets:
        return sorted(explicit_markets | referenced_markets)
    return None


def _resolve_output_feature_markets(universe_spec) -> list[str] | None:
    if not universe_spec.allowed_markets:
        return None
    return sorted(str(market).upper() for market in universe_spec.allowed_markets)


def _read_warning_frame(
    source_cache_dir: Path,
    *,
    market_columns: list[str] | None = None,
    max_markets: int | None = None,
    require_warning: bool = False,
) -> pd.DataFrame:
    warning_path = source_cache_dir / "market_warning.parquet"
    if not warning_path.exists():
        if require_warning:
            raise FileNotFoundError(f"market_warning.parquet is required when exclude_warnings=true: {warning_path}")
        return pd.DataFrame()

    requested_columns = None if market_columns is None else sorted({str(column).upper() for column in market_columns})
    try:
        warning_frame = pd.read_parquet(warning_path, columns=requested_columns)
    except Exception:
        warning_frame = pd.read_parquet(warning_path)
        if requested_columns is not None:
            warning_frame = warning_frame.reindex(columns=requested_columns)
    warning_frame.index = pd.to_datetime(warning_frame.index, utc=False)
    if requested_columns is not None:
        warning_frame = warning_frame.reindex(columns=requested_columns)
    elif max_markets is not None:
        warning_frame = warning_frame.reindex(columns=sorted(warning_frame.columns)[: max_markets])
    return warning_frame.sort_index().sort_index(axis=1)


def main() -> None:
    args = build_parser().parse_args()
    source_cache_dir = Path(args.source_cache_dir)
    portfolio_dir = Path(args.portfolio_dir)
    out_dir = Path(args.backtest_out_dir)
    portfolio_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_specs = load_feature_specs(Path(args.feature_spec_json))
    market_score_spec = load_market_score_spec(Path(args.market_scores_spec_json)) if args.market_scores_spec_json else None
    universe_spec = load_universe_spec(Path(args.universe_spec_json))
    weight_spec = load_weight_spec(Path(args.weight_spec_json))
    required_feature_markets = _resolve_required_feature_markets(feature_specs, universe_spec, market_score_spec)
    output_feature_markets = _resolve_output_feature_markets(universe_spec)
    effective_max_markets = None if required_feature_markets is not None else args.max_markets

    feature_frames = build_feature_frames_from_cache(
        source_cache_dir,
        feature_specs,
        market_columns=required_feature_markets,
        output_market_columns=output_feature_markets,
        max_markets=effective_max_markets,
        tail_rows=args.tail_hours,
    )
    if market_score_spec is not None:
        feature_frames[market_score_spec.output_column] = build_market_score_frame(feature_frames, market_score_spec)
    warning_frame = _read_warning_frame(
        source_cache_dir,
        market_columns=required_feature_markets,
        max_markets=effective_max_markets,
        require_warning=bool(universe_spec.exclude_warnings),
    )
    reference_index = next(iter(feature_frames.values())).index if feature_frames else warning_frame.index
    warning_frame = warning_frame.reindex(index=reference_index)

    universe_result = build_universe_mask_v2(feature_frames, warning_frame, universe_spec)
    weight_frame = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames)
    change_only_weights = is_change_only_weight_output(weight_spec)
    if change_only_weights:
        weight_frame = weight_frame.ffill().fillna(0.0)

    if args.save_weights_parquet:
        weights_path = portfolio_dir / "weights_v2.parquet"
        _write_wide_frame(weights_path, weight_frame)

    benchmark_price_column = str(args.benchmark_price_column or args.execution_price_column)
    benchmark_market = str(args.benchmark_market).upper()
    benchmark_source_cache_dir = (
        Path(args.benchmark_source_cache_dir) if args.benchmark_source_cache_dir else source_cache_dir
    )
    benchmark_uses_strategy_price_frame = (
        args.benchmark_mode == "single_market"
        and benchmark_price_column == args.execution_price_column
        and benchmark_source_cache_dir.resolve() == source_cache_dir.resolve()
    )
    price_market_columns = None
    if required_feature_markets is not None:
        price_market_set = set(required_feature_markets)
        if benchmark_uses_strategy_price_frame:
            price_market_set.add(benchmark_market)
        price_market_columns = sorted(price_market_set)
    price_frame = load_price_frame(
        Path(args.candle_dir),
        args.execution_price_column,
        load_mode="wide",
        source_cache_dir=source_cache_dir,
        market_columns=price_market_columns,
    )
    benchmark_price_frame = None
    if args.benchmark_mode == "single_market" and not benchmark_uses_strategy_price_frame:
        benchmark_price_frame = load_price_frame(
            Path(args.candle_dir),
            benchmark_price_column,
            load_mode="wide",
            source_cache_dir=benchmark_source_cache_dir,
            market_columns=[benchmark_market],
        )
    price_frame, weight_frame, trimmed_start_timestamp = trim_frames_to_first_weight(
        price_frame,
        weight_frame,
        mode=args.trim_start_mode,
    )
    if benchmark_price_frame is not None and not price_frame.empty:
        benchmark_price_frame = benchmark_price_frame.loc[
            (benchmark_price_frame.index >= price_frame.index.min())
            & (benchmark_price_frame.index <= price_frame.index.max())
        ]
    if change_only_weights:
        weight_frame = to_change_only_target_frame(weight_frame)

    timeframe = infer_timeframe(Path(args.candle_dir), None)
    periods_per_year = resolve_periods_per_year(timeframe, args.periods_per_year)
    periods_per_day = periods_per_day_for_timeframe(timeframe)
    pandas_freq = timeframe_to_pandas_freq(timeframe)

    portfolio = run_portfolio_from_target_weights(
        price_frame=price_frame,
        target_weight_frame=weight_frame,
        spec=VectorBTSpec(
            price_column=args.execution_price_column,
            init_cash=1_000_000.0,
            fees=args.fees,
            slippage=args.slippage,
            freq=pandas_freq,
        ),
    )

    summary = portfolio.stats(settings={"freq": pandas_freq})
    if "Benchmark Return [%]" in summary.index:
        summary = summary.rename(index={"Benchmark Return [%]": "VectorBT Benchmark Return [%]"})
    equity_curve = portfolio.value()
    benchmark_curve, benchmark_label = resolve_benchmark_curve(
        price_frame=price_frame,
        portfolio=portfolio,
        init_cash=1_000_000.0,
        benchmark_mode=args.benchmark_mode,
        benchmark_market=benchmark_market,
        benchmark_price_frame=benchmark_price_frame,
        vectorbt_spec=VectorBTSpec(
            price_column=args.execution_price_column,
            init_cash=1_000_000.0,
            fees=args.fees,
            slippage=args.slippage,
            freq=pandas_freq,
        ),
        benchmark_fixed_weights=(json.loads(args.benchmark_fixed_weights_json) if args.benchmark_fixed_weights_json else None),
        benchmark_rebalance_frequency=args.benchmark_rebalance_frequency,
        benchmark_listed_normalize=bool(args.benchmark_listed_normalize),
    )
    comparison_equity_curve, aligned_benchmark_curve = build_benchmark_comparison_curves(
        equity_curve,
        benchmark_curve,
        1_000_000.0,
    )
    benchmark_stats = benchmark_summary(
        aligned_benchmark_curve,
        1_000_000.0,
        benchmark_label,
        annualization_factor=periods_per_year,
        benchmark_mode=args.benchmark_mode,
    )
    comparison_period_stats = benchmark_comparison_period_summary(
        comparison_equity_curve,
        annualization_factor=periods_per_year,
    )
    comparison_strategy_returns = compute_return_series(comparison_equity_curve)
    benchmark_returns = compute_return_series(aligned_benchmark_curve)
    ir_stats = compute_information_ratio(
        comparison_strategy_returns,
        benchmark_returns,
        annualization_factor=periods_per_year,
    )
    regression_stats = compute_benchmark_regression_stats(
        comparison_strategy_returns,
        benchmark_returns,
        annualization_factor=periods_per_year,
    )
    excess_returns, excess_equity_curve = compute_excess_curves(
        comparison_equity_curve,
        aligned_benchmark_curve,
        1_000_000.0,
    )
    rolling_ir = compute_rolling_information_ratio(
        excess_returns,
        periods_per_day=periods_per_day,
        annualization_factor=periods_per_year,
    )
    recent_1y_stats = compute_recent_1y_stats(
        comparison_equity_curve,
        aligned_benchmark_curve,
        annualization_factor=periods_per_year,
    )
    recent_2y_stats = compute_recent_2y_stats(
        comparison_equity_curve,
        aligned_benchmark_curve,
        annualization_factor=periods_per_year,
    )
    rolling_ir_summary = summarize_rolling_information_ratio(rolling_ir)
    strategy_stats = strategy_performance_summary(
        equity_curve,
        annualization_factor=periods_per_year,
    )
    for metric_name, metric_value in strategy_stats.items():
        summary.loc[metric_name] = metric_value
    summary.loc["Load Mode"] = "wide"
    summary.loc["Weights CSV Format"] = "wide"
    summary.loc["Trim Start Mode"] = args.trim_start_mode
    if trimmed_start_timestamp is not None:
        summary.loc["Trimmed Start Timestamp"] = trimmed_start_timestamp.isoformat()
    summary.loc["Timeframe"] = timeframe
    summary.loc["Execution Price Column"] = args.execution_price_column
    summary.loc["Benchmark Price Column"] = benchmark_price_column
    summary.loc["Benchmark Source Cache Dir"] = str(benchmark_source_cache_dir)
    summary.loc["Periods Per Year"] = periods_per_year
    summary = pd.concat(
        [
            summary,
            comparison_period_stats,
            benchmark_stats,
            ir_stats,
            regression_stats,
            recent_1y_stats,
            recent_2y_stats,
            compute_drawdown_recovery_stats(equity_curve),
            rolling_ir_summary,
        ]
    )

    print_summary(summary)
    write_summary_csv(out_dir / "summary.csv", summary)
    write_equity_csv(out_dir / "equity_curve.csv", equity_curve)
    write_equity_csv(out_dir / "benchmark_curve.csv", aligned_benchmark_curve)
    write_equity_csv(out_dir / "excess_equity_curve.csv", excess_equity_curve)
    if args.save_excess_returns_csv:
        write_equity_csv(out_dir / "excess_returns.csv", excess_returns)
    if args.save_rolling_ir_csv:
        write_equity_csv(out_dir / "rolling_information_ratio.csv", rolling_ir)
    if args.save_target_weights_full:
        weight_frame.to_csv(out_dir / "target_weights_full.csv", encoding="utf-8-sig")

    print("Resolved load mode: wide")
    print(f"Wrote vectorbt results to {out_dir}")


if __name__ == "__main__":
    main()
