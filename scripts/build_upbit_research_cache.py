#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import build_long_frame_from_candle_dir, build_wide_frames_from_candle_dir


DEFAULT_VALUE_COLUMNS = (
    "trade_price",
    "opening_price",
    "high_price",
    "low_price",
    "candle_acc_trade_volume",
    "candle_acc_trade_price",
    "timestamp",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build wide parquet cache files from per-market Upbit raw candle CSVs."
    )
    parser.add_argument(
        "--candle-dir",
        default="data/upbit_research/minutes/60",
        help="Directory containing per-market raw candle CSV files",
    )
    parser.add_argument(
        "--out-dir",
        default="data/upbit_research_cache/60",
        help="Directory to write wide parquet cache files",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Optional market limit for smoke validation",
    )
    parser.add_argument(
        "--tail-rows",
        type=int,
        default=None,
        help="Optional per-market tail length for smoke validation",
    )
    return parser


def _write_numeric_frames(
    candle_dir: Path,
    out_dir: Path,
    max_markets: int | None,
    tail_rows: int | None,
) -> dict[str, pd.DataFrame]:
    frames, meta_by_market = build_wide_frames_from_candle_dir(
        candle_dir,
        DEFAULT_VALUE_COLUMNS,
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for column, frame in frames.items():
        frame.to_parquet(out_dir / f"{column}.parquet")
    meta_frame = (
        pd.DataFrame.from_records(sorted(meta_by_market.values(), key=lambda item: item["market"]))
        if meta_by_market
        else pd.DataFrame(columns=["market", "korean_name", "english_name", "market_warning"])
    )
    meta_frame.to_parquet(out_dir / "market_meta.parquet", index=False)
    return frames


def _write_warning_frame(
    candle_dir: Path,
    out_dir: Path,
    max_markets: int | None,
    tail_rows: int | None,
) -> pd.DataFrame:
    long_frame = build_long_frame_from_candle_dir(
        candle_dir,
        usecols=["date_utc", "market", "market_warning"],
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    if long_frame.empty:
        warning_frame = pd.DataFrame()
    else:
        warning_frame = (
            long_frame.assign(
                date_utc=pd.to_datetime(long_frame["date_utc"], utc=False),
                market=long_frame["market"].astype(str).str.upper(),
                market_warning=long_frame["market_warning"].astype(str).str.upper(),
            )
            .pivot(index="date_utc", columns="market", values="market_warning")
            .sort_index()
            .sort_index(axis=1)
        )
    warning_frame.to_parquet(out_dir / "market_warning.parquet")
    return warning_frame


def main() -> int:
    args = build_parser().parse_args()
    candle_dir = Path(args.candle_dir)
    out_dir = Path(args.out_dir)

    frames = _write_numeric_frames(
        candle_dir,
        out_dir,
        max_markets=args.max_markets,
        tail_rows=args.tail_rows,
    )
    warning_frame = _write_warning_frame(
        candle_dir,
        out_dir,
        max_markets=args.max_markets,
        tail_rows=args.tail_rows,
    )

    market_count = len(next(iter(frames.values())).columns) if frames else 0
    row_count = len(next(iter(frames.values())).index) if frames else 0
    print(f"Wrote cache files to {out_dir}")
    print(f"Markets: {market_count}")
    print(f"Rows: {row_count}")
    print(f"Warning rows: {len(warning_frame.index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
