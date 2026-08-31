#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import (
    apply_by_market_column,
    build_wide_frames_from_candle_dir,
    compute_market_beta_frame,
    compute_market_consistency_ratio_frame,
    compute_market_forward_return_frame,
    compute_market_median_momentum_frame,
    compute_market_momentum_frame,
    compute_market_residual_momentum_frame,
    compute_market_rolling_sum_frame,
    compute_market_trend_quality_frame,
    compute_market_turnover_weighted_momentum_frame,
    compute_market_win_rate_frame,
)


DEFAULT_EXCLUDE_MARKETS = [
    "KRW-BTC",
    "KRW-ETH",
    "KRW-XRP",
    "KRW-ADA",
    "KRW-DOGE",
    "KRW-SOL",
    "KRW-AVAX",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research cross-sectional turnover filters on Upbit 60m data."
    )
    parser.add_argument(
        "--candle-dir",
        default="data/upbit_research/minutes/60",
        help="Directory containing per-market 60m candle CSV files",
    )
    parser.add_argument(
        "--out-dir",
        default="data/research/turnover_cross_section_60m",
        help="Output directory for summaries",
    )
    parser.add_argument(
        "--turnover-hours",
        type=int,
        default=24,
        help="Window used to aggregate candle_acc_trade_price into turnover",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24 * 30,
        help="Lookback used for per-market time-series standardization",
    )
    parser.add_argument(
        "--turnover-factor-mode",
        choices=["ts_score", "raw_turnover"],
        default="ts_score",
        help="How turnover quantiles are formed: standardized turnover score or raw turnover cross-section",
    )
    parser.add_argument(
        "--forward-hours",
        type=int,
        default=24,
        help="Forward return horizon measured in 60m bars",
    )
    parser.add_argument(
        "--cross-buckets",
        type=int,
        default=5,
        help="Number of cross-sectional percentile buckets",
    )
    parser.add_argument(
        "--min-cross-section-size",
        type=int,
        default=20,
        help="Minimum active markets required at a timestamp",
    )
    parser.add_argument(
        "--quantile-factors",
        default="",
        help="Comma-separated factors used for quantile analysis. 1 factor => 1D summary, 2 => 2D matrix, 3+ => cell summary.",
    )
    parser.add_argument(
        "--single-factors",
        default="",
        help="Comma-separated factors to summarize independently, for example raw_turnover,z_turnover,momentum,median_momentum,win_rate,trend_quality,consistency_ratio. Empty means auto-select available factors.",
    )
    parser.add_argument(
        "--bucket-factor",
        default="turnover",
        help="Deprecated alias for single-factor summary selection",
    )
    parser.add_argument(
        "--matrix-row-factor",
        default=None,
        help="Factor used for 2D matrix rows; omitted means do not produce a 2D matrix",
    )
    parser.add_argument(
        "--matrix-col-factor",
        default=None,
        help="Factor used for 2D matrix columns; omitted means do not produce a 2D matrix",
    )
    parser.add_argument(
        "--ts-mode",
        choices=["percentile", "ratio", "zscore"],
        default="percentile",
        help="Per-market time-series standardization mode",
    )
    parser.add_argument(
        "--log1p-turnover",
        action="store_true",
        help="Apply log(1 + turnover_24h) before time-series standardization",
    )
    parser.add_argument(
        "--exclude-warnings",
        action="store_true",
        help="Drop markets with market_warning != NONE",
    )
    parser.add_argument(
        "--exclude-markets",
        default=",".join(DEFAULT_EXCLUDE_MARKETS),
        help="Comma-separated markets to exclude from the cross section",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Optional limit for smoke tests",
    )
    parser.add_argument(
        "--tail-hours",
        type=int,
        default=None,
        help="Optional limit to only keep the latest N 60m rows per market",
    )
    parser.add_argument(
        "--momentum-hours",
        type=int,
        default=None,
        help="Optional price momentum lookback in 60m bars",
    )
    parser.add_argument(
        "--momentum-mode",
        choices=["price", "turnover_weighted", "residual_btc"],
        default="price",
        help="Momentum definition: plain price return, turnover-weighted mean return, or BTC-residual momentum",
    )
    parser.add_argument(
        "--benchmark-market",
        default="KRW-BTC",
        help="Benchmark market used for beta/residual calculations",
    )
    parser.add_argument(
        "--beta-lookback-hours",
        type=int,
        default=48,
        help="Lookback used for rolling beta estimation when momentum-mode=residual_btc",
    )
    parser.add_argument(
        "--require-positive-momentum",
        action="store_true",
        help="If set, only keep observations with positive price momentum",
    )
    parser.add_argument(
        "--min-momentum-cross-percentile",
        type=float,
        default=None,
        help="Optional minimum cross-sectional momentum percentile in [0, 1], for example 0.8 for top 20 percent",
    )
    parser.add_argument(
        "--momentum-rank-change-hours",
        type=int,
        default=24,
        help="Lookback used for momentum cross-percentile change factor",
    )
    parser.add_argument(
        "--turnover-rank-change-hours",
        type=int,
        default=24,
        help="Lookback used for turnover cross-percentile change factor",
    )
    parser.add_argument(
        "--win-rate-hours",
        type=int,
        default=None,
        help="Optional lookback used for win-rate factor in 60m bars",
    )
    parser.add_argument(
        "--win-rate-threshold",
        type=float,
        default=0.0,
        help="Return threshold used for win-rate factor, for example 0.0 for positive bars",
    )
    parser.add_argument(
        "--trend-quality-hours",
        type=int,
        default=None,
        help="Optional lookback used for slope*R^2 trend-quality factor in 60m bars",
    )
    parser.add_argument(
        "--min-listing-hours",
        type=int,
        default=0,
        help="Minimum hours since first valid candle required for a market to be eligible at a timestamp",
    )
    parser.add_argument(
        "--consistency-ratio-hours",
        type=int,
        default=None,
        help="Optional lookback used for rolling median/abs(mean) return consistency factor in 60m bars",
    )
    return parser


def _build_score_series(
    turnover_series: pd.Series,
    lookback_hours: int,
    ts_mode: str,
    use_log1p: bool,
) -> pd.Series:
    base_series = np.log1p(turnover_series) if use_log1p else turnover_series
    if ts_mode == "percentile":
        return base_series.rolling(lookback_hours, min_periods=lookback_hours).rank(pct=True)
    if ts_mode == "ratio":
        baseline = base_series.rolling(lookback_hours, min_periods=lookback_hours).mean()
        return base_series / baseline
    baseline = base_series.rolling(lookback_hours, min_periods=lookback_hours).mean()
    dispersion = base_series.rolling(lookback_hours, min_periods=lookback_hours).std()
    return (base_series - baseline) / dispersion.replace(0.0, np.nan)


def _bucketize_cross_section(
    percentile_frame: pd.DataFrame,
    bucket_count: int,
) -> pd.DataFrame:
    clipped = percentile_frame.clip(lower=0.0, upper=1.0)
    buckets = np.ceil(clipped * bucket_count).astype("float64")
    buckets = buckets.where(clipped.notna(), np.nan)
    buckets = buckets.clip(lower=1, upper=bucket_count)
    return buckets


def _parse_factor_list(raw: str) -> list[str]:
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    unique_values: list[str] = []
    for value in values:
        if value not in unique_values:
            unique_values.append(value)
    return unique_values


def _normalize_factor_alias(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "turnover":
        return "turnover"
    return normalized


def _normalize_factor_list(values: list[str]) -> list[str]:
    normalized_values: list[str] = []
    for value in values:
        normalized = _normalize_factor_alias(value)
        if normalized not in normalized_values:
            normalized_values.append(normalized)
    return normalized_values


def _compute_rank_change_frame(
    cross_pct_frame: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    if periods <= 0:
        raise ValueError("periods must be positive for rank change")
    return cross_pct_frame - cross_pct_frame.shift(periods)


def _build_listing_age_eligibility_frame(
    price_frame: pd.DataFrame,
    min_listing_hours: int,
) -> pd.DataFrame:
    if min_listing_hours <= 0:
        return pd.DataFrame(True, index=price_frame.index, columns=price_frame.columns)

    eligibility: dict[str, pd.Series] = {}
    min_age = pd.Timedelta(hours=min_listing_hours)
    for market in price_frame.columns:
        series = price_frame[market].dropna()
        if series.empty:
            eligibility[market] = pd.Series(False, index=price_frame.index, dtype=bool)
            continue
        first_valid = series.index[0]
        eligible_index = price_frame.index >= (first_valid + min_age)
        eligibility[market] = pd.Series(eligible_index, index=price_frame.index, dtype=bool)
    return pd.DataFrame(eligibility, index=price_frame.index).sort_index(axis=1)


def _summarize_buckets(
    forward_frame: pd.DataFrame,
    cross_pct_frame: pd.DataFrame,
    bucket_frame: pd.DataFrame,
    min_cross_section_size: int,
    bucket_count: int,
    eligibility_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    summary_rows: list[dict[str, object]] = []
    eligible_forward = forward_frame if eligibility_frame is None else forward_frame.where(eligibility_frame)
    eligible_cross_pct = cross_pct_frame if eligibility_frame is None else cross_pct_frame.where(eligibility_frame)
    eligible_buckets = bucket_frame if eligibility_frame is None else bucket_frame.where(eligibility_frame)

    active_counts = eligible_forward.notna().sum(axis=1)
    valid_timestamps = active_counts >= min_cross_section_size
    filtered_forward = eligible_forward.loc[valid_timestamps]
    filtered_cross_pct = eligible_cross_pct.loc[valid_timestamps]
    filtered_buckets = eligible_buckets.loc[valid_timestamps]

    for bucket in range(1, bucket_count + 1):
        mask = filtered_buckets == bucket
        values = filtered_forward.where(mask).stack(future_stack=True).dropna().to_numpy(dtype=float)
        cross_values = filtered_cross_pct.where(mask).stack(future_stack=True).dropna().to_numpy(dtype=float)
        summary_rows.append(
            {
                "bucket": bucket,
                "bucket_label": f"Q{bucket}/{bucket_count}",
                "observations": int(values.size),
                "mean_forward_return_pct": float(values.mean() * 100.0) if values.size else math.nan,
                "median_forward_return_pct": float(np.median(values) * 100.0) if values.size else math.nan,
                "positive_ratio_pct": float((values > 0).mean() * 100.0) if values.size else math.nan,
                "mean_cross_percentile_pct": float(cross_values.mean() * 100.0) if cross_values.size else math.nan,
            }
        )
    return pd.DataFrame(summary_rows)


def _build_latest_snapshot(
    factor_value_frames: dict[str, pd.DataFrame],
    factor_cross_pct_frames: dict[str, pd.DataFrame],
    sort_factor: str,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if not factor_value_frames:
        return pd.DataFrame()

    anchor_factor = next(iter(factor_value_frames.keys()))
    anchor_frame = factor_value_frames[anchor_factor]
    for market in anchor_frame.columns:
        anchor_series = anchor_frame[market].dropna()
        if anchor_series.empty:
            continue
        latest_timestamp = anchor_series.index[-1]
        next_row = {
            "market": market,
            "date_utc": latest_timestamp.isoformat(),
        }
        for factor_name, factor_frame in factor_value_frames.items():
            if market in factor_frame.columns and latest_timestamp in factor_frame.index:
                value = factor_frame.at[latest_timestamp, market]
                next_row[factor_name] = float(value) if pd.notna(value) else math.nan
        for factor_name, factor_cross_pct_frame in factor_cross_pct_frames.items():
            if market in factor_cross_pct_frame.columns and latest_timestamp in factor_cross_pct_frame.index:
                value = factor_cross_pct_frame.at[latest_timestamp, market]
                next_row[f"{factor_name}_cross_percentile_pct"] = float(value * 100.0) if pd.notna(value) else math.nan
        rows.append(next_row)
    latest_frame = pd.DataFrame(rows)
    if latest_frame.empty:
        return latest_frame
    sort_column = f"{sort_factor}_cross_percentile_pct"
    if sort_column not in latest_frame.columns:
        sort_column = next(
            (f"{factor_name}_cross_percentile_pct" for factor_name in factor_cross_pct_frames if f"{factor_name}_cross_percentile_pct" in latest_frame.columns),
            sort_column,
        )
    secondary_column = next(
        (factor_name for factor_name in factor_value_frames if factor_name in latest_frame.columns),
        sort_column,
    )
    ascending_flags = [False, False] if secondary_column in latest_frame.columns else [False]
    sort_columns = [sort_column, secondary_column] if secondary_column in latest_frame.columns else [sort_column]
    return latest_frame.sort_values(sort_columns, ascending=ascending_flags).reset_index(drop=True)


def _summarize_bucket_matrix(
    forward_frame: pd.DataFrame,
    row_bucket_frame: pd.DataFrame,
    col_bucket_frame: pd.DataFrame,
    min_cross_section_size: int,
    bucket_count: int,
    row_factor_name: str,
    col_factor_name: str,
    eligibility_frame: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    eligible_forward = forward_frame if eligibility_frame is None else forward_frame.where(eligibility_frame)
    eligible_row = row_bucket_frame if eligibility_frame is None else row_bucket_frame.where(eligibility_frame)
    eligible_col = col_bucket_frame if eligibility_frame is None else col_bucket_frame.where(eligibility_frame)

    active_counts = eligible_forward.notna().sum(axis=1)
    valid_timestamps = active_counts >= min_cross_section_size
    filtered_forward = eligible_forward.loc[valid_timestamps]
    filtered_row = eligible_row.loc[valid_timestamps]
    filtered_col = eligible_col.loc[valid_timestamps]

    mean_matrix = pd.DataFrame(index=range(1, bucket_count + 1), columns=range(1, bucket_count + 1), dtype=float)
    median_matrix = pd.DataFrame(index=range(1, bucket_count + 1), columns=range(1, bucket_count + 1), dtype=float)
    positive_matrix = pd.DataFrame(index=range(1, bucket_count + 1), columns=range(1, bucket_count + 1), dtype=float)
    observation_matrix = pd.DataFrame(index=range(1, bucket_count + 1), columns=range(1, bucket_count + 1), dtype=float)

    for row_bucket in range(1, bucket_count + 1):
        for col_bucket in range(1, bucket_count + 1):
            mask = (filtered_row == row_bucket) & (filtered_col == col_bucket)
            values = filtered_forward.where(mask).stack(future_stack=True).dropna().to_numpy(dtype=float)
            if values.size == 0:
                mean_value = math.nan
                median_value = math.nan
                positive_value = math.nan
                observation_value = 0.0
            else:
                mean_value = float(values.mean() * 100.0)
                median_value = float(np.median(values) * 100.0)
                positive_value = float((values > 0).mean() * 100.0)
                observation_value = float(values.size)
            mean_matrix.at[row_bucket, col_bucket] = mean_value
            median_matrix.at[row_bucket, col_bucket] = median_value
            positive_matrix.at[row_bucket, col_bucket] = positive_value
            observation_matrix.at[row_bucket, col_bucket] = observation_value

    for frame in [mean_matrix, median_matrix, positive_matrix, observation_matrix]:
        frame.index = [f"{row_factor_name}_Q{idx}" for idx in frame.index]
        frame.columns = [f"{col_factor_name}_Q{idx}" for idx in frame.columns]

    return mean_matrix, median_matrix, positive_matrix, observation_matrix


def _summarize_quantile_cells(
    forward_frame: pd.DataFrame,
    bucket_frames: dict[str, pd.DataFrame],
    factors: list[str],
    min_cross_section_size: int,
    bucket_count: int,
    eligibility_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    eligible_forward = forward_frame if eligibility_frame is None else forward_frame.where(eligibility_frame)
    eligible_bucket_frames = {
        factor: (bucket_frames[factor] if eligibility_frame is None else bucket_frames[factor].where(eligibility_frame))
        for factor in factors
    }

    active_mask = eligible_forward.notna()
    for factor_frame in eligible_bucket_frames.values():
        active_mask &= factor_frame.notna()
    valid_timestamps = active_mask.sum(axis=1) >= min_cross_section_size

    filtered_forward = eligible_forward.loc[valid_timestamps]
    filtered_bucket_frames = {
        factor: factor_frame.loc[valid_timestamps]
        for factor, factor_frame in eligible_bucket_frames.items()
    }

    rows: list[dict[str, object]] = []
    for combo in itertools.product(range(1, bucket_count + 1), repeat=len(factors)):
        mask = pd.DataFrame(True, index=filtered_forward.index, columns=filtered_forward.columns)
        for factor, bucket in zip(factors, combo):
            mask &= filtered_bucket_frames[factor] == bucket
        values = filtered_forward.where(mask).stack(future_stack=True).dropna().to_numpy(dtype=float)
        row: dict[str, object] = {
            "cell_label": " x ".join(f"{factor}_Q{bucket}" for factor, bucket in zip(factors, combo)),
            "observations": int(values.size),
            "mean_forward_return_pct": float(values.mean() * 100.0) if values.size else math.nan,
            "median_forward_return_pct": float(np.median(values) * 100.0) if values.size else math.nan,
            "positive_ratio_pct": float((values > 0).mean() * 100.0) if values.size else math.nan,
        }
        for factor, bucket in zip(factors, combo):
            row[f"{factor}_bucket"] = bucket
        rows.append(row)

    columns = [f"{factor}_bucket" for factor in factors] + [
        "cell_label",
        "observations",
        "mean_forward_return_pct",
        "median_forward_return_pct",
        "positive_ratio_pct",
    ]
    return pd.DataFrame(rows)[columns]


def main() -> None:
    args = build_parser().parse_args()

    candle_dir = ROOT_DIR / args.candle_dir
    out_dir = ROOT_DIR / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    exclude_markets = {
        item.strip().upper()
        for item in args.exclude_markets.split(",")
        if item.strip()
    }

    raw_frames, meta_by_market = build_wide_frames_from_candle_dir(
        candle_dir,
        value_columns=["trade_price", "candle_acc_trade_price"],
        pattern="KRW-*.csv",
        max_markets=args.max_markets,
        tail_rows=args.tail_hours,
    )
    if raw_frames["trade_price"].empty:
        raise SystemExit(f"No candle files found in {candle_dir}")

    eligible_markets = [
        market
        for market in raw_frames["trade_price"].columns
        if market not in exclude_markets
        and (not args.exclude_warnings or meta_by_market[market]["market_warning"] == "NONE")
    ]
    if not eligible_markets:
        raise SystemExit("No eligible markets remained after filtering")

    price_frame = raw_frames["trade_price"][eligible_markets].sort_index().sort_index(axis=1)
    trade_value_frame = raw_frames["candle_acc_trade_price"][eligible_markets].sort_index().sort_index(axis=1)
    valid_rows = price_frame.notna().any(axis=1)
    price_frame = price_frame.loc[valid_rows]
    trade_value_frame = trade_value_frame.loc[valid_rows]
    benchmark_price = raw_frames["trade_price"].get(args.benchmark_market)
    if args.momentum_mode == "residual_btc":
        if benchmark_price is None:
            raise SystemExit(f"Benchmark market not found for residual momentum: {args.benchmark_market}")
        benchmark_price = benchmark_price.reindex(price_frame.index)

    turnover_frame = compute_market_rolling_sum_frame(trade_value_frame, args.turnover_hours)
    score_frame = apply_by_market_column(
        turnover_frame,
        lambda series: _build_score_series(
            turnover_series=series,
            lookback_hours=args.lookback_hours,
            ts_mode=args.ts_mode,
            use_log1p=args.log1p_turnover,
        ),
    )
    forward_frame = compute_market_forward_return_frame(price_frame, args.forward_hours)
    momentum_frame = None
    median_momentum_frame = None
    win_rate_frame = None
    trend_quality_frame = None
    consistency_ratio_frame = None
    if args.momentum_hours is not None:
        if args.momentum_mode == "price":
            momentum_frame = compute_market_momentum_frame(price_frame, args.momentum_hours)
        elif args.momentum_mode == "turnover_weighted":
            momentum_frame = compute_market_turnover_weighted_momentum_frame(
                price_frame,
                trade_value_frame,
                args.momentum_hours,
            )
        else:
            momentum_frame = compute_market_residual_momentum_frame(
                price_frame,
                benchmark_price,
                args.momentum_hours,
                args.beta_lookback_hours,
            )
        median_momentum_frame = compute_market_median_momentum_frame(price_frame, args.momentum_hours)
    if args.win_rate_hours is not None:
        win_rate_frame = compute_market_win_rate_frame(
            price_frame,
            args.win_rate_hours,
            threshold=args.win_rate_threshold,
        )
    if args.trend_quality_hours is not None:
        trend_quality_frame = compute_market_trend_quality_frame(
            price_frame,
            args.trend_quality_hours,
        )
    if args.consistency_ratio_hours is not None:
        consistency_ratio_frame = compute_market_consistency_ratio_frame(
            price_frame,
            args.consistency_ratio_hours,
        )
    momentum_cross_pct_frame = (
        momentum_frame.rank(axis=1, pct=True, method="average")
        if momentum_frame is not None
        else None
    )
    turnover_factor_frame = score_frame if args.turnover_factor_mode == "ts_score" else turnover_frame
    eligibility_frame: pd.DataFrame | None = None
    if args.require_positive_momentum:
        if momentum_frame is None:
            raise SystemExit("--require-positive-momentum requires --momentum-hours")
        eligibility_frame = momentum_frame > 0.0
    if args.min_momentum_cross_percentile is not None:
        if momentum_cross_pct_frame is None:
            raise SystemExit("--min-momentum-cross-percentile requires --momentum-hours")
        if not 0.0 <= args.min_momentum_cross_percentile <= 1.0:
            raise SystemExit("--min-momentum-cross-percentile must be in [0, 1]")
        cross_mask = momentum_cross_pct_frame >= args.min_momentum_cross_percentile
        eligibility_frame = cross_mask if eligibility_frame is None else (eligibility_frame & cross_mask)
    if args.min_listing_hours > 0:
        listing_age_mask = _build_listing_age_eligibility_frame(price_frame, args.min_listing_hours)
        eligibility_frame = listing_age_mask if eligibility_frame is None else (eligibility_frame & listing_age_mask)

    factor_value_frames: dict[str, pd.DataFrame] = {
        "raw_turnover": turnover_frame,
        "z_turnover": score_frame,
        "turnover": turnover_factor_frame,
    }
    if momentum_frame is not None:
        factor_value_frames["momentum"] = momentum_frame
    if median_momentum_frame is not None:
        factor_value_frames["median_momentum"] = median_momentum_frame
    if win_rate_frame is not None:
        factor_value_frames["win_rate"] = win_rate_frame
    if trend_quality_frame is not None:
        factor_value_frames["trend_quality"] = trend_quality_frame
    if consistency_ratio_frame is not None:
        factor_value_frames["consistency_ratio"] = consistency_ratio_frame

    factor_cross_pct_frames: dict[str, pd.DataFrame] = {
        factor_name: factor_frame.rank(axis=1, pct=True, method="average")
        for factor_name, factor_frame in factor_value_frames.items()
    }
    factor_value_frames["turnover_rank_change"] = _compute_rank_change_frame(
        factor_cross_pct_frames["turnover"],
        args.turnover_rank_change_hours,
    )
    factor_cross_pct_frames["turnover_rank_change"] = factor_value_frames["turnover_rank_change"].rank(
        axis=1,
        pct=True,
        method="average",
    )
    if momentum_cross_pct_frame is not None:
        factor_value_frames["momentum_rank_change"] = _compute_rank_change_frame(
            momentum_cross_pct_frame,
            args.momentum_rank_change_hours,
        )
        factor_cross_pct_frames["momentum_rank_change"] = factor_value_frames["momentum_rank_change"].rank(
            axis=1,
            pct=True,
            method="average",
        )
    factor_bucket_frames: dict[str, pd.DataFrame] = {}

    for factor_name, factor_cross_pct_frame in factor_cross_pct_frames.items():
        factor_bucket_frames[factor_name] = _bucketize_cross_section(
            factor_cross_pct_frame,
            bucket_count=args.cross_buckets,
        )

    requested_single_factors = _normalize_factor_list(_parse_factor_list(args.single_factors))
    if not requested_single_factors:
        requested_single_factors = list(factor_cross_pct_frames.keys())
    requested_quantile_factors = _normalize_factor_list(_parse_factor_list(args.quantile_factors))
    if requested_quantile_factors:
        invalid_quantile_factors = [factor for factor in requested_quantile_factors if factor not in factor_cross_pct_frames]
        if invalid_quantile_factors:
            raise SystemExit(f"Unsupported quantile factors requested: {', '.join(invalid_quantile_factors)}")
    invalid_single_factors = [factor for factor in requested_single_factors if factor not in factor_cross_pct_frames]
    if invalid_single_factors:
        raise SystemExit(f"Unsupported single-factor summaries requested: {', '.join(invalid_single_factors)}")
    if (args.matrix_row_factor is None) != (args.matrix_col_factor is None):
        raise SystemExit("--matrix-row-factor and --matrix-col-factor must be provided together")
    matrix_row_factor = _normalize_factor_alias(args.matrix_row_factor) if args.matrix_row_factor else None
    matrix_col_factor = _normalize_factor_alias(args.matrix_col_factor) if args.matrix_col_factor else None
    if matrix_row_factor is not None and matrix_row_factor not in factor_cross_pct_frames:
        raise SystemExit(f"Unsupported matrix row factor requested: {matrix_row_factor}")
    if matrix_col_factor is not None and matrix_col_factor not in factor_cross_pct_frames:
        raise SystemExit(f"Unsupported matrix col factor requested: {matrix_col_factor}")
    bucket_summaries: dict[str, pd.DataFrame] = {}
    for factor_name in requested_single_factors:
        bucket_summaries[factor_name] = _summarize_buckets(
            forward_frame=forward_frame,
            cross_pct_frame=factor_cross_pct_frames[factor_name],
            bucket_frame=factor_bucket_frames[factor_name],
            min_cross_section_size=args.min_cross_section_size,
            bucket_count=args.cross_buckets,
            eligibility_frame=eligibility_frame,
        )
    matrix_outputs: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame] | None = None
    quantile_cell_summary: pd.DataFrame | None = None
    quantile_mode_message: str | None = None
    if requested_quantile_factors:
        if len(requested_quantile_factors) == 1:
            bucket_summaries = {
                requested_quantile_factors[0]: _summarize_buckets(
                    forward_frame=forward_frame,
                    cross_pct_frame=factor_cross_pct_frames[requested_quantile_factors[0]],
                    bucket_frame=factor_bucket_frames[requested_quantile_factors[0]],
                    min_cross_section_size=args.min_cross_section_size,
                    bucket_count=args.cross_buckets,
                    eligibility_frame=eligibility_frame,
                )
            }
            quantile_mode_message = f"1D quantile summary for {requested_quantile_factors[0]}"
        elif len(requested_quantile_factors) == 2:
            matrix_outputs = _summarize_bucket_matrix(
                forward_frame=forward_frame,
                row_bucket_frame=factor_bucket_frames[requested_quantile_factors[0]],
                col_bucket_frame=factor_bucket_frames[requested_quantile_factors[1]],
                min_cross_section_size=args.min_cross_section_size,
                bucket_count=args.cross_buckets,
                row_factor_name=requested_quantile_factors[0],
                col_factor_name=requested_quantile_factors[1],
                eligibility_frame=eligibility_frame,
            )
            quantile_mode_message = (
                f"2D quantile matrix for {requested_quantile_factors[0]} x {requested_quantile_factors[1]}"
            )
            bucket_summaries = {}
        else:
            quantile_cell_summary = _summarize_quantile_cells(
                forward_frame=forward_frame,
                bucket_frames=factor_bucket_frames,
                factors=requested_quantile_factors,
                min_cross_section_size=args.min_cross_section_size,
                bucket_count=args.cross_buckets,
                eligibility_frame=eligibility_frame,
            )
            quantile_mode_message = f"{len(requested_quantile_factors)}D quantile cell summary"
            bucket_summaries = {}
    elif args.matrix_row_factor is not None and args.matrix_col_factor is not None:
        matrix_outputs = _summarize_bucket_matrix(
            forward_frame=forward_frame,
            row_bucket_frame=factor_bucket_frames[matrix_row_factor],
            col_bucket_frame=factor_bucket_frames[matrix_col_factor],
            min_cross_section_size=args.min_cross_section_size,
            bucket_count=args.cross_buckets,
            row_factor_name=matrix_row_factor,
            col_factor_name=matrix_col_factor,
            eligibility_frame=eligibility_frame,
        )
    latest_snapshot = _build_latest_snapshot(
        factor_value_frames=factor_value_frames,
        factor_cross_pct_frames=factor_cross_pct_frames,
        sort_factor=(requested_quantile_factors[0] if requested_quantile_factors else requested_single_factors[0]),
    )

    latest_path = out_dir / "latest_snapshot.csv"
    latest_snapshot.to_csv(latest_path, index=False, encoding="utf-8-sig")
    if requested_quantile_factors and quantile_cell_summary is not None:
        bucket_path = out_dir / "cell_summary.csv"
        quantile_cell_summary.to_csv(bucket_path, index=False, encoding="utf-8-sig")
    elif len(bucket_summaries) == 1:
        only_factor = next(iter(bucket_summaries))
        bucket_path = out_dir / "bucket_summary.csv"
        bucket_summaries[only_factor].to_csv(bucket_path, index=False, encoding="utf-8-sig")
    else:
        bucket_path = out_dir / "bucket_summary.csv"
        for factor_name, bucket_summary in bucket_summaries.items():
            bucket_summary.to_csv(out_dir / f"bucket_summary_{factor_name}.csv", index=False, encoding="utf-8-sig")
    if matrix_outputs is not None:
        mean_matrix, median_matrix, positive_matrix, observation_matrix = matrix_outputs
        mean_matrix.to_csv(out_dir / "matrix_mean_forward_return_pct.csv", encoding="utf-8-sig")
        median_matrix.to_csv(out_dir / "matrix_median_forward_return_pct.csv", encoding="utf-8-sig")
        positive_matrix.to_csv(out_dir / "matrix_positive_ratio_pct.csv", encoding="utf-8-sig")
        observation_matrix.to_csv(out_dir / "matrix_observations.csv", encoding="utf-8-sig")

    if requested_quantile_factors and quantile_cell_summary is not None:
        print(f"Wrote cell summary to {bucket_path}")
    elif requested_quantile_factors and len(requested_quantile_factors) == 2:
        pass
    elif len(bucket_summaries) == 1:
        print(f"Wrote bucket summary to {bucket_path}")
    else:
        print(f"Wrote single-factor bucket summaries to {out_dir}")
    print(f"Wrote latest snapshot to {latest_path}")
    if quantile_mode_message is not None:
        print(quantile_mode_message)
    if matrix_outputs is not None:
        print(f"Wrote 2D factor matrix to {out_dir}")
    for factor_name in bucket_summaries.keys():
        bucket_summary = bucket_summaries[factor_name]
        if bucket_summary.empty:
            continue
        print(f"\nBucket Summary: {factor_name}")
        print(bucket_summary.to_string(index=False))
    if quantile_cell_summary is not None:
        print("\nCell Summary")
        print(quantile_cell_summary.to_string(index=False))
    if matrix_outputs is not None:
        mean_matrix, _, _, observation_matrix = matrix_outputs
        print("\nMean Forward Return Matrix (%)")
        print(mean_matrix.to_string())
        print("\nObservation Matrix")
        print(observation_matrix.to_string())


if __name__ == "__main__":
    main()
