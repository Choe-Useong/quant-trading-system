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
)
from scripts.run_vectorbt import load_price_frame


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
        help="Per-subperiod score used for IS/OOS ranking",
    )
    parser.add_argument(
        "--max-splits",
        type=int,
        default=0,
        help="Maximum CSCV splits to evaluate. 0 means use every available split.",
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


def _safe_float(value: Any) -> float:
    if value in ("", None):
        return float("nan")
    try:
        return float(value)
    except Exception:
        return float("nan")


def _score_from_result(result: dict[str, Any], metric: str) -> float:
    if result.get("status") != "ok":
        return float("nan")
    if metric == "air":
        return _safe_float(result.get("Annualized Information Ratio"))
    if metric == "sharpe":
        return _safe_float(result.get("Sharpe Ratio"))
    if metric == "cvar_sharpe":
        return _safe_float(result.get("CVaR 5% Sharpe"))
    if metric == "martin":
        return _safe_float(result.get("Martin Ratio"))
    if metric == "calmar":
        return _safe_float(result.get("Calmar Ratio"))
    start_value = _safe_float(result.get("Start Value"))
    end_value = _safe_float(result.get("End Value"))
    if not np.isfinite(start_value) or not np.isfinite(end_value) or start_value <= 0.0 or end_value <= 0.0:
        return float("nan")
    if metric == "log_return":
        return math.log(end_value / start_value)
    if metric == "simple_return":
        return (end_value / start_value) - 1.0
    raise ValueError(f"Unsupported score metric: {metric}")


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
        n_periods -= 1
    half = n_periods // 2
    all_is_combos = list(itertools.combinations(range(n_periods), half))
    if n_periods % 2 == 0:
        all_is_combos = [combo for combo in all_is_combos if 0 in combo]
    if max_splits and len(all_is_combos) > max_splits:
        rng = np.random.default_rng(random_seed)
        chosen = rng.choice(len(all_is_combos), size=max_splits, replace=False)
        all_is_combos = [all_is_combos[int(idx)] for idx in sorted(chosen.tolist())]

    all_indices = set(range(n_periods))
    splits: list[tuple[list[int], list[int]]] = []
    for combo in all_is_combos:
        is_idx = sorted(combo)
        oos_idx = sorted(all_indices - set(is_idx))
        splits.append((is_idx, oos_idx))
    return splits


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
    ranks = scores.rank(method="average", ascending=False)
    rank = float(ranks.loc[target_label])
    count = int(len(scores))
    return (count - rank + 0.5) / count


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
    config_path = Path(args.grid_config_json)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir(config_path, args.score_metric, args.subperiod_months)
    out_dir.mkdir(parents=True, exist_ok=True)
    anchor_offsets = _parse_anchor_offsets(args.anchor_month_offsets, args.subperiod_months)

    candle_dir, source_cache_dir, primary_market, combinations, candidate_payloads = _build_candidate_frames(config)
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
    all_periods: list[dict[str, Any]] = []
    split_groups: list[tuple[int, list[dict[str, Any]], list[tuple[list[int], list[int]]]]] = []
    period_counts_by_anchor: dict[str, int] = {}
    split_counts_by_anchor: dict[str, int] = {}
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
    if not split_groups:
        raise SystemExit("Need at least one anchor with 4+ non-overlapping subperiods for CSCV/PBO")

    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame] = {}
    max_markets = config.get("max_markets")

    score_matrix = pd.DataFrame(
        index=[period["period_id"] for period in all_periods],
        columns=sorted(candidate_payloads.keys()),
        dtype=float,
    )
    period_rows: list[dict[str, Any]] = []

    for candidate_label, payloads in candidate_payloads.items():
        universe_payload, weight_payload, vectorbt_payload, required_feature_markets, feature_frames, warning_frame = payloads
        for period in all_periods:
            result = _run_v2_backtest_window(
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
                eval_start=period["start"],
                eval_end=period["end"],
            )
            score = _score_from_result(result, args.score_metric)
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
                    "status": result.get("status", ""),
                    "Annualized Information Ratio": _safe_float(result.get("Annualized Information Ratio")),
                    "Sharpe Ratio": _safe_float(result.get("Sharpe Ratio")),
                    "CVaR 5% Sharpe": _safe_float(result.get("CVaR 5% Sharpe")),
                    "Martin Ratio": _safe_float(result.get("Martin Ratio")),
                    "Ulcer Index [%]": _safe_float(result.get("Ulcer Index [%]")),
                    "Calmar Ratio": _safe_float(result.get("Calmar Ratio")),
                    "CAGR [%]": _safe_float(result.get("CAGR [%]")),
                    "Max Drawdown [%]": _safe_float(result.get("Max Drawdown [%]")),
                    "Start Value": _safe_float(result.get("Start Value")),
                    "End Value": _safe_float(result.get("End Value")),
                }
            )

    split_rows: list[dict[str, Any]] = []
    candidate_selection_counts: dict[str, int] = {}
    candidate_percentiles: dict[str, list[float]] = {}
    candidate_lambdas: dict[str, list[float]] = {}
    split_sequence = 1

    for anchor_offset, anchor_periods, anchor_splits in split_groups:
        for is_idx, oos_idx in anchor_splits:
            is_period_ids = [anchor_periods[idx]["period_id"] for idx in is_idx]
            oos_period_ids = [anchor_periods[idx]["period_id"] for idx in oos_idx]
            is_scores = score_matrix.loc[is_period_ids].mean(axis=0, skipna=True)
            oos_scores = score_matrix.loc[oos_period_ids].mean(axis=0, skipna=True)
            valid = is_scores.notna() & oos_scores.notna()
            is_scores = is_scores.loc[valid]
            oos_scores = oos_scores.loc[valid]
            if len(is_scores) < 2:
                continue

            is_best_label = str(is_scores.idxmax())
            is_best_score = float(is_scores.loc[is_best_label])
            oos_percentile = float(_rank_percentile_desc(oos_scores, is_best_label))
            clipped_percentile = min(max(oos_percentile, 1e-12), 1.0 - 1e-12)
            lambda_value = math.log(clipped_percentile / (1.0 - clipped_percentile))
            oos_rank_desc = int(oos_scores.rank(method="average", ascending=False).loc[is_best_label])

            candidate_selection_counts[is_best_label] = candidate_selection_counts.get(is_best_label, 0) + 1
            candidate_percentiles.setdefault(is_best_label, []).append(oos_percentile)
            candidate_lambdas.setdefault(is_best_label, []).append(lambda_value)

            split_rows.append(
                {
                    "split_id": f"split_{split_sequence:04d}",
                    "anchor_month_offset": int(anchor_offset),
                    "is_period_ids": json.dumps(is_period_ids),
                    "oos_period_ids": json.dumps(oos_period_ids),
                    "candidate_count": int(len(is_scores)),
                    "is_best_label": is_best_label,
                    "is_best_score": is_best_score,
                    "oos_score_for_is_best": float(oos_scores.loc[is_best_label]),
                    "oos_rank_desc": oos_rank_desc,
                    "oos_rank_percentile": oos_percentile,
                    "lambda": lambda_value,
                }
            )
            split_sequence += 1

    split_frame = pd.DataFrame(split_rows)
    if split_frame.empty:
        raise SystemExit("No valid CSCV split results were produced")

    pbo = float((split_frame["lambda"] < 0).mean())
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
                "mean_lambda": float(np.mean(lambdas)) if lambdas else float("nan"),
            }
        )
    candidate_summary = pd.DataFrame(candidate_summary_rows).sort_values(
        ["selection_count", "mean_oos_rank_percentile"],
        ascending=[False, False],
    )

    summary = {
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
        "pbo": pbo,
        "lambda_negative_count": int((split_frame["lambda"] < 0).sum()),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
