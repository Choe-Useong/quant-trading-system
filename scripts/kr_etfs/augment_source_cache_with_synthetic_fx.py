#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_CACHE_DIR = ROOT / "data" / "stocks_cache" / "kr_etf_daily"
DEFAULT_OUT_DIR = ROOT / "data" / "stocks_cache" / "kr_etf_daily_usdsynthetic"
DEFAULT_UNIVERSE_JSON = ROOT / "data" / "kr_etfs" / "universe" / "kr_etf_cat24_structure_excluded_markets.json"
DEFAULT_UNIVERSE_OUT_JSON = (
    ROOT
    / "data"
    / "kr_etfs"
    / "universe"
    / "kr_etf_cat24_structure_excluded_plus_usdkrw_cash_markets.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy a v2 Korean ETF source cache and append one synthetic FX market "
            "using yfinance close/adjusted-close data."
        )
    )
    parser.add_argument("--source-cache-dir", default=str(DEFAULT_SOURCE_CACHE_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--fx-ticker", default="KRW=X")
    parser.add_argument("--synthetic-symbol", default="USD_KRW_CASH")
    parser.add_argument("--synthetic-name", default="Synthetic USD/KRW cash exposure")
    parser.add_argument(
        "--price-mode",
        choices=["raw", "normalized"],
        default="raw",
        help="Use raw FX levels by default; normalized keeps first valid value at --base-value.",
    )
    parser.add_argument("--base-value", type=float, default=10000.0)
    parser.add_argument("--min-fx", type=float, default=500.0)
    parser.add_argument("--max-fx", type=float, default=3000.0)
    parser.add_argument("--max-daily-abs-return", type=float, default=0.10)
    parser.add_argument(
        "--liquidity-mode",
        choices=["median", "zero", "max"],
        default="median",
        help="Synthetic liquidity proxy for candle_acc_trade_price/volume.",
    )
    parser.add_argument("--universe-json", default=str(DEFAULT_UNIVERSE_JSON))
    parser.add_argument("--universe-out-json", default=str(DEFAULT_UNIVERSE_OUT_JSON))
    parser.add_argument(
        "--no-universe",
        action="store_true",
        help="Do not write an appended universe JSON.",
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _load_yfinance():
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ImportError("yfinance is required. Install it with: py -m pip install yfinance") from exc
    return yf


def _read_cache_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_parquet(path)
    frame.index = pd.to_datetime(frame.index).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.columns = [str(column).strip().upper() for column in frame.columns]
    return frame


def _extract_yahoo_series(data: pd.DataFrame, ticker: str) -> pd.Series:
    if data.empty:
        raise ValueError(f"Yahoo returned no data for {ticker}")

    for field in ("Adj Close", "Close"):
        if isinstance(data.columns, pd.MultiIndex):
            if (field, ticker) in data.columns:
                series = data[(field, ticker)]
                break
            if field in data.columns.get_level_values(0):
                series = data.xs(field, axis=1, level=0).iloc[:, 0]
                break
        elif field in data.columns:
            series = data[field]
            break
    else:
        raise ValueError(f"Yahoo response for {ticker} missing Adj Close/Close")

    series = pd.to_numeric(series, errors="coerce").dropna()
    if series.empty:
        raise ValueError(f"Yahoo close series for {ticker} is empty")
    series.index = pd.to_datetime(series.index).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def _validate_fx_series(
    series: pd.Series,
    *,
    min_fx: float,
    max_fx: float,
    max_daily_abs_return: float,
) -> None:
    bad_level = series[(series < min_fx) | (series > max_fx)]
    if not bad_level.empty:
        sample = ", ".join(f"{idx.date()}={value:.4f}" for idx, value in bad_level.head(10).items())
        raise ValueError(f"FX sanity check failed for absolute level: {sample}")

    jumps = series.pct_change()
    bad_jump = jumps[jumps.abs() > max_daily_abs_return]
    if not bad_jump.empty:
        sample = ", ".join(f"{idx.date()}={value:.4%}" for idx, value in bad_jump.head(10).items())
        raise ValueError(f"FX sanity check failed for daily jump: {sample}")


def _download_fx_series(
    ticker: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    min_fx: float,
    max_fx: float,
    max_daily_abs_return: float,
) -> pd.Series:
    yf = _load_yfinance()
    # yfinance end is exclusive. Add a buffer so the final cache date is covered.
    end_arg = (end + timedelta(days=3)).date().isoformat()
    data = yf.download(
        ticker,
        start=start.date().isoformat(),
        end=end_arg,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    series = _extract_yahoo_series(data, ticker)
    _validate_fx_series(
        series,
        min_fx=min_fx,
        max_fx=max_fx,
        max_daily_abs_return=max_daily_abs_return,
    )
    return series


def _build_synthetic_price(
    fx_series: pd.Series,
    index: pd.DatetimeIndex,
    *,
    price_mode: str,
    base_value: float,
) -> pd.Series:
    aligned = fx_series.reindex(index).ffill()
    valid = aligned.dropna()
    if valid.empty:
        raise ValueError("FX data does not overlap source cache index")
    if price_mode == "raw":
        synthetic = aligned.copy()
        synthetic.name = "synthetic_price"
        return synthetic
    if price_mode != "normalized":
        raise ValueError(f"Unsupported price mode: {price_mode}")
    base_fx = float(valid.iloc[0])
    synthetic = aligned.div(base_fx).mul(float(base_value))
    synthetic.name = "synthetic_price"
    return synthetic


def _liquidity_proxy(frame: pd.DataFrame, mode: str, symbol: str) -> pd.Series:
    if mode == "zero":
        return pd.Series(0.0, index=frame.index, name=symbol)
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    if mode == "median":
        return numeric.median(axis=1, skipna=True).rename(symbol)
    if mode == "max":
        return numeric.max(axis=1, skipna=True).rename(symbol)
    raise ValueError(f"Unsupported liquidity mode: {mode}")


def _copy_source_cache(source_dir: Path, out_dir: Path) -> None:
    if not source_dir.exists():
        raise FileNotFoundError(source_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        if item.is_file():
            shutil.copy2(item, out_dir / item.name)


def _append_column(frame: pd.DataFrame, symbol: str, series: pd.Series) -> pd.DataFrame:
    result = frame.copy()
    result[symbol] = series.reindex(result.index)
    return result.sort_index(axis=1)


def _augment_cache(
    source_dir: Path,
    out_dir: Path,
    *,
    symbol: str,
    synthetic_name: str,
    synthetic_price: pd.Series,
    liquidity_mode: str,
    fx_ticker: str,
    price_mode: str,
    base_value: float,
) -> dict[str, Any]:
    _copy_source_cache(source_dir, out_dir)

    trade_price = _read_cache_frame(source_dir / "trade_price.parquet")
    signal_price = _read_cache_frame(source_dir / "signal_price.parquet")
    common_index = pd.DatetimeIndex(sorted(set(trade_price.index) | set(signal_price.index)))
    synthetic_price = synthetic_price.reindex(common_index).ffill()

    for name in ("trade_price", "signal_price"):
        frame = _read_cache_frame(source_dir / f"{name}.parquet").reindex(common_index)
        _append_column(frame, symbol, synthetic_price).to_parquet(out_dir / f"{name}.parquet")

    for name in ("candle_acc_trade_price", "candle_acc_trade_volume"):
        frame = _read_cache_frame(source_dir / f"{name}.parquet").reindex(common_index)
        proxy = _liquidity_proxy(frame, liquidity_mode, symbol).where(synthetic_price.notna())
        _append_column(frame, symbol, proxy).to_parquet(out_dir / f"{name}.parquet")

    if (source_dir / "price_available.parquet").exists():
        available = _read_cache_frame(source_dir / "price_available.parquet").reindex(common_index)
    else:
        available = trade_price.notna().astype(float).reindex(common_index)
    available_series = synthetic_price.notna().astype(float)
    _append_column(available, symbol, available_series).to_parquet(out_dir / "price_available.parquet")

    if (source_dir / "market_warning.parquet").exists():
        warning = _read_cache_frame(source_dir / "market_warning.parquet").reindex(common_index)
        warning[symbol] = "NONE"
        warning.sort_index(axis=1).to_parquet(out_dir / "market_warning.parquet")

    if (source_dir / "market_meta.parquet").exists():
        meta = pd.read_parquet(source_dir / "market_meta.parquet").copy()
        if "market" not in meta.columns:
            raise ValueError("market_meta.parquet must contain a market column")
        meta["market"] = meta["market"].astype(str).str.strip().str.upper()
        meta = meta[meta["market"] != symbol]
        row = {
            "market": symbol,
            "korean_name": synthetic_name,
            "english_name": f"SYNTHETIC_FX:{fx_ticker}",
            "market_warning": "NONE",
        }
        meta = pd.concat([meta, pd.DataFrame([row])], ignore_index=True)
        meta.sort_values("market").to_parquet(out_dir / "market_meta.parquet", index=False)

    non_null = int(synthetic_price.notna().sum())
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_cache_dir": str(source_dir),
        "out_dir": str(out_dir),
        "synthetic_symbol": symbol,
        "synthetic_name": synthetic_name,
        "fx_ticker": fx_ticker,
        "price_mode": price_mode,
        "base_value": float(base_value),
        "liquidity_mode": liquidity_mode,
        "rows": int(len(common_index)),
        "synthetic_non_null_rows": non_null,
        "synthetic_start": synthetic_price.dropna().index.min().date().isoformat() if non_null else None,
        "synthetic_end": synthetic_price.dropna().index.max().date().isoformat() if non_null else None,
        "synthetic_last": float(synthetic_price.dropna().iloc[-1]) if non_null else None,
    }
    (out_dir / "synthetic_fx_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _write_universe(
    universe_json: Path,
    out_json: Path,
    *,
    symbol: str,
) -> None:
    if not universe_json.exists():
        raise FileNotFoundError(universe_json)
    payload = json.loads(universe_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ValueError(f"{universe_json} must be a JSON object with a markets list")
    markets = [str(market).strip().upper() for market in payload["markets"]]
    if symbol not in markets:
        markets.append(symbol)
    payload["markets"] = sorted(dict.fromkeys(markets))
    old_name = str(payload.get("name", "universe"))
    payload["name"] = f"{old_name}_plus_{symbol.lower()}"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    source_dir = _resolve(args.source_cache_dir)
    out_dir = _resolve(args.out_dir)
    symbol = str(args.synthetic_symbol).strip().upper()
    if not symbol:
        raise ValueError("--synthetic-symbol must not be empty")

    signal_price = _read_cache_frame(source_dir / "signal_price.parquet")
    if signal_price.empty:
        raise ValueError("source signal_price cache is empty")

    fx_series = _download_fx_series(
        args.fx_ticker,
        start=pd.Timestamp(signal_price.index.min()),
        end=pd.Timestamp(signal_price.index.max()),
        min_fx=args.min_fx,
        max_fx=args.max_fx,
        max_daily_abs_return=args.max_daily_abs_return,
    )
    synthetic_price = _build_synthetic_price(
        fx_series,
        signal_price.index,
        price_mode=str(args.price_mode),
        base_value=args.base_value,
    )
    report = _augment_cache(
        source_dir,
        out_dir,
        symbol=symbol,
        synthetic_name=str(args.synthetic_name),
        synthetic_price=synthetic_price,
        liquidity_mode=str(args.liquidity_mode),
        fx_ticker=str(args.fx_ticker),
        price_mode=str(args.price_mode),
        base_value=float(args.base_value),
    )

    if not args.no_universe:
        _write_universe(
            _resolve(args.universe_json),
            _resolve(args.universe_out_json),
            symbol=symbol,
        )
        print(f"Wrote universe: {_resolve(args.universe_out_json)}")

    print(f"Wrote augmented source cache: {out_dir}")
    print(
        "Synthetic rows: "
        f"{report['synthetic_non_null_rows']} "
        f"{report['synthetic_start']} -> {report['synthetic_end']} "
        f"last={report['synthetic_last']:.4f}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
