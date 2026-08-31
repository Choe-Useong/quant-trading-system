#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

import pandas as pd


FRED_GRAPH_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download stock/ETF prices from Yahoo Finance, optionally joined with FRED series, into a wide CSV."
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="Comma-separated Yahoo Finance tickers, for example SPY,QQQ,AAPL,MSFT",
    )
    parser.add_argument(
        "--fred-series",
        default="",
        help="Optional comma-separated FRED series IDs to join, for example DFII10,DFII5,DGS10,T10YIE",
    )
    parser.add_argument(
        "--out-csv",
        required=True,
        help="Output wide CSV path. The first column is date and each ticker is one price column.",
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
        help="Yahoo Finance interval, for example 1d, 1wk, 1mo, 60m",
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
        "--preserve-fred-case",
        action="store_true",
        help="Do not uppercase output FRED series columns.",
    )
    parser.add_argument(
        "--fred-join",
        choices=["price_index", "outer"],
        default="price_index",
        help=(
            "How to align FRED series when Yahoo prices are also downloaded. "
            "price_index reindexes FRED to Yahoo trading dates and forward-fills; outer keeps all dates."
        ),
    )
    return parser


def _parse_tickers(raw: str, *, preserve_case: bool) -> list[str]:
    tickers = [item.strip() for item in raw.split(",") if item.strip()]
    if preserve_case:
        return tickers
    return [ticker.upper() for ticker in tickers]


def _parse_fred_series(raw: str, *, preserve_case: bool) -> list[str]:
    series_ids = [item.strip() for item in raw.split(",") if item.strip()]
    if preserve_case:
        return series_ids
    return [series_id.upper() for series_id in series_ids]


def _select_price_field(data: pd.DataFrame, price_field: str) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        field_names = list(data.columns.get_level_values(0).unique())
        if price_field not in field_names:
            raise ValueError(f"Price field not found: {price_field}. Available fields: {field_names}")
        prices = data[price_field]
    else:
        if price_field not in data.columns:
            raise ValueError(f"Price field not found: {price_field}. Available fields: {list(data.columns)}")
        prices = data[[price_field]]

    if isinstance(prices, pd.Series):
        prices = prices.to_frame()
    return prices


def _download_yahoo_prices(args: argparse.Namespace, tickers: list[str]) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: yfinance. Install with `py -m pip install -r requirements.txt`."
        ) from exc

    download_kwargs: dict[str, object] = {
        "tickers": tickers,
        "interval": args.interval,
        "auto_adjust": not args.no_auto_adjust,
        "progress": False,
        "actions": False,
        "threads": True,
        "group_by": "column",
    }
    if args.period:
        download_kwargs["period"] = args.period
    else:
        download_kwargs["start"] = args.start
        if args.end:
            download_kwargs["end"] = args.end

    data = yf.download(**download_kwargs)
    prices = _select_price_field(data, args.price_field)

    if len(tickers) == 1 and list(prices.columns) == [args.price_field]:
        prices = prices.rename(columns={args.price_field: tickers[0]})
    if not args.preserve_ticker_case:
        prices = prices.rename(columns={column: str(column).upper() for column in prices.columns})
    prices = prices.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    prices.index = pd.to_datetime(prices.index, utc=False)
    prices.index.name = "date"
    return prices.sort_index().sort_index(axis=1)


def _download_one_fred_series(series_id: str) -> pd.Series:
    url = FRED_GRAPH_CSV_URL.format(series_id=quote(series_id, safe=""))
    frame = pd.read_csv(url, na_values=[".", ""])
    if frame.empty:
        raise ValueError(f"No data returned for FRED series: {series_id}")

    date_column = "observation_date" if "observation_date" in frame.columns else frame.columns[0]
    value_columns = [column for column in frame.columns if column != date_column]
    if not value_columns:
        raise ValueError(f"No value column returned for FRED series: {series_id}")

    value_column = series_id if series_id in value_columns else value_columns[0]
    result = pd.Series(
        pd.to_numeric(frame[value_column], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame[date_column], utc=False),
        name=series_id,
        dtype="float64",
    )
    result.index.name = "date"
    return result.sort_index()


def _download_fred_series(args: argparse.Namespace, series_ids: list[str]) -> pd.DataFrame:
    fred = pd.concat([_download_one_fred_series(series_id) for series_id in series_ids], axis=1)
    fred = fred.sort_index().sort_index(axis=1)
    if args.start:
        fred = fred.loc[fred.index >= pd.Timestamp(args.start)]
    if args.end:
        fred = fred.loc[fred.index <= pd.Timestamp(args.end)]
    return fred.dropna(axis=1, how="all")


def main() -> int:
    args = build_parser().parse_args()
    tickers = _parse_tickers(args.tickers, preserve_case=bool(args.preserve_ticker_case))
    fred_series = _parse_fred_series(args.fred_series, preserve_case=bool(args.preserve_fred_case))
    if not tickers and not fred_series:
        raise SystemExit("Provide at least one --tickers or --fred-series value")

    frames: list[pd.DataFrame] = []
    prices = pd.DataFrame()
    if tickers:
        prices = _download_yahoo_prices(args, tickers)
        if prices.empty or prices.shape[1] == 0:
            raise SystemExit("No price data downloaded")
        frames.append(prices)

    if fred_series:
        fred = _download_fred_series(args, fred_series)
        if fred.empty or fred.shape[1] == 0:
            raise SystemExit("No FRED data downloaded")
        if not prices.empty and args.fred_join == "price_index":
            fred = fred.reindex(prices.index).ffill()
        frames.append(fred)

    prices = pd.concat(frames, axis=1).sort_index().sort_index(axis=1)

    out_path = Path(args.out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prices.to_csv(out_path, encoding="utf-8-sig")

    print(f"Wrote stock/FRED wide CSV to {out_path}")
    print(f"Yahoo tickers: {len(tickers)}")
    print(f"FRED series: {len(fred_series)}")
    print(f"Columns: {prices.shape[1]}")
    print(f"Rows: {prices.shape[0]}")
    print(f"Start: {prices.index.min()}")
    print(f"End: {prices.index.max()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
