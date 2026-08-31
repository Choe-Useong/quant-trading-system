#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import UTC, datetime, timedelta

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.storage import read_candles_csv, write_candles_csv, write_market_manifest
from lib.upbit_collector import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PAUSE_SECONDS,
    CandleRow,
    collect_minute_candles,
    fetch_minute_candle_batch,
    list_markets,
)


VALID_UNITS = {1, 3, 5, 10, 15, 30, 60, 240}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Upbit minute candles and save them as per-market CSV files."
    )
    parser.add_argument(
        "--unit",
        type=int,
        default=240,
        help="Minute candle unit. Supported values: 1,3,5,10,15,30,60,240",
    )
    parser.add_argument(
        "--quote",
        default="KRW",
        help="Quote currency prefix used to filter markets",
    )
    parser.add_argument(
        "--candles",
        type=int,
        default=None,
        help="Maximum number of minute candles per market; omit to collect as far back as possible",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Maximum candles requested per API call",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="Delay between API calls for one market",
    )
    parser.add_argument(
        "--exclude-warnings",
        action="store_true",
        help="Exclude markets currently marked with investment warnings",
    )
    parser.add_argument(
        "--markets",
        default="",
        help="Comma-separated subset of markets to download, for example KRW-BTC,KRW-ETH",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Limit the number of markets after filtering",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip markets whose output CSV already exists",
    )
    parser.add_argument(
        "--merge-existing",
        action="store_true",
        help="Merge downloaded rows with existing CSV rows by date_utc instead of overwriting",
    )
    parser.add_argument(
        "--drop-incomplete",
        action="store_true",
        help="Drop the current incomplete minute candle before writing",
    )
    parser.add_argument(
        "--start-market",
        default="",
        help="Start from this market code after filtering/sorting, for example KRW-ADA",
    )
    parser.add_argument(
        "--out-dir",
        default="data/upbit",
        help="Base output directory",
    )
    return parser


def _parse_date_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def _last_closed_candle_start_utc(unit: int, now: datetime | None = None) -> datetime:
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    current_bucket_minute = (current.minute // unit) * unit
    current_bucket = current.replace(
        minute=current_bucket_minute,
        second=0,
        microsecond=0,
    )
    return current_bucket - timedelta(minutes=unit)


def _drop_incomplete_rows(rows: list[CandleRow], unit: int) -> tuple[list[CandleRow], int]:
    cutoff = _last_closed_candle_start_utc(unit)
    completed = [row for row in rows if _parse_date_utc(row.date_utc) <= cutoff]
    return completed, len(rows) - len(completed)


def _merge_rows(
    existing_rows: list[CandleRow],
    downloaded_rows: list[CandleRow],
    max_rows: int | None,
) -> tuple[list[CandleRow], int]:
    merged_by_date = {row.date_utc: row for row in existing_rows}
    new_rows = 0
    for row in downloaded_rows:
        if row.date_utc not in merged_by_date:
            new_rows += 1
        merged_by_date[row.date_utc] = row
    merged_rows = [merged_by_date[key] for key in sorted(merged_by_date)]
    if max_rows is not None and max_rows > 0:
        merged_rows = merged_rows[-max_rows:]
    return merged_rows, new_rows


def _resolve_fetch_candles(
    output_path: Path,
    market,
    unit: int,
    requested_candles: int | None,
    merge_existing: bool,
) -> tuple[int | None, list[CandleRow]]:
    existing_rows: list[CandleRow] = []
    if merge_existing and output_path.exists():
        existing_rows = read_candles_csv(output_path)
    if not merge_existing or requested_candles is None or not existing_rows:
        return requested_candles, existing_rows

    latest_existing = max(_parse_date_utc(row.date_utc) for row in existing_rows)
    latest_probe = fetch_minute_candle_batch(market, unit=unit, count=1)
    latest_api = max((_parse_date_utc(row.date_utc) for row in latest_probe), default=latest_existing)
    interval_seconds = max(unit * 60, 1)
    missing_bars = max(0, int((latest_api - latest_existing).total_seconds() // interval_seconds))
    fetch_candles = min(requested_candles, max(5, missing_bars + 5))
    return fetch_candles, existing_rows


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.unit not in VALID_UNITS:
        raise SystemExit(f"Unsupported --unit {args.unit}. Valid values: {sorted(VALID_UNITS)}")

    out_dir = Path(args.out_dir)
    minute_dir = out_dir / "minutes" / str(args.unit)
    markets = list_markets(
        quote=args.quote,
        include_warnings=not args.exclude_warnings,
    )

    if args.markets:
        allowed = {market.strip().upper() for market in args.markets.split(",") if market.strip()}
        markets = [market for market in markets if market.market in allowed]
    if args.start_market:
        start_market = args.start_market.strip().upper()
        markets = [market for market in markets if market.market >= start_market]
    if args.max_markets is not None:
        markets = markets[: args.max_markets]

    write_market_manifest(out_dir / "markets.csv", markets)
    print(f"Found {len(markets)} {args.quote.upper()} markets for {args.unit}-minute candles")

    failures: list[str] = []
    for idx, market in enumerate(markets, start=1):
        output_path = minute_dir / f"{market.market}.csv"
        if args.skip_existing and output_path.exists():
            print(f"[{idx}/{len(markets)}] {market.market}: skipped existing")
            continue
        try:
            fetch_candles, existing_rows = _resolve_fetch_candles(
                output_path=output_path,
                market=market,
                unit=args.unit,
                requested_candles=args.candles,
                merge_existing=args.merge_existing,
            )
            candles = collect_minute_candles(
                market=market,
                unit=args.unit,
                candles=fetch_candles,
                batch_size=args.batch_size,
                pause_seconds=args.pause_seconds,
            )
            fetched_rows = len(candles)
            dropped_incomplete = 0
            if args.drop_incomplete:
                candles, dropped_incomplete = _drop_incomplete_rows(candles, args.unit)
            if args.merge_existing:
                candles, new_rows = _merge_rows(existing_rows, candles, args.candles)
            else:
                new_rows = len(candles)
            row_count = write_candles_csv(output_path, candles)
            print(
                f"[{idx}/{len(markets)}] {market.market}: saved {row_count} rows "
                f"(fetched={fetched_rows}, "
                f"new={new_rows}, dropped_incomplete={dropped_incomplete})"
            )
        except Exception as exc:
            failures.append(f"{market.market}: {exc}")
            print(f"[{idx}/{len(markets)}] {market.market}: failed ({exc})")

    if failures:
        print(f"Completed with {len(failures)} failed markets")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
