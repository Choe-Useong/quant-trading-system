#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import build_wide_frame_from_candle_dir, read_wide_frame_from_cache
from lib.storage import read_candles_csv, read_table
from lib.vectorbt_adapter import (
    VectorBTSpec,
    build_price_frame,
    build_target_weight_frame,
    build_target_weight_frame_from_wide_csv,
    run_portfolio_from_target_weights,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run vectorbt portfolio simulation from candle CSVs and target weight CSV."
    )
    parser.add_argument(
        "--candle-dir",
        "--daily-dir",
        dest="candle_dir",
        default="data/upbit/daily",
        help="Directory containing per-market candle CSV files",
    )
    parser.add_argument(
        "--source-cache-dir",
        default="",
        help="Optional wide-parquet source cache directory, e.g. data/upbit_research_cache/60",
    )
    parser.add_argument(
        "--benchmark-source-cache-dir",
        default="",
        help="Optional wide source cache used only for a single-market benchmark",
    )
    parser.add_argument(
        "--load-mode",
        choices=["auto", "candles", "wide"],
        default="auto",
        help="auto/wide: dataframe-first loading, candles: legacy CandleRow loading",
    )
    parser.add_argument(
        "--weights-csv",
        "--weights-path",
        required=True,
        help="Weights table path (.csv or .parquet), sparse or wide format",
    )
    parser.add_argument(
        "--price-column",
        default="trade_price",
        help="CandleRow field to use as the close price input",
    )
    parser.add_argument(
        "--init-cash",
        type=float,
        default=1_000_000.0,
        help="Initial cash for the portfolio",
    )
    parser.add_argument(
        "--fees",
        type=float,
        default=0.0,
        help="Per-order proportional fees passed to vectorbt",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.0,
        help="Per-order proportional slippage passed to vectorbt",
    )
    parser.add_argument(
        "--out-dir",
        default="data/backtest/vectorbt",
        help="Directory to write result CSVs",
    )
    parser.add_argument(
        "--show-plot",
        action="store_true",
        help="Show equity curve plot in a browser window",
    )
    parser.add_argument(
        "--plot-html",
        default="comparison_plot.html",
        help="HTML filename for the comparison plot inside out-dir",
    )
    parser.add_argument(
        "--save-rolling-ir-csv",
        action="store_true",
        help="Write rolling_information_ratio.csv to out-dir; disabled by default to reduce output size",
    )
    parser.add_argument(
        "--benchmark-market",
        default="KRW-BTC",
        help="Market to use for buy-and-hold benchmark",
    )
    parser.add_argument(
        "--benchmark-price-column",
        default="",
        help="Optional source column for single_market benchmark prices. Defaults to --price-column.",
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
        help="Optional JSON object of fixed weights for portfolio_group_rebalance benchmark, e.g. {\"KRW-BTC\":0.5,\"KRW-ETH\":0.5}",
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
        "--timeframe",
        default=None,
        help="Timeframe label such as daily, 240m, or 60m; omitted means infer from candle-dir",
    )
    parser.add_argument(
        "--periods-per-year",
        type=int,
        default=None,
        help="Annualization periods per year; omitted means infer from timeframe",
    )
    parser.add_argument("--strategy-family", default="", help="Optional strategy family metadata")
    parser.add_argument("--strategy-label", default="", help="Optional strategy label metadata")
    parser.add_argument("--asset-scope", default="", help="Optional asset scope metadata")
    parser.add_argument(
        "--parameter-metadata-json",
        default="",
        help="Optional JSON file containing parameter_* metadata fields to write into summary.csv",
    )
    parser.add_argument(
        "--trim-start-mode",
        choices=["first_weight", "none"],
        default="first_weight",
        help="Trim the simulation start to the first timestamp with non-zero target weights; default is first_weight",
    )
    return parser


def load_all_candles(candle_dir: Path) -> list:
    rows = []
    for csv_path in sorted(candle_dir.glob("*.csv")):
        rows.extend(read_candles_csv(csv_path))
    return rows


def load_price_frame(
    candle_dir: Path,
    price_column: str,
    *,
    load_mode: str,
    source_cache_dir: Path | None = None,
    market_columns: list[str] | tuple[str, ...] | None = None,
) -> pd.DataFrame:
    if load_mode == "wide":
        if source_cache_dir is not None:
            cache_path = source_cache_dir / f"{price_column}.parquet"
            if cache_path.exists():
                return read_wide_frame_from_cache(
                    source_cache_dir,
                    price_column,
                    market_columns=market_columns,
                )
        frame = build_wide_frame_from_candle_dir(candle_dir, price_column)
        if market_columns is not None:
            requested_columns = sorted({str(column).upper() for column in market_columns})
            frame = frame.reindex(columns=requested_columns)
        return frame

    candle_rows = load_all_candles(candle_dir)
    return build_price_frame(candle_rows, price_column)


def resolve_load_mode(load_mode: str) -> str:
    if load_mode == "auto":
        return "wide"
    return load_mode


def detect_weight_csv_format(weights_csv: Path) -> str:
    if weights_csv.suffix.lower() == ".parquet":
        columns = set(pd.read_parquet(weights_csv).columns)
    else:
        header = pd.read_csv(weights_csv, nrows=0, encoding="utf-8-sig")
        columns = set(header.columns)
    if {"date_utc", "market", "target_weight"}.issubset(columns):
        return "sparse"
    if "date_utc" in columns:
        return "wide"
    raise ValueError(f"Unsupported weights CSV format: {weights_csv}")


def trim_frames_to_first_weight(
    price_frame: pd.DataFrame,
    target_weight_frame: pd.DataFrame,
    mode: str = "first_weight",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp | None]:
    if mode == "none":
        return price_frame, target_weight_frame, None
    if mode != "first_weight":
        raise ValueError(f"Unsupported trim_start_mode: {mode}")
    active_mask = target_weight_frame.fillna(0.0).abs().sum(axis=1) > 0.0
    if not bool(active_mask.any()):
        return price_frame, target_weight_frame, None
    first_active = pd.Timestamp(active_mask[active_mask].index[0])
    return (
        price_frame.loc[price_frame.index >= first_active],
        target_weight_frame.loc[target_weight_frame.index >= first_active],
        first_active,
    )


def write_summary_csv(path: Path, summary: pd.Series) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_frame(name="value").to_csv(path, encoding="utf-8-sig")


def write_equity_csv(path: Path, portfolio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(portfolio, pd.Series):
        portfolio.to_frame(name=portfolio.name or "value").to_csv(path, encoding="utf-8-sig")
    else:
        portfolio.to_csv(path, encoding="utf-8-sig")


def print_summary(summary: pd.Series) -> None:
    preferred_keys = [
        "Start Value",
        "End Value",
        "Total Return [%]",
        "CAGR [%]",
        "Longest Peak-to-Recovery Bars",
        "Second Longest Peak-to-Recovery Bars",
        "Longest Drawdown Duration Bars",
        "Drawdown Duration P90 Bars",
        "Mean Top-5 Drawdown Duration Bars",
        "Current Drawdown [%]",
        "Current Drawdown Bars",
        "Underwater Time [%]",
        "CDaR 95% [%]",
        "VectorBT Benchmark Return [%]",
        "Benchmark Mode",
        "Benchmark Market",
        "Benchmark Start Value",
        "Benchmark End Value",
        "Benchmark Total Return [%]",
        "Benchmark CAGR [%]",
        "Benchmark Max Drawdown [%]",
        "Benchmark Sharpe Ratio",
        "Benchmark Sortino Ratio",
        "Benchmark Calmar Ratio",
        "Annualized Excess Return [%]",
        "Annualized Jensen Alpha [%]",
        "Benchmark Beta",
        "Benchmark R2",
        "Information Ratio",
        "Annualized Information Ratio",
        "Recent 1Y Return [%]",
        "Recent 1Y Benchmark Return [%]",
        "Recent 1Y Information Ratio",
        "Recent 1Y AIR",
        "Recent 1Y Max Drawdown [%]",
        "Recent 2Y Return [%]",
        "Recent 2Y Benchmark Return [%]",
        "Recent 2Y Information Ratio",
        "Recent 2Y AIR",
        "Recent 2Y Max Drawdown [%]",
        "Max Drawdown [%]",
        "Sharpe Ratio",
        "CVaR 5% Sharpe",
        "Martin Ratio",
        "Ulcer Index [%]",
        "Calmar Ratio",
        "Sortino Ratio",
        "Total Trades",
        "Win Rate [%]",
    ]
    print("VectorBT Summary")
    for key in preferred_keys:
        if key in summary.index:
            print(f"{key}: {summary[key]}")


def infer_timeframe(candle_dir: Path, timeframe: str | None = None) -> str:
    if timeframe:
        return timeframe.lower()

    parts = [part.lower() for part in candle_dir.parts]
    if "daily" in parts:
        return "daily"

    for idx, part in enumerate(parts):
        if part == "minutes" and idx + 1 < len(parts) and parts[idx + 1].isdigit():
            return f"{parts[idx + 1]}m"

    return "daily"


def periods_per_day_for_timeframe(timeframe: str) -> int:
    normalized = timeframe.lower()
    if normalized == "daily":
        return 1
    matched = re.fullmatch(r"(\d+)m", normalized)
    if not matched:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    minutes = int(matched.group(1))
    if minutes <= 0 or 1440 % minutes != 0:
        raise ValueError(f"Unsupported minute timeframe: {timeframe}")
    return 1440 // minutes


def infer_periods_per_year(timeframe: str) -> int:
    normalized = timeframe.lower()
    if normalized == "daily":
        return 252
    return 365 * periods_per_day_for_timeframe(normalized)


def resolve_periods_per_year(timeframe: str, configured_value: object = None) -> int:
    if configured_value is None:
        return infer_periods_per_year(timeframe)
    if isinstance(configured_value, bool):
        raise ValueError("periods_per_year must be a positive integer")
    try:
        numeric_value = float(configured_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("periods_per_year must be a positive integer") from exc
    if not math.isfinite(numeric_value) or numeric_value <= 0 or not numeric_value.is_integer():
        raise ValueError("periods_per_year must be a positive integer")
    return int(numeric_value)


def timeframe_to_pandas_freq(timeframe: str) -> str:
    normalized = timeframe.lower()
    if normalized == "daily":
        return "1D"
    matched = re.fullmatch(r"(\d+)m", normalized)
    if not matched:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    minutes = int(matched.group(1))
    return f"{minutes}min"


def build_benchmark_curve(
    price_frame: pd.DataFrame,
    benchmark_market: str,
    init_cash: float,
) -> pd.Series:
    if benchmark_market not in price_frame.columns:
        raise ValueError(f"Benchmark market not found in price frame: {benchmark_market}")
    benchmark_prices = price_frame[benchmark_market].dropna()
    if benchmark_prices.empty:
        raise ValueError(f"No valid benchmark prices for market: {benchmark_market}")
    first_price = float(benchmark_prices.iloc[0])
    curve = (benchmark_prices / first_price) * init_cash
    curve.name = f"{benchmark_market}_benchmark_value"
    return curve


def build_portfolio_group_benchmark_curve(portfolio) -> pd.Series:
    curve = portfolio.benchmark_value(group_by=True)
    if isinstance(curve, pd.DataFrame):
        if curve.shape[1] != 1:
            raise ValueError("portfolio_group benchmark requires a single grouped benchmark column")
        curve = curve.iloc[:, 0]
    curve = pd.to_numeric(curve, errors="coerce").dropna()
    curve.name = "portfolio_group_benchmark_value"
    return curve


def _normalize_benchmark_weights(
    price_frame: pd.DataFrame,
    benchmark_fixed_weights: dict[str, float] | None,
) -> pd.Series:
    available_columns = [
        str(column)
        for column in price_frame.columns
        if pd.to_numeric(price_frame[column], errors="coerce").notna().any()
    ]
    if not available_columns:
        raise ValueError("No valid markets available for portfolio_group_rebalance benchmark")

    if benchmark_fixed_weights:
        weights = pd.Series(
            {str(key).upper(): float(value) for key, value in benchmark_fixed_weights.items()},
            dtype=float,
        )
        weights = weights.reindex(sorted(column for column in weights.index if column in available_columns))
        if weights.empty:
            raise ValueError(
                "benchmark_fixed_weights does not include any markets present in price_frame"
            )
    else:
        weights = pd.Series(1.0, index=sorted(available_columns), dtype=float)

    positive_sum = float(weights.sum())
    if positive_sum <= 0.0:
        raise ValueError("benchmark_fixed_weights must sum to a positive value")
    return weights / positive_sum


def _rebalance_schedule(index: pd.Index, rebalance_frequency: str) -> list[pd.Timestamp]:
    timestamps = [pd.Timestamp(value) for value in index]
    if rebalance_frequency == "every_bar":
        return timestamps

    if rebalance_frequency == "daily":
        keys = [(timestamp.year, timestamp.month, timestamp.day) for timestamp in timestamps]
    elif rebalance_frequency == "weekly":
        keys = [timestamp.isocalendar()[:2] for timestamp in timestamps]
    elif rebalance_frequency == "monthly":
        keys = [(timestamp.year, timestamp.month) for timestamp in timestamps]
    elif rebalance_frequency in {"quarterly", "quarter", "q"}:
        keys = [(timestamp.year, (timestamp.month - 1) // 3 + 1) for timestamp in timestamps]
    elif rebalance_frequency in {"semiannual", "semi_annually", "half_year", "half-year"}:
        keys = [(timestamp.year, 1 if timestamp.month <= 6 else 2) for timestamp in timestamps]
    else:
        raise ValueError(f"Unsupported benchmark_rebalance_frequency: {rebalance_frequency}")

    chosen: list[pd.Timestamp] = []
    seen: set[tuple[int, ...]] = set()
    for timestamp, key in zip(timestamps, keys):
        if key in seen:
            continue
        seen.add(key)
        chosen.append(timestamp)
    return chosen


def build_portfolio_group_rebalance_benchmark_curve(
    *,
    price_frame: pd.DataFrame,
    vectorbt_spec: VectorBTSpec,
    benchmark_fixed_weights: dict[str, float] | None = None,
    benchmark_rebalance_frequency: str = "every_bar",
    benchmark_listed_normalize: bool = False,
) -> pd.Series:
    weights = _normalize_benchmark_weights(price_frame, benchmark_fixed_weights)
    benchmark_price_frame = price_frame.reindex(columns=list(weights.index)).copy()
    listed_mask = benchmark_price_frame.notna().cummax()
    target_weight_frame = pd.DataFrame(
        float("nan"),
        index=benchmark_price_frame.index,
        columns=benchmark_price_frame.columns,
    )
    for timestamp in _rebalance_schedule(benchmark_price_frame.index, benchmark_rebalance_frequency):
        if timestamp not in target_weight_frame.index:
            continue
        if benchmark_listed_normalize:
            listed = listed_mask.loc[timestamp]
            row_weights = weights.where(listed, 0.0)
            row_sum = float(row_weights.sum())
            if row_sum <= 0.0:
                continue
            target_weight_frame.loc[timestamp, :] = (row_weights / row_sum).to_numpy(dtype=float)
        else:
            target_weight_frame.loc[timestamp, :] = weights.to_numpy(dtype=float)

    benchmark_portfolio = run_portfolio_from_target_weights(
        price_frame=benchmark_price_frame,
        target_weight_frame=target_weight_frame,
        spec=VectorBTSpec(
            price_column=vectorbt_spec.price_column,
            init_cash=vectorbt_spec.init_cash,
            fees=vectorbt_spec.fees,
            slippage=vectorbt_spec.slippage,
            cash_sharing=vectorbt_spec.cash_sharing,
            group_by=vectorbt_spec.group_by,
            size_type=vectorbt_spec.size_type,
            call_seq=vectorbt_spec.call_seq,
            freq=vectorbt_spec.freq,
        ),
    )
    curve = benchmark_portfolio.value()
    if isinstance(curve, pd.DataFrame):
        if curve.shape[1] != 1:
            raise ValueError("portfolio_group_rebalance benchmark requires a single grouped benchmark column")
        curve = curve.iloc[:, 0]
    curve = pd.to_numeric(curve, errors="coerce").dropna()
    curve.name = "portfolio_group_rebalance_benchmark_value"
    return curve


def resolve_benchmark_curve(
    *,
    price_frame: pd.DataFrame,
    portfolio,
    init_cash: float,
    benchmark_mode: str,
    benchmark_market: str,
    benchmark_price_frame: pd.DataFrame | None = None,
    vectorbt_spec: VectorBTSpec | None = None,
    benchmark_fixed_weights: dict[str, float] | None = None,
    benchmark_rebalance_frequency: str = "every_bar",
    benchmark_listed_normalize: bool = False,
) -> tuple[pd.Series, str]:
    if benchmark_mode == "portfolio_group":
        return build_portfolio_group_benchmark_curve(portfolio), "PORTFOLIO_GROUP"
    if benchmark_mode == "portfolio_group_rebalance":
        effective_spec = vectorbt_spec or VectorBTSpec(init_cash=init_cash)
        label = "PORTFOLIO_GROUP_REBALANCE"
        if benchmark_listed_normalize:
            label = "PORTFOLIO_GROUP_REBALANCE_LISTED_NORMALIZED"
        return (
            build_portfolio_group_rebalance_benchmark_curve(
                price_frame=price_frame,
                vectorbt_spec=effective_spec,
                benchmark_fixed_weights=benchmark_fixed_weights,
                benchmark_rebalance_frequency=benchmark_rebalance_frequency,
                benchmark_listed_normalize=benchmark_listed_normalize,
            ),
            label,
        )
    if benchmark_mode != "single_market":
        raise ValueError(f"Unsupported benchmark_mode: {benchmark_mode}")
    single_market_price_frame = benchmark_price_frame if benchmark_price_frame is not None else price_frame
    return build_benchmark_curve(single_market_price_frame, benchmark_market, init_cash), benchmark_market


def build_benchmark_comparison_curves(
    equity_curve: pd.Series,
    benchmark_curve: pd.Series,
    init_cash: float,
) -> tuple[pd.Series, pd.Series]:
    aligned = pd.concat(
        [
            pd.to_numeric(equity_curve, errors="coerce").rename("strategy"),
            pd.to_numeric(benchmark_curve, errors="coerce").rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < 2:
        raise ValueError("Strategy and benchmark need at least 2 common observations")

    strategy_start = float(aligned["strategy"].iloc[0])
    benchmark_start = float(aligned["benchmark"].iloc[0])
    if strategy_start <= 0.0 or benchmark_start <= 0.0:
        raise ValueError("Strategy and benchmark curves must be positive at the comparison start")

    comparison_equity = (aligned["strategy"] / strategy_start) * init_cash
    comparison_benchmark = (aligned["benchmark"] / benchmark_start) * init_cash
    comparison_equity.name = "comparison_strategy_value"
    comparison_benchmark.name = benchmark_curve.name or "benchmark_value"
    return comparison_equity, comparison_benchmark


def benchmark_comparison_period_summary(
    comparison_equity_curve: pd.Series,
    annualization_factor: int,
) -> pd.Series:
    return pd.Series(
        {
            "Benchmark Comparison Start": pd.Timestamp(comparison_equity_curve.index[0]).isoformat(),
            "Benchmark Comparison End": pd.Timestamp(comparison_equity_curve.index[-1]).isoformat(),
            "Benchmark Comparison Observations": int(len(comparison_equity_curve)),
            "Benchmark Comparison Strategy Total Return [%]": (
                (float(comparison_equity_curve.iloc[-1] / comparison_equity_curve.iloc[0]) - 1.0)
                * 100.0
            ),
            "Benchmark Comparison Strategy CAGR [%]": (
                compute_annualized_return(
                    comparison_equity_curve,
                    annualization_factor=annualization_factor,
                )
                * 100.0
            ),
        }
    )


def benchmark_summary(
    benchmark_curve: pd.Series,
    init_cash: float,
    benchmark_label: str,
    annualization_factor: int = 252,
    benchmark_mode: str = "single_market",
) -> pd.Series:
    end_value = float(benchmark_curve.iloc[-1])
    total_return = ((end_value / init_cash) - 1.0) * 100.0
    returns = compute_return_series(benchmark_curve)
    max_drawdown_pct = compute_max_drawdown_pct(benchmark_curve)
    sharpe_ratio = compute_sharpe_ratio(returns, annualization_factor=annualization_factor)
    sortino_ratio = compute_sortino_ratio(returns, annualization_factor=annualization_factor)
    annualized_return = compute_annualized_return(benchmark_curve, annualization_factor=annualization_factor)
    calmar_ratio = float("nan")
    if max_drawdown_pct != 0:
        calmar_ratio = annualized_return / (abs(max_drawdown_pct) / 100.0)
    return pd.Series(
        {
            "Benchmark Market": benchmark_label,
            "Benchmark Mode": benchmark_mode,
            "Benchmark Start Value": init_cash,
            "Benchmark End Value": end_value,
            "Benchmark Total Return [%]": total_return,
            "Benchmark CAGR [%]": annualized_return * 100.0,
            "Benchmark Max Drawdown [%]": max_drawdown_pct,
            "Benchmark Sharpe Ratio": sharpe_ratio,
            "Benchmark Sortino Ratio": sortino_ratio,
            "Benchmark Calmar Ratio": calmar_ratio,
        }
    )


def compute_return_series(curve: pd.Series) -> pd.Series:
    returns = curve.pct_change()
    returns.iloc[0] = 0.0
    return returns.fillna(0.0)


def compute_max_drawdown_pct(curve: pd.Series) -> float:
    running_max = curve.cummax()
    drawdown = (curve / running_max) - 1.0
    return float(drawdown.min() * 100.0)


def compute_cdar_pct(curve: pd.Series, confidence: float = 0.95) -> float:
    clean_curve = pd.to_numeric(curve, errors="coerce").dropna()
    if clean_curve.empty:
        return float("nan")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")

    drawdown_losses_pct = ((clean_curve / clean_curve.cummax()) - 1.0).abs() * 100.0
    raw_tail_count = (1.0 - confidence) * len(drawdown_losses_pct)
    tail_count = max(1, int(math.ceil(raw_tail_count - 1e-12)))
    return float(drawdown_losses_pct.nlargest(tail_count).mean())


def compute_drawdown_recovery_stats(curve: pd.Series) -> pd.Series:
    series = pd.to_numeric(curve, errors="coerce").dropna()
    if series.empty:
        return pd.Series(
            {
                "Longest Peak-to-Recovery Bars": float("nan"),
                "Second Longest Peak-to-Recovery Bars": float("nan"),
                "Longest Drawdown Duration Bars": float("nan"),
                "Drawdown Duration P90 Bars": float("nan"),
                "Mean Top-5 Drawdown Duration Bars": float("nan"),
                "Current Drawdown [%]": float("nan"),
                "Current Drawdown Bars": float("nan"),
                "Underwater Time [%]": float("nan"),
                "CDaR 95% [%]": float("nan"),
            }
        )

    drawdown = (series / series.cummax()) - 1.0
    running_max = float(series.iloc[0])
    running_max_time = pd.Timestamp(series.index[0])
    in_drawdown = False
    completed_duration_bars: list[int] = []

    for timestamp, value in series.items():
        current_time = pd.Timestamp(timestamp)
        current_value = float(value)

        if current_value >= running_max:
            if in_drawdown:
                peak_to_recovery_bars = int(series.loc[running_max_time:current_time].shape[0] - 1)
                if peak_to_recovery_bars > 0:
                    completed_duration_bars.append(peak_to_recovery_bars)
                in_drawdown = False

            running_max = current_value
            running_max_time = current_time
            continue

        if not in_drawdown:
            in_drawdown = True
            continue

    current_drawdown_bars = 0
    if in_drawdown:
        current_drawdown_bars = int(series.loc[running_max_time:].shape[0] - 1)

    completed_duration_bars.sort(reverse=True)
    all_duration_bars = completed_duration_bars.copy()
    if current_drawdown_bars > 0:
        all_duration_bars.append(current_drawdown_bars)
    all_duration_bars.sort(reverse=True)

    longest = float("nan")
    second_longest = float("nan")
    if completed_duration_bars:
        longest = float(completed_duration_bars[0])
    if len(completed_duration_bars) >= 2:
        second_longest = float(completed_duration_bars[1])

    longest_duration = float("nan")
    duration_p90 = float("nan")
    mean_top5_duration = float("nan")
    if all_duration_bars:
        duration_series = pd.Series(all_duration_bars, dtype=float)
        longest_duration = float(duration_series.max())
        duration_p90 = float(duration_series.quantile(0.90))
        mean_top5_duration = float(duration_series.nlargest(5).mean())

    return pd.Series(
        {
            "Longest Peak-to-Recovery Bars": longest,
            "Second Longest Peak-to-Recovery Bars": second_longest,
            "Longest Drawdown Duration Bars": longest_duration,
            "Drawdown Duration P90 Bars": duration_p90,
            "Mean Top-5 Drawdown Duration Bars": mean_top5_duration,
            "Current Drawdown [%]": float(drawdown.iloc[-1] * 100.0),
            "Current Drawdown Bars": float(current_drawdown_bars),
            "Underwater Time [%]": float((drawdown < 0.0).mean() * 100.0),
            "CDaR 95% [%]": compute_cdar_pct(series, confidence=0.95),
        }
    )


def compute_sharpe_ratio(returns: pd.Series, annualization_factor: int = 252) -> float:
    std = float(returns.std(ddof=0))
    if std == 0.0:
        return float("nan")
    return float((returns.mean() / std) * (annualization_factor ** 0.5))


def compute_sortino_ratio(returns: pd.Series, annualization_factor: int = 252) -> float:
    clean_returns = pd.to_numeric(returns, errors="coerce").dropna()
    if clean_returns.empty:
        return float("nan")
    downside_returns = clean_returns.clip(upper=0.0)
    downside_deviation = float((downside_returns.pow(2).mean()) ** 0.5)
    if downside_deviation == 0.0:
        return float("nan")
    return float((clean_returns.mean() / downside_deviation) * (annualization_factor ** 0.5))


def compute_cvar_sharpe_ratio(
    returns: pd.Series,
    annualization_factor: int = 252,
    alpha: float = 0.05,
) -> float:
    clean_returns = pd.to_numeric(returns, errors="coerce").dropna()
    if clean_returns.empty:
        return float("nan")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between 0 and 1")
    var_threshold = float(clean_returns.quantile(alpha))
    tail_losses = clean_returns[clean_returns <= var_threshold]
    if tail_losses.empty:
        return float("nan")
    cvar_loss = abs(float(tail_losses.mean()))
    if cvar_loss == 0.0:
        return float("nan")
    return float((clean_returns.mean() / cvar_loss) * (annualization_factor ** 0.5))


def compute_ulcer_index_pct(curve: pd.Series) -> float:
    clean_curve = pd.to_numeric(curve, errors="coerce").dropna()
    if clean_curve.empty:
        return float("nan")
    running_max = clean_curve.cummax()
    drawdown_pct = ((clean_curve / running_max) - 1.0) * 100.0
    return float((drawdown_pct.pow(2).mean()) ** 0.5)


def compute_martin_ratio(
    curve: pd.Series,
    annualization_factor: int = 252,
) -> float:
    annualized_return = compute_annualized_return(curve, annualization_factor=annualization_factor)
    ulcer_index_pct = compute_ulcer_index_pct(curve)
    if not math.isfinite(annualized_return) or not math.isfinite(ulcer_index_pct) or ulcer_index_pct == 0.0:
        return float("nan")
    return float(annualized_return / (ulcer_index_pct / 100.0))


def compute_annualized_return(curve: pd.Series, annualization_factor: int = 252) -> float:
    if len(curve) < 2:
        return float("nan")
    total_return = float(curve.iloc[-1] / curve.iloc[0])
    periods = len(curve) - 1
    if total_return <= 0 or periods <= 0:
        return float("nan")
    return float(total_return ** (annualization_factor / periods) - 1.0)


def strategy_performance_summary(
    curve: pd.Series,
    annualization_factor: int = 252,
) -> pd.Series:
    returns = compute_return_series(curve)
    annualized_return = compute_annualized_return(curve, annualization_factor=annualization_factor)
    max_drawdown_pct = compute_max_drawdown_pct(curve)
    calmar_ratio = float("nan")
    if math.isfinite(annualized_return) and max_drawdown_pct != 0.0:
        calmar_ratio = annualized_return / (abs(max_drawdown_pct) / 100.0)
    return pd.Series(
        {
            "CAGR [%]": annualized_return * 100.0,
            "Sharpe Ratio": compute_sharpe_ratio(returns, annualization_factor=annualization_factor),
            "Sortino Ratio": compute_sortino_ratio(returns, annualization_factor=annualization_factor),
            "Calmar Ratio": calmar_ratio,
            "CVaR 5% Sharpe": compute_cvar_sharpe_ratio(
                returns,
                annualization_factor=annualization_factor,
                alpha=0.05,
            ),
            "Ulcer Index [%]": compute_ulcer_index_pct(curve),
            "Martin Ratio": compute_martin_ratio(curve, annualization_factor=annualization_factor),
        }
    )


def compute_information_ratio(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    annualization_factor: int = 252,
) -> pd.Series:
    aligned = pd.concat(
        [
            strategy_returns.rename("strategy"),
            benchmark_returns.rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    excess_returns = aligned["strategy"] - aligned["benchmark"]
    tracking_error = float(excess_returns.std(ddof=0))
    if tracking_error == 0.0:
        info_ratio = float("nan")
        annualized_info_ratio = float("nan")
    else:
        info_ratio = float(excess_returns.mean() / tracking_error)
        annualized_info_ratio = info_ratio * (annualization_factor ** 0.5)
    return pd.Series(
        {
            "Information Ratio": info_ratio,
            "Annualized Information Ratio": annualized_info_ratio,
        }
    )


def compute_benchmark_regression_stats(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    annualization_factor: int = 252,
) -> pd.Series:
    aligned = pd.concat(
        [
            strategy_returns.rename("strategy"),
            benchmark_returns.rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    empty = pd.Series(
        {
            "Annualized Excess Return [%]": float("nan"),
            "Annualized Jensen Alpha [%]": float("nan"),
            "Benchmark Beta": float("nan"),
            "Benchmark R2": float("nan"),
        }
    )
    if len(aligned) < 2:
        return empty

    strategy = aligned["strategy"]
    benchmark = aligned["benchmark"]
    annualized_excess_return = float((strategy.mean() - benchmark.mean()) * annualization_factor * 100.0)
    benchmark_var = float(benchmark.var(ddof=0))
    strategy_std = float(strategy.std(ddof=0))
    benchmark_std = float(benchmark.std(ddof=0))
    if benchmark_var == 0.0 or benchmark_std == 0.0:
        return pd.Series(
            {
                "Annualized Excess Return [%]": annualized_excess_return,
                "Annualized Jensen Alpha [%]": float("nan"),
                "Benchmark Beta": float("nan"),
                "Benchmark R2": float("nan"),
            }
        )

    covariance = float(((strategy - strategy.mean()) * (benchmark - benchmark.mean())).mean())
    beta = covariance / benchmark_var
    alpha_per_period = float(strategy.mean() - (beta * benchmark.mean()))
    annualized_alpha = alpha_per_period * annualization_factor * 100.0
    if strategy_std == 0.0:
        r2 = float("nan")
    else:
        correlation = covariance / (strategy_std * benchmark_std)
        r2 = correlation * correlation
    return pd.Series(
        {
            "Annualized Excess Return [%]": annualized_excess_return,
            "Annualized Jensen Alpha [%]": annualized_alpha,
            "Benchmark Beta": beta,
            "Benchmark R2": r2,
        }
    )


def compute_excess_curves(
    equity_curve: pd.Series,
    benchmark_curve: pd.Series,
    init_cash: float,
) -> tuple[pd.Series, pd.Series]:
    strategy_returns = compute_return_series(equity_curve)
    benchmark_returns = compute_return_series(benchmark_curve)
    aligned = pd.concat(
        [
            strategy_returns.rename("strategy"),
            benchmark_returns.rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    excess_returns = aligned["strategy"] - aligned["benchmark"]
    relative_curve = ((1.0 + excess_returns).cumprod()) * init_cash
    relative_curve.name = "excess_equity_curve"
    return excess_returns.rename("excess_return"), relative_curve


def compute_rolling_information_ratio(
    excess_returns: pd.Series,
    windows: tuple[int, ...] = (126, 252),
    periods_per_day: int = 1,
    annualization_factor: int = 252,
) -> pd.DataFrame:
    frame = pd.DataFrame(index=excess_returns.index)
    for window_days in windows:
        window_periods = max(1, int(math.ceil(window_days * periods_per_day)))
        rolling_mean = excess_returns.rolling(window_periods).mean()
        rolling_std = excess_returns.rolling(window_periods).std(ddof=0)
        rolling_ir = rolling_mean / rolling_std
        frame[f"rolling_ir_{window_days}d"] = rolling_ir * (annualization_factor ** 0.5)
    return frame


def compute_recent_1y_stats(
    equity_curve: pd.Series,
    benchmark_curve: pd.Series,
    annualization_factor: int = 252,
) -> pd.Series:
    empty_result = pd.Series(
        {
            "Recent 1Y Return [%]": float("nan"),
            "Recent 1Y Benchmark Return [%]": float("nan"),
            "Recent 1Y Information Ratio": float("nan"),
            "Recent 1Y AIR": float("nan"),
            "Recent 1Y Max Drawdown [%]": float("nan"),
        }
    )
    if equity_curve.empty or benchmark_curve.empty:
        return empty_result

    last_timestamp = pd.Timestamp(equity_curve.index[-1])
    start_timestamp = last_timestamp - pd.DateOffset(years=1)
    aligned = pd.concat(
        [
            equity_curve[equity_curve.index >= start_timestamp].rename("strategy"),
            benchmark_curve[benchmark_curve.index >= start_timestamp].rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < 2:
        return empty_result

    recent_equity = aligned["strategy"]
    recent_benchmark = aligned["benchmark"]
    recent_return = ((float(recent_equity.iloc[-1] / recent_equity.iloc[0])) - 1.0) * 100.0
    recent_benchmark_return = (
        (float(recent_benchmark.iloc[-1] / recent_benchmark.iloc[0])) - 1.0
    ) * 100.0
    recent_ir = compute_information_ratio(
        compute_return_series(recent_equity),
        compute_return_series(recent_benchmark),
        annualization_factor=annualization_factor,
    )
    return pd.Series(
        {
            "Recent 1Y Return [%]": recent_return,
            "Recent 1Y Benchmark Return [%]": recent_benchmark_return,
            "Recent 1Y Information Ratio": recent_ir["Information Ratio"],
            "Recent 1Y AIR": recent_ir["Annualized Information Ratio"],
            "Recent 1Y Max Drawdown [%]": compute_max_drawdown_pct(recent_equity),
        }
    )


def compute_recent_2y_stats(
    equity_curve: pd.Series,
    benchmark_curve: pd.Series,
    annualization_factor: int = 252,
) -> pd.Series:
    empty_result = pd.Series(
        {
            "Recent 2Y Return [%]": float("nan"),
            "Recent 2Y Benchmark Return [%]": float("nan"),
            "Recent 2Y Information Ratio": float("nan"),
            "Recent 2Y AIR": float("nan"),
            "Recent 2Y Max Drawdown [%]": float("nan"),
        }
    )
    if equity_curve.empty or benchmark_curve.empty:
        return empty_result

    last_timestamp = pd.Timestamp(equity_curve.index[-1])
    start_timestamp = last_timestamp - pd.DateOffset(years=2)
    aligned = pd.concat(
        [
            equity_curve[equity_curve.index >= start_timestamp].rename("strategy"),
            benchmark_curve[benchmark_curve.index >= start_timestamp].rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < 2:
        return empty_result

    recent_equity = aligned["strategy"]
    recent_benchmark = aligned["benchmark"]
    recent_return = ((float(recent_equity.iloc[-1] / recent_equity.iloc[0])) - 1.0) * 100.0
    recent_benchmark_return = (
        (float(recent_benchmark.iloc[-1] / recent_benchmark.iloc[0])) - 1.0
    ) * 100.0
    recent_ir = compute_information_ratio(
        compute_return_series(recent_equity),
        compute_return_series(recent_benchmark),
        annualization_factor=annualization_factor,
    )
    return pd.Series(
        {
            "Recent 2Y Return [%]": recent_return,
            "Recent 2Y Benchmark Return [%]": recent_benchmark_return,
            "Recent 2Y Information Ratio": recent_ir["Information Ratio"],
            "Recent 2Y AIR": recent_ir["Annualized Information Ratio"],
            "Recent 2Y Max Drawdown [%]": compute_max_drawdown_pct(recent_equity),
        }
    )


def summarize_rolling_information_ratio(rolling_ir: pd.DataFrame) -> pd.Series:
    summary: dict[str, float] = {}
    for column in rolling_ir.columns:
        series = (
            pd.to_numeric(rolling_ir[column], errors="coerce")
            .replace([float("inf"), float("-inf")], float("nan"))
            .dropna()
        )
        label = column.replace("rolling_ir_", "Rolling IR ").replace("d", "d")
        if series.empty:
            summary[f"{label} Mean"] = float("nan")
            summary[f"{label} Std"] = float("nan")
            summary[f"{label} Median"] = float("nan")
            summary[f"{label} Q25"] = float("nan")
            summary[f"{label} Positive Ratio"] = float("nan")
            summary[f"{label} Min"] = float("nan")
            summary[f"{label} Max"] = float("nan")
            continue
        summary[f"{label} Mean"] = float(series.mean())
        summary[f"{label} Std"] = float(series.std(ddof=0))
        summary[f"{label} Median"] = float(series.median())
        summary[f"{label} Q25"] = float(series.quantile(0.25))
        summary[f"{label} Positive Ratio"] = float((series > 0).mean())
        summary[f"{label} Min"] = float(series.min())
        summary[f"{label} Max"] = float(series.max())
    return pd.Series(summary)


def _as_single_series(curve, label: str) -> pd.Series:
    if isinstance(curve, pd.DataFrame):
        if curve.shape[1] != 1:
            raise ValueError(f"{label} must contain exactly one curve")
        return curve.iloc[:, 0]
    return curve


def build_comparison_figure(equity_curve, benchmark_curve: pd.Series | None = None) -> go.Figure:
    series = _as_single_series(equity_curve, "equity_curve")
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=series.index,
            y=series.values,
            mode="lines",
            name="Strategy",
        )
    )
    if benchmark_curve is not None:
        benchmark_series = _as_single_series(benchmark_curve, "benchmark_curve")
        fig.add_trace(
            go.Scatter(
                x=benchmark_series.index,
                y=benchmark_series.values,
                mode="lines",
                name=str(benchmark_series.name or "Benchmark"),
            )
        )
    fig.update_layout(
        title="Strategy vs Benchmark",
        xaxis_title="Date",
        yaxis_title="Portfolio Value",
    )
    return fig


def show_equity_plot(
    equity_curve,
    benchmark_curve: pd.Series | None = None,
    html_path: Path | None = None,
) -> None:
    fig = build_comparison_figure(equity_curve, benchmark_curve)
    if html_path is not None:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        fig.write_html(str(html_path), auto_open=False)
        webbrowser.open(html_path.resolve().as_uri())
        return
    fig.show()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    candle_dir = Path(args.candle_dir)
    source_cache_dir = Path(args.source_cache_dir) if args.source_cache_dir else None
    timeframe = infer_timeframe(Path(args.candle_dir), args.timeframe)
    periods_per_year = resolve_periods_per_year(timeframe, args.periods_per_year)
    periods_per_day = periods_per_day_for_timeframe(timeframe)
    pandas_freq = timeframe_to_pandas_freq(timeframe)
    parameter_metadata: dict[str, str | int | float] = {}
    if args.parameter_metadata_json:
        parameter_metadata = json.loads(Path(args.parameter_metadata_json).read_text(encoding="utf-8-sig"))

    resolved_load_mode = resolve_load_mode(args.load_mode)
    candle_rows = None
    price_frame = load_price_frame(
        candle_dir,
        args.price_column,
        load_mode=resolved_load_mode,
        source_cache_dir=source_cache_dir,
    )
    if resolved_load_mode != "wide":
        candle_rows = load_all_candles(candle_dir)
        if not candle_rows:
            raise SystemExit(f"No candle rows found in {args.candle_dir}")
    elif price_frame.empty:
        raise SystemExit(f"No price rows found in {args.candle_dir}")
    benchmark_price_column = str(args.benchmark_price_column or args.price_column)
    benchmark_market = str(args.benchmark_market).upper()
    benchmark_source_cache_dir = (
        Path(args.benchmark_source_cache_dir) if args.benchmark_source_cache_dir else source_cache_dir
    )
    benchmark_uses_strategy_price_frame = (
        args.benchmark_mode == "single_market"
        and benchmark_price_column == args.price_column
        and benchmark_source_cache_dir == source_cache_dir
    )
    benchmark_price_frame = None
    if args.benchmark_mode == "single_market" and not benchmark_uses_strategy_price_frame:
        benchmark_price_frame = load_price_frame(
            candle_dir,
            benchmark_price_column,
            load_mode="wide" if args.benchmark_source_cache_dir else resolved_load_mode,
            source_cache_dir=benchmark_source_cache_dir,
            market_columns=[benchmark_market],
        )
        if benchmark_price_frame.empty:
            raise SystemExit(f"No benchmark price rows found for {benchmark_price_column}")
    weight_csv_format = detect_weight_csv_format(Path(args.weights_csv))
    if weight_csv_format == "wide":
        target_weight_frame = build_target_weight_frame_from_wide_csv(
            args.weights_csv,
            price_frame,
        )
    else:
        weight_rows = read_table(Path(args.weights_csv))
        if not weight_rows:
            raise SystemExit(f"No weight rows found in {args.weights_csv}")
        target_weight_frame = build_target_weight_frame(weight_rows, price_frame)
    trimmed_start_timestamp = None
    if args.trim_start_mode == "first_weight":
        price_frame, target_weight_frame, trimmed_start_timestamp = trim_frames_to_first_weight(
            price_frame,
            target_weight_frame,
        )
        if benchmark_price_frame is not None and trimmed_start_timestamp is not None:
            benchmark_price_frame = benchmark_price_frame.loc[benchmark_price_frame.index >= trimmed_start_timestamp]
    if benchmark_price_frame is not None and not price_frame.empty:
        benchmark_price_frame = benchmark_price_frame.loc[
            (benchmark_price_frame.index >= price_frame.index.min())
            & (benchmark_price_frame.index <= price_frame.index.max())
        ]
    portfolio = run_portfolio_from_target_weights(
        price_frame=price_frame,
        target_weight_frame=target_weight_frame,
        spec=VectorBTSpec(
            price_column=args.price_column,
            init_cash=args.init_cash,
            fees=args.fees,
            slippage=args.slippage,
            freq=pandas_freq,
        ),
    )

    out_dir = Path(args.out_dir)
    summary = portfolio.stats(settings={"freq": pandas_freq})
    if "Benchmark Return [%]" in summary.index:
        summary = summary.rename(index={"Benchmark Return [%]": "VectorBT Benchmark Return [%]"})
    equity_curve = portfolio.value()
    benchmark_fixed_weights = (
        json.loads(args.benchmark_fixed_weights_json)
        if args.benchmark_fixed_weights_json
        else None
    )
    benchmark_curve, benchmark_label = resolve_benchmark_curve(
        price_frame=price_frame,
        portfolio=portfolio,
        init_cash=args.init_cash,
        benchmark_mode=args.benchmark_mode,
        benchmark_market=benchmark_market,
        benchmark_price_frame=benchmark_price_frame,
        vectorbt_spec=VectorBTSpec(
            price_column=args.price_column,
            init_cash=args.init_cash,
            fees=args.fees,
            slippage=args.slippage,
            freq=pandas_freq,
        ),
        benchmark_fixed_weights=benchmark_fixed_weights,
        benchmark_rebalance_frequency=args.benchmark_rebalance_frequency,
        benchmark_listed_normalize=bool(args.benchmark_listed_normalize),
    )
    comparison_equity_curve, aligned_benchmark_curve = build_benchmark_comparison_curves(
        equity_curve,
        benchmark_curve,
        args.init_cash,
    )
    benchmark_stats = benchmark_summary(
        aligned_benchmark_curve,
        args.init_cash,
        benchmark_label,
        annualization_factor=periods_per_year,
        benchmark_mode=args.benchmark_mode,
    )
    comparison_period_stats = benchmark_comparison_period_summary(
        comparison_equity_curve,
        annualization_factor=periods_per_year,
    )
    strategy_returns = compute_return_series(equity_curve)
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
        args.init_cash,
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
    summary.loc["Load Mode"] = resolved_load_mode
    summary.loc["Weights CSV Format"] = weight_csv_format
    summary.loc["Trim Start Mode"] = args.trim_start_mode
    if trimmed_start_timestamp is not None:
        summary.loc["Trimmed Start Timestamp"] = trimmed_start_timestamp.isoformat()
    summary.loc["Timeframe"] = timeframe
    summary.loc["Benchmark Price Column"] = benchmark_price_column
    summary.loc["Benchmark Source Cache Dir"] = (
        str(benchmark_source_cache_dir) if benchmark_source_cache_dir is not None else ""
    )
    summary.loc["Periods Per Year"] = periods_per_year
    if args.strategy_family:
        summary.loc["Strategy Family"] = args.strategy_family
    if args.strategy_label:
        summary.loc["Strategy Label"] = args.strategy_label
    if args.asset_scope:
        summary.loc["Asset Scope"] = args.asset_scope
    for key, value in parameter_metadata.items():
        if str(key).startswith("parameter_"):
            summary.loc[str(key)] = value
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
    write_equity_csv(out_dir / "excess_returns.csv", excess_returns)
    if args.save_rolling_ir_csv:
        write_equity_csv(out_dir / "rolling_information_ratio.csv", rolling_ir)
    target_weight_frame.to_csv(out_dir / "target_weights_full.csv", encoding="utf-8-sig")
    if args.show_plot:
        show_equity_plot(equity_curve, aligned_benchmark_curve, out_dir / args.plot_html)
    print(f"Resolved load mode: {resolved_load_mode}")
    print(f"Wrote vectorbt results to {out_dir}")


if __name__ == "__main__":
    main()
