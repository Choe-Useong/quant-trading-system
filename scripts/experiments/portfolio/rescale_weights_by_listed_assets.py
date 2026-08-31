#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.storage import read_candles_csv, read_table_csv, write_table_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rescale fixed sleeve weights by the number of listed assets at each timestamp."
    )
    parser.add_argument("--candle-dir", required=True, help="Directory containing per-market candle CSV files")
    parser.add_argument("--weights-csv", required=True, help="Input sparse weights CSV")
    parser.add_argument("--output-csv", required=True, help="Output rescaled weights CSV")
    parser.add_argument(
        "--weights-name-suffix",
        default="_listed_norm",
        help="Suffix appended to weights_name in output rows",
    )
    return parser


def load_listing_counts(candle_dir: Path) -> dict[str, int]:
    timestamps: dict[str, set[str]] = {}
    for csv_path in sorted(candle_dir.glob("*.csv")):
        for row in read_candles_csv(csv_path):
            timestamps.setdefault(row.date_utc, set()).add(row.market)
    return {date_utc: len(markets) for date_utc, markets in timestamps.items()}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    listing_counts = load_listing_counts(Path(args.candle_dir))
    if not listing_counts:
        raise SystemExit(f"No candle rows found in {args.candle_dir}")

    weight_rows = read_table_csv(Path(args.weights_csv))
    if not weight_rows:
        raise SystemExit(f"No weight rows found in {args.weights_csv}")

    output_rows: list[dict[str, str]] = []
    for row in weight_rows:
        date_utc = row["date_utc"]
        listed_count = listing_counts.get(date_utc)
        if not listed_count or listed_count <= 0:
            continue
        next_row = dict(row)
        base_weight = float(row["target_weight"])
        next_row["target_weight"] = f"{(base_weight * 7.0 / listed_count):.12g}"
        next_row["weights_name"] = f"{row['weights_name']}{args.weights_name_suffix}"
        output_rows.append(next_row)

    write_table_csv(Path(args.output_csv), output_rows, list(weight_rows[0].keys()))
    print(f"Wrote {len(output_rows)} listed-normalized weight rows to {args.output_csv}")


if __name__ == "__main__":
    main()
