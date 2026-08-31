#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from arch.bootstrap import StationaryBootstrap, optimal_block_length


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate uncertainty for selected v2 backtest candidates using paired "
            "stationary block bootstrap."
        )
    )
    parser.add_argument(
        "--backtest-dir",
        action="append",
        required=True,
        help=(
            "Candidate backtest directory containing equity_curve.csv, benchmark_curve.csv, "
            "and summary.csv. Repeat for multiple candidates."
        ),
    )
    parser.add_argument(
        "--bootstrap-reps",
        type=int,
        default=1000,
        help="Number of stationary bootstrap replications per candidate",
    )
    parser.add_argument(
        "--block-size",
        default="auto",
        help="Positive integer or 'auto' to use the median optimal stationary block length",
    )
    parser.add_argument("--random-seed", type=int, default=42, help="Random seed")
    parser.add_argument("--start-date", help="Optional inclusive analysis start date")
    parser.add_argument("--end-date", help="Optional inclusive analysis end date")
    parser.add_argument(
        "--out-dir",
        help=(
            "Output directory. By default uses data/research/uncertainty/<grid_name>; "
            "all candidates must then belong to the same grid."
        ),
    )
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _read_curve(path: Path, label: str) -> pd.Series:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label} curve: {path}")
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    if frame.empty or frame.shape[1] != 1:
        raise ValueError(f"{label} curve must contain exactly one value column: {path}")
    curve = pd.to_numeric(frame.iloc[:, 0], errors="coerce").dropna().sort_index()
    if len(curve) < 3:
        raise ValueError(f"{label} curve needs at least 3 observations: {path}")
    if curve.index.has_duplicates:
        raise ValueError(f"{label} curve index contains duplicate timestamps: {path}")
    if (curve <= 0.0).any():
        raise ValueError(f"{label} curve must remain positive: {path}")
    curve.name = label
    return curve


def _read_summary(path: Path) -> pd.Series:
    if not path.exists():
        raise FileNotFoundError(f"Missing backtest summary: {path}")
    frame = pd.read_csv(path, index_col=0)
    if frame.empty or frame.shape[1] != 1:
        raise ValueError(f"Backtest summary must contain exactly one value column: {path}")
    return frame.iloc[:, 0]


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if math.isfinite(result) else float("nan")


def _annualization_factor(summary: pd.Series) -> int:
    value = _safe_float(summary.get("Periods Per Year"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("summary.csv must contain a positive 'Periods Per Year'")
    return int(round(value))


def _aligned_returns(
    backtest_dir: Path,
    *,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    strategy_curve = _read_curve(backtest_dir / "equity_curve.csv", "strategy")
    benchmark_curve = _read_curve(backtest_dir / "benchmark_curve.csv", "benchmark")
    aligned = pd.concat([strategy_curve, benchmark_curve], axis=1, join="inner").dropna()
    if start_date is not None:
        aligned = aligned.loc[aligned.index >= start_date]
    if end_date is not None:
        aligned = aligned.loc[aligned.index <= end_date]
    if len(aligned) < 3:
        raise ValueError(f"Strategy and benchmark have insufficient common observations: {backtest_dir}")
    boundary_tolerance = pd.Timedelta(days=7)
    if start_date is not None and aligned.index.min() > start_date + boundary_tolerance:
        raise ValueError(
            f"Common data starts at {aligned.index.min().date()}, later than requested {start_date.date()}: "
            f"{backtest_dir}"
        )
    if end_date is not None and aligned.index.max() < end_date - boundary_tolerance:
        raise ValueError(
            f"Common data ends at {aligned.index.max().date()}, earlier than requested {end_date.date()}: "
            f"{backtest_dir}"
        )
    returns = aligned.pct_change().dropna()
    if returns.empty or not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise ValueError(f"Aligned returns contain invalid values: {backtest_dir}")
    if (returns <= -1.0).any(axis=None):
        raise ValueError(f"Returns at or below -100% cannot be compounded: {backtest_dir}")
    return returns, aligned


def _resolve_block_size(returns: pd.DataFrame, requested: str) -> tuple[int, float | None]:
    if requested.lower() != "auto":
        block_size = int(requested)
        if block_size <= 0:
            raise ValueError("block-size must be positive")
        return block_size, None

    block_table = optimal_block_length(returns)
    raw_values = pd.to_numeric(block_table["stationary"], errors="coerce").dropna()
    if raw_values.empty:
        return 1, None
    raw_median = float(raw_values.median())
    return max(1, int(round(raw_median))), raw_median


def _cagr_from_returns(returns: np.ndarray, annualization_factor: int) -> float:
    total_factor = float(np.prod(1.0 + returns))
    if total_factor <= 0.0:
        return float("nan")
    return float(total_factor ** (annualization_factor / len(returns)) - 1.0)


def _max_drawdown_loss_pct(returns: np.ndarray) -> float:
    curve = np.concatenate(([1.0], np.cumprod(1.0 + returns)))
    running_max = np.maximum.accumulate(curve)
    return float(-np.min((curve / running_max) - 1.0) * 100.0)


def _longest_drawdown_duration_bars(returns: np.ndarray) -> int:
    curve = np.concatenate(([1.0], np.cumprod(1.0 + returns)))
    peak = float(curve[0])
    current_duration = 0
    longest_duration = 0
    for value in curve[1:]:
        current_value = float(value)
        if current_value >= peak:
            if current_duration > 0:
                longest_duration = max(longest_duration, current_duration + 1)
            peak = current_value
            current_duration = 0
        else:
            current_duration += 1
            longest_duration = max(longest_duration, current_duration)
    return longest_duration


def _observed_stats(aligned: pd.DataFrame, annualization_factor: int) -> dict[str, float]:
    strategy_returns = aligned["strategy"].pct_change().dropna().to_numpy(dtype=float)
    benchmark_returns = aligned["benchmark"].pct_change().dropna().to_numpy(dtype=float)
    strategy_cagr = _cagr_from_returns(strategy_returns, annualization_factor)
    benchmark_cagr = _cagr_from_returns(benchmark_returns, annualization_factor)
    return {
        "Observed Strategy CAGR [%]": strategy_cagr * 100.0,
        "Observed Benchmark CAGR [%]": benchmark_cagr * 100.0,
        "Observed Excess CAGR [pp]": (strategy_cagr - benchmark_cagr) * 100.0,
        "Observed Strategy MDD Loss [%]": _max_drawdown_loss_pct(strategy_returns),
        "Observed Strategy Longest Drawdown Duration Bars": float(
            _longest_drawdown_duration_bars(strategy_returns)
        ),
    }


def compute_bootstrap_uncertainty(
    returns: pd.DataFrame,
    *,
    annualization_factor: int,
    bootstrap_reps: int,
    block_size: int,
    random_seed: int,
) -> dict[str, float]:
    if bootstrap_reps <= 0:
        raise ValueError("bootstrap-reps must be positive")

    excess_cagrs = np.empty(bootstrap_reps, dtype=float)
    strategy_cagrs = np.empty(bootstrap_reps, dtype=float)
    mdd_losses = np.empty(bootstrap_reps, dtype=float)
    drawdown_durations = np.empty(bootstrap_reps, dtype=float)
    underperformed = np.empty(bootstrap_reps, dtype=bool)

    bootstrap = StationaryBootstrap(block_size, returns, seed=random_seed)
    for idx, (positional, _) in enumerate(bootstrap.bootstrap(bootstrap_reps)):
        sample = positional[0]
        sample_values = sample.to_numpy(dtype=float) if isinstance(sample, pd.DataFrame) else np.asarray(sample)
        strategy_sample = sample_values[:, 0]
        benchmark_sample = sample_values[:, 1]
        strategy_cagr = _cagr_from_returns(strategy_sample, annualization_factor)
        benchmark_cagr = _cagr_from_returns(benchmark_sample, annualization_factor)
        strategy_total_factor = float(np.prod(1.0 + strategy_sample))
        benchmark_total_factor = float(np.prod(1.0 + benchmark_sample))

        strategy_cagrs[idx] = strategy_cagr * 100.0
        excess_cagrs[idx] = (strategy_cagr - benchmark_cagr) * 100.0
        mdd_losses[idx] = _max_drawdown_loss_pct(strategy_sample)
        drawdown_durations[idx] = _longest_drawdown_duration_bars(strategy_sample)
        underperformed[idx] = strategy_total_factor < benchmark_total_factor

    return {
        "Bootstrap Strategy CAGR P05 [%]": float(np.quantile(strategy_cagrs, 0.05)),
        "Bootstrap Excess CAGR P05 [pp]": float(np.quantile(excess_cagrs, 0.05)),
        "Bootstrap Benchmark Underperformance Probability [%]": float(underperformed.mean() * 100.0),
        "Bootstrap MDD Loss P95 [%]": float(np.quantile(mdd_losses, 0.95)),
        "Bootstrap Drawdown Duration P95 Bars": float(np.quantile(drawdown_durations, 0.95)),
    }


def _infer_grid_name(backtest_dir: Path) -> str:
    if backtest_dir.name.lower() != "backtest" or len(backtest_dir.parents) < 2:
        raise ValueError(
            "Cannot infer grid name; expected path ending in <grid>/<candidate>/backtest. "
            "Provide --out-dir explicitly."
        )
    return backtest_dir.parent.parent.name


def _default_out_dir(backtest_dirs: list[Path]) -> Path:
    grid_names = {_infer_grid_name(path) for path in backtest_dirs}
    if len(grid_names) != 1:
        raise ValueError("Default output requires all candidates to belong to the same grid; provide --out-dir")
    return ROOT_DIR / "data" / "research" / "uncertainty" / next(iter(grid_names))


def main() -> int:
    args = build_parser().parse_args()
    if args.bootstrap_reps <= 0:
        raise SystemExit("bootstrap-reps must be positive")

    start_date = pd.Timestamp(args.start_date).normalize() if args.start_date else None
    end_date = pd.Timestamp(args.end_date).normalize() if args.end_date else None
    if start_date is not None and end_date is not None and start_date > end_date:
        raise SystemExit("start-date must not be after end-date")

    backtest_dirs = [_resolve_path(value) for value in args.backtest_dir]
    out_dir = _resolve_path(args.out_dir) if args.out_dir else _default_out_dir(backtest_dirs)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    metadata_candidates: list[dict[str, Any]] = []
    for candidate_index, backtest_dir in enumerate(backtest_dirs):
        if not backtest_dir.is_dir():
            raise FileNotFoundError(f"Backtest directory does not exist: {backtest_dir}")
        summary = _read_summary(backtest_dir / "summary.csv")
        annualization_factor = _annualization_factor(summary)
        returns, aligned = _aligned_returns(
            backtest_dir,
            start_date=start_date,
            end_date=end_date,
        )
        block_size, auto_block_size_raw = _resolve_block_size(returns, str(args.block_size))
        candidate_seed = int(args.random_seed) + candidate_index
        candidate_label = backtest_dir.parent.name
        benchmark_label = str(summary.get("Benchmark Market", ""))

        row: dict[str, Any] = {
            "candidate_label": candidate_label,
            "source_backtest_dir": str(backtest_dir),
            "benchmark_label": benchmark_label,
            "benchmark_source": "backtest_artifact",
            "start": pd.Timestamp(aligned.index[0]).isoformat(),
            "end": pd.Timestamp(aligned.index[-1]).isoformat(),
            "common_observations": int(len(aligned)),
            "return_observations": int(len(returns)),
            "annualization_factor": int(annualization_factor),
            "bootstrap_reps": int(args.bootstrap_reps),
            "block_size": int(block_size),
            "block_size_mode": "auto" if str(args.block_size).lower() == "auto" else "manual",
            "random_seed": candidate_seed,
        }
        row.update(_observed_stats(aligned, annualization_factor))
        row.update(
            compute_bootstrap_uncertainty(
                returns,
                annualization_factor=annualization_factor,
                bootstrap_reps=int(args.bootstrap_reps),
                block_size=block_size,
                random_seed=candidate_seed,
            )
        )
        rows.append(row)
        metadata_candidates.append(
            {
                "candidate_label": candidate_label,
                "source_backtest_dir": str(backtest_dir),
                "equity_curve_csv": str(backtest_dir / "equity_curve.csv"),
                "benchmark_source": "backtest_artifact",
                "benchmark_curve_csv": str(backtest_dir / "benchmark_curve.csv"),
                "benchmark_label": benchmark_label,
                "summary_csv": str(backtest_dir / "summary.csv"),
                "auto_block_size_raw_stationary_median": auto_block_size_raw,
                "resolved_block_size": block_size,
                "random_seed": candidate_seed,
            }
        )

    summary_path = out_dir / "candidate_uncertainty_summary.csv"
    pd.DataFrame(rows).to_csv(summary_path, index=False, encoding="utf-8-sig")
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "paired_stationary_block_bootstrap",
        "candidate_count": len(rows),
        "bootstrap_reps": int(args.bootstrap_reps),
        "requested_block_size": str(args.block_size),
        "base_random_seed": int(args.random_seed),
        "requested_start_date": start_date.date().isoformat() if start_date is not None else None,
        "requested_end_date": end_date.date().isoformat() if end_date is not None else None,
        "benchmark": {"source": "backtest_artifact"},
        "definitions": {
            "Bootstrap Strategy CAGR P05 [%]": "5th percentile of bootstrapped strategy CAGR",
            "Bootstrap Excess CAGR P05 [pp]": (
                "5th percentile of strategy CAGR minus benchmark CAGR in percentage points"
            ),
            "Bootstrap Benchmark Underperformance Probability [%]": (
                "Share of samples where terminal strategy wealth is below terminal benchmark wealth"
            ),
            "Bootstrap MDD Loss P95 [%]": "95th percentile of positive maximum drawdown magnitude",
            "Bootstrap Drawdown Duration P95 Bars": (
                "95th percentile of the longest peak-to-recovery episode, including an unresolved final episode"
            ),
        },
        "candidates": metadata_candidates,
        "output_summary_csv": str(summary_path),
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote uncertainty analysis for {len(rows)} candidate(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
