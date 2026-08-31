#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a minimal v2 wide source cache from price-only CSV data."
    )
    parser.add_argument("--input-csv", required=True, help="Input price CSV path")
    parser.add_argument("--out-dir", required=True, help="Directory to write wide parquet cache files")
    parser.add_argument(
        "--input-format",
        choices=["wide", "long"],
        default="wide",
        help="wide: date + one column per market, long: date/market/price rows",
    )
    parser.add_argument("--date-column", default="date", help="Date column name; falls back to date_utc if absent")
    parser.add_argument("--market-column", default="market", help="Market/ticker column for long input")
    parser.add_argument("--price-column", default="close", help="Price column for long input")
    parser.add_argument(
        "--markets",
        default="",
        help="Optional comma-separated market/ticker allow-list after normalization",
    )
    parser.add_argument(
        "--preserve-market-case",
        action="store_true",
        help="Do not uppercase market/ticker columns",
    )
    parser.add_argument(
        "--write-market-warning",
        action="store_true",
        help="Also write market_warning.parquet filled with NONE",
    )
    parser.add_argument(
        "--write-market-meta",
        action="store_true",
        help="Also write market_meta.parquet with minimal names and NONE warnings",
    )
    parser.add_argument(
        "--output-column-name",
        default="trade_price",
        help="Parquet column/frame name to write, for example trade_price or signal_price",
    )
    return parser


def _resolve_date_column(frame: pd.DataFrame, configured: str) -> str:
    if configured in frame.columns:
        return configured
    if configured != "date_utc" and "date_utc" in frame.columns:
        return "date_utc"
    raise ValueError(f"Date column not found: {configured}")


def _normalize_market(value: object, *, preserve_case: bool) -> str:
    market = str(value).strip()
    return market if preserve_case else market.upper()


def _parse_market_allowlist(raw: str, *, preserve_case: bool) -> set[str] | None:
    if not raw.strip():
        return None
    markets = {
        _normalize_market(item, preserve_case=preserve_case)
        for item in raw.split(",")
        if item.strip()
    }
    return markets or None


def _read_wide_prices(
    path: Path,
    *,
    date_column: str,
    markets: set[str] | None,
    preserve_market_case: bool,
) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    resolved_date_column = _resolve_date_column(frame, date_column)
    frame[resolved_date_column] = pd.to_datetime(frame[resolved_date_column], utc=False)
    frame = frame.set_index(resolved_date_column).sort_index()

    rename_map = {
        column: _normalize_market(column, preserve_case=preserve_market_case)
        for column in frame.columns
    }
    frame = frame.rename(columns=rename_map)
    if markets is not None:
        frame = frame.reindex(columns=sorted(markets))

    prices = frame.apply(pd.to_numeric, errors="coerce")
    prices = prices.dropna(axis=1, how="all")
    return prices.sort_index().sort_index(axis=1)


def _read_long_prices(
    path: Path,
    *,
    date_column: str,
    market_column: str,
    price_column: str,
    markets: set[str] | None,
    preserve_market_case: bool,
) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    resolved_date_column = _resolve_date_column(frame, date_column)
    required = {resolved_date_column, market_column, price_column}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing required long input columns: {sorted(missing)}")

    frame = frame[[resolved_date_column, market_column, price_column]].copy()
    frame[resolved_date_column] = pd.to_datetime(frame[resolved_date_column], utc=False)
    frame[market_column] = frame[market_column].map(
        lambda value: _normalize_market(value, preserve_case=preserve_market_case)
    )
    if markets is not None:
        frame = frame[frame[market_column].isin(markets)]
    frame[price_column] = pd.to_numeric(frame[price_column], errors="coerce")

    prices = (
        frame.pivot_table(
            index=resolved_date_column,
            columns=market_column,
            values=price_column,
            aggfunc="last",
        )
        .sort_index()
        .sort_index(axis=1)
    )
    prices = prices.dropna(axis=1, how="all")
    return prices


def _write_market_warning(out_dir: Path, prices: pd.DataFrame) -> None:
    warning = pd.DataFrame("NONE", index=prices.index, columns=prices.columns)
    warning.to_parquet(out_dir / "market_warning.parquet")


def _write_market_meta(out_dir: Path, markets: list[str]) -> None:
    meta = pd.DataFrame(
        [
            {
                "market": market,
                "korean_name": market,
                "english_name": market,
                "market_warning": "NONE",
            }
            for market in markets
        ]
    )
    meta.to_parquet(out_dir / "market_meta.parquet", index=False)


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input_csv)
    out_dir = Path(args.out_dir)
    markets = _parse_market_allowlist(args.markets, preserve_case=bool(args.preserve_market_case))

    if args.input_format == "wide":
        prices = _read_wide_prices(
            input_path,
            date_column=args.date_column,
            markets=markets,
            preserve_market_case=bool(args.preserve_market_case),
        )
    else:
        prices = _read_long_prices(
            input_path,
            date_column=args.date_column,
            market_column=args.market_column,
            price_column=args.price_column,
            markets=markets,
            preserve_market_case=bool(args.preserve_market_case),
        )

    if prices.empty or prices.shape[1] == 0:
        raise SystemExit("No price data found after parsing input")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_column_name = str(args.output_column_name).strip()
    if not output_column_name:
        raise SystemExit("--output-column-name must not be empty")

    prices.to_parquet(out_dir / f"{output_column_name}.parquet")
    if args.write_market_warning:
        _write_market_warning(out_dir, prices)
    if args.write_market_meta:
        _write_market_meta(out_dir, list(prices.columns))

    print(f"Wrote price-only source cache to {out_dir}")
    print(f"Markets: {prices.shape[1]}")
    print(f"Rows: {prices.shape[0]}")
    print(f"Start: {prices.index.min()}")
    print(f"End: {prices.index.max()}")
    print(f"Wrote: {output_column_name}.parquet")
    if args.write_market_warning:
        print("Wrote: market_warning.parquet")
    if args.write_market_meta:
        print("Wrote: market_meta.parquet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
