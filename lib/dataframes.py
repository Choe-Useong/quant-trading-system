from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
import pandas as pd


CSV_ENCODING = "utf-8-sig"
CACHE_ELIGIBILITY_FRAME = "__cache_eligible__"


@lru_cache(maxsize=4)
def _cache_data_starts(path: str, mtime_ns: int, size: int):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    policy = payload.get("data_usage_policy")
    records = payload.get("records", {})
    if policy is None and not any(r.get("data_usage_basis") == "observed_prices"
                                  or (r.get("lifecycle_verified", True) and r.get("known_end_exclusive_utc"))
                                  for r in records.values()):
        return None
    if policy not in {None, "exclude_incomplete_prefix"}:
        raise ValueError("Unsupported cache data_usage_policy")
    starts = {}
    for market, record in records.items():
        observed = not record.get("lifecycle_verified", True)
        value = (record["data_start_inclusive"] if policy else record.get("data_start_inclusive")
                 if observed else record["active_bar_start_inclusive"])
        stamp = pd.Timestamp(value) if value is not None else None
        ending = record.get("data_end_exclusive")
        if stamp is not None and not observed and record.get("known_end_exclusive_utc"):
            # The collector excludes an incomplete terminal candle. Its active
            # end is bounded by the sourced termination time and cache period.
            bounds = [pd.to_datetime(record["active_bar_end_exclusive"], utc=True),
                      pd.to_datetime(record["known_end_exclusive_utc"], utc=True)]
            if ending is not None:
                bounds.append(pd.to_datetime(ending, utc=True))
            ending = min(bounds)
        ending = pd.Timestamp(ending) if ending is not None else None
        if stamp is not None:
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            lower, upper = (pd.Timestamp(record[key]) for key in ("active_bar_start_inclusive", "active_bar_end_exclusive"))
            if (pd.isna(stamp) or not lower <= stamp < upper
                    or (observed and (record.get("data_usage_basis") != "observed_prices" or ending is None))):
                raise ValueError(f"Invalid cache data start: {market}")
            if ending is not None:
                ending = ending.tz_localize("UTC") if ending.tzinfo is None else ending.tz_convert("UTC")
                if pd.isna(ending) or not stamp < ending <= upper:
                    raise ValueError(f"Invalid cache data end: {market}")
        elif ending is not None:
            raise ValueError(f"Cache data end without a start: {market}")
        starts[market] = (stamp, ending)
    return starts


def cache_data_usage_mask(cache_dir: Path, index, markets, *, data_usage_starts=None) -> pd.DataFrame | None:
    """Explicit research coverage, separate from the exchange listing history."""
    path = Path(cache_dir) / "instruments.json"
    starts = None
    if path.is_file():
        stat = path.stat()
        starts = _cache_data_starts(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if starts is None and not data_usage_starts:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    columns = pd.Index(markets)
    if dates.hasnans or (starts is not None and not set(columns) <= starts.keys()):
        raise ValueError("Cache data usage metadata is missing requested markets or dates")
    values = np.ones((len(dates), len(columns)), dtype=bool) if starts is None else np.zeros((len(dates), len(columns)), dtype=bool)
    for column, market in enumerate(columns):
        if starts is not None:
            beginning, ending = starts[market]
            if beginning is not None:
                values[:, column] = dates >= beginning
                if ending is not None:
                    values[:, column] &= dates < ending
        if data_usage_starts and market in data_usage_starts:
            beginning = data_usage_starts[market]
            if beginning is None:
                values[:, column] = False
            else:
                beginning = pd.Timestamp(beginning)
                beginning = beginning.tz_localize("UTC") if beginning.tzinfo is None else beginning.tz_convert("UTC")
                values[:, column] &= dates >= beginning
    result = pd.DataFrame(values, index=index, columns=columns)
    result.attrs["data_usage_signature"] = (
        tuple((market, *(s.value if s is not None else None for s in starts[market])) for market in columns) if starts is not None else (),
        tuple((market, str(data_usage_starts[market])) for market in columns if data_usage_starts and market in data_usage_starts),
    )
    return result


@lru_cache(maxsize=4)
def _cache_instrument_records(path: str, mtime_ns: int, size: int):
    return json.loads(Path(path).read_text(encoding="utf-8")).get("records", {})


def cache_instrument_records(cache_dir: Path, markets) -> dict:
    path = Path(cache_dir) / "instruments.json"
    if not path.is_file():
        return {}
    stat = path.stat()
    records = _cache_instrument_records(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    return {market: records[market] for market in markets if market in records}


def cache_unverified_lifecycle_records(cache_dir: Path, markets) -> dict:
    return {market: record for market, record in cache_instrument_records(cache_dir, markets).items()
            if not record.get("lifecycle_verified", True)}


@lru_cache(maxsize=4)
def _funding_start_adjustments(cache_path: str, signature: tuple) -> dict:
    cache = Path(cache_path)
    manifest = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("cache_kind") != "archive_observed_futures":
        return {}
    candidates = {m: [i for i in issues if i["reason"] == "missing_funding"]
                  for m, issues in manifest["quality_intervals"].items()}
    records = cache_instrument_records(cache, candidates)
    candidates = {m: issues for m, issues in candidates.items() if issues or not records.get(m, {}).get("lifecycle_verified", True)}
    if not candidates:
        return {}
    rates = pd.read_parquet(cache / "funding_rate.parquet", columns=sorted(candidates))
    rates.index = pd.to_datetime(rates.index, utc=True)
    if rates.index.has_duplicates or not rates.index.is_monotonic_increasing:
        raise ValueError("Funding observations require unique sorted timestamps")
    result = {}
    for market, issues in candidates.items():
        record = records.get(market, {})
        floor = record.get("data_start_inclusive") or record.get("active_bar_start_inclusive") or manifest["cache_start_inclusive"]
        floor = pd.Timestamp(floor)
        floor = floor.tz_localize("UTC") if floor.tzinfo is None else floor.tz_convert("UTC")
        upper = pd.to_datetime(record.get("data_end_exclusive") or record.get("active_bar_end_exclusive")
                               or manifest["cache_end_exclusive"], utc=True)
        series = rates.loc[(rates.index >= floor) & (rates.index < upper), market]
        actual = series.index[np.isfinite(series.to_numpy(dtype=float))]
        first = actual[0] if len(actual) else None
        prefix = [i for i in issues if pd.to_datetime(i["end_exclusive"], utc=True) > floor
                  and (first is None or pd.to_datetime(i["start_inclusive"], utc=True) < first)]
        if not prefix and not record.get("lifecycle_verified", True):
            from lib.funding_schedule_v2 import funding_settlement_index
            if first is None:
                prefix = [{"reason": "missing_funding", "source": "no_observed_funding"}]
            elif record.get("funding_schedule"):
                expected = funding_settlement_index(floor, first, record.get("funding_interval_hours", 8), record["funding_schedule"])
                if len(expected):
                    prefix = [{"reason": "missing_funding", "source": "observed_calendar",
                               "start_inclusive": expected[0].isoformat(), "end_exclusive": first.isoformat()}]
        if prefix:
            result[market] = {"reason": "leading_missing_funding", "previous_data_start_inclusive": floor.isoformat(),
                              "data_start_inclusive": first.isoformat() if first is not None else None,
                              "missing_intervals": prefix}
    return result


def cache_funding_start_adjustments(cache_dir: Path) -> dict:
    cache = Path(cache_dir).resolve()
    if not (cache / "manifest.json").is_file():
        return {}
    signature = []
    for name in ("manifest.json", "instruments.json", "funding_rate.parquet"):
        path = cache / name
        stat = path.stat() if path.is_file() else None
        signature.append((name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if stat is not None else (name, None))
    return _funding_start_adjustments(str(cache), tuple(signature))


def read_market_candle_frame(
    path: Path,
    usecols: Sequence[str] | None = None,
    tail_rows: int | None = None,
) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=list(usecols) if usecols is not None else None, encoding=CSV_ENCODING)
    if tail_rows is not None and len(frame) > tail_rows:
        frame = frame.iloc[-tail_rows:].copy()
    if "date_utc" in frame.columns:
        frame["date_utc"] = pd.to_datetime(frame["date_utc"], utc=False)
        frame = frame.sort_values("date_utc")
    return frame


def iter_market_candle_frames(
    candle_dir: Path,
    usecols: Sequence[str] | None = None,
    pattern: str = "*.csv",
    max_markets: int | None = None,
    tail_rows: int | None = None,
) -> Iterator[tuple[Path, pd.DataFrame]]:
    paths = sorted(candle_dir.glob(pattern))
    if max_markets is not None:
        paths = paths[:max_markets]
    for path in paths:
        yield path, read_market_candle_frame(path, usecols=usecols, tail_rows=tail_rows)


def build_wide_frames_from_candle_dir(
    candle_dir: Path,
    value_columns: Sequence[str],
    *,
    pattern: str = "*.csv",
    max_markets: int | None = None,
    tail_rows: int | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, str]]]:
    required_columns = [
        "market",
        "korean_name",
        "english_name",
        "market_warning",
        "date_utc",
        *value_columns,
    ]
    series_by_column: dict[str, list[pd.Series]] = {column: [] for column in value_columns}
    meta_by_market: dict[str, dict[str, str]] = {}

    for _, frame in iter_market_candle_frames(
        candle_dir,
        usecols=required_columns,
        pattern=pattern,
        max_markets=max_markets,
        tail_rows=tail_rows,
    ):
        if frame.empty:
            continue
        market = str(frame["market"].iloc[0]).upper()
        meta_by_market[market] = {
            "market": market,
            "korean_name": str(frame["korean_name"].iloc[0]),
            "english_name": str(frame["english_name"].iloc[0]),
            "market_warning": str(frame["market_warning"].iloc[0]).upper(),
        }
        indexed = frame.set_index("date_utc")
        for column in value_columns:
            series = indexed[column].astype(float).rename(market)
            series_by_column[column].append(series)

    frames: dict[str, pd.DataFrame] = {}
    for column, series_list in series_by_column.items():
        if not series_list:
            frames[column] = pd.DataFrame()
            continue
        frames[column] = pd.concat(series_list, axis=1).sort_index().sort_index(axis=1)
    return frames, meta_by_market


def build_wide_frame_from_candle_dir(
    candle_dir: Path,
    value_column: str,
    *,
    pattern: str = "*.csv",
    max_markets: int | None = None,
    tail_rows: int | None = None,
) -> pd.DataFrame:
    frames, _ = build_wide_frames_from_candle_dir(
        candle_dir,
        [value_column],
        pattern=pattern,
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    return frames[value_column]


def read_wide_frame_from_cache(
    cache_dir: Path,
    value_column: str,
    *,
    market_columns: Sequence[str] | None = None,
    max_markets: int | None = None,
    tail_rows: int | None = None,
) -> pd.DataFrame:
    path = cache_dir / f"{value_column}.parquet"
    requested_columns = None
    if market_columns is not None:
        requested_columns = sorted({str(column).upper() for column in market_columns})
    try:
        frame = pd.read_parquet(path, columns=requested_columns)
    except Exception:
        frame = pd.read_parquet(path)
        if requested_columns is not None:
            frame = frame.reindex(columns=requested_columns)
    frame.index = pd.to_datetime(frame.index, utc=False)
    frame = frame.sort_index().sort_index(axis=1)
    if requested_columns is not None:
        frame = frame.reindex(columns=requested_columns)
    elif max_markets is not None:
        frame = frame.reindex(columns=sorted(frame.columns)[:max_markets])
    if tail_rows is not None and len(frame) > tail_rows:
        frame = frame.iloc[-tail_rows:].copy()
    return frame


def read_wide_frames_from_cache(
    cache_dir: Path,
    value_columns: Sequence[str],
    *,
    market_columns: Sequence[str] | None = None,
    max_markets: int | None = None,
    tail_rows: int | None = None,
) -> dict[str, pd.DataFrame]:
    return {
        value_column: read_wide_frame_from_cache(
            cache_dir,
            value_column,
            market_columns=market_columns,
            max_markets=max_markets,
            tail_rows=tail_rows,
        )
        for value_column in value_columns
    }


def build_long_frame_from_candle_dir(
    candle_dir: Path,
    usecols: Sequence[str] | None = None,
    *,
    pattern: str = "*.csv",
    max_markets: int | None = None,
    tail_rows: int | None = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for _, frame in iter_market_candle_frames(
        candle_dir,
        usecols=usecols,
        pattern=pattern,
        max_markets=max_markets,
        tail_rows=tail_rows,
    ):
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=0, ignore_index=True).sort_values(["date_utc", "market"])


def apply_by_market_column(
    frame: pd.DataFrame,
    transform,
) -> pd.DataFrame:
    result: dict[str, pd.Series] = {}
    for column in frame.columns:
        series = frame[column]
        valid = series.dropna()
        transformed = transform(valid)
        result[column] = transformed.reindex(frame.index)
    if not result:
        return pd.DataFrame(index=frame.index)
    return pd.DataFrame(result, index=frame.index).sort_index(axis=1)


def compute_market_momentum_frame(
    price_frame: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    return apply_by_market_column(
        price_frame,
        lambda series: (series / series.shift(periods)) - 1.0,
    )


def compute_market_median_momentum_frame(
    price_frame: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    return apply_by_market_column(
        price_frame,
        lambda series: series.pct_change(fill_method=None).rolling(periods, min_periods=periods).median(),
    )


def compute_market_win_rate_frame(
    price_frame: pd.DataFrame,
    periods: int,
    threshold: float = 0.0,
) -> pd.DataFrame:
    return apply_by_market_column(
        price_frame,
        lambda series: (
            series.pct_change(fill_method=None) > threshold
        ).astype(float).rolling(periods, min_periods=periods).mean(),
    )


def compute_market_trend_quality_frame(
    price_frame: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    n = float(periods)
    weights = np.arange(periods, dtype=float)
    sum_x = float(weights.sum())
    sum_x2 = float((weights**2).sum())
    denom_x = (n * sum_x2) - (sum_x**2)

    def _trend_quality(series: pd.Series) -> pd.Series:
        log_price = np.log(series.where(series > 0.0).astype(float))
        sum_y = log_price.rolling(periods, min_periods=periods).sum()
        sum_y2 = log_price.pow(2).rolling(periods, min_periods=periods).sum()

        sum_xy = pd.Series(np.nan, index=log_price.index, dtype=float)
        y_values = log_price.to_numpy(dtype=float, copy=False)
        if len(y_values) >= periods:
            weighted_sum = np.correlate(y_values, weights, mode="valid")
            sum_xy.iloc[periods - 1 :] = weighted_sum

        numer = (n * sum_xy) - (sum_x * sum_y)
        denom_y = (n * sum_y2) - sum_y.pow(2)
        slope = numer / denom_x
        r_squared = (numer.pow(2)) / (denom_x * denom_y)
        r_squared = r_squared.where(denom_y > 0.0).clip(lower=0.0)
        return slope * r_squared

    return apply_by_market_column(price_frame, _trend_quality)


def compute_market_consistency_ratio_frame(
    price_frame: pd.DataFrame,
    periods: int,
    epsilon: float = 1e-12,
) -> pd.DataFrame:
    def _consistency_ratio(series: pd.Series) -> pd.Series:
        returns = series.pct_change(fill_method=None)
        rolling_mean = returns.rolling(periods, min_periods=periods).mean()
        rolling_median = returns.rolling(periods, min_periods=periods).median()
        denom = rolling_mean.abs().where(lambda value: value > epsilon)
        return rolling_median / denom

    return apply_by_market_column(price_frame, _consistency_ratio)


def compute_market_beta_frame(
    price_frame: pd.DataFrame,
    benchmark_price: pd.Series,
    periods: int,
) -> pd.DataFrame:
    benchmark_series = benchmark_price.reindex(price_frame.index).astype(float)
    benchmark_returns = benchmark_series.pct_change(fill_method=None)
    benchmark_var = benchmark_returns.rolling(periods, min_periods=periods).var().replace(0.0, pd.NA)

    result: dict[str, pd.Series] = {}
    for column in price_frame.columns:
        asset_returns = price_frame[column].astype(float).pct_change(fill_method=None)
        cov = asset_returns.rolling(periods, min_periods=periods).cov(benchmark_returns)
        result[column] = (cov / benchmark_var).reindex(price_frame.index)
    if not result:
        return pd.DataFrame(index=price_frame.index)
    return pd.DataFrame(result, index=price_frame.index).sort_index(axis=1)


def compute_market_residual_momentum_frame(
    price_frame: pd.DataFrame,
    benchmark_price: pd.Series,
    momentum_periods: int,
    beta_lookback_periods: int,
) -> pd.DataFrame:
    beta_frame = compute_market_beta_frame(price_frame, benchmark_price, beta_lookback_periods)
    benchmark_series = benchmark_price.reindex(price_frame.index).astype(float)
    benchmark_momentum = (benchmark_series / benchmark_series.shift(momentum_periods)) - 1.0
    market_momentum = compute_market_momentum_frame(price_frame, momentum_periods)
    return market_momentum.sub(beta_frame.mul(benchmark_momentum, axis=0), axis=0)


def compute_market_turnover_weighted_momentum_frame(
    price_frame: pd.DataFrame,
    turnover_frame: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    if not price_frame.columns.equals(turnover_frame.columns):
        turnover_frame = turnover_frame.reindex(columns=price_frame.columns)
    if not price_frame.index.equals(turnover_frame.index):
        turnover_frame = turnover_frame.reindex(index=price_frame.index)

    result: dict[str, pd.Series] = {}
    for column in price_frame.columns:
        price_series = price_frame[column].dropna()
        weight_series = turnover_frame[column].reindex(price_series.index).dropna()
        common_index = price_series.index.intersection(weight_series.index)
        if common_index.empty:
            result[column] = pd.Series(index=price_frame.index, dtype=float)
            continue
        aligned_price = price_series.loc[common_index]
        aligned_weight = weight_series.loc[common_index].astype(float)
        returns = aligned_price.pct_change()
        weighted_returns = returns * aligned_weight
        numerator = weighted_returns.rolling(periods, min_periods=periods).sum()
        denominator = aligned_weight.rolling(periods, min_periods=periods).sum()
        transformed = numerator / denominator.replace(0.0, pd.NA)
        result[column] = transformed.reindex(price_frame.index)
    if not result:
        return pd.DataFrame(index=price_frame.index)
    return pd.DataFrame(result, index=price_frame.index).sort_index(axis=1)


def compute_market_forward_return_frame(
    price_frame: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    return apply_by_market_column(
        price_frame,
        lambda series: (series.shift(-periods) / series) - 1.0,
    )


def compute_market_rolling_sum_frame(
    frame: pd.DataFrame,
    window: int,
) -> pd.DataFrame:
    return apply_by_market_column(
        frame,
        lambda series: series.rolling(window, min_periods=window).sum(),
    )
