#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.storage import read_table_csv, write_table_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append market-specific custom scores to a features CSV."
    )
    parser.add_argument("--features-csv", required=True, help="Input features CSV path")
    parser.add_argument("--spec-json", required=True, help="JSON file containing market score rules")
    parser.add_argument("--output-csv", required=True, help="Output feature CSV path with score column appended")
    return parser


def _as_binary(value: str) -> float:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "1.0", "true"}:
        return 1.0
    return 0.0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    rows = read_table_csv(Path(args.features_csv))
    if not rows:
        raise SystemExit(f"No feature rows found in {args.features_csv}")

    payload = json.loads(Path(args.spec_json).read_text(encoding="utf-8-sig"))
    output_column = payload.get("output_column", "custom_score")
    market_rules = {
        item["market"]: {
            "mode": item.get("mode", "weighted_sum"),
            "components": item.get("components", []),
        }
        for item in payload.get("rules", [])
    }

    columns = list(rows[0].keys())
    if output_column not in columns:
        columns.append(output_column)

    scored_rows: list[dict[str, str]] = []
    for row in rows:
        rule = market_rules.get(row["market"], {"mode": "weighted_sum", "components": []})
        components = rule["components"]
        score = 0.0
        if rule["mode"] == "all_true":
            score = 1.0 if components and all(_as_binary(row.get(component["feature_column"], "")) > 0.0 for component in components) else 0.0
        else:
            for component in components:
                feature_column = component["feature_column"]
                weight = float(component.get("weight", 1.0))
                score += weight * _as_binary(row.get(feature_column, ""))
        next_row = dict(row)
        next_row[output_column] = f"{score:.12g}"
        scored_rows.append(next_row)

    write_table_csv(Path(args.output_csv), scored_rows, columns)
    print(f"Wrote {len(scored_rows)} scored feature rows to {args.output_csv}")


if __name__ == "__main__":
    main()
