#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.storage import read_table_csv, write_table_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert dense target weight frame into sparse change-only weight rows."
    )
    parser.add_argument("--target-weights-csv", required=True, help="Dense target weight frame CSV")
    parser.add_argument("--weights-csv", required=True, help="Original sparse weights CSV for market metadata")
    parser.add_argument("--output-csv", required=True, help="Output sparse weights CSV with change-only rows")
    parser.add_argument(
        "--weights-name-suffix",
        default="_change_only",
        help="Suffix appended to weights_name in output rows",
    )
    return parser


def _metadata_by_market(weight_rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for row in weight_rows:
        market = row["market"]
        metadata.setdefault(
            market,
            {
                "korean_name": row["korean_name"],
                "english_name": row["english_name"],
                "market_warning": row["market_warning"],
                "feature_column": row["feature_column"],
                "gross_exposure": row["gross_exposure"],
                "weighting": row["weighting"],
                "rebalance_frequency": row["rebalance_frequency"],
                "weights_name": row["weights_name"],
                "universe_name": row["universe_name"],
            },
        )
    return metadata


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    target_frame = pd.read_csv(Path(args.target_weights_csv), encoding="utf-8-sig")
    if target_frame.empty:
        raise SystemExit(f"No target weights found in {args.target_weights_csv}")

    target_frame["date_utc"] = pd.to_datetime(target_frame["date_utc"])
    target_frame = target_frame.set_index("date_utc").sort_index()

    change_mask = target_frame.ne(target_frame.shift(1)).any(axis=1)
    if not change_mask.empty:
        change_mask.iloc[0] = True
    change_frame = target_frame.loc[change_mask]

    source_weight_rows = read_table_csv(Path(args.weights_csv))
    if not source_weight_rows:
        raise SystemExit(f"No weight rows found in {args.weights_csv}")
    metadata = _metadata_by_market(source_weight_rows)

    output_rows: list[dict[str, str]] = []
    fallback_market = sorted(metadata.keys())[0]
    for timestamp, weights in change_frame.iterrows():
        date_utc = timestamp.isoformat()
        date_kst = (timestamp + pd.Timedelta(hours=9)).isoformat()
        nonzero = weights[weights > 0]
        rank = 1
        if nonzero.empty:
            market_meta = metadata[fallback_market]
            output_rows.append(
                {
                    "date_utc": date_utc,
                    "date_kst": date_kst,
                    "market": fallback_market,
                    "korean_name": market_meta["korean_name"],
                    "english_name": market_meta["english_name"],
                    "market_warning": market_meta["market_warning"],
                    "feature_column": market_meta["feature_column"],
                    "feature_value": "0",
                    "rank": "1",
                    "selected_rank": "1",
                    "weight_rank": "1",
                    "target_weight": "0",
                    "gross_exposure": market_meta["gross_exposure"],
                    "weighting": market_meta["weighting"],
                    "rebalance_frequency": "sparse",
                    "weights_name": f"{market_meta['weights_name']}{args.weights_name_suffix}",
                    "universe_name": market_meta["universe_name"],
                }
            )
            continue
        for market, target_weight in nonzero.sort_index().items():
            market_meta = metadata[market]
            output_rows.append(
                {
                    "date_utc": date_utc,
                    "date_kst": date_kst,
                    "market": market,
                    "korean_name": market_meta["korean_name"],
                    "english_name": market_meta["english_name"],
                    "market_warning": market_meta["market_warning"],
                    "feature_column": market_meta["feature_column"],
                    "feature_value": f"{float(target_weight):.12g}",
                    "rank": str(rank),
                    "selected_rank": str(rank),
                    "weight_rank": str(rank),
                    "target_weight": f"{float(target_weight):.12g}",
                    "gross_exposure": market_meta["gross_exposure"],
                    "weighting": market_meta["weighting"],
                    "rebalance_frequency": "sparse",
                    "weights_name": f"{market_meta['weights_name']}{args.weights_name_suffix}",
                    "universe_name": market_meta["universe_name"],
                }
            )
            rank += 1

    write_table_csv(
        Path(args.output_csv),
        output_rows,
        columns=[
            "date_utc",
            "date_kst",
            "market",
            "korean_name",
            "english_name",
            "market_warning",
            "feature_column",
            "feature_value",
            "rank",
            "selected_rank",
            "weight_rank",
            "target_weight",
            "gross_exposure",
            "weighting",
            "rebalance_frequency",
            "weights_name",
            "universe_name",
        ],
    )
    print(f"Wrote {len(output_rows)} change-only weight rows to {args.output_csv}")


if __name__ == "__main__":
    main()
