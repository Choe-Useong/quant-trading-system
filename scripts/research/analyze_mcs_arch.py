#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from arch.bootstrap import MCS, optimal_block_length


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run arch.bootstrap.MCS on an excess return matrix.")
    parser.add_argument("--excess-return-matrix-parquet", required=True, help="Path to T x K excess return matrix parquet")
    parser.add_argument("--size", type=float, default=0.05, help="MCS test size, e.g. 0.05")
    parser.add_argument("--method", default="R", choices=["R", "max"], help="MCS elimination method")
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
        help="Bootstrap scheme to pass to arch.bootstrap.MCS",
    )
    parser.add_argument("--random-seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--drop-duplicate-columns",
        action="store_true",
        help="Drop candidates with exactly duplicated loss series before running MCS.",
    )
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

    duplicate_count = int(excess.T.duplicated().sum())
    if args.drop_duplicate_columns:
        keep_mask = ~excess.T.duplicated()
        excess = excess.loc[:, keep_mask.to_numpy()]
        if excess.shape[1] < 2:
            raise SystemExit("Need at least 2 unique candidates after dropping duplicate columns")

    if str(args.block_size).lower() == "auto":
        block_table = optimal_block_length(excess)
        auto_block_size_raw = float(block_table["stationary"].median())
        block_size = max(1, int(round(auto_block_size_raw)))
    else:
        auto_block_size_raw = None
        block_size = int(args.block_size)
        if block_size <= 0:
            raise SystemExit("block-size must be positive")

    losses = -excess
    mcs = MCS(
        losses,
        size=args.size,
        reps=args.bootstrap_reps,
        block_size=block_size,
        method=args.method,
        bootstrap=args.bootstrap_type,
        seed=args.random_seed,
    )
    mcs.compute()

    included = [str(value) for value in list(mcs.included)]
    excluded = [str(value) for value in list(mcs.excluded)]
    pvalues = mcs.pvalues.copy()
    if isinstance(pvalues, pd.DataFrame):
        pvalues = pvalues.reset_index()
        if "model" not in pvalues.columns:
            first_col = pvalues.columns[0]
            pvalues = pvalues.rename(columns={first_col: "model"})
    else:
        pvalues = pd.DataFrame({"model": list(excess.columns), "pvalue": list(pvalues)})

    summary = {
        "excess_return_matrix_parquet": str(matrix_path),
        "candidate_count": int(excess.shape[1]),
        "duplicate_columns_removed": int(duplicate_count if args.drop_duplicate_columns else 0),
        "duplicate_columns_detected": int(duplicate_count),
        "common_sample_bars": int(excess.shape[0]),
        "size": float(args.size),
        "method": str(args.method),
        "bootstrap_reps": int(args.bootstrap_reps),
        "block_size": int(block_size),
        "block_size_mode": "auto" if auto_block_size_raw is not None else "manual",
        "auto_block_size_raw_stationary_median": auto_block_size_raw,
        "bootstrap_type": str(args.bootstrap_type),
        "included_count": int(len(included)),
        "excluded_count": int(len(excluded)),
        "included_models": included,
        "excluded_models": excluded,
    }

    candidate_stats = pd.DataFrame(
        {
            "candidate_label": list(excess.columns),
            "mean_excess_return_per_bar": excess.mean(axis=0).to_numpy(dtype=float),
            "std_excess_return_per_bar": excess.std(axis=0, ddof=0).to_numpy(dtype=float),
            "is_included": [label in included for label in excess.columns],
        }
    ).sort_values(["is_included", "mean_excess_return_per_bar"], ascending=[False, False])

    (out_dir / "mcs_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    candidate_stats.to_csv(out_dir / "mcs_candidate_stats.csv", index=False, encoding="utf-8-sig")
    pvalues.to_csv(out_dir / "mcs_pvalues.csv", index=False, encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
