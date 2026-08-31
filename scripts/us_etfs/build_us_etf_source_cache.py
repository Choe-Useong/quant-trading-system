#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_PRICE_DIR = ROOT / "data" / "us_etfs" / "raw" / "prices" / "leveraged_2x"
DEFAULT_UNIVERSE = ROOT / "data" / "us_etfs" / "universe" / "leveraged_2x.csv"
DEFAULT_OUT_DIR = ROOT / "data" / "stocks_cache" / "us_etf_leveraged_2x_daily"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a v2 wide source cache for US ETFs from raw Yahoo price caches."
    )
    parser.add_argument("--raw-price-dir", default=str(DEFAULT_RAW_PRICE_DIR))
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
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


def _load_universe(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    universe = pd.read_csv(path, dtype=str).copy()
    if "Symbol" not in universe.columns:
        raise ValueError(f"{path} must contain a Symbol column")
    universe["Symbol"] = universe["Symbol"].map(_normalize_symbol)
    universe = universe.drop_duplicates(subset=["Symbol"], keep="last")
    return universe.sort_values("Symbol").reset_index(drop=True)


def _load_wide(path: Path, start: str, end: str) -> pd.DataFrame:
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


def _build_market_meta(universe: pd.DataFrame, markets: list[str]) -> pd.DataFrame:
    keyed = universe.set_index("Symbol")
    rows: list[dict[str, object]] = []
    for symbol in markets:
        row = keyed.loc[symbol] if symbol in keyed.index else None
        name = str(row["Name"]) if row is not None and "Name" in row else symbol
        universe_name = (
            str(row["Universe"])
            if row is not None and "Universe" in row and pd.notna(row["Universe"])
            else "US_ETF"
        )
        rows.append(
            {
                "market": symbol,
                "korean_name": name or symbol,
                "english_name": name or symbol,
                "market_warning": "NONE",
                "universe": universe_name,
            }
        )
    return pd.DataFrame(rows)


def _write_report(
    out_dir: Path,
    *,
    raw_price_dir: Path,
    universe_path: Path,
    frames: dict[str, pd.DataFrame],
    markets: list[str],
    index: pd.DatetimeIndex,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_price_dir": str(raw_price_dir),
        "universe": str(universe_path),
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
    raw_price_dir = _resolve(args.raw_price_dir)
    universe_path = _resolve(args.universe)
    out_dir = _resolve(args.out_dir)
    universe = _load_universe(universe_path)
    adj_close = _load_wide(raw_price_dir / "adj_close.parquet", args.start, args.end)
    volume = _load_wide(raw_price_dir / "volume.parquet", args.start, args.end)

    if adj_close.empty or adj_close.shape[1] == 0:
        raise SystemExit("No US ETF adjusted close data available after filtering")
    if volume.empty or volume.shape[1] == 0:
        raise SystemExit("No US ETF volume data available after filtering")

    markets = sorted(set(universe["Symbol"]) & set(adj_close.columns) & set(volume.columns))
    if not markets:
        raise SystemExit("No common US ETF symbols between universe, adjusted close, and volume")

    index = pd.DatetimeIndex(sorted(set(adj_close.index) | set(volume.index)))
    if len(index) == 0:
        raise SystemExit("No dates available for US ETF source cache")

    trade_price = adj_close.reindex(index=index, columns=markets)
    signal_price = trade_price.copy()
    trade_volume = volume.reindex(index=index, columns=markets)
    trade_amount = trade_price * trade_volume

    frames: dict[str, pd.DataFrame] = {
        "trade_price": trade_price,
        "signal_price": signal_price,
        "candle_acc_trade_volume": trade_volume,
        "candle_acc_trade_price": trade_amount,
        "price_available": trade_price.notna().astype("float64"),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_parquet(out_dir / f"{name}.parquet")

    market_warning = pd.DataFrame("NONE", index=index, columns=markets)
    market_warning.to_parquet(out_dir / "market_warning.parquet")
    market_meta = _build_market_meta(universe, markets)
    market_meta.to_parquet(out_dir / "market_meta.parquet", index=False)
    _write_report(
        out_dir,
        raw_price_dir=raw_price_dir,
        universe_path=universe_path,
        frames=frames,
        markets=markets,
        index=index,
    )

    print(f"Wrote US ETF source cache to {out_dir}")
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
