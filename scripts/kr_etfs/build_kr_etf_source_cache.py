#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "data" / "kr_etfs" / "raw"
DEFAULT_OUT_DIR = ROOT / "data" / "stocks_cache" / "kr_etf_daily"
DEFAULT_MASTER_FILE = DEFAULT_RAW_DIR / "etf_master.parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a v2 wide source cache for Korean ETFs from raw pykrx/Yahoo caches."
    )
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--master-file", default=str(DEFAULT_MASTER_FILE))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _normalize_symbol(value: object) -> str:
    return str(value).strip().upper()


def _date_filter(index: pd.DatetimeIndex, start: str, end: str) -> pd.DatetimeIndex:
    mask = pd.Series(True, index=index)
    if start:
        mask &= index >= pd.Timestamp(start).normalize()
    if end:
        mask &= index <= pd.Timestamp(end).normalize()
    return pd.DatetimeIndex(index[mask.to_numpy()])


def _load_master(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    master = pd.read_parquet(path).copy()
    if "Symbol" not in master.columns:
        raise ValueError(f"{path} must contain a Symbol column")
    master["Symbol"] = master["Symbol"].map(_normalize_symbol)
    master = master.drop_duplicates(subset=["Symbol"], keep="last")
    return master.sort_values("Symbol").reset_index(drop=True)


def _load_signal_price(path: Path, start: str, end: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_parquet(path)
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [_normalize_symbol(column) for column in frame.columns]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.loc[:, ~pd.Index(frame.columns).duplicated()]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(axis=1, how="all")
    frame = frame.loc[_date_filter(frame.index, start, end)]
    return frame.sort_index(axis=1)


def _load_pykrx_long(path: Path, start: str, end: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_parquet(path).copy()
    required = {"Date", "Symbol", "Close", "Volume", "Amount"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} missing required columns: {sorted(missing)}")
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    frame["Symbol"] = frame["Symbol"].map(_normalize_symbol)
    if start:
        frame = frame[frame["Date"] >= pd.Timestamp(start).normalize()]
    if end:
        frame = frame[frame["Date"] <= pd.Timestamp(end).normalize()]
    frame = frame.drop_duplicates(subset=["Date", "Symbol"], keep="last")
    return frame.sort_values(["Date", "Symbol"]).reset_index(drop=True)


def _pivot(
    frame: pd.DataFrame,
    *,
    value_column: str,
    index: pd.DatetimeIndex,
    columns: list[str],
) -> pd.DataFrame:
    wide = (
        frame.pivot_table(
            index="Date",
            columns="Symbol",
            values=value_column,
            aggfunc="last",
        )
        .sort_index()
        .sort_index(axis=1)
    )
    wide = wide.reindex(index=index, columns=columns)
    return wide.apply(pd.to_numeric, errors="coerce")


def _build_market_meta(master: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    keyed = master.set_index("Symbol")
    rows: list[dict[str, object]] = []
    for symbol in columns:
        row = keyed.loc[symbol] if symbol in keyed.index else None
        name = str(row["Name"]) if row is not None and "Name" in row else symbol
        category_name = (
            str(row["CategoryName"])
            if row is not None and "CategoryName" in row
            else symbol
        )
        rows.append(
            {
                "market": symbol,
                "korean_name": name or symbol,
                "english_name": category_name or symbol,
                "market_warning": "NONE",
            }
        )
    return pd.DataFrame(rows)


def _write_report(
    out_dir: Path,
    *,
    raw_dir: Path,
    frames: dict[str, pd.DataFrame],
    markets: list[str],
    index: pd.DatetimeIndex,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_dir": str(raw_dir),
        "out_dir": str(out_dir),
        "rows": int(len(index)),
        "markets": int(len(markets)),
        "start": index.min().date().isoformat() if len(index) else None,
        "end": index.max().date().isoformat() if len(index) else None,
        "frames": {
            name: {
                "rows": int(frame.shape[0]),
                "columns": int(frame.shape[1]),
                "non_null": int(frame.notna().sum().sum()),
            }
            for name, frame in frames.items()
        },
    }
    (out_dir / "source_cache_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    raw_dir = _resolve(args.raw_dir)
    master_file = _resolve(args.master_file)
    out_dir = _resolve(args.out_dir)
    master = _load_master(master_file)
    signal_price = _load_signal_price(raw_dir / "signal_price_yahoo.parquet", args.start, args.end)
    pykrx = _load_pykrx_long(raw_dir / "ohlcv_pykrx.parquet", args.start, args.end)

    if signal_price.empty or signal_price.shape[1] == 0:
        raise SystemExit("No Yahoo signal price data available after filtering")
    if pykrx.empty:
        raise SystemExit("No pykrx trade data available after filtering")

    markets = sorted(set(signal_price.columns) & set(pykrx["Symbol"]))
    if not markets:
        raise SystemExit("No common ETF symbols between Yahoo signal prices and pykrx trade data")

    index = pd.DatetimeIndex(sorted(set(signal_price.index) | set(pykrx["Date"])))
    if len(index) == 0:
        raise SystemExit("No dates available for ETF source cache")

    frames: dict[str, pd.DataFrame] = {}
    frames["trade_price"] = _pivot(pykrx, value_column="Close", index=index, columns=markets)
    frames["signal_price"] = signal_price.reindex(index=index, columns=markets)
    frames["candle_acc_trade_volume"] = _pivot(
        pykrx,
        value_column="Volume",
        index=index,
        columns=markets,
    )
    frames["candle_acc_trade_price"] = _pivot(
        pykrx,
        value_column="Amount",
        index=index,
        columns=markets,
    )
    frames["price_available"] = frames["trade_price"].notna().astype("float64")

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_parquet(out_dir / f"{name}.parquet")

    market_warning = pd.DataFrame("NONE", index=index, columns=markets)
    market_warning.to_parquet(out_dir / "market_warning.parquet")
    market_meta = _build_market_meta(master, markets)
    market_meta.to_parquet(out_dir / "market_meta.parquet", index=False)
    _write_report(out_dir, raw_dir=raw_dir, frames=frames, markets=markets, index=index)

    print(f"Wrote Korean ETF source cache to {out_dir}")
    print(f"Rows: {len(index)}")
    print(f"Markets: {len(markets)}")
    print(f"Start: {index.min().date()}")
    print(f"End: {index.max().date()}")
    print(
        "Wrote: trade_price, signal_price, candle_acc_trade_volume, "
        "candle_acc_trade_price, price_available, market_warning, market_meta"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
