from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

import pandas as pd

from lib.upbit_collector import CandleRow, Market


CSV_ENCODING = "utf-8-sig"
MARKET_COLUMNS = ["market", "korean_name", "english_name", "market_warning"]
CANDLE_COLUMNS = [
    "market",
    "korean_name",
    "english_name",
    "market_warning",
    "date_utc",
    "date_kst",
    "opening_price",
    "high_price",
    "low_price",
    "trade_price",
    "candle_acc_trade_volume",
    "candle_acc_trade_price",
    "timestamp",
]


def write_market_manifest(path: Path, markets: Iterable[Market]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=CSV_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=MARKET_COLUMNS)
        writer.writeheader()
        for market in markets:
            writer.writerow(
                {
                    "market": market.market,
                    "korean_name": market.korean_name,
                    "english_name": market.english_name,
                    "market_warning": market.market_warning,
                }
            )


def write_candles_csv(path: Path, rows: Iterable[CandleRow]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with path.open("w", newline="", encoding=CSV_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDLE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())
            row_count += 1
    return row_count


def read_candles_csv(path: Path) -> list[CandleRow]:
    with path.open("r", newline="", encoding=CSV_ENCODING) as handle:
        reader = csv.DictReader(handle)
        return [
            CandleRow(
                market=row["market"],
                korean_name=row["korean_name"],
                english_name=row["english_name"],
                market_warning=row["market_warning"],
                date_utc=row["date_utc"],
                date_kst=row["date_kst"],
                opening_price=float(row["opening_price"]),
                high_price=float(row["high_price"]),
                low_price=float(row["low_price"]),
                trade_price=float(row["trade_price"]),
                candle_acc_trade_volume=float(row["candle_acc_trade_volume"]),
                candle_acc_trade_price=float(row["candle_acc_trade_price"]),
                timestamp=(
                    None
                    if row["timestamp"] == ""
                    else int(float(row["timestamp"]))
                ),
            )
            for row in reader
        ]


def read_table_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding=CSV_ENCODING) as handle:
        return list(csv.DictReader(handle))


def write_table_csv(path: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding=CSV_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def read_table(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
        if frame.empty:
            return []
        frame = frame.where(pd.notna(frame), "")
        records = frame.to_dict(orient="records")
        normalized: list[dict[str, str]] = []
        for record in records:
            normalized.append(
                {
                    str(key): ("" if value is None else str(value))
                    for key, value in record.items()
                }
            )
        return normalized
    return read_table_csv(path)


def write_table(path: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> None:
    if path.suffix.lower() == ".parquet":
        path.parent.mkdir(parents=True, exist_ok=True)
        if columns is None:
            columns = list(rows[0].keys()) if rows else []
        if rows:
            frame = pd.DataFrame.from_records(rows, columns=columns)
        else:
            frame = pd.DataFrame(columns=columns)
        frame.to_parquet(path, index=False)
        return
    write_table_csv(path, rows, columns)
