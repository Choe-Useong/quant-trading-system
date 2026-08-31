#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from arch.bootstrap import StepM, optimal_block_length


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run arch.bootstrap.StepM on an excess return matrix.")
    parser.add_argument("--excess-return-matrix-parquet", required=True, help="Path to T x K excess return matrix parquet")
    parser.add_argument("--size", type=float, default=0.05, help="StepM size, e.g. 0.05")
    parser.add_argument(
        "--block-size",
        default="auto",
        help="Bootstrap block size. Use an integer or 'auto' (default) to use arch optimal stationary block length.",
    )
    parser.add_argument("--bootstrap-reps", type=int, default=3000, help="Number of bootstrap replications")
    parser.add_argument(
        "--bootstrap-type",
        default="stationary",
        choices=["stationary", "sb", "circular", "cbb", "moving block", "mbb"],
        help="Bootstrap scheme to pass to arch.bootstrap.StepM",
    )
    parser.add_argument("--random-seed", type=int, default=42, help="Random seed")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    matrix_path = Path(args.excess_return_matrix_parquet)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    excess = pd.read_parquet(matrix_path)
    if excess.empty or excess.shape[1] < 2:
        raise SystemExit("Need a non-empty excess return matrix with at least 2 candidates")

    if str(args.block_size).lower() == "auto":
        block_table = optimal_block_length(excess)
        auto_block_size_raw = float(block_table["stationary"].median())
        block_size = max(1, int(round(auto_block_size_raw)))
    else:
        auto_block_size_raw = None
        block_size = int(args.block_size)
        if block_size <= 0:
            raise SystemExit("block-size must be positive")

    benchmark_losses = pd.Series(0.0, index=excess.index, name="benchmark")
    model_losses = -excess

    stepm = StepM(
        benchmark_losses,
        model_losses,
        size=args.size,
        block_size=block_size,
        reps=args.bootstrap_reps,
        bootstrap=args.bootstrap_type,
        studentize=True,
        nested=False,
        seed=args.random_seed,
    )
    stepm.compute()
    superior = [str(value) for value in list(stepm.superior_models)]

    candidate_stats = pd.DataFrame(
        {
            "candidate_label": list(excess.columns),
            "mean_excess_return_per_bar": excess.mean(axis=0).to_numpy(dtype=float),
            "std_excess_return_per_bar": excess.std(axis=0, ddof=0).to_numpy(dtype=float),
            "is_superior_model": [label in superior for label in excess.columns],
        }
    ).sort_values(["is_superior_model", "mean_excess_return_per_bar"], ascending=[False, False])

    summary = {
        "excess_return_matrix_parquet": str(matrix_path),
        "candidate_count": int(excess.shape[1]),
        "common_sample_bars": int(excess.shape[0]),
        "size": float(args.size),
        "bootstrap_reps": int(args.bootstrap_reps),
        "block_size": int(block_size),
        "block_size_mode": "auto" if auto_block_size_raw is not None else "manual",
        "auto_block_size_raw_stationary_median": auto_block_size_raw,
        "bootstrap_type": str(args.bootstrap_type),
        "superior_model_count": int(len(superior)),
        "superior_models": superior,
    }

    (out_dir / "stepm_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    candidate_stats.to_csv(out_dir / "stepm_candidate_stats.csv", index=False, encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
