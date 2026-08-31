#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

STRATEGY_DB_PATH = ROOT_DIR / "data" / "strategy_db" / "provisional_best_strategies_by_asset.csv"
CANDLE_DIR = ROOT_DIR / "data" / "upbit" / "minutes" / "60"

from lib.upbit_collector import Market, fetch_minute_candle_batch


def _load_price_series(asset: str) -> pd.Series:
    market = f"KRW-{asset.upper()}"
    path = CANDLE_DIR / f"{market}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing candle file: {path}")
    frame = pd.read_csv(path)
    if "date_utc" not in frame.columns or "trade_price" not in frame.columns:
        raise ValueError(f"Unexpected candle columns in {path}")
    series = (
        frame[["date_utc", "trade_price"]]
        .drop_duplicates("date_utc")
        .assign(date_utc=lambda df: pd.to_datetime(df["date_utc"]))
        .sort_values("date_utc")
        .set_index("date_utc")["trade_price"]
        .astype(float)
    )
    return series


def _load_live_price_series(asset: str, refresh_candles: int = 240) -> pd.Series:
    market = f"KRW-{asset.upper()}"
    series = _load_price_series(asset)
    market_meta = Market(
        market=market,
        korean_name=asset,
        english_name=asset,
        market_warning="NONE",
    )
    latest_rows = fetch_minute_candle_batch(
        market=market_meta,
        unit=60,
        count=refresh_candles,
    )
    if latest_rows:
        live_series = (
            pd.DataFrame(
                [{"date_utc": pd.Timestamp(row.date_utc), "trade_price": float(row.trade_price)} for row in latest_rows]
            )
            .drop_duplicates("date_utc")
            .sort_values("date_utc")
            .set_index("date_utc")["trade_price"]
            .astype(float)
        )
        series = pd.concat([series, live_series])
        series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def _rolling_mean(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def _signal_ma_cross(series: pd.Series, short: int, long: int) -> pd.Series:
    return _rolling_mean(series, short) > _rolling_mean(series, long)


def _signal_ma_cross_persist(series: pd.Series, short: int, long: int, persist: int) -> pd.Series:
    raw = _signal_ma_cross(series, short, long)
    return raw.astype(float).rolling(persist).sum() >= persist


def _signal_ma_cross_exit_persist(series: pd.Series, short: int, long: int, exit_persist: int) -> pd.Series:
    short_ma = _rolling_mean(series, short)
    long_ma = _rolling_mean(series, long)
    entry = short_ma > long_ma
    exit_signal = (short_ma <= long_ma).astype(float).rolling(exit_persist).sum() >= exit_persist

    holding = False
    values: list[bool] = []
    for entry_value, exit_value in zip(entry.fillna(False), exit_signal.fillna(False)):
        if holding:
            if bool(exit_value):
                holding = False
        elif bool(entry_value):
            holding = True
        values.append(holding)
    return pd.Series(values, index=series.index, dtype=bool)


def _signal_ma_stack(series: pd.Series, short: int, mid: int, long: int) -> pd.Series:
    short_ma = _rolling_mean(series, short)
    mid_ma = _rolling_mean(series, mid)
    long_ma = _rolling_mean(series, long)
    return (short_ma > mid_ma) & (mid_ma > long_ma)


def _build_signal(asset: str, strategy_family: str, run_name: str, series: pd.Series) -> tuple[pd.Series | None, str | None]:
    if strategy_family == "ma_cross":
        match = re.fullmatch(rf"{asset.lower()}_ma_60m_(\d+)_(\d+)_every_bar_fee5bp", run_name)
        if not match:
            return None, "unsupported_ma_cross_run_name"
        short, long = map(int, match.groups())
        return _signal_ma_cross(series, short, long), None

    if strategy_family == "ma_cross_persist":
        match = re.fullmatch(rf"{asset.lower()}_ma_60m_(\d+)_(\d+)_persist(\d+)_every_bar_fee5bp", run_name)
        if not match:
            return None, "unsupported_ma_cross_persist_run_name"
        short, long, persist = map(int, match.groups())
        return _signal_ma_cross_persist(series, short, long, persist), None

    if strategy_family == "ma_cross_exit_persist":
        match = re.fullmatch(rf"{asset.lower()}_ma_60m_(\d+)_(\d+)_exit_persist(\d+)_stateful_fee5bp", run_name)
        if not match:
            return None, "unsupported_ma_cross_exit_persist_run_name"
        short, long, exit_persist = map(int, match.groups())
        return _signal_ma_cross_exit_persist(series, short, long, exit_persist), None

    if strategy_family == "ma_stack":
        match = re.fullmatch(rf"{asset.lower()}_ma_stack_60m_(\d+)_(\d+)_(\d+)_every_bar_fee5bp", run_name)
        if not match:
            return None, "unsupported_ma_stack_run_name"
        short, mid, long = map(int, match.groups())
        return _signal_ma_stack(series, short, mid, long), None

    return None, f"unsupported_strategy_family:{strategy_family}"


def main() -> int:
    strategies = pd.read_csv(STRATEGY_DB_PATH)
    rows: list[dict[str, object]] = []

    for row in strategies.to_dict(orient="records"):
        asset = str(row["asset"])
        strategy_family = str(row["strategy_family"])
        run_name = str(row["run_name"])
        try:
            series = _load_live_price_series(asset)
            signal, error = _build_signal(asset, strategy_family, run_name, series)
            if error is not None or signal is None:
                rows.append(
                    {
                        "asset": asset,
                        "strategy_family": strategy_family,
                        "run_name": run_name,
                        "supported": False,
                        "error": error,
                    }
                )
                continue

            latest_timestamp = signal.index[-1]
            latest_price = float(series.iloc[-1])
            latest_signal = bool(signal.fillna(False).iloc[-1])
            rows.append(
                {
                    "asset": asset,
                    "strategy_family": strategy_family,
                    "run_name": run_name,
                    "supported": True,
                    "latest_timestamp_utc": latest_timestamp.isoformat(),
                    "latest_price": latest_price,
                    "signal_on": latest_signal,
                    "target_weight": 1.0 if latest_signal else 0.0,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "asset": asset,
                    "strategy_family": strategy_family,
                    "run_name": run_name,
                    "supported": False,
                    "error": str(exc),
                }
            )

    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
