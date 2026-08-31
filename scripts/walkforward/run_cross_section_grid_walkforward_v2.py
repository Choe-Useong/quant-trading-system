#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.feature_graph_v2 import referenced_markets_for_feature_specs, required_source_columns_for_feature_specs
from lib.features_v2 import SUPPORTED_SOURCE_COLUMNS, build_feature_frames_from_cache
from lib.market_scores_v2 import build_market_score_frame, required_markets_for_market_score_spec
from lib.spec_io import (
    load_feature_specs_from_payload,
    load_market_score_spec,
    load_market_score_spec_from_payload,
    load_universe_spec_from_payload,
    load_weight_spec_from_payload,
)
from lib.universe_v2 import build_universe_mask_v2
from lib.vectorbt_adapter import VectorBTSpec, run_portfolio_from_target_weights
from lib.weights_v2 import build_weight_frame_v2, is_change_only_weight_output, to_change_only_target_frame
from scripts.run_cross_section_grid_v2 import (
    _grid_combinations,
    _passes_constraints,
    _read_warning_frame,
    _render_value,
    _resolve_prune_inactive_price_columns,
    _resolve_source_cache_dir,
)
from scripts.run_vectorbt import (
    benchmark_comparison_period_summary,
    benchmark_summary,
    build_benchmark_comparison_curves,
    compute_annualized_return,
    compute_benchmark_regression_stats,
    compute_cvar_sharpe_ratio,
    compute_drawdown_recovery_stats,
    compute_excess_curves,
    compute_information_ratio,
    compute_martin_ratio,
    compute_max_drawdown_pct,
    compute_recent_1y_stats,
    compute_recent_2y_stats,
    compute_return_series,
    compute_sharpe_ratio,
    compute_sortino_ratio,
    compute_ulcer_index_pct,
    infer_timeframe,
    load_price_frame,
    periods_per_day_for_timeframe,
    resolve_benchmark_curve,
    resolve_periods_per_year,
    strategy_performance_summary,
    timeframe_to_pandas_freq,
    trim_frames_to_first_weight,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run walk-forward validation for a v2 cross-sectional grid config.")
    parser.add_argument("--grid-config-json", required=True, help="v2 grid configuration JSON path")
    parser.add_argument("--out-dir", required=True, help="Directory to write fold and summary CSVs")
    parser.add_argument("--is-months", type=int, default=24, help="In-sample months")
    parser.add_argument("--oos-months", type=int, default=12, help="Out-of-sample months")
    parser.add_argument("--step-months", type=int, default=6, help="Step months")
    parser.add_argument(
        "--window-mode",
        choices=("rolling", "expanding"),
        default="rolling",
        help="Use a fixed rolling IS window or an expanding IS window anchored at the first timestamp",
    )
    parser.add_argument(
        "--ranking-metric",
        default="Annualized Information Ratio",
        help="IS metric used to choose each fold winner",
    )
    return parser


def _render_feature_payload(config: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    if config.get("shared_feature_spec_template") is not None:
        rendered_shared = _render_value(config["shared_feature_spec_template"], context)
        payload.extend(rendered_shared or [])
    if config.get("feature_spec_template") is not None:
        rendered_features = _render_value(config["feature_spec_template"], context)
        payload.extend(rendered_features or [])
    return payload


def _safe_float(value: Any) -> float:
    if value in ("", None):
        return float("nan")
    return float(value)


def _summarize_candidates(
    frame: pd.DataFrame,
    winners_frame: pd.DataFrame,
    ranking_metric: str,
) -> pd.DataFrame:
    summary_rows: list[dict[str, Any]] = []
    winner_counts_by_asset_candidate: dict[tuple[str, str], int] = defaultdict(int)
    winner_shares_by_asset_candidate: dict[tuple[str, str], float] = {}
    for asset, asset_winners in winners_frame.groupby("asset"):
        asset_fold_count = int(len(asset_winners))
        asset_counter = Counter(asset_winners["winner_label"])
        for candidate_label, count in asset_counter.items():
            key = (asset, candidate_label)
            winner_counts_by_asset_candidate[key] = count
            winner_shares_by_asset_candidate[key] = count / asset_fold_count if asset_fold_count else float("nan")
    for (asset, candidate_label), candidate_frame in frame.groupby(["asset", "candidate_label"]):
        oos_air = pd.to_numeric(candidate_frame["oos_Annualized Information Ratio"], errors="coerce")
        oos_cagr = pd.to_numeric(candidate_frame["oos_CAGR [%]"], errors="coerce")
        oos_mdd = pd.to_numeric(candidate_frame["oos_Max Drawdown [%]"], errors="coerce")
        oos_recovery = pd.to_numeric(candidate_frame["oos_Longest Peak-to-Recovery Bars"], errors="coerce")
        is_metric = pd.to_numeric(candidate_frame[f"is_{ranking_metric}"], errors="coerce")
        winner_key = (asset, candidate_label)
        summary_rows.append(
            {
                "asset": asset,
                "candidate_label": candidate_label,
                "fold_count": int(len(candidate_frame)),
                "winner_count": winner_counts_by_asset_candidate.get(winner_key, 0),
                "winner_share": winner_shares_by_asset_candidate.get(winner_key, 0.0),
                f"median_is_{ranking_metric}": is_metric.median(),
                f"mean_is_{ranking_metric}": is_metric.mean(),
                "median_oos_air": oos_air.median(),
                "mean_oos_air": oos_air.mean(),
                "worst_oos_air": oos_air.min(),
                "best_oos_air": oos_air.max(),
                "oos_air_positive_ratio": oos_air.gt(0).mean(),
                "median_oos_cagr_pct": oos_cagr.median(),
                "mean_oos_cagr_pct": oos_cagr.mean(),
                "best_oos_cagr_pct": oos_cagr.max(),
                "worst_oos_mdd_pct": oos_mdd.max(),
                "median_oos_longest_recovery_bars": oos_recovery.median(),
                "mean_oos_longest_recovery_bars": oos_recovery.mean(),
            }
        )
    return pd.DataFrame(summary_rows)


def _summarize_winners(frame: pd.DataFrame, ranking_metric: str) -> pd.DataFrame:
    summary_rows: list[dict[str, Any]] = []
    for asset, asset_frame in frame.groupby("asset"):
        winner_counter = Counter(asset_frame["winner_label"])
        oos_air = pd.to_numeric(asset_frame["oos_Annualized Information Ratio"], errors="coerce")
        oos_cagr = pd.to_numeric(asset_frame["oos_CAGR [%]"], errors="coerce")
        oos_mdd = pd.to_numeric(asset_frame["oos_Max Drawdown [%]"], errors="coerce")
        oos_recovery = pd.to_numeric(asset_frame["oos_Longest Peak-to-Recovery Bars"], errors="coerce")
        fold_count = int(len(asset_frame))
        top_winner_label, top_winner_count = winner_counter.most_common(1)[0]
        winner_shares = {label: count / fold_count for label, count in winner_counter.items()}
        summary_rows.append(
            {
                "asset": asset,
                "fold_count": fold_count,
                "median_oos_air": oos_air.median(),
                "mean_oos_air": oos_air.mean(),
                "worst_oos_air": oos_air.min(),
                "best_oos_air": oos_air.max(),
                "oos_air_positive_ratio": oos_air.gt(0).mean(),
                "median_oos_cagr_pct": oos_cagr.median(),
                "mean_oos_cagr_pct": oos_cagr.mean(),
                "best_oos_cagr_pct": oos_cagr.max(),
                "worst_oos_mdd_pct": oos_mdd.max(),
                "median_oos_longest_recovery_bars": oos_recovery.median(),
                "mean_oos_longest_recovery_bars": oos_recovery.mean(),
                "ranking_metric": ranking_metric,
                "top_winner_label": top_winner_label,
                "top_winner_count": top_winner_count,
                "top_winner_share": top_winner_count / fold_count,
                "winner_counts": json.dumps(dict(winner_counter), ensure_ascii=False),
                "winner_shares": json.dumps(winner_shares, ensure_ascii=False),
            }
        )
    return pd.DataFrame(summary_rows)


def _resolve_required_feature_markets(
    feature_specs,
    universe_payload: dict[str, Any],
    market_score_spec=None,
) -> list[str] | None:
    universe_spec = load_universe_spec_from_payload(universe_payload)
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


def _resolve_output_feature_markets(universe_payload: dict[str, Any]) -> list[str] | None:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    if not universe_spec.allowed_markets:
        return None
    return sorted(str(market).upper() for market in universe_spec.allowed_markets)


def _available_timestamps(price_frame: pd.DataFrame, market: str) -> pd.DatetimeIndex:
    if market not in price_frame.columns:
        return pd.DatetimeIndex([])
    series = pd.to_numeric(price_frame[market], errors="coerce").dropna()
    return pd.DatetimeIndex(series.index)


def _available_any_timestamps(price_frame: pd.DataFrame) -> pd.DatetimeIndex:
    if price_frame.empty:
        return pd.DatetimeIndex([])
    mask = price_frame.notna().any(axis=1)
    return pd.DatetimeIndex(price_frame.index[mask])


def _build_folds(
    timestamps: pd.DatetimeIndex,
    is_months: int,
    oos_months: int,
    step_months: int,
    window_mode: str,
) -> list[dict[str, pd.Timestamp]]:
    if timestamps.empty:
        return []
    folds: list[dict[str, pd.Timestamp]] = []
    initial_start = pd.Timestamp(timestamps.min())
    current_start = initial_start
    max_timestamp = pd.Timestamp(timestamps.max())
    while True:
        is_end = current_start + pd.DateOffset(months=is_months)
        oos_end = is_end + pd.DateOffset(months=oos_months)
        if oos_end > max_timestamp:
            break
        folds.append(
            {
                "is_start": initial_start if window_mode == "expanding" else current_start,
                "is_end": is_end,
                "oos_start": is_end,
                "oos_end": oos_end,
            }
        )
        current_start = current_start + pd.DateOffset(months=step_months)
    return folds


def _run_v2_backtest_window(
    candle_dir: Path,
    source_cache_dir: Path,
    feature_frames: dict[str, pd.DataFrame],
    warning_frame: pd.DataFrame,
    required_feature_markets: list[str] | None,
    universe_payload: dict[str, Any],
    weight_payload: dict[str, Any],
    vectorbt_payload: dict[str, Any],
    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame],
    max_markets: int | None,
    eval_start: pd.Timestamp,
    eval_end: pd.Timestamp,
    *,
    trim_start_mode: str | None = None,
    include_curves: bool = False,
) -> dict[str, Any]:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    weight_spec = load_weight_spec_from_payload(weight_payload)

    reference_index = next(iter(feature_frames.values())).index
    aligned_warning_frame = warning_frame.reindex(index=reference_index)
    universe_result = build_universe_mask_v2(feature_frames, aligned_warning_frame, universe_spec)
    weight_frame = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames)
    change_only_weights = is_change_only_weight_output(weight_spec)
    if change_only_weights:
        weight_frame = weight_frame.ffill().fillna(0.0)
    eval_weight_frame = weight_frame.loc[(weight_frame.index >= eval_start) & (weight_frame.index < eval_end)]
    if change_only_weights:
        eval_weight_frame = to_change_only_target_frame(eval_weight_frame)
    if eval_weight_frame.empty or not bool(eval_weight_frame.notna().any(axis=1).any()):
        return {"status": "empty_weights"}

    price_column = str(vectorbt_payload.get("price_column", "trade_price"))
    benchmark_price_column = str(vectorbt_payload.get("benchmark_price_column") or price_column)
    benchmark_source_cache_dir = Path(
        str(vectorbt_payload.get("benchmark_source_cache_dir") or source_cache_dir)
    )
    init_cash = float(vectorbt_payload.get("init_cash", 1_000_000.0))
    fees = float(vectorbt_payload.get("fees", 0.0))
    slippage = float(vectorbt_payload.get("slippage", 0.0))
    benchmark_market = str(vectorbt_payload.get("benchmark_market", "KRW-BTC")).upper()
    benchmark_mode = str(vectorbt_payload.get("benchmark_mode", "single_market"))
    benchmark_uses_strategy_price_frame = (
        benchmark_mode == "single_market"
        and benchmark_price_column == price_column
        and benchmark_source_cache_dir.resolve() == source_cache_dir.resolve()
    )
    prune_inactive_price_columns = _resolve_prune_inactive_price_columns(
        vectorbt_payload,
        has_explicit_universe_markets=bool(universe_spec.allowed_markets),
    )
    if prune_inactive_price_columns:
        active_columns = eval_weight_frame.fillna(0.0).abs().sum(axis=0).gt(0.0)
        required_price_market_set = {str(column).upper() for column in eval_weight_frame.columns[active_columns]}
    elif universe_spec.allowed_markets:
        required_price_market_set = {str(market).upper() for market in universe_spec.allowed_markets}
    else:
        required_price_market_set = set()
    if benchmark_uses_strategy_price_frame:
        required_price_market_set.add(benchmark_market)
    for market in (vectorbt_payload.get("benchmark_fixed_weights") or {}).keys():
        required_price_market_set.add(str(market).upper())
    required_price_markets = sorted(required_price_market_set) if required_price_market_set else None
    price_cache_key = (
        str(candle_dir),
        str(source_cache_dir),
        price_column,
        tuple(required_price_markets) if required_price_markets is not None else (),
        max_markets,
    )
    price_frame = price_frame_cache.get(price_cache_key)
    if price_frame is None:
        price_frame = load_price_frame(
            candle_dir,
            price_column,
            load_mode="wide",
            source_cache_dir=source_cache_dir,
            market_columns=required_price_markets,
        )
        if required_price_markets is None and max_markets is not None:
            price_frame = price_frame.reindex(columns=sorted(price_frame.columns)[:max_markets])
        price_frame_cache[price_cache_key] = price_frame
    benchmark_price_frame = None
    if benchmark_mode == "single_market" and not benchmark_uses_strategy_price_frame:
        benchmark_price_markets = [benchmark_market]
        benchmark_price_cache_key = (
            str(candle_dir),
            str(benchmark_source_cache_dir),
            benchmark_price_column,
            tuple(benchmark_price_markets),
            max_markets,
        )
        benchmark_price_frame = price_frame_cache.get(benchmark_price_cache_key)
        if benchmark_price_frame is None:
            benchmark_price_frame = load_price_frame(
                candle_dir,
                benchmark_price_column,
                load_mode="wide",
                source_cache_dir=benchmark_source_cache_dir,
                market_columns=benchmark_price_markets,
            )
            price_frame_cache[benchmark_price_cache_key] = benchmark_price_frame

    eval_price_frame = price_frame.loc[(price_frame.index >= eval_start) & (price_frame.index < eval_end)]
    eval_benchmark_price_frame = None
    if benchmark_price_frame is not None:
        eval_benchmark_price_frame = benchmark_price_frame.loc[
            (benchmark_price_frame.index >= eval_start) & (benchmark_price_frame.index < eval_end)
        ]
    if eval_price_frame.empty:
        return {"status": "empty_prices"}

    effective_trim_start_mode = (
        str(vectorbt_payload.get("trim_start_mode", "none"))
        if trim_start_mode is None
        else str(trim_start_mode)
    )
    trimmed_start_timestamp = None
    if effective_trim_start_mode == "timestamp":
        trim_start_timestamp_value = vectorbt_payload.get("trim_start_timestamp")
        if trim_start_timestamp_value is None:
            raise ValueError("trim_start_mode='timestamp' requires vectorbt_spec.trim_start_timestamp")
        configured_start = pd.Timestamp(str(trim_start_timestamp_value))
        effective_start = max(pd.Timestamp(eval_start), configured_start)
        eval_price_frame = eval_price_frame.loc[eval_price_frame.index >= effective_start]
        eval_weight_frame = eval_weight_frame.loc[eval_weight_frame.index >= effective_start]
        if eval_benchmark_price_frame is not None:
            eval_benchmark_price_frame = eval_benchmark_price_frame.loc[
                eval_benchmark_price_frame.index >= effective_start
            ]
        if change_only_weights:
            eval_weight_frame = weight_frame.loc[
                (weight_frame.index >= effective_start) & (weight_frame.index < eval_end)
            ]
            eval_weight_frame = to_change_only_target_frame(eval_weight_frame)
        trimmed_start_timestamp = effective_start
        if eval_price_frame.empty or eval_weight_frame.empty:
            return {"status": "empty_weights"}
    elif effective_trim_start_mode != "none":
        eval_price_frame, eval_weight_frame, trimmed_start_timestamp = trim_frames_to_first_weight(
            eval_price_frame,
            eval_weight_frame,
            mode=effective_trim_start_mode,
        )
        if change_only_weights:
            eval_weight_frame = to_change_only_target_frame(eval_weight_frame.ffill().fillna(0.0))
        if eval_price_frame.empty or eval_weight_frame.empty:
            return {"status": "empty_weights"}
    if eval_benchmark_price_frame is not None and not eval_price_frame.empty:
        eval_benchmark_price_frame = eval_benchmark_price_frame.loc[
            (eval_benchmark_price_frame.index >= eval_price_frame.index.min())
            & (eval_benchmark_price_frame.index <= eval_price_frame.index.max())
        ]

    timeframe = infer_timeframe(candle_dir, None)
    periods_per_year = resolve_periods_per_year(
        timeframe,
        vectorbt_payload.get("periods_per_year"),
    )
    periods_per_day = periods_per_day_for_timeframe(timeframe)
    pandas_freq = timeframe_to_pandas_freq(timeframe)

    portfolio = run_portfolio_from_target_weights(
        price_frame=eval_price_frame,
        target_weight_frame=eval_weight_frame,
        spec=VectorBTSpec(
            price_column=price_column,
            init_cash=init_cash,
            fees=fees,
            slippage=slippage,
            freq=pandas_freq,
        ),
    )

    summary = portfolio.stats(settings={"freq": pandas_freq})
    if "Benchmark Return [%]" in summary.index:
        summary = summary.rename(index={"Benchmark Return [%]": "VectorBT Benchmark Return [%]"})
    equity_curve = portfolio.value()
    benchmark_curve, benchmark_label = resolve_benchmark_curve(
        price_frame=eval_price_frame,
        portfolio=portfolio,
        init_cash=init_cash,
        benchmark_mode=benchmark_mode,
        benchmark_market=benchmark_market,
        benchmark_price_frame=eval_benchmark_price_frame,
        vectorbt_spec=VectorBTSpec(
            price_column=price_column,
            init_cash=init_cash,
            fees=fees,
            slippage=slippage,
            freq=pandas_freq,
        ),
        benchmark_fixed_weights=vectorbt_payload.get("benchmark_fixed_weights"),
        benchmark_rebalance_frequency=str(vectorbt_payload.get("benchmark_rebalance_frequency", "every_bar")),
        benchmark_listed_normalize=bool(vectorbt_payload.get("benchmark_listed_normalize", False)),
    )
    comparison_equity_curve, aligned_benchmark_curve = build_benchmark_comparison_curves(
        equity_curve,
        benchmark_curve,
        init_cash,
    )
    benchmark_stats = benchmark_summary(
        aligned_benchmark_curve,
        init_cash,
        benchmark_label,
        annualization_factor=periods_per_year,
        benchmark_mode=benchmark_mode,
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
    _, excess_equity_curve = compute_excess_curves(
        comparison_equity_curve,
        aligned_benchmark_curve,
        init_cash,
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
    strategy_stats = strategy_performance_summary(
        equity_curve,
        annualization_factor=periods_per_year,
    )
    for metric_name, metric_value in strategy_stats.items():
        summary.loc[metric_name] = metric_value
    summary.loc["Load Mode"] = "wide"
    summary.loc["Weights CSV Format"] = "wide"
    summary.loc["Trim Start Mode"] = effective_trim_start_mode
    if trimmed_start_timestamp is not None:
        summary.loc["Trimmed Start Timestamp"] = trimmed_start_timestamp.isoformat()
    summary.loc["Timeframe"] = timeframe
    summary.loc["Periods Per Year"] = periods_per_year
    summary.loc["Benchmark Price Column"] = benchmark_price_column
    summary.loc["Benchmark Source Cache Dir"] = str(benchmark_source_cache_dir)
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
        ]
    )
    result = {"status": "ok"}
    result.update(summary.to_dict())
    if include_curves:
        result["_equity_curve"] = equity_curve
        result["_benchmark_curve"] = aligned_benchmark_curve
    return result


def _winner_deployment_segments(fold_winner_frame: pd.DataFrame) -> list[dict[str, Any]]:
    if fold_winner_frame.empty:
        return []
    winners = fold_winner_frame.copy()
    winners["oos_start_ts"] = pd.to_datetime(winners["oos_start"], utc=False)
    winners["oos_end_ts"] = pd.to_datetime(winners["oos_end"], utc=False)
    winners = winners.sort_values("oos_start_ts").reset_index(drop=True)

    segments: list[dict[str, Any]] = []
    for idx, row in winners.iterrows():
        deploy_start = pd.Timestamp(row["oos_start_ts"])
        deploy_end = pd.Timestamp(row["oos_end_ts"])
        if idx + 1 < len(winners):
            next_start = pd.Timestamp(winners.loc[idx + 1, "oos_start_ts"])
            if next_start < deploy_end:
                deploy_end = next_start
        if deploy_end <= deploy_start:
            continue
        segment = row.drop(labels=["oos_start_ts", "oos_end_ts"]).to_dict()
        segment["deploy_start"] = deploy_start
        segment["deploy_end"] = deploy_end
        segments.append(segment)
    return segments


def _stitch_curves(
    curves: list[pd.Series],
    *,
    init_cash: float,
    name: str,
) -> pd.Series:
    if not curves:
        return pd.Series(dtype=float, name=name)
    stitched_returns: list[pd.Series] = []
    for curve in curves:
        returns = compute_return_series(curve)
        stitched_returns.append(returns)
    stitched_return_series = pd.concat(stitched_returns).sort_index()
    stitched_curve = ((1.0 + stitched_return_series).cumprod()) * init_cash
    stitched_curve.name = name
    return stitched_curve


def _build_stitched_summary(
    equity_curve: pd.Series,
    benchmark_curve: pd.Series,
    *,
    init_cash: float,
    benchmark_label: str,
    benchmark_mode: str,
    periods_per_year: int,
    segment_count: int,
) -> pd.Series:
    strategy_returns = compute_return_series(equity_curve)
    comparison_equity_curve, aligned_benchmark_curve = build_benchmark_comparison_curves(
        equity_curve,
        benchmark_curve,
        init_cash,
    )
    comparison_strategy_returns = compute_return_series(comparison_equity_curve)
    benchmark_returns = compute_return_series(aligned_benchmark_curve)
    summary = pd.Series(
        {
            "Start Value": float(equity_curve.iloc[0]),
            "End Value": float(equity_curve.iloc[-1]),
            "Total Return [%]": ((float(equity_curve.iloc[-1]) / float(equity_curve.iloc[0])) - 1.0) * 100.0,
            "CAGR [%]": compute_annualized_return(equity_curve, annualization_factor=periods_per_year) * 100.0,
            "Max Drawdown [%]": compute_max_drawdown_pct(equity_curve),
            "Sharpe Ratio": compute_sharpe_ratio(strategy_returns, annualization_factor=periods_per_year),
            "CVaR 5% Sharpe": compute_cvar_sharpe_ratio(
                strategy_returns,
                annualization_factor=periods_per_year,
                alpha=0.05,
            ),
            "Ulcer Index [%]": compute_ulcer_index_pct(equity_curve),
            "Martin Ratio": compute_martin_ratio(
                equity_curve,
                annualization_factor=periods_per_year,
            ),
            "Sortino Ratio": compute_sortino_ratio(strategy_returns, annualization_factor=periods_per_year),
            "Deployment Segment Count": float(segment_count),
        }
    )
    max_drawdown_pct = float(summary["Max Drawdown [%]"])
    cagr = float(summary["CAGR [%]"]) / 100.0
    summary.loc["Calmar Ratio"] = float("nan") if max_drawdown_pct == 0.0 else cagr / (abs(max_drawdown_pct) / 100.0)
    summary = pd.concat(
        [
            summary,
            benchmark_comparison_period_summary(
                comparison_equity_curve,
                annualization_factor=periods_per_year,
            ),
            benchmark_summary(
                aligned_benchmark_curve,
                init_cash,
                benchmark_label,
                annualization_factor=periods_per_year,
                benchmark_mode=benchmark_mode,
            ),
            compute_information_ratio(
                comparison_strategy_returns,
                benchmark_returns,
                annualization_factor=periods_per_year,
            ),
            compute_benchmark_regression_stats(
                comparison_strategy_returns,
                benchmark_returns,
                annualization_factor=periods_per_year,
            ),
            compute_recent_1y_stats(
                comparison_equity_curve,
                aligned_benchmark_curve,
                annualization_factor=periods_per_year,
            ),
            compute_recent_2y_stats(
                comparison_equity_curve,
                aligned_benchmark_curve,
                annualization_factor=periods_per_year,
            ),
            compute_drawdown_recovery_stats(equity_curve),
        ]
    )
    return summary


def main() -> None:
    args = build_parser().parse_args()
    config_path = Path(args.grid_config_json)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    candle_dir_value = config.get("candle_dir") or config.get("daily_dir")
    if not candle_dir_value:
        raise SystemExit("config must define candle_dir or daily_dir")
    candle_dir = Path(str(candle_dir_value))
    source_cache_dir = _resolve_source_cache_dir(config, candle_dir, Path(config.get("out_dir", out_dir)))
    market_score_spec_path = config.get("market_scores_spec_json")
    static_market_score_spec = (
        load_market_score_spec(Path(str(market_score_spec_path))) if market_score_spec_path else None
    )

    run_name_template = config["run_name_template"]
    max_markets = config.get("max_markets")
    tail_hours = config.get("tail_hours")
    ranking_metric = args.ranking_metric

    raw_combinations = _grid_combinations(config.get("grid", {}))
    combinations = [combo for combo in raw_combinations if _passes_constraints(combo, config.get("constraints", []))]
    if not combinations:
        raise SystemExit("No valid grid combinations after applying constraints")
    combo_keys = sorted({key for combo in combinations for key in combo.keys()})

    first_context = dict(combinations[0])
    first_context["run_name"] = run_name_template.format(**first_context)
    first_vectorbt_payload = _render_value(config.get("vectorbt_spec_template", {}), first_context)
    first_universe_payload = _render_value(config["universe_spec_template"], first_context)
    primary_market = None
    allowed_markets = first_universe_payload.get("allowed_markets", [])
    if allowed_markets:
        primary_market = str(allowed_markets[0]).upper()
    else:
        primary_market = str(first_vectorbt_payload.get("benchmark_market", "KRW-BTC")).upper()

    timestamp_price_frame = load_price_frame(
        candle_dir,
        str(first_vectorbt_payload.get("price_column", "trade_price")),
        load_mode="wide",
        source_cache_dir=source_cache_dir,
        market_columns=[primary_market],
    )
    timestamps = _available_timestamps(timestamp_price_frame, primary_market)
    if timestamps.empty:
        timestamp_price_frame = load_price_frame(
            candle_dir,
            str(first_vectorbt_payload.get("price_column", "trade_price")),
            load_mode="wide",
            source_cache_dir=source_cache_dir,
            market_columns=None,
        )
        if max_markets is not None:
            timestamp_price_frame = timestamp_price_frame.reindex(columns=sorted(timestamp_price_frame.columns)[:max_markets])
        timestamps = _available_any_timestamps(timestamp_price_frame)
    folds = _build_folds(
        timestamps,
        is_months=args.is_months,
        oos_months=args.oos_months,
        step_months=args.step_months,
        window_mode=args.window_mode,
    )
    if not folds:
        raise SystemExit("No valid folds for requested walk-forward window")

    feature_cache: dict[tuple[str, tuple[str, ...], str], dict[str, pd.DataFrame]] = {}
    source_frame_cache: dict[tuple[tuple[str, ...], tuple[str, ...], int | None], dict[str, pd.DataFrame]] = {}
    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame] = {}
    warning_frame_cache: dict[tuple[tuple[str, ...], int | None], pd.DataFrame] = {}

    fold_candidate_rows: list[dict[str, Any]] = []
    fold_winner_rows: list[dict[str, Any]] = []

    for fold_idx, fold in enumerate(folds, start=1):
        fold_label = f"fold_{fold_idx:02d}"
        print(f"[{fold_idx}/{len(folds)}] {fold_label}")
        candidate_rows_for_fold: list[dict[str, Any]] = []

        for combo in combinations:
            context = dict(combo)
            run_name = run_name_template.format(**context)
            context["run_name"] = run_name
            universe_payload = _render_value(config["universe_spec_template"], context)
            weight_payload = _render_value(config["weight_spec_template"], context)
            vectorbt_payload = _render_value(config.get("vectorbt_spec_template", {}), context)
            market_score_payload = (
                _render_value(config["market_scores_spec_template"], context)
                if config.get("market_scores_spec_template") is not None
                else None
            )
            weight_market_score_payload = (
                _render_value(config["weight_market_scores_spec_template"], context)
                if config.get("weight_market_scores_spec_template") is not None
                else None
            )
            market_score_spec = (
                load_market_score_spec_from_payload(market_score_payload)
                if market_score_payload is not None
                else static_market_score_spec
            )
            weight_market_score_spec = (
                load_market_score_spec_from_payload(weight_market_score_payload)
                if weight_market_score_payload is not None
                else None
            )
            feature_payload = _render_feature_payload(config, context)
            feature_specs = load_feature_specs_from_payload(feature_payload)
            required_feature_markets = _resolve_required_feature_markets(
                feature_specs,
                universe_payload,
                market_score_spec,
            )
            output_feature_markets = _resolve_output_feature_markets(universe_payload)
            feature_key = (
                json.dumps(feature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                tuple(required_feature_markets) if required_feature_markets is not None else (),
                tuple(output_feature_markets) if output_feature_markets is not None else (),
                json.dumps(market_score_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if market_score_payload is not None
                else (str(Path(str(market_score_spec_path)).resolve()) if market_score_spec_path else ""),
                json.dumps(weight_market_score_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if weight_market_score_payload is not None
                else "",
            )
            feature_frames = feature_cache.get(feature_key)
            if feature_frames is None:
                required_columns, uses_market_source = required_source_columns_for_feature_specs(
                    feature_specs,
                    SUPPORTED_SOURCE_COLUMNS,
                )
                source_cache_key = (
                    tuple(sorted(required_columns or {"trade_price"})),
                    tuple(required_feature_markets) if required_feature_markets is not None else (),
                    None if (uses_market_source or required_feature_markets is not None) else max_markets,
                )
                source_frames = source_frame_cache.get(source_cache_key)
                if source_frames is None:
                    from lib.dataframes import read_wide_frames_from_cache

                    source_frames = read_wide_frames_from_cache(
                        source_cache_dir,
                        list(source_cache_key[0]),
                        market_columns=required_feature_markets,
                        max_markets=source_cache_key[2],
                    )
                    source_frame_cache[source_cache_key] = source_frames
                feature_frames = build_feature_frames_from_cache(
                    source_cache_dir,
                    feature_specs,
                    market_columns=required_feature_markets,
                    output_market_columns=output_feature_markets,
                    max_markets=None if required_feature_markets is not None else max_markets,
                    tail_rows=tail_hours,
                    source_frames=source_frames,
                )
                if market_score_spec is not None:
                    feature_frames = dict(feature_frames)
                    feature_frames[market_score_spec.output_column] = build_market_score_frame(feature_frames, market_score_spec)
                if weight_market_score_spec is not None:
                    feature_frames = dict(feature_frames)
                    feature_frames[weight_market_score_spec.output_column] = build_market_score_frame(
                        feature_frames,
                        weight_market_score_spec,
                    )
                feature_cache[feature_key] = feature_frames

            warning_cache_key = (
                tuple(required_feature_markets) if required_feature_markets is not None else (),
                None if required_feature_markets is not None else max_markets,
                bool(load_universe_spec_from_payload(universe_payload).exclude_warnings),
            )
            warning_frame = warning_frame_cache.get(warning_cache_key)
            if warning_frame is None:
                warning_frame = _read_warning_frame(
                    source_cache_dir,
                    market_columns=required_feature_markets,
                    max_markets=None if required_feature_markets is not None else max_markets,
                    require_warning=warning_cache_key[2],
                )
                warning_frame_cache[warning_cache_key] = warning_frame

            is_result = _run_v2_backtest_window(
                candle_dir,
                source_cache_dir,
                feature_frames,
                warning_frame,
                required_feature_markets,
                universe_payload,
                weight_payload,
                vectorbt_payload,
                price_frame_cache,
                max_markets,
                eval_start=fold["is_start"],
                eval_end=fold["is_end"],
            )
            oos_result = _run_v2_backtest_window(
                candle_dir,
                source_cache_dir,
                feature_frames,
                warning_frame,
                required_feature_markets,
                universe_payload,
                weight_payload,
                vectorbt_payload,
                price_frame_cache,
                max_markets,
                eval_start=fold["oos_start"],
                eval_end=fold["oos_end"],
            )

            row = {
                "asset": primary_market,
                "fold": fold_label,
                "candidate_label": run_name,
                "is_start": fold["is_start"].isoformat(),
                "is_end": fold["is_end"].isoformat(),
                "oos_start": fold["oos_start"].isoformat(),
                "oos_end": fold["oos_end"].isoformat(),
                **combo,
            }
            for prefix, result in (("is", is_result), ("oos", oos_result)):
                row[f"{prefix}_status"] = result.get("status", "")
                for key, value in result.items():
                    if key == "status":
                        continue
                    row[f"{prefix}_{key}"] = value
            fold_candidate_rows.append(row)
            candidate_rows_for_fold.append(row)

        ok_is_rows = [
            row
            for row in candidate_rows_for_fold
            if row.get("is_status") == "ok" and pd.notna(_safe_float(row.get(f"is_{ranking_metric}")))
        ]
        if not ok_is_rows:
            continue
        winner = max(ok_is_rows, key=lambda row: _safe_float(row.get(f"is_{ranking_metric}")))
        fold_winner_rows.append(
            {
                "asset": primary_market,
                "fold": fold_label,
                "winner_label": winner["candidate_label"],
                "is_start": winner["is_start"],
                "is_end": winner["is_end"],
                "oos_start": winner["oos_start"],
                "oos_end": winner["oos_end"],
                f"is_{ranking_metric}": winner.get(f"is_{ranking_metric}"),
                "is_CAGR [%]": winner.get("is_CAGR [%]"),
                "is_Max Drawdown [%]": winner.get("is_Max Drawdown [%]"),
                "oos_Annualized Information Ratio": winner.get("oos_Annualized Information Ratio"),
                "oos_CAGR [%]": winner.get("oos_CAGR [%]"),
                "oos_Max Drawdown [%]": winner.get("oos_Max Drawdown [%]"),
                "oos_Longest Peak-to-Recovery Bars": winner.get("oos_Longest Peak-to-Recovery Bars"),
                **{key: winner.get(key) for key in combo_keys},
            }
        )

    fold_candidate_frame = pd.DataFrame(fold_candidate_rows)
    fold_winner_frame = pd.DataFrame(fold_winner_rows)
    fold_candidate_frame.to_csv(out_dir / "fold_candidate_results.csv", index=False, encoding="utf-8-sig")
    fold_winner_frame.to_csv(out_dir / "fold_winners.csv", index=False, encoding="utf-8-sig")
    _summarize_candidates(fold_candidate_frame, fold_winner_frame, ranking_metric).to_csv(
        out_dir / "candidate_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    _summarize_winners(fold_winner_frame, ranking_metric).to_csv(
        out_dir / "walkforward_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    if not fold_winner_frame.empty:
        stitched_equity_segments: list[pd.Series] = []
        stitched_benchmark_segments: list[pd.Series] = []
        segments = _winner_deployment_segments(fold_winner_frame)
        for segment in segments:
            combo = {key: segment.get(key) for key in combo_keys if key in segment}
            context = dict(combo)
            run_name = run_name_template.format(**context)
            context["run_name"] = run_name
            universe_payload = _render_value(config["universe_spec_template"], context)
            weight_payload = _render_value(config["weight_spec_template"], context)
            vectorbt_payload = _render_value(config.get("vectorbt_spec_template", {}), context)
            market_score_payload = (
                _render_value(config["market_scores_spec_template"], context)
                if config.get("market_scores_spec_template") is not None
                else None
            )
            weight_market_score_payload = (
                _render_value(config["weight_market_scores_spec_template"], context)
                if config.get("weight_market_scores_spec_template") is not None
                else None
            )
            market_score_spec = (
                load_market_score_spec_from_payload(market_score_payload)
                if market_score_payload is not None
                else static_market_score_spec
            )
            weight_market_score_spec = (
                load_market_score_spec_from_payload(weight_market_score_payload)
                if weight_market_score_payload is not None
                else None
            )
            feature_payload = _render_feature_payload(config, context)
            feature_specs = load_feature_specs_from_payload(feature_payload)
            required_feature_markets = _resolve_required_feature_markets(
                feature_specs,
                universe_payload,
                market_score_spec,
            )
            output_feature_markets = _resolve_output_feature_markets(universe_payload)
            feature_key = (
                json.dumps(feature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                tuple(required_feature_markets) if required_feature_markets is not None else (),
                tuple(output_feature_markets) if output_feature_markets is not None else (),
                json.dumps(market_score_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if market_score_payload is not None
                else (str(Path(str(market_score_spec_path)).resolve()) if market_score_spec_path else ""),
                json.dumps(weight_market_score_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if weight_market_score_payload is not None
                else "",
            )
            feature_frames = feature_cache.get(feature_key)
            if feature_frames is None:
                required_columns, uses_market_source = required_source_columns_for_feature_specs(
                    feature_specs,
                    SUPPORTED_SOURCE_COLUMNS,
                )
                source_cache_key = (
                    tuple(sorted(required_columns or {"trade_price"})),
                    tuple(required_feature_markets) if required_feature_markets is not None else (),
                    None if (uses_market_source or required_feature_markets is not None) else max_markets,
                )
                source_frames = source_frame_cache.get(source_cache_key)
                if source_frames is None:
                    from lib.dataframes import read_wide_frames_from_cache

                    source_frames = read_wide_frames_from_cache(
                        source_cache_dir,
                        list(source_cache_key[0]),
                        market_columns=required_feature_markets,
                        max_markets=source_cache_key[2],
                    )
                    source_frame_cache[source_cache_key] = source_frames
                feature_frames = build_feature_frames_from_cache(
                    source_cache_dir,
                    feature_specs,
                    market_columns=required_feature_markets,
                    output_market_columns=output_feature_markets,
                    max_markets=None if required_feature_markets is not None else max_markets,
                    tail_rows=tail_hours,
                    source_frames=source_frames,
                )
                if market_score_spec is not None:
                    feature_frames = dict(feature_frames)
                    feature_frames[market_score_spec.output_column] = build_market_score_frame(
                        feature_frames,
                        market_score_spec,
                    )
                if weight_market_score_spec is not None:
                    feature_frames = dict(feature_frames)
                    feature_frames[weight_market_score_spec.output_column] = build_market_score_frame(
                        feature_frames,
                        weight_market_score_spec,
                    )
                feature_cache[feature_key] = feature_frames

            warning_cache_key = (
                tuple(required_feature_markets) if required_feature_markets is not None else (),
                None if required_feature_markets is not None else max_markets,
                bool(load_universe_spec_from_payload(universe_payload).exclude_warnings),
            )
            warning_frame = warning_frame_cache.get(warning_cache_key)
            if warning_frame is None:
                warning_frame = _read_warning_frame(
                    source_cache_dir,
                    market_columns=required_feature_markets,
                    max_markets=None if required_feature_markets is not None else max_markets,
                    require_warning=warning_cache_key[2],
                )
                warning_frame_cache[warning_cache_key] = warning_frame

            segment_result = _run_v2_backtest_window(
                candle_dir,
                source_cache_dir,
                feature_frames,
                warning_frame,
                required_feature_markets,
                universe_payload,
                weight_payload,
                vectorbt_payload,
                price_frame_cache,
                max_markets,
                eval_start=pd.Timestamp(segment["deploy_start"]),
                eval_end=pd.Timestamp(segment["deploy_end"]),
                trim_start_mode="none",
                include_curves=True,
            )
            if segment_result.get("status") != "ok":
                continue
            stitched_equity_segments.append(segment_result["_equity_curve"])
            stitched_benchmark_segments.append(segment_result["_benchmark_curve"])

        if stitched_equity_segments and stitched_benchmark_segments:
            init_cash = float(first_vectorbt_payload.get("init_cash", 1_000_000.0))
            benchmark_market = str(first_vectorbt_payload.get("benchmark_market", "KRW-BTC")).upper()
            benchmark_mode = str(first_vectorbt_payload.get("benchmark_mode", "single_market"))
            benchmark_label = {
                "portfolio_group": "PORTFOLIO_GROUP",
                "portfolio_group_rebalance": "PORTFOLIO_GROUP_REBALANCE",
            }.get(benchmark_mode, benchmark_market)
            periods_per_year = resolve_periods_per_year(
                infer_timeframe(candle_dir, None),
                first_vectorbt_payload.get("periods_per_year"),
            )
            stitched_equity_curve = _stitch_curves(
                stitched_equity_segments,
                init_cash=init_cash,
                name="stitched_oos_equity",
            )
            stitched_benchmark_curve = _stitch_curves(
                stitched_benchmark_segments,
                init_cash=init_cash,
                name="stitched_oos_benchmark",
            )
            stitched_summary = _build_stitched_summary(
                stitched_equity_curve,
                stitched_benchmark_curve,
                init_cash=init_cash,
                benchmark_label=benchmark_label,
                benchmark_mode=benchmark_mode,
                periods_per_year=periods_per_year,
                segment_count=len(stitched_equity_segments),
            )
            stitched_equity_curve.to_frame(name="value").to_csv(
                out_dir / "stitched_oos_equity_curve.csv",
                encoding="utf-8-sig",
            )
            stitched_benchmark_curve.to_frame(name="value").to_csv(
                out_dir / "stitched_oos_benchmark_curve.csv",
                encoding="utf-8-sig",
            )
            stitched_summary.to_frame(name="value").to_csv(
                out_dir / "stitched_oos_summary.csv",
                encoding="utf-8-sig",
            )
    print(f"Wrote walk-forward results to {out_dir}")


if __name__ == "__main__":
    main()
