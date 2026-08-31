from __future__ import annotations

from pathlib import Path

import pandas as pd

from lib.upbit_collector import CandleRow, Market, fetch_minute_candle_batch


def apply_market_filters(markets: list[str], execution_config: dict[str, object]) -> list[str]:
    filtered = list(markets)
    only_markets = set(execution_config.get("only_markets", []))
    exclude_markets = set(execution_config.get("exclude_markets", []))
    if only_markets:
        filtered = [market for market in filtered if market in only_markets]
    if exclude_markets:
        filtered = [market for market in filtered if market not in exclude_markets]
    return filtered


def _read_tail_candles_csv(path: Path, tail_rows: int) -> list[CandleRow]:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if tail_rows > 0 and len(frame) > tail_rows:
        frame = frame.tail(tail_rows).copy()
    frame = frame.sort_values("date_utc")
    rows: list[CandleRow] = []
    for row in frame.to_dict(orient="records"):
        rows.append(
            CandleRow(
                market=str(row["market"]),
                korean_name=str(row["korean_name"]),
                english_name=str(row["english_name"]),
                market_warning=str(row["market_warning"]),
                date_utc=str(row["date_utc"]),
                date_kst=str(row["date_kst"]),
                opening_price=float(row["opening_price"]),
                high_price=float(row["high_price"]),
                low_price=float(row["low_price"]),
                trade_price=float(row["trade_price"]),
                candle_acc_trade_volume=float(row["candle_acc_trade_volume"]),
                candle_acc_trade_price=float(row["candle_acc_trade_price"]),
                timestamp=None if row.get("timestamp") in {"", None} or pd.isna(row.get("timestamp")) else int(row["timestamp"]),
            )
        )
    return rows


def merge_live_rows(candle_dir: Path, market: str, refresh_candles: int, base_tail_rows: int, minute_unit: int = 60) -> list[CandleRow]:
    path = candle_dir / f"{market}.csv"
    local_rows = _read_tail_candles_csv(path, base_tail_rows)
    meta = local_rows[-1] if local_rows else None
    if meta is None:
        raise FileNotFoundError(f"Missing local candle file: {path}")
    market_meta = Market(
        market=market,
        korean_name=meta.korean_name,
        english_name=meta.english_name,
        market_warning=meta.market_warning,
    )
    latest_rows = fetch_minute_candle_batch(market_meta, unit=minute_unit, count=refresh_candles)
    merged_by_date = {row.date_utc: row for row in local_rows}
    for row in latest_rows:
        merged_by_date[row.date_utc] = row
    return [merged_by_date[key] for key in sorted(merged_by_date)]
