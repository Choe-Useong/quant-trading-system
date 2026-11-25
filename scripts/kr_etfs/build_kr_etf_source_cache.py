#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "data" / "kr_etfs" / "raw"
DEFAULT_OUT_DIR = ROOT / "data" / "stocks_cache" / "kr_etf_daily"
DEFAULT_MASTER_FILE = DEFAULT_RAW_DIR / "etf_master.parquet"
DISTRIBUTION_FILE = "distributions_yahoo.parquet"
# Distribution checks against the price (see evaluate_distributions). excess = ETF return minus
# index (IndexValue) return from the previous close row; noise = median |excess| over the previous
# NOISE_WINDOW rows (at least NOISE_MIN_OBS values, the current row excluded).
NOISE_WINDOW = 250
NOISE_MIN_OBS = 20
DETECTABLE_MULTIPLE = 4.0  # share >= this x noise: the price can show the distribution
# confirmed when the ETF return or excess <= -fraction x share on the bar or the close row before (the
# ETF return covers price indices that drop with the ETF, e.g. on the holdings' own ex-dividend date)
CONFIRM_FRACTION = 0.5
IMPLAUSIBLE_SHARE = 0.30  # an unconfirmed distribution above this share of the previous close is dropped
REPORT_LIST_LIMIT = 300
APPLIED_STATUSES = ("applied_confirmed", "applied_small", "applied_no_check")


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


def _load_distributions(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing; run scripts/kr_etfs/update_kr_etf_raw_prices.py --distributions-only first"
        )
    frame = pd.read_parquet(path)
    missing = {"Date", "Symbol", "Amount"}.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} missing required columns: {sorted(missing)}")
    out = frame.loc[:, ["Date", "Symbol", "Amount"]].copy()
    out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
    out["Symbol"] = out["Symbol"].map(_normalize_symbol)
    out["Amount"] = pd.to_numeric(out["Amount"], errors="coerce")
    return out.sort_values(["Symbol", "Date"]).reset_index(drop=True)


def map_distribution_bars(
    events: pd.DataFrame,
    close: pd.DataFrame,
    market_days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """The ETF's own close row each distribution is applied on, with the rule used.

    rule: same_day (the ETF has a close on the Yahoo date); non_trading_day_to_previous (the Yahoo date
    is not a KRX trading day, so the ex-date is taken as the ETF's previous close row);
    no_price_to_next (a KRX trading day without an ETF close, e.g. a halt: the next close row).
    Events left unapplied get bar NaT and a reason: invalid_amount, unknown_symbol,
    outside_cache_window, before_first_price (no earlier close to measure the return against) or
    after_last_price.
    """
    out = events.loc[:, ["Date", "Symbol", "Amount"]].copy().reset_index(drop=True)
    out["bar"] = pd.NaT
    out["rule"] = None
    out["unapplied"] = None
    out.loc[~out["Amount"].gt(0.0), "unapplied"] = "invalid_amount"
    out.loc[out["unapplied"].isna() & ~out["Symbol"].isin(close.columns), "unapplied"] = "unknown_symbol"
    if len(close.index):
        window = out["Date"].between(close.index.min(), close.index.max())
        out.loc[out["unapplied"].isna() & ~window, "unapplied"] = "outside_cache_window"
    market_days = pd.DatetimeIndex(market_days)
    for symbol, rows in out.loc[out["unapplied"].isna()].groupby("Symbol"):
        valid = pd.DatetimeIndex(close[symbol].dropna().index)
        dates = pd.DatetimeIndex(rows["Date"])
        if len(valid) == 0:
            out.loc[rows.index, "unapplied"] = "after_last_price"
            continue
        same = dates.isin(valid)
        trading = dates.isin(market_days)
        previous = valid.searchsorted(dates, side="right") - 1
        following = valid.searchsorted(dates, side="left")
        for label, date, is_same, is_trading, prev_pos, next_pos in zip(
            rows.index, dates, same, trading, previous, following
        ):
            if is_same:
                bar, rule = date, "same_day"
            elif not is_trading:
                bar = valid[prev_pos] if prev_pos >= 0 else pd.NaT
                rule = "non_trading_day_to_previous"
            else:
                bar = valid[next_pos] if next_pos < len(valid) else pd.NaT
                rule = "no_price_to_next"
            if pd.isna(bar):
                out.at[label, "unapplied"] = "after_last_price" if rule == "no_price_to_next" else "before_first_price"
            elif bar == valid[0]:
                out.at[label, "unapplied"] = "before_first_price"
            else:
                out.at[label, "bar"] = bar
                out.at[label, "rule"] = rule
    out["bar"] = pd.to_datetime(out["bar"])
    return out


def distributions_on_bars(bars: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Per close row and ETF, the applied distribution amount (0 elsewhere)."""
    applied = bars.loc[bars["status"].isin(APPLIED_STATUSES)]
    if applied.empty:
        return pd.DataFrame(0.0, index=close.index, columns=close.columns)
    wide = applied.pivot_table(index="bar", columns="Symbol", values="amount", aggfunc="sum")
    return wide.reindex(index=close.index, columns=close.columns).fillna(0.0)


def compute_total_return_price(close: pd.DataFrame, distributions: pd.DataFrame) -> pd.DataFrame:
    """Close-based total return price: starts at each ETF's first close and grows by
    (close_t + distribution_t) / previous close each close row, i.e. a distribution reinvested in
    the same ETF at the ex-date close. Values never depend on later rows.
    """
    if (close <= 0).any().any():
        raise ValueError("trade_price has non-positive closes")
    dist = distributions.reindex(index=close.index, columns=close.columns).fillna(0.0)
    previous = close.ffill().shift(1)
    measured = close.notna() & previous.notna()
    ratio = ((close + dist) / previous).where(measured, 1.0)
    first = close.bfill().iloc[0] if len(close.index) else pd.Series(dtype="float64")
    return ratio.cumprod().mul(first, axis=1).where(close.notna())


def evaluate_distributions(
    mapped: pd.DataFrame,
    close: pd.DataFrame,
    index_value: pd.DataFrame,
) -> pd.DataFrame:
    """One row per (ETF, applied bar) with the price check and its status.

    share = amount / previous close; excess = ETF return - index return; noise = median |excess| over
    the previous NOISE_WINDOW rows (NOISE_MIN_OBS values or more, current row excluded). Only data up
    to the bar is used, so later rows never change a decision.

    status: applied_confirmed (share >= DETECTABLE_MULTIPLE x noise and the ETF return or excess <=
    -CONFIRM_FRACTION x share on the bar or the ETF's previous close row); excluded_contradicted
    (detectable but none of these shows that drop); applied_small (index and noise known, share too small to see);
    applied_no_check (no index return or noise history); excluded_implausible (not confirmed and share
    > IMPLAUSIBLE_SHARE).
    """
    columns = ["Symbol", "bar", "amount", "yahoo_dates", "rules", "share", "etf_return", "index_return",
               "excess_previous_row", "noise", "status"]
    applied = mapped.loc[mapped["unapplied"].isna()]
    if applied.empty:
        return pd.DataFrame(columns=columns)
    bars = applied.groupby(["Symbol", "bar"], as_index=False).agg(
        amount=("Amount", "sum"),
        yahoo_dates=("Date", lambda values: ",".join(v.date().isoformat() for v in values)),
        rules=("rule", lambda values: ",".join(sorted(set(values)))),
    )
    index_value = index_value.reindex(index=close.index, columns=close.columns).where(lambda frame: frame > 0)
    previous_close = close.ffill().shift(1)
    etf_return = close / previous_close - 1.0
    index_on_close = index_value.where(close.notna())
    index_return = index_on_close / index_on_close.ffill().shift(1) - 1.0
    excess = (etf_return - index_return).where(close.notna())
    excess_previous_row = excess.where(close.notna()).ffill().shift(1).where(close.notna())
    return_previous_row = etf_return.where(close.notna()).ffill().shift(1).where(close.notna())
    noise = excess.abs().rolling(NOISE_WINDOW, min_periods=NOISE_MIN_OBS).median().shift(1)
    rows = close.index.get_indexer(bars["bar"])
    cols = close.columns.get_indexer(bars["Symbol"])

    def pick(frame: pd.DataFrame) -> np.ndarray:
        return frame.to_numpy()[rows, cols]

    share = bars["amount"].to_numpy(dtype="float64") / pick(previous_close)
    bar_excess, before_excess, bar_noise = pick(excess), pick(excess_previous_row), pick(noise)
    checkable = ~np.isnan(bar_excess) & ~np.isnan(bar_noise)
    detectable = checkable & (share >= DETECTABLE_MULTIPLE * bar_noise)
    limit = -CONFIRM_FRACTION * share
    moves = (bar_excess, before_excess, pick(etf_return), pick(return_previous_row))
    shown = np.logical_or.reduce([np.nan_to_num(move, nan=np.inf) <= limit for move in moves])
    confirmed = detectable & shown
    status = np.where(
        confirmed, "applied_confirmed",
        np.where(share > IMPLAUSIBLE_SHARE, "excluded_implausible",
                 np.where(detectable, "excluded_contradicted",
                          np.where(checkable, "applied_small", "applied_no_check"))),
    )
    return bars.assign(
        share=share, etf_return=pick(etf_return), index_return=pick(index_return),
        excess_previous_row=before_excess, noise=bar_noise, status=status,
    ).loc[:, columns]


def _rounded(value: object) -> float | None:
    value = float(value)
    return None if np.isnan(value) else round(value * 100, 4)


def build_total_return_price(
    close: pd.DataFrame,
    events: pd.DataFrame,
    index_value: pd.DataFrame,
    market_days: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, dict[str, object]]:
    mapped = map_distribution_bars(events, close, market_days)
    bars = evaluate_distributions(mapped, close, index_value)
    total_return = compute_total_return_price(close, distributions_on_bars(bars, close))
    unapplied = mapped.loc[mapped["unapplied"].notna()]
    excluded = bars.loc[~bars["status"].isin(APPLIED_STATUSES)].sort_values("share", ascending=False)
    report = {
        "source": DISTRIBUTION_FILE,
        "events": int(len(mapped)),
        "mapping": {str(key): int(value) for key, value in mapped["rule"].value_counts().items()},
        "unapplied": {str(key): int(value) for key, value in unapplied["unapplied"].value_counts().items()},
        "unapplied_events": [
            {"symbol": row.Symbol, "date": row.Date.date().isoformat(), "amount": float(row.Amount),
             "reason": row.unapplied}
            for row in unapplied.loc[unapplied["unapplied"] != "outside_cache_window"]
            .head(REPORT_LIST_LIMIT).itertuples()
        ],
        "bars": int(len(bars)),
        "applied_bars": int(bars["status"].isin(APPLIED_STATUSES).sum()),
        "status": {str(key): int(value) for key, value in bars["status"].value_counts().items()},
        "rule": (
            f"share = amount / previous close; excess = ETF return - IndexValue return; noise = median |excess| "
            f"over the previous {NOISE_WINDOW} rows (>= {NOISE_MIN_OBS} values). Detectable when share >= "
            f"{DETECTABLE_MULTIPLE} x noise; confirmed when the ETF return or excess <= -{CONFIRM_FRACTION} x share "
            f"on the bar or the previous close row. Excluded: detectable but not confirmed, or not confirmed with share > "
            f"{IMPLAUSIBLE_SHARE}."
        ),
        "excluded_bars": [
            {"symbol": row.Symbol, "bar": row.bar.date().isoformat(), "yahoo_dates": row.yahoo_dates,
             "status": row.status, "amount": float(row.amount), "share_pct": _rounded(row.share),
             "etf_return_pct": _rounded(row.etf_return), "index_return_pct": _rounded(row.index_return),
             "excess_previous_row_pct": _rounded(row.excess_previous_row), "noise_pct": _rounded(row.noise)}
            for row in excluded.head(REPORT_LIST_LIMIT).itertuples()
        ],
        "assumption": "distribution reinvested in the same ETF at the ex-date close; gross of tax",
    }
    return total_return, report


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
    distributions: dict[str, object],
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
        "distributions": distributions,
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
    market_days = pd.DatetimeIndex(sorted(pykrx.loc[pykrx["Close"].notna(), "Date"].unique()))
    frames["total_return_price"], distribution_report = build_total_return_price(
        frames["trade_price"],
        _load_distributions(raw_dir / DISTRIBUTION_FILE),
        _pivot(pykrx, value_column="IndexValue", index=index, columns=markets),
        market_days,
    )
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
    _write_report(
        out_dir, raw_dir=raw_dir, frames=frames, markets=markets, index=index,
        distributions=distribution_report,
    )

    print(f"Wrote Korean ETF source cache to {out_dir}")
    print(f"Rows: {len(index)}")
    print(f"Markets: {len(markets)}")
    print(f"Start: {index.min().date()}")
    print(f"End: {index.max().date()}")
    print(
        f"Distributions: events={distribution_report['events']} mapping={distribution_report['mapping']} "
        f"unapplied={distribution_report['unapplied']} bars={distribution_report['bars']} "
        f"status={distribution_report['status']}"
    )
    print(
        "Wrote: trade_price, total_return_price, signal_price, candle_acc_trade_volume, "
        "candle_acc_trade_price, price_available, market_warning, market_meta"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
