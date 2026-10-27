#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
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
from scripts.run_cross_section_grid_v2 import (
    _grid_combinations,
    _passes_constraints,
    _read_warning_frame,
    _render_value,
    _resolve_source_cache_dir,
)
from scripts.walkforward.run_cross_section_grid_walkforward_v2 import (
    _available_any_timestamps,
    _run_v2_backtest_window,
    _trim_validation_timestamps,
)
from scripts.run_vectorbt import compute_return_series, load_price_frame


SCORE_METRICS = ("air", "sharpe", "cvar_sharpe", "martin", "calmar", "log_return", "simple_return")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run CSCV-style PBO analysis for a v2 grid family.")
    parser.add_argument("--grid-config-json", required=True, help="Grid config JSON path")
    parser.add_argument("--subperiod-months", type=int, default=6, help="Non-overlapping subperiod size in months")
    parser.add_argument(
        "--anchor-month-offsets",
        default="0",
        help="Comma-separated month offsets for subperiod anchoring, e.g. 0,3 for a 6-month grid",
    )
    parser.add_argument(
        "--score-metric",
        choices=SCORE_METRICS,
        default="air",
        help="Metric recomputed on concatenated IS/OOS returns (not averaged period scores)",
    )
    parser.add_argument(
        "--max-splits",
        type=int,
        default=0,
        help="Maximum CSCV splits; use an even count to sample complementary pairs. 0 uses all.",
    )
    parser.add_argument("--random-seed", type=int, default=42, help="Random seed for split subsampling")
    parser.add_argument("--out-dir", help="Directory for outputs")
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


def _score_returns(returns: np.ndarray, metric: str, periods_per_year: np.ndarray) -> np.ndarray:
    """Score pooled observations; AIR input must already be benchmark excess returns."""
    if metric not in SCORE_METRICS:
        raise ValueError(f"Unsupported score metric: {metric}")
    if returns.ndim != 2 or len(returns) == 0 or not np.isfinite(returns).all():
        raise ValueError("PBO requires a non-empty, finite observations-by-candidates return matrix")
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if metric in ("air", "sharpe"):
            std = returns.std(axis=0, ddof=0)
            return returns.mean(axis=0) / np.where(std == 0, np.nan, std) * np.sqrt(periods_per_year)
        if metric == "cvar_sharpe":
            threshold = np.quantile(returns, 0.05, axis=0)
            tail = returns <= threshold
            cvar = np.abs(np.where(tail, returns, 0.0).sum(axis=0) / tail.sum(axis=0))
            return returns.mean(axis=0) / np.where(cvar == 0, np.nan, cvar) * np.sqrt(periods_per_year)
        if (returns <= -1.0).any():
            raise ValueError("Compounded PBO metrics require returns greater than -100%")
        log_return = np.log1p(returns).sum(axis=0)
        if metric == "log_return":
            return log_return
        if metric == "simple_return":
            return np.expm1(log_return)
        # Unit initial equity makes every input row one compounding interval.
        equity = np.vstack([np.ones(returns.shape[1]), np.cumprod(1.0 + returns, axis=0)])
        drawdown = equity / np.maximum.accumulate(equity, axis=0) - 1.0
        cagr = np.expm1(log_return * periods_per_year / len(returns))
        if metric == "martin":
            risk = np.sqrt(np.mean(drawdown ** 2, axis=0))
        else:
            risk = np.abs(drawdown.min(axis=0))
        return cagr / np.where(risk == 0, np.nan, risk)


class _ReturnBlockScorer:
    """Retain only blocks and sufficient statistics, never all split return arrays."""

    def __init__(self, blocks: list[np.ndarray], metric: str, periods_per_year: np.ndarray):
        if not blocks or any(block.ndim != 2 or len(block) == 0 for block in blocks):
            raise ValueError("PBO return blocks must contain observations")
        if any(block.shape[1] != len(periods_per_year) or not np.isfinite(block).all() for block in blocks):
            raise ValueError("PBO blocks must have the same candidates and finite returns")
        self.blocks = blocks
        self.metric = metric
        self.periods_per_year = periods_per_year
        self.counts = np.array([len(block) for block in blocks])[:, None]
        self.means = np.array([block.mean(axis=0) for block in blocks])
        self.m2 = np.array([((block - mean) ** 2).sum(axis=0) for block, mean in zip(blocks, self.means)])

    def score(self, indices: list[int]) -> np.ndarray:
        indices = sorted(indices)
        if self.metric not in ("air", "sharpe"):
            return _score_returns(np.concatenate([self.blocks[i] for i in indices]), self.metric, self.periods_per_year)
        counts = self.counts[indices]
        means = self.means[indices]
        count = counts.sum()
        mean = (means * counts).sum(axis=0) / count
        variance = (self.m2[indices] + counts * (means - mean) ** 2).sum(axis=0) / count
        with np.errstate(divide="ignore", invalid="ignore"):
            return mean / np.sqrt(np.where(variance == 0, np.nan, variance)) * np.sqrt(self.periods_per_year)


def _returns_from_result(result: dict[str, Any], metric: str) -> pd.Series:
    if result.get("status") != "ok":
        raise ValueError(f"PBO candidate backtest failed: {result.get('status')}")
    equity = result["_equity_curve"]
    curves = [equity] + ([result["_benchmark_curve"]] if metric == "air" else [])
    for curve in curves:
        if (curve.empty or not curve.index.is_unique or not curve.index.is_monotonic_increasing
                or not np.isfinite(curve.to_numpy()).all() or (curve <= 0).any()):
            raise ValueError("PBO requires positive finite curves with unique chronological timestamps")
    returns = compute_return_series(equity)
    if metric == "air":
        # Compute returns before alignment; never fill a missing benchmark return with zero.
        aligned = pd.concat([returns.rename("strategy"),
                             compute_return_series(curves[1]).rename("benchmark")], axis=1, join="inner")
        returns = aligned["strategy"] - aligned["benchmark"]
    return returns


def _split_selection(is_scores: pd.Series, oos_scores: pd.Series) -> dict[str, Any]:
    if (len(is_scores) < 2 or not is_scores.index.equals(oos_scores.index)
            or not np.isfinite(is_scores).all() or not np.isfinite(oos_scores).all()):
        raise ValueError("Undefined PBO scores: keep the candidate family fixed; check zero risk/variance or metric")
    label = str(is_scores.idxmax())
    percentile = _rank_percentile_desc(oos_scores, label)
    return {
        "candidate_count": len(is_scores),
        "is_best_label": label,
        "is_best_score": float(is_scores.loc[label]),
        "oos_score_for_is_best": float(oos_scores.loc[label]),
        "oos_rank_desc": float(oos_scores.rank(method="average", ascending=False).loc[label]),
        "oos_rank_percentile": percentile,
        "lambda": math.log(percentile / (1.0 - percentile)),
        "is_overfit": percentile <= 0.5,
    }


def _build_subperiods(
    timestamps: pd.DatetimeIndex,
    months: int,
    anchor_month_offset: int = 0,
) -> list[dict[str, Any]]:
    if timestamps.empty:
        return []
    periods: list[dict[str, Any]] = []
    current_start = pd.Timestamp(timestamps.min()) + pd.DateOffset(months=anchor_month_offset)
    max_timestamp = pd.Timestamp(timestamps.max())
    period_idx = 1
    while True:
        current_end = current_start + pd.DateOffset(months=months)
        if current_end > max_timestamp:
            break
        period_timestamps = timestamps[(timestamps >= current_start) & (timestamps < current_end)]
        if len(period_timestamps) >= 2:
            periods.append(
                {
                    "period_id": f"anchor_{anchor_month_offset:02d}_period_{period_idx:02d}",
                    "start": current_start,
                    "end": current_end,
                    "rows": int(len(period_timestamps)),
                    "anchor_month_offset": int(anchor_month_offset),
                }
            )
            period_idx += 1
        current_start = current_end
    return periods


def _build_cscv_splits(
    period_ids: list[str],
    max_splits: int,
    random_seed: int,
) -> list[tuple[list[int], list[int]]]:
    n_periods = len(period_ids)
    if n_periods < 4:
        return []
    if n_periods % 2 == 1:
        raise ValueError("CSCV requires an even number of periods")
    if max_splits < 0:
        raise ValueError("max-splits must be nonnegative")
    half = n_periods // 2
    # Each canonical pair contains block zero on one side; evaluate BOTH directions.
    all_is_combos = list(itertools.combinations(range(1, n_periods), half - 1))
    if max_splits and len(all_is_combos) * 2 > max_splits:
        if max_splits % 2:
            raise ValueError("max-splits must be even when subsampling complementary CSCV pairs")
        rng = np.random.default_rng(random_seed)
        chosen = rng.choice(len(all_is_combos), size=max_splits // 2, replace=False)
        all_is_combos = [all_is_combos[int(idx)] for idx in sorted(chosen.tolist())]

    all_indices = set(range(n_periods))
    splits: list[tuple[list[int], list[int]]] = []
    for combo in all_is_combos:
        is_idx = [0, *combo]
        oos_idx = sorted(all_indices - set(is_idx))
        splits.append((is_idx, oos_idx))
        splits.append((oos_idx, is_idx))
    return sorted(splits, key=lambda split: split[0])


def _build_candidate_frames(
    config: dict[str, Any],
) -> tuple[
    Path,
    Path,
    str,
    list[dict[str, Any]],
    dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str] | None, dict[str, pd.DataFrame], pd.DataFrame]],
]:
    candle_dir_value = config.get("candle_dir") or config.get("daily_dir")
    if not candle_dir_value:
        raise SystemExit("config must define candle_dir or daily_dir")
    candle_dir = Path(str(candle_dir_value))
    source_cache_dir = _resolve_source_cache_dir(config, candle_dir, Path(config.get("out_dir", candle_dir.parent)))
    market_score_spec_path = config.get("market_scores_spec_json")
    static_market_score_spec = (
        load_market_score_spec(Path(str(market_score_spec_path))) if market_score_spec_path else None
    )

    run_name_template = config["run_name_template"]
    combinations = [
        combo
        for combo in _grid_combinations(config.get("grid", {}))
        if _passes_constraints(combo, config.get("constraints", []))
    ]
    if not combinations:
        raise SystemExit("No valid grid combinations after applying constraints")

    feature_cache: dict[tuple[str, tuple[str, ...], str], dict[str, pd.DataFrame]] = {}
    source_frame_cache: dict[tuple[tuple[str, ...], tuple[str, ...], int | None], dict[str, pd.DataFrame]] = {}
    warning_frame_cache: dict[tuple[tuple[str, ...], int | None], pd.DataFrame] = {}
    candidate_payloads: dict[
        str,
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str] | None, dict[str, pd.DataFrame], pd.DataFrame],
    ] = {}

    first_context = dict(combinations[0])
    first_context["run_name"] = run_name_template.format(**first_context)
    first_vectorbt_payload = _render_value(config.get("vectorbt_spec_template", {}), first_context)
    first_universe_payload = _render_value(config["universe_spec_template"], first_context)
    allowed_markets = first_universe_payload.get("allowed_markets", [])
    primary_market = (
        str(allowed_markets[0]).upper()
        if allowed_markets
        else str(first_vectorbt_payload.get("benchmark_market", "KRW-BTC")).upper()
    )

    max_markets = config.get("max_markets")
    tail_hours = config.get("tail_hours")

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
        market_score_spec = (
            load_market_score_spec_from_payload(market_score_payload)
            if market_score_payload is not None
            else static_market_score_spec
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

        candidate_payloads[run_name] = (
            universe_payload,
            weight_payload,
            vectorbt_payload,
            required_feature_markets,
            feature_frames,
            warning_frame,
        )

    return candle_dir, source_cache_dir, primary_market, combinations, candidate_payloads


def _rank_percentile_desc(scores: pd.Series, target_label: str) -> float:
    ranks = scores.rank(method="average", ascending=True)
    rank = float(ranks.loc[target_label])
    count = int(len(scores))
    return rank / (count + 1)


def _default_out_dir(grid_config_json: Path, score_metric: str, subperiod_months: int) -> Path:
    return ROOT_DIR / "data" / "research" / "pbo" / f"{grid_config_json.stem}_{score_metric}_{subperiod_months}m"


def _parse_anchor_offsets(raw_value: str, months: int) -> list[int]:
    offsets: list[int] = []
    for chunk in str(raw_value).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        value = int(chunk)
        if value < 0 or value >= months:
            raise SystemExit(f"anchor-month-offsets must be in [0, {months - 1}]")
        offsets.append(value)
    unique_offsets = sorted(set(offsets))
    if not unique_offsets:
        raise SystemExit("anchor-month-offsets must contain at least one valid offset")
    return unique_offsets


def _resolve_timestamp_basis_markets(
    primary_market: str,
    candidate_payloads: dict[
        str,
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str] | None, dict[str, pd.DataFrame], pd.DataFrame],
    ],
) -> list[str]:
    markets: set[str] = set()
    for universe_payload, _, _, required_feature_markets, _, _ in candidate_payloads.values():
        allowed_markets = universe_payload.get("allowed_markets") or []
        if allowed_markets:
            markets.update(str(market).upper() for market in allowed_markets)
        elif required_feature_markets:
            markets.update(str(market).upper() for market in required_feature_markets)
    if not markets:
        markets.add(primary_market)
    return sorted(markets)


def main() -> int:
    args = build_parser().parse_args()
    if args.subperiod_months <= 0:
        raise SystemExit("subperiod-months must be positive")
    if args.max_splits < 0 or args.max_splits % 2:
        raise SystemExit("max-splits must be zero or a positive even number (complementary pairs)")
    config_path = Path(args.grid_config_json)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir(config_path, args.score_metric, args.subperiod_months)
    out_dir.mkdir(parents=True, exist_ok=True)
    anchor_offsets = _parse_anchor_offsets(args.anchor_month_offsets, args.subperiod_months)

    candle_dir, source_cache_dir, primary_market, _, candidate_payloads = _build_candidate_frames(config)
    timestamp_basis_markets = _resolve_timestamp_basis_markets(primary_market, candidate_payloads)

    timestamp_price_frame = load_price_frame(
        candle_dir,
        "trade_price",
        load_mode="wide",
        source_cache_dir=source_cache_dir,
        market_columns=timestamp_basis_markets,
    )
    common_timestamp_mask = timestamp_price_frame[timestamp_basis_markets].notna().all(axis=1)
    timestamps = pd.DatetimeIndex(timestamp_price_frame.index[common_timestamp_mask])
    if timestamps.empty:
        timestamp_price_frame = load_price_frame(
            candle_dir,
            "trade_price",
            load_mode="wide",
            source_cache_dir=source_cache_dir,
            market_columns=None,
        )
        timestamps = _available_any_timestamps(timestamp_price_frame)
    timestamps = _trim_validation_timestamps(
        timestamps,
        [payloads[2] for payloads in candidate_payloads.values()],
    )
    if timestamps.empty:
        raise SystemExit("No evaluation timestamps after applying the configured start")

    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame] = {}
    candidate_returns: dict[str, pd.Series] = {}
    annualization: dict[str, float] = {}
    for candidate_label in sorted(candidate_payloads):
        payloads = candidate_payloads[candidate_label]
        universe_payload, weight_payload, vectorbt_payload, required_markets, features, warnings = payloads
        result = _run_v2_backtest_window(
            candle_dir, source_cache_dir, features, warnings, required_markets,
            universe_payload, weight_payload, vectorbt_payload, price_frame_cache,
            config.get("max_markets"), eval_start=timestamps.min(),
            eval_end=timestamps.max() + pd.Timedelta(nanoseconds=1), include_curves=True,
        )
        try:
            candidate_returns[candidate_label] = _returns_from_result(result, args.score_metric)
            annualization[candidate_label] = float(result["Periods Per Year"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"PBO candidate {candidate_label}: {exc}") from exc
        print(f"PBO returns {len(candidate_returns)}/{len(candidate_payloads)}: {candidate_label}", flush=True)

    return_frame = pd.concat(candidate_returns, axis=1, join="inner").sort_index()
    return_frame = return_frame.loc[return_frame.index.intersection(timestamps)]
    if return_frame.empty or not np.isfinite(return_frame.to_numpy()).all():
        raise SystemExit("PBO candidates have no common finite return observations")
    common_observations = len(return_frame)
    available_timestamps = timestamps
    timestamps = pd.DatetimeIndex(return_frame.index)
    periods_per_year = np.array([annualization[label] for label in return_frame.columns])
    if not np.isfinite(periods_per_year).all() or (periods_per_year <= 0).any():
        raise SystemExit("PBO candidates require positive finite periods-per-year")
    all_periods: list[dict[str, Any]] = []
    split_groups: list[tuple[int, list[dict[str, Any]], list[tuple[list[int], list[int]]]]] = []
    period_counts_by_anchor: dict[str, int] = {}
    split_counts_by_anchor: dict[str, int] = {}
    coverage_by_anchor: dict[str, dict[str, Any]] = {}
    for anchor_offset in anchor_offsets:
        anchor_periods = _build_subperiods(timestamps, args.subperiod_months, anchor_month_offset=anchor_offset)
        if len(anchor_periods) < 4:
            continue
        if len(anchor_periods) % 2 == 1:
            anchor_periods = anchor_periods[:-1]
        anchor_splits = _build_cscv_splits(
            [period["period_id"] for period in anchor_periods],
            args.max_splits,
            args.random_seed + anchor_offset,
        )
        if not anchor_splits:
            continue
        all_periods.extend(anchor_periods)
        split_groups.append((anchor_offset, anchor_periods, anchor_splits))
        period_counts_by_anchor[str(anchor_offset)] = int(len(anchor_periods))
        split_counts_by_anchor[str(anchor_offset)] = int(len(anchor_splits))
        covered = (timestamps >= anchor_periods[0]["start"]) & (timestamps < anchor_periods[-1]["end"])
        coverage_by_anchor[str(anchor_offset)] = {
            "start": timestamps[covered].min().isoformat(),
            "end": timestamps[covered].max().isoformat(),
            "observations": int(covered.sum()),
            "excluded_head_observations": int((timestamps < anchor_periods[0]["start"]).sum()),
            "excluded_tail_observations": int((timestamps >= anchor_periods[-1]["end"]).sum()),
            "observations_by_period": [period["rows"] for period in anchor_periods],
        }
    if not split_groups:
        raise SystemExit("Need at least one anchor with 4+ non-overlapping subperiods for CSCV/PBO")

    score_matrix = pd.DataFrame(
        index=[period["period_id"] for period in all_periods],
        columns=sorted(candidate_payloads.keys()),
        dtype=float,
    )
    period_rows: list[dict[str, Any]] = []

    for period in all_periods:
        block = return_frame.loc[(timestamps >= period["start"]) & (timestamps < period["end"])]
        scores = _score_returns(block.to_numpy(), args.score_metric, periods_per_year)
        for candidate_label, score in zip(return_frame.columns, scores):
            score_matrix.loc[period["period_id"], candidate_label] = score
            period_rows.append(
                {
                    "candidate_label": candidate_label,
                    "period_id": period["period_id"],
                    "period_start": period["start"].isoformat(),
                    "period_end": period["end"].isoformat(),
                    "anchor_month_offset": period["anchor_month_offset"],
                    "score_metric": args.score_metric,
                    "score": score,
                    "status": "ok" if np.isfinite(score) else "undefined_metric",
                    "observations": len(block),
                    "score_role": "diagnostic_only_not_averaged_for_splits",
                }
            )

    split_rows: list[dict[str, Any]] = []
    candidate_selection_counts: dict[str, int] = {}
    candidate_percentiles: dict[str, list[float]] = {}
    candidate_lambdas: dict[str, list[float]] = {}
    split_sequence = 1

    for anchor_offset, anchor_periods, anchor_splits in split_groups:
        blocks = [return_frame.loc[(timestamps >= period["start"]) & (timestamps < period["end"])].to_numpy()
                  for period in anchor_periods]
        scorer = _ReturnBlockScorer(blocks, args.score_metric, periods_per_year)
        for is_idx, oos_idx in anchor_splits:
            is_period_ids = [anchor_periods[idx]["period_id"] for idx in is_idx]
            oos_period_ids = [anchor_periods[idx]["period_id"] for idx in oos_idx]
            is_scores = pd.Series(scorer.score(is_idx), index=return_frame.columns)
            oos_scores = pd.Series(scorer.score(oos_idx), index=return_frame.columns)
            try:
                selection = _split_selection(is_scores, oos_scores)
            except ValueError as exc:
                raise ValueError(f"anchor={anchor_offset}, IS={is_period_ids}, OOS={oos_period_ids}: {exc}") from exc
            is_best_label = selection["is_best_label"]
            oos_percentile = selection["oos_rank_percentile"]
            lambda_value = selection["lambda"]

            candidate_selection_counts[is_best_label] = candidate_selection_counts.get(is_best_label, 0) + 1
            candidate_percentiles.setdefault(is_best_label, []).append(oos_percentile)
            candidate_lambdas.setdefault(is_best_label, []).append(lambda_value)

            split_rows.append(
                {
                    "split_id": f"split_{split_sequence:04d}",
                    "anchor_month_offset": int(anchor_offset),
                    "is_period_ids": json.dumps(is_period_ids),
                    "oos_period_ids": json.dumps(oos_period_ids),
                    "is_observations": sum(len(blocks[i]) for i in is_idx),
                    "oos_observations": sum(len(blocks[i]) for i in oos_idx),
                    **selection,
                }
            )
            split_sequence += 1

    split_frame = pd.DataFrame(split_rows)
    if split_frame.empty:
        raise SystemExit("No valid CSCV split results were produced")

    pbo = float(split_frame["is_overfit"].mean())
    candidate_summary_rows: list[dict[str, Any]] = []
    for candidate_label in score_matrix.columns:
        percentiles = candidate_percentiles.get(candidate_label, [])
        lambdas = candidate_lambdas.get(candidate_label, [])
        candidate_summary_rows.append(
            {
                "candidate_label": candidate_label,
                "selection_count": int(candidate_selection_counts.get(candidate_label, 0)),
                "selection_share": candidate_selection_counts.get(candidate_label, 0) / len(split_frame),
                "mean_oos_rank_percentile": float(np.mean(percentiles)) if percentiles else float("nan"),
                "median_oos_rank_percentile": float(np.median(percentiles)) if percentiles else float("nan"),
                "oos_below_median_ratio": float(np.mean(np.array(percentiles) < 0.5)) if percentiles else float("nan"),
                "oos_at_or_below_median_ratio": float(np.mean(np.array(percentiles) <= 0.5)) if percentiles else float("nan"),
                "mean_lambda": float(np.mean(lambdas)) if lambdas else float("nan"),
            }
        )
    candidate_summary = pd.DataFrame(candidate_summary_rows).sort_values(
        ["selection_count", "mean_oos_rank_percentile"],
        ascending=[False, False],
    )

    summary = {
        "method_version": 2,
        "score_aggregation": "metric_on_concatenated_returns",
        "return_input": "benchmark_excess_returns" if args.score_metric == "air" else "strategy_returns",
        "split_directions": "both",
        "rank_normalization": "ascending_average_rank / (candidate_count + 1)",
        "pbo_event": "lambda <= 0 (OOS rank at or below median)",
        "partition_method": "calendar_months_not_equal_observation_blocks",
        "period_score_matrix_role": "diagnostic_only",
        "candidate_tie_break": "first_label_in_sorted_order",
        "undefined_split_score_policy": "error_no_candidate_dropping",
        "max_splits": args.max_splits,
        "random_seed": args.random_seed,
        "grid_config_json": str(config_path),
        "score_metric": args.score_metric,
        "subperiod_months": args.subperiod_months,
        "anchor_month_offsets": anchor_offsets,
        "timestamp_basis_markets": timestamp_basis_markets,
        "period_count": int(len(all_periods)),
        "period_counts_by_anchor": period_counts_by_anchor,
        "candidate_count": int(score_matrix.shape[1]),
        "split_count": int(len(split_frame)),
        "split_counts_by_anchor": split_counts_by_anchor,
        "coverage_by_anchor": coverage_by_anchor,
        "common_return_start": timestamps.min().isoformat(),
        "common_return_end": timestamps.max().isoformat(),
        "common_return_observations": common_observations,
        "timestamp_basis_observations_excluded_by_alignment": int(len(available_timestamps) - common_observations),
        "candidate_return_observations": {label: len(values) for label, values in candidate_returns.items()},
        "periods_per_year_by_candidate": annualization,
        "pbo": pbo,
        "lambda_negative_count": int((split_frame["lambda"] < 0).sum()),
        "lambda_zero_count": int((split_frame["lambda"] == 0).sum()),
        "lambda_nonpositive_count": int(split_frame["is_overfit"].sum()),
        "pbo_by_anchor": {str(anchor): float(group["is_overfit"].mean())
                          for anchor, group in split_frame.groupby("anchor_month_offset")},
        "mean_lambda": float(split_frame["lambda"].mean()),
        "median_lambda": float(split_frame["lambda"].median()),
        "mean_oos_rank_percentile": float(split_frame["oos_rank_percentile"].mean()),
        "median_oos_rank_percentile": float(split_frame["oos_rank_percentile"].median()),
        "top_is_best_label": str(split_frame["is_best_label"].value_counts().idxmax()),
        "top_is_best_count": int(split_frame["is_best_label"].value_counts().iloc[0]),
        "primary_market": primary_market,
    }

    score_matrix.to_parquet(out_dir / "pbo_period_score_matrix.parquet")
    pd.DataFrame(period_rows).to_csv(out_dir / "pbo_period_scores_long.csv", index=False)
    split_frame.to_csv(out_dir / "pbo_split_results.csv", index=False)
    candidate_summary.to_csv(out_dir / "pbo_candidate_summary.csv", index=False)
    (out_dir / "pbo_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    try:
        display_out_dir = out_dir.resolve().relative_to(ROOT_DIR.resolve())
    except Exception:
        display_out_dir = out_dir
    print(f"Wrote PBO analysis to {display_out_dir}")
    print(f"PBO: {pbo:.6%} ({len(split_frame)} splits, both directions, median included)")
    for anchor, coverage in coverage_by_anchor.items():
        print(f"Anchor {anchor}: {coverage['start']} .. {coverage['end']}; "
              f"excluded head/tail rows={coverage['excluded_head_observations']}/{coverage['excluded_tail_observations']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
