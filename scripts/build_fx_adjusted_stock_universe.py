#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.download_stock_prices import (  # noqa: E402
    _download_fred_series,
    _download_yahoo_prices,
    _parse_fred_series,
    _parse_tickers,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a wide CSV that keeps original stock/ETF prices and adds KRW-converted "
            "columns for non-Korean Yahoo tickers using a FRED FX series."
        )
    )
    parser.add_argument(
        "--tickers",
        required=True,
        help="Comma-separated Yahoo tickers, for example QQQ,SPY,SOXX,091160.KS",
    )
    parser.add_argument(
        "--out-csv",
        required=True,
        help="Output wide CSV path for trade/execution prices.",
    )
    parser.add_argument(
        "--signal-out-csv",
        default="",
        help=(
            "Optional output wide CSV path for signal prices. "
            "Non-Korean tickers are remapped onto their -KRW tradable column names using USD prices."
        ),
    )
    parser.add_argument(
        "--signal-only",
        action="store_true",
        help=(
            "Write only --signal-out-csv from USD prices and skip FRED FX conversion. "
            "--out-csv is ignored in this mode."
        ),
    )
    parser.add_argument(
        "--start",
        default="2010-01-01",
        help="Start date when --period is omitted, for example 2010-01-01",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Optional exclusive end date, for example 2026-01-01",
    )
    parser.add_argument(
        "--period",
        default=None,
        help="Optional yfinance period such as 5y, 10y, max. Overrides --start/--end.",
    )
    parser.add_argument(
        "--interval",
        default="1d",
        help="Yahoo Finance interval. Default is 1d.",
    )
    parser.add_argument(
        "--price-field",
        default="Close",
        help="Downloaded field to save. Usually Close with --auto-adjust enabled.",
    )
    parser.add_argument(
        "--no-auto-adjust",
        action="store_true",
        help="Disable yfinance auto_adjust. Default is adjusted OHLC/Close.",
    )
    parser.add_argument(
        "--preserve-ticker-case",
        action="store_true",
        help="Do not uppercase output ticker columns.",
    )
    parser.add_argument(
        "--fx-series",
        default="DEXKOUS",
        help="FRED FX series used for KRW conversion. Default: DEXKOUS",
    )
    parser.add_argument(
        "--fx-column-name",
        default="DEXKOUS",
        help="Output column name for the FX series. Default: DEXKOUS",
    )
    parser.add_argument(
        "--krw-suffix",
        default="-KRW",
        help="Suffix appended to converted non-Korean tickers. Default: -KRW",
    )
    parser.add_argument(
        "--drop-fx-column",
        action="store_true",
        help="Do not include the raw FX series column in the output.",
    )
    return parser


def _is_korean_ticker(ticker: str) -> bool:
    upper = str(ticker).upper()
    return upper.endswith(".KS") or upper.endswith(".KQ")


def _is_non_convertible_series(ticker: str, fx_column_name: str) -> bool:
    upper = str(ticker).upper()
    return upper.endswith("=X") or upper == str(fx_column_name).upper()


def _should_create_krw_clone(ticker: str, fx_column_name: str) -> bool:
    return not _is_korean_ticker(ticker) and not _is_non_convertible_series(ticker, fx_column_name)


def _build_fx_frame(args: argparse.Namespace, price_index: pd.Index) -> pd.Series:
    fred_args = argparse.Namespace(
        tickers="",
        fred_series=args.fx_series,
        out_csv="",
        start=args.start,
        end=args.end,
        period=args.period,
        interval=args.interval,
        price_field=args.price_field,
        no_auto_adjust=args.no_auto_adjust,
        preserve_ticker_case=args.preserve_ticker_case,
        preserve_fred_case=False,
        fred_join="price_index",
    )
    fx_series_ids = _parse_fred_series(fred_args.fred_series, preserve_case=False)
    if len(fx_series_ids) != 1:
        raise ValueError("Exactly one --fx-series value is required")
    fx_frame = _download_fred_series(fred_args, fx_series_ids)
    if fx_frame.empty:
        raise ValueError(f"No FX data downloaded for series: {fx_series_ids[0]}")
    fx_series = fx_frame.iloc[:, 0].rename(str(args.fx_column_name).upper())
    fx_series = fx_series.reindex(price_index).ffill()
    if fx_series.isna().any():
        first_missing = fx_series[fx_series.isna()].index.min()
        raise ValueError(f"FX series has missing values after reindex/ffill at {first_missing}")
    return fx_series.astype(float)


def _ordered_columns(
    tickers: list[str],
    fx_column_name: str,
    krw_suffix: str,
    include_fx_column: bool,
) -> list[str]:
    columns: list[str] = []
    for ticker in tickers:
        columns.append(ticker)
        if _should_create_krw_clone(ticker, fx_column_name):
            columns.append(f"{ticker}{krw_suffix}")
    if include_fx_column:
        columns.append(fx_column_name)
    return columns


def _build_signal_output(
    prices: pd.DataFrame,
    *,
    fx_column_name: str,
    krw_suffix: str,
) -> pd.DataFrame:
    signal = pd.DataFrame(index=prices.index)
    for ticker in prices.columns:
        ticker_str = str(ticker)
        if _should_create_krw_clone(ticker_str, fx_column_name):
            signal[f"{ticker_str}{krw_suffix}"] = pd.to_numeric(prices[ticker_str], errors="coerce")
        else:
            signal[ticker_str] = pd.to_numeric(prices[ticker_str], errors="coerce")
    signal.index.name = "date"
    return signal.sort_index().sort_index(axis=1)


def main() -> int:
    args = build_parser().parse_args()
    tickers = _parse_tickers(args.tickers, preserve_case=bool(args.preserve_ticker_case))
    if not tickers:
        raise SystemExit("Provide at least one ticker in --tickers")

    prices = _download_yahoo_prices(args, tickers)
    if prices.empty or prices.shape[1] == 0:
        raise SystemExit("No Yahoo price data downloaded")

    fx_column_name = str(args.fx_column_name).upper()
    signal_path_raw = str(args.signal_out_csv).strip()
    if args.signal_only:
        if not signal_path_raw:
            raise SystemExit("--signal-out-csv is required with --signal-only")
        signal_output = _build_signal_output(
            prices,
            fx_column_name=fx_column_name,
            krw_suffix=str(args.krw_suffix),
        )
        signal_path = Path(signal_path_raw)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_output.to_csv(signal_path, encoding="utf-8-sig")
        print(f"Wrote signal-only stock universe CSV to {signal_path}")
        print(f"Input tickers: {len(tickers)}")
        print(f"Columns: {signal_output.shape[1]}")
        print(f"Rows: {signal_output.shape[0]}")
        print(f"Start: {signal_output.index.min()}")
        print(f"End: {signal_output.index.max()}")
        return 0

    fx_series = _build_fx_frame(args, prices.index)

    output = prices.copy()
    for ticker in prices.columns:
        if not _should_create_krw_clone(str(ticker), fx_column_name):
            continue
        output[f"{ticker}{args.krw_suffix}"] = pd.to_numeric(prices[ticker], errors="coerce") * fx_series

    if not args.drop_fx_column:
        output[fx_column_name] = fx_series

    output = output.reindex(columns=_ordered_columns(tickers, fx_column_name, args.krw_suffix, not args.drop_fx_column))
    output.index.name = "date"

    out_path = Path(args.out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(out_path, encoding="utf-8-sig")

    if signal_path_raw:
        signal_output = _build_signal_output(
            prices,
            fx_column_name=fx_column_name,
            krw_suffix=str(args.krw_suffix),
        )
        signal_path = Path(signal_path_raw)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_output.to_csv(signal_path, encoding="utf-8-sig")

    created_krw_columns = [column for column in output.columns if column.endswith(str(args.krw_suffix))]
    print(f"Wrote FX-adjusted stock universe CSV to {out_path}")
    print(f"Input tickers: {len(tickers)}")
    print(f"KRW-converted columns: {len(created_krw_columns)}")
    print(f"Columns: {output.shape[1]}")
    print(f"Rows: {output.shape[0]}")
    print(f"Start: {output.index.min()}")
    print(f"End: {output.index.max()}")
    print(f"FX series: {fx_column_name}")
    if signal_path_raw:
        print(f"Wrote signal-only CSV to {signal_path_raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
