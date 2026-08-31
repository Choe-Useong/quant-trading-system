#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.research.analyze_pbo import _build_candidate_frames
from scripts.walkforward.run_cross_section_grid_walkforward_v2 import (
    _available_any_timestamps,
    _available_timestamps,
    _run_v2_backtest_window,
)
from scripts.run_vectorbt import (
    compute_return_series,
    infer_periods_per_year,
    infer_timeframe,
    load_price_frame,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a common-sample excess return matrix for a v2 grid family.")
    parser.add_argument("--grid-config-json", required=True, help="Grid config JSON path")
    parser.add_argument("--out-dir", help="Directory for outputs")
    return parser


def _default_out_dir(grid_config_json: Path) -> Path:
    return ROOT_DIR / "data" / "research" / "spa" / grid_config_json.stem


def _annualized_excess_return_pct(mean_excess_per_bar: float, periods_per_year: int) -> float:
    if not pd.notna(mean_excess_per_bar):
        return float("nan")
    return mean_excess_per_bar * periods_per_year * 100.0


def _annualized_excess_sharpe(excess_returns: pd.Series, periods_per_year: int) -> float:
    clean = pd.to_numeric(excess_returns, errors="coerce").dropna()
    if clean.empty:
        return float("nan")
    std = float(clean.std(ddof=0))
    if std <= 0.0 or not pd.notna(std):
        return float("nan")
    mean = float(clean.mean())
    return (mean / std) * (periods_per_year ** 0.5)


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.grid_config_json)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir(config_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    candle_dir, source_cache_dir, primary_market, combinations, candidate_payloads = _build_candidate_frames(config)
    max_markets = config.get("max_markets")

    first_candidate = next(iter(candidate_payloads.values()))
    first_vectorbt_payload = first_candidate[2]
    benchmark_market = str(first_vectorbt_payload.get("benchmark_market", "KRW-BTC")).upper()

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
        timestamps = _available_any_timestamps(timestamp_price_frame)
    if timestamps.empty:
        raise SystemExit("No timestamps available for candidate family")
    eval_start = pd.Timestamp(timestamps.min())
    eval_end = pd.Timestamp(timestamps.max()) + pd.Timedelta(days=1)

    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame] = {}
    excess_return_series: dict[str, pd.Series] = {}
    candidate_rows: list[dict[str, Any]] = []

    timeframe = infer_timeframe(candle_dir, None)
    periods_per_year = infer_periods_per_year(timeframe)

    for candidate_label, payloads in sorted(candidate_payloads.items()):
        universe_payload, weight_payload, vectorbt_payload, required_feature_markets, feature_frames, warning_frame = payloads
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
            eval_start=eval_start,
            eval_end=eval_end,
            include_curves=True,
        )
        if result.get("status") != "ok":
            continue
        equity_curve = result["_equity_curve"]
        benchmark_curve = result["_benchmark_curve"]
        strategy_returns = compute_return_series(equity_curve)
        benchmark_returns = compute_return_series(benchmark_curve)
        excess_returns = strategy_returns.subtract(benchmark_returns, fill_value=float("nan")).dropna()
        if excess_returns.empty:
            continue
        excess_return_series[candidate_label] = excess_returns
        mean_excess = float(excess_returns.mean())
        candidate_rows.append(
            {
                "candidate_label": candidate_label,
                "candidate_full_sample_mean_excess_return_per_bar": mean_excess,
                "candidate_full_sample_annualized_excess_return_pct": _annualized_excess_return_pct(mean_excess, periods_per_year),
                "candidate_full_sample_annualized_excess_sharpe": _annualized_excess_sharpe(excess_returns, periods_per_year),
                "candidate_full_sample_information_ratio": float(result.get("Annualized Information Ratio", float("nan"))),
                "candidate_full_sample_sharpe": float(result.get("Sharpe Ratio", float("nan"))),
                "candidate_full_sample_cagr_pct": float(result.get("CAGR [%]", float("nan"))),
                "candidate_full_sample_mdd_pct": float(result.get("Max Drawdown [%]", float("nan"))),
                "candidate_full_sample_bars": int(len(excess_returns)),
            }
        )

    if len(excess_return_series) < 2:
        raise SystemExit("Need at least two valid candidates for SPA analysis")

    excess_frame = pd.concat(excess_return_series, axis=1, join="inner").sort_index()
    if excess_frame.empty:
        raise SystemExit("No common overlapping excess return history across candidates")

    candidate_labels = list(excess_frame.columns)
    sample_length = len(excess_frame)
    full_sample_lengths = {
        label: int(excess_return_series[label].shape[0]) for label in candidate_labels
    }
    min_full_sample_bars = min(full_sample_lengths.values())
    max_full_sample_bars = max(full_sample_lengths.values())
    common_sample_ratio_vs_min = sample_length / min_full_sample_bars if min_full_sample_bars else float("nan")
    common_sample_ratio_vs_max = sample_length / max_full_sample_bars if max_full_sample_bars else float("nan")

    candidate_summary = pd.DataFrame(candidate_rows)
    common_mean_excess = excess_frame.mean(axis=0)
    common_annualized_excess_return_pct = common_mean_excess * periods_per_year * 100.0
    common_annualized_excess_sharpe = excess_frame.apply(
        lambda series: _annualized_excess_sharpe(series, periods_per_year),
        axis=0,
    )
    candidate_summary["common_sample_bars"] = int(sample_length)
    candidate_summary["common_sample_ratio_vs_candidate_full_sample"] = candidate_summary["candidate_label"].map(
        {label: (sample_length / bars if bars else float("nan")) for label, bars in full_sample_lengths.items()}
    )
    candidate_summary["common_sample_mean_excess_return_per_bar"] = candidate_summary["candidate_label"].map(common_mean_excess)
    candidate_summary["common_sample_annualized_excess_return_pct"] = candidate_summary["candidate_label"].map(
        common_annualized_excess_return_pct
    )
    candidate_summary["common_sample_annualized_excess_sharpe"] = candidate_summary["candidate_label"].map(
        common_annualized_excess_sharpe
    )
    candidate_summary = candidate_summary.sort_values(
        ["common_sample_mean_excess_return_per_bar", "candidate_full_sample_mean_excess_return_per_bar"],
        ascending=[False, False],
    )

    observed_best_label = str(common_mean_excess.idxmax())
    observed_best_mean = float(common_mean_excess.loc[observed_best_label])

    summary = {
        "grid_config_json": str(config_path),
        "primary_market": primary_market,
        "benchmark_market": benchmark_market,
        "timeframe": timeframe,
        "periods_per_year": periods_per_year,
        "candidate_count": int(len(candidate_labels)),
        "common_sample_bars": int(sample_length),
        "min_candidate_full_sample_bars": int(min_full_sample_bars),
        "max_candidate_full_sample_bars": int(max_full_sample_bars),
        "common_sample_ratio_vs_min_full_sample": float(common_sample_ratio_vs_min),
        "common_sample_ratio_vs_max_full_sample": float(common_sample_ratio_vs_max),
        "observed_best_label_by_mean_excess": observed_best_label,
        "observed_best_mean_excess_return_per_bar": observed_best_mean,
        "observed_best_annualized_excess_return_pct": _annualized_excess_return_pct(observed_best_mean, periods_per_year),
    }

    excess_frame.to_parquet(out_dir / "spa_excess_return_matrix.parquet")
    candidate_summary.to_csv(out_dir / "spa_candidate_stats.csv", index=False)
    (out_dir / "spa_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    try:
        display_out_dir = out_dir.resolve().relative_to(ROOT_DIR.resolve())
    except Exception:
        display_out_dir = out_dir
    print(f"Wrote SPA matrix to {display_out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
