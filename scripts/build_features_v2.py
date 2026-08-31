#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.features_v2 import build_feature_frames_from_cache
from lib.spec_io import load_feature_specs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build frame-native v2 feature frames from wide source cache."
    )
    parser.add_argument(
        "--source-cache-dir",
        required=True,
        help="Wide source cache directory, e.g. data/upbit_research_cache/60",
    )
    parser.add_argument(
        "--spec-json",
        required=True,
        help="Feature spec JSON path",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Directory to write per-feature parquet files",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Optional market limit for smoke validation",
    )
    parser.add_argument(
        "--tail-hours",
        type=int,
        default=None,
        help="Optional per-market tail length for smoke validation",
    )
    parser.add_argument(
        "--include-sources",
        action="store_true",
        help="Also write raw source frames used by the feature graph",
    )
    return parser


def _write_wide_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.copy()
    output.index = pd.to_datetime(output.index, utc=False)
    output = output.sort_index().sort_index(axis=1)
    output = output.reset_index(names="date_utc")
    output.to_parquet(path, index=False)


def main() -> int:
    args = build_parser().parse_args()
    source_cache_dir = Path(args.source_cache_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_specs = load_feature_specs(Path(args.spec_json))
    feature_frames = build_feature_frames_from_cache(
        source_cache_dir,
        feature_specs,
        max_markets=args.max_markets,
        tail_rows=args.tail_hours,
    )

    requested_columns = [spec.resolved_column_name() for spec in feature_specs]
    columns_to_write = sorted(feature_frames.keys()) if args.include_sources else requested_columns
    for column_name in columns_to_write:
        if column_name not in feature_frames:
            raise SystemExit(f"Missing frame for requested feature column: {column_name}")
        _write_wide_frame(out_dir / f"{column_name}.parquet", feature_frames[column_name])

    primary_name = requested_columns[0] if requested_columns else next(iter(feature_frames.keys()), None)
    primary_frame = feature_frames[primary_name] if primary_name is not None else pd.DataFrame()
    manifest = {
        "source_cache_dir": str(source_cache_dir),
        "spec_json": str(Path(args.spec_json)),
        "feature_columns": requested_columns,
        "written_columns": columns_to_write,
        "max_markets": args.max_markets,
        "tail_hours": args.tail_hours,
        "row_count": int(len(primary_frame.index)),
        "market_count": int(len(primary_frame.columns)),
    }
    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {len(columns_to_write)} feature frames to {out_dir}")
    print(f"Rows: {manifest['row_count']}")
    print(f"Markets: {manifest['market_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
