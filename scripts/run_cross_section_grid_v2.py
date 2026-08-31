#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import build_long_frame_from_candle_dir, build_wide_frames_from_candle_dir, read_wide_frames_from_cache
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


DEFAULT_CACHE_VALUE_COLUMNS = (
    "trade_price",
    "opening_price",
    "high_price",
    "low_price",
    "candle_acc_trade_volume",
    "candle_acc_trade_price",
    "timestamp",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run frame-native v2 cross-sectional grid from config JSON."
    )
    parser.add_argument("--config-json", required=True, help="Cross-sectional grid configuration JSON path")
    return parser


def _render_value(template: Any, context: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {key: _render_value(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_value(value, context) for value in template]
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template.count("{") == 1 and template.count("}") == 1:
            key = template[1:-1]
            if key in context:
                return context[key]
        return template.format(**context)
    return template


def _grid_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = list(grid.keys())
    values = [grid[key] for key in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _passes_constraints(combo: dict[str, Any], constraints: list[str]) -> bool:
    if not constraints:
        return True
    safe_globals = {"__builtins__": {}}
    safe_locals = dict(combo)
    for constraint in constraints:
        if not bool(eval(constraint, safe_globals, safe_locals)):
            return False
    return True


def _canonical_payload_key(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dynamic_universe_usage_key(
    *,
    feature_payload: Any,
    universe_payload: dict[str, Any],
    market_score_payload: Any,
    market_score_spec_path: object,
) -> str:
    return _canonical_payload_key(
        {
            "feature_payload": feature_payload,
            "universe_payload": _universe_payload_for_cache(universe_payload),
            "market_score_payload": market_score_payload,
            "market_score_spec_path": (
                str(Path(str(market_score_spec_path)).resolve())
                if market_score_spec_path
                else ""
            ),
        }
    )


def _dynamic_rank_usage_key(
    *,
    feature_payload: Any,
    market_score_payload: Any,
    market_score_spec_path: object,
) -> str:
    return _canonical_payload_key(
        {
            "feature_payload": feature_payload,
            "market_score_payload": market_score_payload,
            "market_score_spec_path": (
                str(Path(str(market_score_spec_path)).resolve())
                if market_score_spec_path
                else ""
            ),
        }
    )


def _drop_node_cache_by_namespace_prefix(
    cache: dict[tuple[Any, ...], pd.DataFrame],
    prefix: tuple[Any, ...],
) -> int:
    keys = [
        key
        for key in cache
        if isinstance(key, tuple)
        and key
        and isinstance(key[0], tuple)
        and key[0][: len(prefix)] == prefix
    ]
    for key in keys:
        cache.pop(key, None)
    return len(keys)


def _drop_rank_cache_by_namespace(
    cache: dict[tuple[object, ...], pd.DataFrame],
    namespace: tuple[object, ...],
) -> int:
    keys = [
        key
        for key in cache
        if isinstance(key, tuple) and key[: len(namespace)] == namespace
    ]
    for key in keys:
        cache.pop(key, None)
    return len(keys)


def _universe_payload_for_cache(payload: dict[str, Any]) -> dict[str, Any]:
    cache_payload = copy.deepcopy(payload)
    cache_payload.pop("name", None)
    return cache_payload


def _universe_referenced_feature_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def _add(value: Any) -> None:
        if value is not None and str(value):
            names.add(str(value))

    _add(payload.get("feature_column"))
    _add(payload.get("sort_column"))
    if payload.get("min_age_days") is not None:
        _add("trade_price")
    for item in payload.get("value_filters", []) or []:
        _add(item.get("feature_column"))
    for item in payload.get("rank_filters", []) or []:
        _add(item.get("feature_column"))
    for stage in payload.get("filter_stages", []) or []:
        for item in stage.get("filters", []) or []:
            _add(item.get("feature_column"))
    return names


def _feature_identity_map_from_payloads(payloads: list[Any]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for payload in payloads:
        if not payload:
            continue
        specs = load_feature_specs_from_payload(payload)
        for item, spec in zip(payload, specs):
            identities[spec.resolved_column_name()] = _canonical_payload_key(item)
    return identities


def _feature_outputs_only(
    frames: dict[str, pd.DataFrame],
    feature_specs,
) -> dict[str, pd.DataFrame]:
    return {
        spec.resolved_column_name(): frames[spec.resolved_column_name()]
        for spec in feature_specs
    }


def _resolve_prune_inactive_price_columns(
    vectorbt_payload: dict[str, Any],
    *,
    has_explicit_universe_markets: bool,
) -> bool:
    value = vectorbt_payload.get("prune_inactive_price_columns", "auto")
    if value is None:
        value = "auto"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "auto":
            return not has_explicit_universe_markets
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
        raise ValueError("prune_inactive_price_columns must be true, false, or 'auto'")
    return bool(value)


def _reference_index_key(index: pd.Index) -> tuple[int, str, str]:
    if len(index) == 0:
        return (0, "", "")
    return (len(index), str(index[0]), str(index[-1]))


def _universe_cache_key(
    universe_payload: dict[str, Any],
    feature_identities: dict[str, str],
    warning_cache_key: tuple[Any, ...],
    reference_index: pd.Index,
) -> tuple[Any, ...]:
    referenced_names = sorted(_universe_referenced_feature_names(universe_payload))
    referenced_feature_key = tuple(
        (name, feature_identities.get(name, "<missing>"))
        for name in referenced_names
    )
    # Feature specs can depend on other generated features. A universe may only
    # reference the final score column, while that score depends on a per-run
    # mask such as mcap_topn_mask. Include all feature identities so dependency
    # changes cannot accidentally reuse a stale universe result.
    all_feature_key = tuple(sorted(feature_identities.items()))
    return (
        _canonical_payload_key(_universe_payload_for_cache(universe_payload)),
        referenced_feature_key,
        all_feature_key,
        warning_cache_key,
        _reference_index_key(reference_index),
    )


def _selection_stats(weight_frame: pd.DataFrame) -> dict[str, float | int]:
    rebalance_mask = weight_frame.notna().any(axis=1)
    if not bool(rebalance_mask.any()):
        return {
            "rebalance_dates": 0,
            "active_rebalance_dates": 0,
            "avg_selected_count": 0.0,
            "min_selected_count": 0,
            "max_selected_count": 0,
            "avg_selected_count_active": 0.0,
            "min_selected_count_active": 0,
            "max_selected_count_active": 0,
            "weight_rows": 0,
        }
    counts = weight_frame.loc[rebalance_mask].fillna(0.0).gt(0.0).sum(axis=1)
    values = [int(value) for value in counts.tolist()]
    active_values = [value for value in values if value > 0]
    return {
        "rebalance_dates": len(values),
        "active_rebalance_dates": len(active_values),
        "avg_selected_count": (sum(values) / len(values)) if values else 0.0,
        "min_selected_count": min(values) if values else 0,
        "max_selected_count": max(values) if values else 0,
        "avg_selected_count_active": (sum(active_values) / len(active_values)) if active_values else 0.0,
        "min_selected_count_active": min(active_values) if active_values else 0,
        "max_selected_count_active": max(active_values) if active_values else 0,
        "weight_rows": int(sum(values)),
    }


def _attach_plateau_air_mean(
    result_frame: pd.DataFrame,
    grid_config: dict[str, list[Any]],
) -> pd.DataFrame:
    if result_frame.empty:
        return result_frame
    if "Annualized Information Ratio" not in result_frame.columns:
        return result_frame

    grid_axes = [key for key in grid_config.keys() if key in result_frame.columns]
    if not grid_axes:
        return result_frame

    numeric_axes: list[str] = []
    categorical_axes: list[str] = []
    axis_index: dict[str, dict[Any, int]] = {}
    for axis in grid_axes:
        values = list(grid_config.get(axis, []))
        if not values:
            continue
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            numeric_axes.append(axis)
            axis_index[axis] = {value: idx for idx, value in enumerate(values)}
        else:
            categorical_axes.append(axis)

    if not numeric_axes:
        return result_frame

    frame = result_frame.copy()
    frame["Plateau AIR Mean"] = float("nan")

    def _group_key(value: Any) -> Any:
        if isinstance(value, (list, dict, set)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return value

    group_cols = ["status"] if "status" in frame.columns else []
    temporary_group_cols: list[str] = []
    for axis in categorical_axes:
        group_col = f"__plateau_group_{axis}"
        frame[group_col] = frame[axis].map(_group_key)
        temporary_group_cols.append(group_col)
    group_cols.extend(temporary_group_cols)
    grouped = frame.groupby(group_cols, dropna=False) if group_cols else [(None, frame)]

    for _, group in grouped:
        ok_group = group[group["status"] == "ok"].copy() if "status" in group.columns else group.copy()
        if ok_group.empty:
            continue
        indexed_positions = {
            axis: ok_group[axis].map(axis_index[axis])
            for axis in numeric_axes
        }
        for idx, row in ok_group.iterrows():
            keep = ok_group.index != idx
            for axis in numeric_axes:
                row_pos = axis_index[axis].get(row[axis])
                if row_pos is None:
                    keep &= False
                    continue
                keep &= indexed_positions[axis].sub(row_pos).abs().le(1)
            neighbors = ok_group[keep]
            if neighbors.empty:
                continue
            frame.loc[idx, "Plateau AIR Mean"] = pd.to_numeric(
                neighbors["Annualized Information Ratio"],
                errors="coerce",
            ).mean()
    return frame.drop(columns=temporary_group_cols, errors="ignore")


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
        warning_frame = warning_frame.reindex(columns=sorted(warning_frame.columns)[:max_markets])
    return warning_frame.sort_index().sort_index(axis=1)


def _write_numeric_cache(
    candle_dir: Path,
    cache_dir: Path,
    max_markets: int | None,
    tail_rows: int | None,
) -> None:
    frames, meta_by_market = build_wide_frames_from_candle_dir(
        candle_dir,
        DEFAULT_CACHE_VALUE_COLUMNS,
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    for column, frame in frames.items():
        frame.to_parquet(cache_dir / f"{column}.parquet")
    meta_frame = (
        pd.DataFrame.from_records(sorted(meta_by_market.values(), key=lambda item: item["market"]))
        if meta_by_market
        else pd.DataFrame(columns=["market", "korean_name", "english_name", "market_warning"])
    )
    meta_frame.to_parquet(cache_dir / "market_meta.parquet", index=False)


def _write_warning_cache(
    candle_dir: Path,
    cache_dir: Path,
    max_markets: int | None,
    tail_rows: int | None,
) -> None:
    long_frame = build_long_frame_from_candle_dir(
        candle_dir,
        usecols=["date_utc", "market", "market_warning"],
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    if long_frame.empty:
        warning_frame = pd.DataFrame()
    else:
        warning_frame = (
            long_frame.assign(
                date_utc=pd.to_datetime(long_frame["date_utc"], utc=False),
                market=long_frame["market"].astype(str).str.upper(),
                market_warning=long_frame["market_warning"].astype(str).str.upper(),
            )
            .pivot(index="date_utc", columns="market", values="market_warning")
            .sort_index()
            .sort_index(axis=1)
        )
    warning_frame.to_parquet(cache_dir / "market_warning.parquet")


def _resolve_source_cache_dir(
    config: dict[str, Any],
    candle_dir: Path,
    out_dir: Path,
) -> Path:
    configured = config.get("source_cache_dir")
    if configured:
        return Path(str(configured))

    parts = candle_dir.as_posix().split("/")
    if len(parts) >= 4 and parts[0] == "data":
        timeframe = parts[-1]
        if parts[1] == "upbit_research" and parts[2] == "minutes":
            candidate = ROOT_DIR / "data" / "upbit_research_cache" / timeframe
            if candidate.exists():
                return candidate
        if parts[1] == "upbit" and parts[2] == "minutes":
            candidate = ROOT_DIR / "data" / "upbit_cache" / timeframe
            if candidate.exists():
                return candidate

    cache_dir = out_dir / "_source_cache"
    if not cache_dir.exists():
        _write_numeric_cache(
            candle_dir,
            cache_dir,
            max_markets=config.get("max_markets"),
            tail_rows=config.get("tail_hours"),
        )
        _write_warning_cache(
            candle_dir,
            cache_dir,
            max_markets=config.get("max_markets"),
            tail_rows=config.get("tail_hours"),
        )
    return cache_dir


def _run_v2_backtest(
    candle_dir: Path,
    source_cache_dir: Path,
    feature_frames: dict[str, pd.DataFrame],
    warning_frame: pd.DataFrame,
    required_feature_markets: list[str] | None,
    universe_payload: dict[str, Any],
    weight_payload: dict[str, Any],
    vectorbt_payload: dict[str, Any],
    backtest_dir: Path,
    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame],
    max_markets: int | None,
    tail_hours: int | None,
    compute_rolling_ir_enabled: bool,
    print_run_summary: bool,
    save_run_artifacts: bool,
    universe_cache: dict[tuple[Any, ...], Any] | None = None,
    universe_cache_key: tuple[Any, ...] | None = None,
    universe_rank_cache: dict[tuple[object, ...], pd.DataFrame] | None = None,
    universe_rank_cache_namespace: tuple[object, ...] | None = None,
) -> tuple[pd.Series, pd.DataFrame]:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    weight_spec = load_weight_spec_from_payload(weight_payload)

    reference_index = next(iter(feature_frames.values())).index
    warning_frame = warning_frame.reindex(index=reference_index)

    universe_result = (
        universe_cache.get(universe_cache_key)
        if universe_cache is not None and universe_cache_key is not None
        else None
    )
    if universe_result is None:
        universe_result = build_universe_mask_v2(
            feature_frames,
            warning_frame,
            universe_spec,
            rank_cache=universe_rank_cache,
            rank_cache_namespace=universe_rank_cache_namespace,
        )
        if universe_cache is not None and universe_cache_key is not None:
            universe_cache[universe_cache_key] = universe_result
    weight_frame = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames)
    change_only_weights = is_change_only_weight_output(weight_spec)
    if change_only_weights:
        weight_frame = weight_frame.ffill().fillna(0.0)

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
        active_columns = weight_frame.fillna(0.0).abs().sum(axis=0).gt(0.0)
        required_price_market_set = {str(column).upper() for column in weight_frame.columns[active_columns]}
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
    trim_start_mode = str(vectorbt_payload.get("trim_start_mode", "none"))
    trim_start_timestamp_value = vectorbt_payload.get("trim_start_timestamp")
    if trim_start_mode == "timestamp":
        if trim_start_timestamp_value is None:
            raise ValueError("trim_start_mode='timestamp' requires vectorbt_spec.trim_start_timestamp")
        trimmed_start_timestamp = pd.Timestamp(str(trim_start_timestamp_value))
        price_frame = price_frame.loc[price_frame.index >= trimmed_start_timestamp]
        weight_frame = weight_frame.loc[weight_frame.index >= trimmed_start_timestamp]
    else:
        price_frame, weight_frame, trimmed_start_timestamp = trim_frames_to_first_weight(
            price_frame,
            weight_frame,
            mode=trim_start_mode,
        )
    if change_only_weights:
        weight_frame = to_change_only_target_frame(weight_frame)
    if benchmark_price_frame is not None and not price_frame.empty:
        benchmark_price_frame = benchmark_price_frame.loc[
            (benchmark_price_frame.index >= price_frame.index.min())
            & (benchmark_price_frame.index <= price_frame.index.max())
        ]

    timeframe = infer_timeframe(candle_dir, None)
    periods_per_year = resolve_periods_per_year(
        timeframe,
        vectorbt_payload.get("periods_per_year"),
    )
    periods_per_day = periods_per_day_for_timeframe(timeframe)
    pandas_freq = timeframe_to_pandas_freq(timeframe)

    portfolio = run_portfolio_from_target_weights(
        price_frame=price_frame,
        target_weight_frame=weight_frame,
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
        price_frame=price_frame,
        portfolio=portfolio,
        init_cash=init_cash,
        benchmark_mode=benchmark_mode,
        benchmark_market=benchmark_market,
        benchmark_price_frame=benchmark_price_frame,
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
    excess_returns, excess_equity_curve = compute_excess_curves(
        comparison_equity_curve,
        aligned_benchmark_curve,
        init_cash,
    )
    rolling_ir = pd.DataFrame(index=excess_returns.index)
    if compute_rolling_ir_enabled:
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
    summary.loc["Trim Start Mode"] = trim_start_mode
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
            rolling_ir_summary,
        ]
    )

    if print_run_summary:
        print_summary(summary)
    if save_run_artifacts:
        backtest_dir.mkdir(parents=True, exist_ok=True)
        write_summary_csv(backtest_dir / "summary.csv", summary)
        write_equity_csv(backtest_dir / "equity_curve.csv", equity_curve)
        write_equity_csv(backtest_dir / "benchmark_curve.csv", aligned_benchmark_curve)
        write_equity_csv(backtest_dir / "excess_equity_curve.csv", excess_equity_curve)
        if compute_rolling_ir_enabled:
            write_equity_csv(backtest_dir / "rolling_information_ratio.csv", rolling_ir)
    return summary, weight_frame


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config_path = Path(args.config_json)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))

    candle_dir_value = config.get("candle_dir") or config.get("daily_dir")
    if not candle_dir_value:
        raise SystemExit("config must define candle_dir or daily_dir")
    candle_dir = Path(str(candle_dir_value))

    out_dir = Path(config.get("out_dir", "data/grid/cross_section_v2"))
    out_dir.mkdir(parents=True, exist_ok=True)
    source_cache_dir = _resolve_source_cache_dir(config, candle_dir, out_dir)
    market_score_spec_path = config.get("market_scores_spec_json")
    static_market_score_spec = (
        load_market_score_spec(Path(str(market_score_spec_path)))
        if market_score_spec_path
        else None
    )

    run_name_template = config["run_name_template"]
    compute_rolling_ir_enabled = bool(config.get("compute_rolling_ir", False))
    print_run_summaries = bool(config.get("print_run_summaries", False))
    save_run_artifacts = bool(config.get("save_run_artifacts", False))
    max_markets = config.get("max_markets")
    tail_hours = config.get("tail_hours")

    raw_combinations = _grid_combinations(config.get("grid", {}))
    combinations = [combo for combo in raw_combinations if _passes_constraints(combo, config.get("constraints", []))]
    if not combinations:
        raise SystemExit("No valid grid combinations after applying constraints")

    feature_use_counts: Counter[str] = Counter()
    universe_use_counts: Counter[str] = Counter()
    rank_use_counts: Counter[str] = Counter()
    for combo in combinations:
        usage_context = dict(combo)
        usage_context["run_name"] = run_name_template.format(**usage_context)
        usage_feature_payload = (
            _render_value(config["feature_spec_template"], usage_context)
            if config.get("feature_spec_template") is not None
            else None
        )
        if usage_feature_payload is not None:
            feature_use_counts[_canonical_payload_key(usage_feature_payload)] += 1
        usage_universe_payload = _render_value(
            config["universe_spec_template"], usage_context
        )
        usage_market_score_payload = (
            _render_value(config["market_scores_spec_template"], usage_context)
            if config.get("market_scores_spec_template") is not None
            else None
        )
        universe_use_counts[
            _dynamic_universe_usage_key(
                feature_payload=usage_feature_payload,
                universe_payload=usage_universe_payload,
                market_score_payload=usage_market_score_payload,
                market_score_spec_path=market_score_spec_path,
            )
        ] += 1
        rank_use_counts[
            _dynamic_rank_usage_key(
                feature_payload=usage_feature_payload,
                market_score_payload=usage_market_score_payload,
                market_score_spec_path=market_score_spec_path,
            )
        ] += 1

    feature_cache: dict[tuple[str, tuple[str, ...], str], dict[str, pd.DataFrame]] = {}
    node_frame_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    source_frame_cache: dict[tuple[tuple[str, ...], tuple[str, ...], int | None], dict[str, pd.DataFrame]] = {}
    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame] = {}
    warning_frame_cache: dict[tuple[tuple[str, ...], int | None], pd.DataFrame] = {}
    universe_cache: dict[tuple[Any, ...], Any] = {}
    universe_rank_cache: dict[tuple[object, ...], pd.DataFrame] = {}
    universe_cache_keys_by_usage: dict[str, set[tuple[Any, ...]]] = {}
    universe_rank_namespaces_by_usage: dict[str, set[tuple[object, ...]]] = {}
    results: list[dict[str, Any]] = []

    shared_feature_payload = config.get("shared_feature_spec_template")
    shared_feature_frames: dict[str, pd.DataFrame] | None = None
    shared_feature_rows: int | None = None
    shared_required_feature_markets: list[str] | None = None
    shared_output_feature_markets: list[str] | None = None
    if shared_feature_payload:
        shared_specs = load_feature_specs_from_payload(shared_feature_payload)
        shared_feature_payload_key = _canonical_payload_key(shared_feature_payload)
        shared_required_sets: list[set[str]] = []
        shared_output_sets: list[set[str]] = []
        shared_markets_resolvable = True
        for combo in combinations:
            context = dict(combo)
            context["run_name"] = run_name_template.format(**context)
            universe_payload = _render_value(config["universe_spec_template"], context)
            rendered_market_score_payload = (
                _render_value(config["market_scores_spec_template"], context)
                if config.get("market_scores_spec_template") is not None
                else None
            )
            market_score_spec = (
                load_market_score_spec_from_payload(rendered_market_score_payload)
                if rendered_market_score_payload is not None
                else static_market_score_spec
            )
            required_markets = _resolve_required_feature_markets(shared_specs, universe_payload, market_score_spec)
            if required_markets is None:
                shared_markets_resolvable = False
                break
            shared_required_sets.append(set(required_markets))
            output_markets = _resolve_output_feature_markets(universe_payload)
            if output_markets is not None:
                shared_output_sets.append(set(output_markets))
        if shared_markets_resolvable and shared_required_sets:
            shared_required_feature_markets = sorted(set().union(*shared_required_sets))
        if shared_output_sets:
            shared_output_feature_markets = sorted(set().union(*shared_output_sets))
        shared_required_columns, shared_uses_market_source = required_source_columns_for_feature_specs(
            shared_specs,
            SUPPORTED_SOURCE_COLUMNS,
        )
        shared_source_frames = read_wide_frames_from_cache(
            source_cache_dir,
            sorted(shared_required_columns or {"trade_price"}),
            market_columns=shared_required_feature_markets,
            max_markets=(
                None
                if (shared_uses_market_source or shared_required_feature_markets is not None)
                else max_markets
            ),
        )
        shared_feature_frames = build_feature_frames_from_cache(
            source_cache_dir,
            shared_specs,
            market_columns=shared_required_feature_markets,
            output_market_columns=shared_output_feature_markets,
            max_markets=None if shared_required_feature_markets is not None else max_markets,
            tail_rows=tail_hours,
            source_frames=shared_source_frames,
                frame_cache=node_frame_cache,
                frame_cache_namespace=(
                    "grid_v2",
                    "shared",
                    shared_feature_payload_key,
                    tuple(sorted(shared_required_columns or {"trade_price"})),
                    tuple(shared_required_feature_markets or ()),
                    tuple(shared_output_feature_markets or ()),
                    tail_hours,
            ),
        )
        if static_market_score_spec is not None:
            shared_feature_frames[static_market_score_spec.output_column] = build_market_score_frame(shared_feature_frames, static_market_score_spec)
        primary_frame = next(iter(shared_feature_frames.values()))
        shared_feature_rows = int(primary_frame.notna().sum().sum())

    total_runs = len(combinations)

    for run_index, combo in enumerate(combinations, start=1):
        context = dict(combo)
        run_name = run_name_template.format(**context)
        context["run_name"] = run_name
        print(f"[{run_index}/{total_runs}] {run_name}")

        run_dir = out_dir / run_name
        specs_dir = run_dir / "specs"
        backtest_dir = run_dir / "backtest"
        if save_run_artifacts:
            specs_dir.mkdir(parents=True, exist_ok=True)
            backtest_dir.mkdir(parents=True, exist_ok=True)

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
        feature_payload = (
            _render_value(config["feature_spec_template"], context)
            if config.get("feature_spec_template") is not None
            else None
        )
        feature_payload_key = (
            _canonical_payload_key(feature_payload)
            if feature_payload is not None
            else ""
        )
        universe_usage_key = _dynamic_universe_usage_key(
            feature_payload=feature_payload,
            universe_payload=universe_payload,
            market_score_payload=market_score_payload,
            market_score_spec_path=market_score_spec_path,
        )
        rank_usage_key = _dynamic_rank_usage_key(
            feature_payload=feature_payload,
            market_score_payload=market_score_payload,
            market_score_spec_path=market_score_spec_path,
        )

        if save_run_artifacts and feature_payload is not None:
            (specs_dir / "features.json").write_text(
                json.dumps(feature_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if save_run_artifacts:
            (specs_dir / "universe.json").write_text(
                json.dumps(universe_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (specs_dir / "weights.json").write_text(
                json.dumps(weight_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        result_row: dict[str, Any] = {"run_name": run_name, **combo}
        feature_key = None
        feature_frames = None
        per_run_frames = None
        per_run_source_frames = None
        universe_cache_lookup_key = None
        universe_rank_cache_namespace = None
        weight_frame = None
        try:
            required_feature_markets = shared_required_feature_markets
            output_feature_markets = _resolve_output_feature_markets(universe_payload)
            if shared_feature_frames is not None:
                feature_frames = dict(shared_feature_frames)
                if feature_payload is not None:
                    feature_specs = load_feature_specs_from_payload(feature_payload)
                    per_run_required_feature_markets = _resolve_required_feature_markets(
                        feature_specs,
                        universe_payload,
                        market_score_spec,
                    )
                    if per_run_required_feature_markets is not None:
                        required_feature_markets = sorted(
                            set(required_feature_markets or ()) | set(per_run_required_feature_markets)
                        )
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
                        source_frames = read_wide_frames_from_cache(
                            source_cache_dir,
                            list(source_cache_key[0]),
                            market_columns=required_feature_markets,
                            max_markets=source_cache_key[2],
                        )
                        source_frame_cache[source_cache_key] = source_frames
                    per_run_source_frames = dict(source_frames)
                    per_run_source_frames.update(feature_frames)
                    per_run_frames = build_feature_frames_from_cache(
                        source_cache_dir,
                        feature_specs,
                        market_columns=required_feature_markets,
                        output_market_columns=output_feature_markets,
                        max_markets=None if required_feature_markets is not None else max_markets,
                        tail_rows=tail_hours,
                        source_frames=per_run_source_frames,
                        frame_cache=node_frame_cache,
                        frame_cache_namespace=(
                            "grid_v2",
                            "per_run",
                            feature_payload_key,
                            source_cache_key[0],
                            tuple(required_feature_markets or ()),
                            tuple(output_feature_markets or ()),
                            tail_hours,
                        ),
                    )
                    feature_frames.update(_feature_outputs_only(per_run_frames, feature_specs))
                    if market_score_payload is not None:
                        feature_frames[market_score_spec.output_column] = build_market_score_frame(feature_frames, market_score_spec)
                if weight_market_score_spec is not None:
                    feature_frames[weight_market_score_spec.output_column] = build_market_score_frame(
                        feature_frames,
                        weight_market_score_spec,
                    )
                primary_frame = next(iter(feature_frames.values()))
                result_row["feature_rows"] = int(primary_frame.notna().sum().sum())
                result_row["features_source"] = "shared"
            else:
                feature_specs = load_feature_specs_from_payload(feature_payload)
                required_feature_markets = _resolve_required_feature_markets(
                    feature_specs,
                    universe_payload,
                    market_score_spec,
                )
                feature_key = (
                    _canonical_payload_key(feature_payload),
                    tuple(required_feature_markets) if required_feature_markets is not None else (),
                    tuple(output_feature_markets) if output_feature_markets is not None else (),
                    _canonical_payload_key(market_score_payload) if market_score_payload is not None else (
                        str(Path(str(market_score_spec_path)).resolve()) if market_score_spec_path else ""
                    ),
                    _canonical_payload_key(weight_market_score_payload) if weight_market_score_payload is not None else "",
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
                        frame_cache=node_frame_cache,
                        frame_cache_namespace=(
                            "grid_v2",
                            "per_run",
                            feature_payload_key,
                            source_cache_key[0],
                            tuple(required_feature_markets or ()),
                            tuple(output_feature_markets or ()),
                            tail_hours,
                        ),
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
                primary_frame = next(iter(feature_frames.values()))
                result_row["feature_rows"] = int(primary_frame.notna().sum().sum())
                result_row["features_source"] = "per_run"

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

            feature_identities = _feature_identity_map_from_payloads(
                [
                    shared_feature_payload,
                    feature_payload,
                ]
            )
            if market_score_spec is not None:
                market_score_key = (
                    _canonical_payload_key(market_score_payload)
                    if market_score_payload is not None
                    else (
                        f"path:{Path(str(market_score_spec_path)).resolve()}"
                        if market_score_spec_path
                        else "static_market_score"
                    )
                )
                feature_identities[market_score_spec.output_column] = market_score_key
            if weight_market_score_spec is not None:
                feature_identities[weight_market_score_spec.output_column] = _canonical_payload_key(
                    weight_market_score_payload
                )
            reference_index = next(iter(feature_frames.values())).index
            universe_cache_lookup_key = _universe_cache_key(
                universe_payload,
                feature_identities,
                warning_cache_key,
                reference_index,
            )
            universe_rank_cache_namespace = (
                "grid_v2_universe_rank",
                str(source_cache_dir.resolve()),
                tuple(sorted(feature_identities.items())),
                warning_cache_key,
                _reference_index_key(reference_index),
            )
            universe_cache_keys_by_usage.setdefault(universe_usage_key, set()).add(
                universe_cache_lookup_key
            )
            universe_rank_namespaces_by_usage.setdefault(
                rank_usage_key, set()
            ).add(universe_rank_cache_namespace)

            summary, weight_frame = _run_v2_backtest(
                candle_dir,
                source_cache_dir,
                feature_frames,
                warning_frame,
                required_feature_markets,
                universe_payload,
                weight_payload,
                vectorbt_payload,
                backtest_dir,
                price_frame_cache,
                max_markets=max_markets,
                tail_hours=tail_hours,
                compute_rolling_ir_enabled=compute_rolling_ir_enabled,
                print_run_summary=print_run_summaries,
                save_run_artifacts=save_run_artifacts,
                universe_cache=universe_cache,
                universe_cache_key=universe_cache_lookup_key,
                universe_rank_cache=universe_rank_cache,
                universe_rank_cache_namespace=universe_rank_cache_namespace,
            )
            result_row.update(_selection_stats(weight_frame))
            result_row["status"] = "ok"
            result_row.update(summary.to_dict())
            print(f"[{run_index}/{total_runs}] ok: {run_name}")
        except Exception as exc:
            result_row["status"] = "error"
            result_row["error"] = str(exc)
            print(f"[{run_index}/{total_runs}] error: {run_name} :: {exc}")
        finally:
            if feature_payload_key:
                feature_use_counts[feature_payload_key] -= 1
                if feature_use_counts[feature_payload_key] <= 0:
                    feature_use_counts.pop(feature_payload_key, None)
                    _drop_node_cache_by_namespace_prefix(
                        node_frame_cache,
                        ("grid_v2", "per_run", feature_payload_key),
                    )
                    stale_feature_keys = [
                        key
                        for key in feature_cache
                        if isinstance(key, tuple)
                        and key
                        and key[0] == feature_payload_key
                    ]
                    for key in stale_feature_keys:
                        feature_cache.pop(key, None)

            universe_use_counts[universe_usage_key] -= 1
            if universe_use_counts[universe_usage_key] <= 0:
                universe_use_counts.pop(universe_usage_key, None)
                for cache_key in universe_cache_keys_by_usage.pop(
                    universe_usage_key, set()
                ):
                    universe_cache.pop(cache_key, None)

            rank_use_counts[rank_usage_key] -= 1
            if rank_use_counts[rank_usage_key] <= 0:
                rank_use_counts.pop(rank_usage_key, None)
                for namespace in universe_rank_namespaces_by_usage.pop(
                    rank_usage_key, set()
                ):
                    _drop_rank_cache_by_namespace(
                        universe_rank_cache,
                        namespace,
                    )

            feature_frames = None
            per_run_frames = None
            per_run_source_frames = None
            weight_frame = None
        results.append(result_row)

        result_frame = pd.DataFrame(results)
        result_frame = _attach_plateau_air_mean(result_frame, config.get("grid", {}))
        rows_to_write = result_frame.to_dict(orient="records")
        columns = list(result_frame.columns)
        with (out_dir / "summary_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows_to_write)

    print(f"Wrote {len(results)} grid result rows to {out_dir / 'summary_results.csv'}")


if __name__ == "__main__":
    main()
