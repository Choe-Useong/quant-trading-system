from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
from hmmlearn import _hmmc
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

from lib.dataframes import read_wide_frames_from_cache
from lib.specs import FeatureSpec


SUPPORTED_SOURCE_COLUMNS = {
    "trade_price",
    "signal_price",
    "index_price",
    "opening_price",
    "high_price",
    "low_price",
    "candle_acc_trade_volume",
    "candle_acc_trade_price",
    "market_cap",
    "market_code",
    "price_available",
    "marcap_available",
    "timestamp",
}

SUPPORTED_TRANSFORMS = {
    "abs",
    "log",
    "true_range",
    "hump_step_weight",
    "linear_ramp_weight",
    "quantize_weight",
    "stepped_cap_weight",
    "target_vol_weight",
    "elementwise_min",
    "elementwise_max",
    "mask_by_feature",
    "select_by_state",
    "corr_greedy_filter",
    "clip",
    "expanding_std",
    "expanding_percentile",
    "rolling_mean",
    "rolling_sum",
    "rolling_std",
    "rolling_max",
    "rolling_percentile",
    "rolling_zscore",
    "downside_vol",
    "rolling_drawdown_min",
    "efficiency_ratio",
    "hmm_filtered_state",
    "hmm_filtered_prob",
    "momentum",
    "period_simple_return",
    "simple_return",
    "shift",
    "delta",
    "ewma",
    "wma",
    "age_days",
    "cross_rank",
    "cross_percentile",
    "group_demean",
    "group_rank",
    "group_percentile",
    "calendar_hold",
    "calendar_mean",
    "calendar_rolling_robust_mean",
    "calendar_rolling_sum",
    "gaussian_signed",
    "residualize_reference",
    "subtract_reference",
    "ratio_to_reference",
}


def _apply_per_market_tail(
    frames: dict[str, pd.DataFrame],
    tail_rows: int | None,
) -> dict[str, pd.DataFrame]:
    if tail_rows is None or tail_rows <= 0 or not frames:
        return frames
    primary_name = "trade_price" if "trade_price" in frames else next(iter(frames.keys()))
    primary = frames[primary_name].copy()
    for column in primary.columns:
        valid_index = primary[column].dropna().index
        if len(valid_index) <= tail_rows:
            continue
        trimmed_index = valid_index[:-tail_rows]
        primary.loc[trimmed_index, column] = np.nan
    keep_index = primary.notna().any(axis=1)
    updated: dict[str, pd.DataFrame] = {}
    for name, frame in frames.items():
        next_frame = frame.copy()
        if not frame.columns.equals(primary.columns):
            next_frame = next_frame.reindex(columns=primary.columns)
        for column in primary.columns:
            valid_index = frame[column].dropna().index
            if len(valid_index) <= tail_rows:
                continue
            trimmed_index = valid_index[:-tail_rows]
            next_frame.loc[trimmed_index, column] = np.nan
        updated[name] = next_frame.loc[keep_index].copy()
    return updated


def _ewma_series(series: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    valid = series.dropna()
    if len(valid) < window:
        return result

    alpha = 2.0 / (window + 1.0)
    seed_index = valid.index[:window]
    current = float(valid.iloc[:window].mean())
    result.loc[seed_index[-1]] = current

    for timestamp in valid.index[window:]:
        current = (alpha * float(valid.at[timestamp])) + ((1.0 - alpha) * current)
        result.loc[timestamp] = current
    return result


def _wma_series(series: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    valid = series.dropna()
    if len(valid) < window:
        return result

    weights = np.arange(1, window + 1, dtype=float)
    weight_sum = float(weights.sum())
    values = valid.to_numpy(dtype=float, copy=False)
    windows = np.lib.stride_tricks.sliding_window_view(values, window_shape=window)
    weighted = (windows * weights).sum(axis=1) / weight_sum
    result.loc[valid.index[window - 1 :]] = weighted
    return result


def _age_days_frame(frame: pd.DataFrame) -> pd.DataFrame:
    index = pd.to_datetime(frame.index, utc=False)
    result = pd.DataFrame(np.nan, index=frame.index, columns=frame.columns, dtype=float)
    for column in frame.columns:
        valid_mask = frame[column].notna()
        if not bool(valid_mask.any()):
            continue
        first_valid = pd.Timestamp(index[valid_mask.to_numpy()].min())
        age_days = (index - first_valid).total_seconds() / 86400.0
        result[column] = pd.Series(age_days, index=frame.index).where(valid_mask)
    return result


def _apply_by_market_column(
    frame: pd.DataFrame,
    transform,
) -> pd.DataFrame:
    result: dict[str, pd.Series] = {}
    for column in frame.columns:
        valid = frame[column].dropna().astype(float)
        transformed = transform(valid)
        result[column] = transformed.reindex(frame.index)
    return pd.DataFrame(result, index=frame.index).sort_index(axis=1)


def _delta_frame(frame: pd.DataFrame, periods: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: series - series.shift(periods))


def _shift_frame(frame: pd.DataFrame, periods: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: series.shift(periods))


def _simple_return_frame(frame: pd.DataFrame, periods: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: (series / series.shift(periods)) - 1.0)


def _normalize_period_freq(freq: str) -> str:
    normalized = str(freq).strip().lower()
    if normalized in {"m", "month", "monthly"}:
        return "M"
    if normalized in {"w", "week", "weekly"}:
        return "W"
    if normalized in {"q", "quarter", "quarterly"}:
        return "Q"
    return str(freq).strip()


def _period_simple_return_series(
    series: pd.Series,
    *,
    freq: str,
    periods: int,
    signal_timing: str,
) -> pd.Series:
    if periods <= 0:
        raise ValueError("periods must be positive for period_simple_return")

    period_freq = _normalize_period_freq(freq)
    timing = str(signal_timing).strip().lower()
    if timing not in {"next_period", "same_period"}:
        raise ValueError("signal_timing must be 'next_period' or 'same_period' for period_simple_return")

    valid = series.dropna().astype(float)
    if valid.empty:
        return pd.Series(np.nan, index=series.index, name=series.name, dtype=float)

    period_index = valid.index.to_period(period_freq)
    period_close = valid.groupby(period_index).last()
    period_return = (period_close / period_close.shift(periods)) - 1.0
    if timing == "next_period":
        period_return = period_return.shift(1)

    mapped = pd.Series(period_index.map(period_return), index=valid.index, name=series.name, dtype=float)
    return mapped.reindex(series.index)


def _map_period_series_to_daily(
    series: pd.Series,
    period_values: pd.Series,
    *,
    freq: str,
    signal_timing: str,
) -> pd.Series:
    period_freq = _normalize_period_freq(freq)
    timing = str(signal_timing).strip().lower()
    if timing not in {"next_period", "same_period"}:
        raise ValueError("signal_timing must be 'next_period' or 'same_period'")

    valid = series.dropna().astype(float)
    if valid.empty:
        return pd.Series(np.nan, index=series.index, name=series.name, dtype=float)

    mapped_period_values = period_values.copy()
    if timing == "next_period":
        mapped_period_values.index = mapped_period_values.index + 1

    period_index = valid.index.to_period(period_freq)
    mapped = pd.Series(period_index.map(mapped_period_values), index=valid.index, name=series.name, dtype=float)
    return mapped.reindex(series.index)


def _calendar_mean_series(
    series: pd.Series,
    *,
    freq: str,
    signal_timing: str,
) -> pd.Series:
    valid = series.dropna().astype(float)
    if valid.empty:
        return pd.Series(np.nan, index=series.index, name=series.name, dtype=float)

    period_freq = _normalize_period_freq(freq)
    period_index = valid.index.to_period(period_freq)
    period_mean = valid.groupby(period_index).mean()
    return _map_period_series_to_daily(
        series,
        period_mean,
        freq=period_freq,
        signal_timing=signal_timing,
    )


def _map_period_frame_to_daily(
    frame: pd.DataFrame,
    period_values: pd.DataFrame,
    *,
    freq: str,
    signal_timing: str,
    preserve_input_mask: bool = True,
) -> pd.DataFrame:
    period_freq = _normalize_period_freq(freq)
    timing = str(signal_timing).strip().lower()
    if timing not in {"next_period", "same_period"}:
        raise ValueError("signal_timing must be 'next_period' or 'same_period'")

    if frame.empty:
        return frame.astype(float).copy()

    period_values = period_values.reindex(columns=frame.columns).astype(float)
    if timing == "next_period":
        period_values = period_values.copy()
        period_values.index = period_values.index + 1

    row_periods = pd.Index(pd.to_datetime(frame.index, utc=False).to_period(period_freq))
    positions = period_values.index.get_indexer(row_periods)
    output = np.full((len(frame.index), len(frame.columns)), np.nan, dtype=float)
    matched = positions >= 0
    if bool(matched.any()):
        output[matched, :] = period_values.to_numpy(dtype=float, copy=False)[positions[matched], :]

    result = pd.DataFrame(output, index=frame.index, columns=frame.columns)
    return result.where(frame.notna()) if preserve_input_mask else result


def _calendar_mean_frame(
    frame: pd.DataFrame,
    *,
    freq: str,
    signal_timing: str,
) -> pd.DataFrame:
    period_freq = _normalize_period_freq(freq)
    values = frame.astype(float)
    if values.empty:
        return values.copy()

    period_index = pd.Index(pd.to_datetime(values.index, utc=False).to_period(period_freq))
    period_mean = values.groupby(period_index).mean()
    return _map_period_frame_to_daily(
        values,
        period_mean,
        freq=period_freq,
        signal_timing=signal_timing,
    )


def _calendar_hold_frame(
    frame: pd.DataFrame,
    *,
    freq: str,
    hold_periods: int,
    signal_timing: str,
    anchor: str = "calendar",
    broadcast_sparse: bool = False,
) -> pd.DataFrame:
    if hold_periods <= 0:
        raise ValueError("hold_periods must be positive for calendar_hold")

    period_freq = _normalize_period_freq(freq)
    timing = str(signal_timing).strip().lower()
    if timing not in {"next_period", "same_period"}:
        raise ValueError("signal_timing must be 'next_period' or 'same_period' for calendar_hold")

    values = frame.astype(float)
    if values.empty:
        return values.copy()

    row_periods = pd.Index(pd.to_datetime(values.index, utc=False).to_period(period_freq))
    period_last = values.groupby(row_periods).last()
    if timing == "next_period":
        period_last = period_last.copy()
        period_last.index = period_last.index + 1

    anchor_mode = str(anchor).strip().lower()
    if anchor_mode == "calendar":
        anchor_offset = 0
    elif anchor_mode == "first_valid":
        usable_periods = period_last.notna().any(axis=1)
        if not bool(usable_periods.any()):
            return pd.DataFrame(np.nan, index=values.index, columns=values.columns, dtype=float)
        anchor_offset = int(period_last.index[usable_periods][0].ordinal % hold_periods)
    else:
        raise ValueError("anchor must be 'calendar' or 'first_valid' for calendar_hold")

    all_periods = pd.period_range(row_periods.min(), row_periods.max(), freq=period_freq)
    update_periods = pd.PeriodIndex(
        [period for period in all_periods if int(period.ordinal % hold_periods) == anchor_offset],
        freq=period_freq,
    )
    if len(update_periods) == 0:
        return pd.DataFrame(np.nan, index=values.index, columns=values.columns, dtype=float)

    update_values = period_last.reindex(update_periods)
    held_period_values = pd.DataFrame(np.nan, index=all_periods, columns=values.columns, dtype=float)
    for offset in range(hold_periods):
        target_periods = update_periods + offset
        valid_targets = target_periods.isin(held_period_values.index)
        if not bool(valid_targets.any()):
            continue
        held_period_values.loc[target_periods[valid_targets], :] = update_values.iloc[
            np.flatnonzero(valid_targets)
        ].to_numpy(dtype=float, copy=False)

    return _map_period_frame_to_daily(
        values,
        held_period_values,
        freq=period_freq,
        signal_timing="same_period",
        preserve_input_mask=not broadcast_sparse,
    )


def _calendar_rolling_sum_series(
    series: pd.Series,
    *,
    freq: str,
    periods: int,
    signal_timing: str,
    skip_periods: int = 0,
) -> pd.Series:
    if periods <= 0:
        raise ValueError("periods must be positive for calendar_rolling_sum")
    if skip_periods < 0:
        raise ValueError("skip_periods must be non-negative for calendar_rolling_sum")
    if skip_periods >= periods:
        raise ValueError("skip_periods must be smaller than periods for calendar_rolling_sum")

    valid = series.dropna().astype(float)
    if valid.empty:
        return pd.Series(np.nan, index=series.index, name=series.name, dtype=float)

    period_freq = _normalize_period_freq(freq)
    period_index = valid.index.to_period(period_freq)
    period_last = valid.groupby(period_index).last()
    rolling_sum = period_last.rolling(window=periods, min_periods=periods).sum()
    if skip_periods > 0:
        skip_sum = period_last.rolling(window=skip_periods, min_periods=skip_periods).sum()
        rolling_sum = rolling_sum - skip_sum
    return _map_period_series_to_daily(
        series,
        rolling_sum,
        freq=period_freq,
        signal_timing=signal_timing,
    )


def _columns_with_internal_period_gaps(period_values: pd.DataFrame) -> list[str]:
    valid = period_values.notna()
    gap_columns: list[str] = []
    for column in valid.columns:
        positions = np.flatnonzero(valid[column].to_numpy(dtype=bool, copy=False))
        if len(positions) <= 1:
            continue
        if not bool(valid[column].iloc[positions[0] : positions[-1] + 1].all()):
            gap_columns.append(column)
    return gap_columns


def _period_rolling_sum_frame(
    period_values: pd.DataFrame,
    *,
    periods: int,
    skip_periods: int,
) -> pd.DataFrame:
    rolling_sum = period_values.rolling(window=periods, min_periods=periods).sum()
    if skip_periods <= 0:
        return rolling_sum
    skip_sum = period_values.rolling(window=skip_periods, min_periods=skip_periods).sum()
    return rolling_sum - skip_sum


def _calendar_rolling_sum_frame(
    frame: pd.DataFrame,
    *,
    freq: str,
    periods: int,
    signal_timing: str,
    skip_periods: int = 0,
) -> pd.DataFrame:
    if periods <= 0:
        raise ValueError("periods must be positive for calendar_rolling_sum")
    if skip_periods < 0:
        raise ValueError("skip_periods must be non-negative for calendar_rolling_sum")
    if skip_periods >= periods:
        raise ValueError("skip_periods must be smaller than periods for calendar_rolling_sum")

    period_freq = _normalize_period_freq(freq)
    values = frame.astype(float)
    if values.empty:
        return values.copy()

    period_index = pd.Index(pd.to_datetime(values.index, utc=False).to_period(period_freq))
    period_last = values.groupby(period_index).last()
    rolling_sum = _period_rolling_sum_frame(
        period_last,
        periods=periods,
        skip_periods=skip_periods,
    )
    result = _map_period_frame_to_daily(
        values,
        rolling_sum,
        freq=period_freq,
        signal_timing=signal_timing,
    )

    # The legacy semantics drop NaNs before monthly grouping per column, so a
    # stock with a fully missing month skips that month in its rolling window.
    # The wide path is exact for contiguous monthly histories; sparse columns
    # fall back to the old per-column implementation.
    gap_columns = _columns_with_internal_period_gaps(period_last)
    for column in gap_columns:
        result[column] = _calendar_rolling_sum_series(
            values[column],
            freq=period_freq,
            periods=periods,
            skip_periods=skip_periods,
            signal_timing=signal_timing,
        ).reindex(values.index)
    return result


def _validate_calendar_rolling_robust_mean_params(
    *,
    periods: int,
    skip_periods: int,
    mode: str,
    trim_count: int,
) -> tuple[int, str]:
    if periods <= 0:
        raise ValueError("periods must be positive for calendar_rolling_robust_mean")
    if skip_periods < 0:
        raise ValueError("skip_periods must be non-negative for calendar_rolling_robust_mean")
    if skip_periods >= periods:
        raise ValueError("skip_periods must be smaller than periods for calendar_rolling_robust_mean")
    if trim_count < 0:
        raise ValueError("trim_count must be non-negative for calendar_rolling_robust_mean")

    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"trimmed", "winsorized"}:
        raise ValueError("mode must be 'trimmed' or 'winsorized' for calendar_rolling_robust_mean")

    included_periods = periods - skip_periods
    if trim_count * 2 >= included_periods:
        raise ValueError(
            "trim_count must leave at least one observation for calendar_rolling_robust_mean"
        )
    return included_periods, normalized_mode


def _robust_mean_from_sorted(
    ordered: np.ndarray,
    *,
    mode: str,
    trim_count: int,
) -> np.ndarray:
    if trim_count == 0:
        return ordered.mean(axis=-1)
    if mode == "trimmed":
        return ordered[..., trim_count:-trim_count].mean(axis=-1)

    middle = ordered[..., trim_count:-trim_count]
    winsorized_sum = middle.sum(axis=-1)
    winsorized_sum += float(trim_count) * ordered[..., trim_count]
    winsorized_sum += float(trim_count) * ordered[..., -trim_count - 1]
    return winsorized_sum / float(ordered.shape[-1])


def _calendar_rolling_robust_mean_series(
    series: pd.Series,
    *,
    freq: str,
    periods: int,
    skip_periods: int,
    signal_timing: str,
    mode: str,
    trim_count: int,
) -> pd.Series:
    included_periods, normalized_mode = _validate_calendar_rolling_robust_mean_params(
        periods=periods,
        skip_periods=skip_periods,
        mode=mode,
        trim_count=trim_count,
    )

    valid = series.dropna().astype(float)
    if valid.empty:
        return pd.Series(np.nan, index=series.index, name=series.name, dtype=float)

    period_freq = _normalize_period_freq(freq)
    period_index = valid.index.to_period(period_freq)
    period_last = valid.groupby(period_index).last()
    included_values = period_last.shift(skip_periods)

    def _aggregate(window: np.ndarray) -> float:
        ordered = np.sort(np.asarray(window, dtype=float))
        return float(
            _robust_mean_from_sorted(
                ordered,
                mode=normalized_mode,
                trim_count=trim_count,
            )
        )

    robust_mean = included_values.rolling(
        window=included_periods,
        min_periods=included_periods,
    ).apply(_aggregate, raw=True)
    return _map_period_series_to_daily(
        series,
        robust_mean,
        freq=period_freq,
        signal_timing=signal_timing,
    )


def _period_rolling_robust_mean_frame(
    period_values: pd.DataFrame,
    *,
    included_periods: int,
    skip_periods: int,
    mode: str,
    trim_count: int,
    column_chunk_size: int = 256,
) -> pd.DataFrame:
    shifted_values = period_values.shift(skip_periods)
    values = shifted_values.to_numpy(dtype=float, copy=False)
    output = np.full(values.shape, np.nan, dtype=float)
    if len(values) < included_periods or values.shape[1] == 0:
        return pd.DataFrame(output, index=period_values.index, columns=period_values.columns)

    windows = np.lib.stride_tricks.sliding_window_view(
        values,
        window_shape=included_periods,
        axis=0,
    )
    for start in range(0, values.shape[1], column_chunk_size):
        stop = min(start + column_chunk_size, values.shape[1])
        ordered = np.sort(windows[:, start:stop, :], axis=-1)
        valid = np.isfinite(ordered).all(axis=-1)
        robust_mean = _robust_mean_from_sorted(
            ordered,
            mode=mode,
            trim_count=trim_count,
        )
        robust_mean[~valid] = np.nan
        output[included_periods - 1 :, start:stop] = robust_mean
    return pd.DataFrame(output, index=period_values.index, columns=period_values.columns)


def _calendar_rolling_robust_mean_frame(
    frame: pd.DataFrame,
    *,
    freq: str,
    periods: int,
    skip_periods: int,
    signal_timing: str,
    mode: str,
    trim_count: int,
) -> pd.DataFrame:
    included_periods, normalized_mode = _validate_calendar_rolling_robust_mean_params(
        periods=periods,
        skip_periods=skip_periods,
        mode=mode,
        trim_count=trim_count,
    )

    period_freq = _normalize_period_freq(freq)
    values = frame.astype(float)
    if values.empty:
        return values.copy()

    period_index = pd.Index(pd.to_datetime(values.index, utc=False).to_period(period_freq))
    period_last = values.groupby(period_index).last()
    robust_mean = _period_rolling_robust_mean_frame(
        period_last,
        included_periods=included_periods,
        skip_periods=skip_periods,
        mode=normalized_mode,
        trim_count=trim_count,
    )
    result = _map_period_frame_to_daily(
        values,
        robust_mean,
        freq=period_freq,
        signal_timing=signal_timing,
    )

    # Match calendar_rolling_sum semantics for histories with internal gaps:
    # missing periods are skipped within that market's own valid history.
    gap_columns = _columns_with_internal_period_gaps(period_last)
    for column in gap_columns:
        result[column] = _calendar_rolling_robust_mean_series(
            values[column],
            freq=period_freq,
            periods=periods,
            skip_periods=skip_periods,
            signal_timing=signal_timing,
            mode=normalized_mode,
            trim_count=trim_count,
        ).reindex(values.index)
    return result


def _period_simple_return_frame(
    frame: pd.DataFrame,
    *,
    freq: str,
    periods: int,
    signal_timing: str,
) -> pd.DataFrame:
    return _apply_by_market_column(
        frame,
        lambda series: _period_simple_return_series(
            series,
            freq=freq,
            periods=periods,
            signal_timing=signal_timing,
        ),
    )


def _abs_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.astype(float).abs()


def _true_range_frame(
    high_frame: pd.DataFrame,
    low_frame: pd.DataFrame,
    close_frame: pd.DataFrame,
) -> pd.DataFrame:
    high = high_frame.astype(float)
    low = low_frame.reindex(index=high.index, columns=high.columns).astype(float)
    close = close_frame.reindex(index=high.index, columns=high.columns).astype(float)
    previous_close = _apply_by_market_column(close, lambda series: series.shift(1))

    intrabar_range = high.sub(low).abs()
    high_gap = high.sub(previous_close).abs()
    low_gap = low.sub(previous_close).abs()
    values = np.fmax(
        np.fmax(intrabar_range.to_numpy(dtype=float), high_gap.to_numpy(dtype=float)),
        low_gap.to_numpy(dtype=float),
    )
    result = pd.DataFrame(values, index=high.index, columns=high.columns)
    return result.where(high.notna() & low.notna())


def _stepped_cap_weight_frame(
    frame: pd.DataFrame,
    *,
    start: float,
    end: float,
    min_weight: float,
    steps: int,
) -> pd.DataFrame:
    if steps <= 0:
        raise ValueError("steps must be positive for stepped_cap_weight")
    if start >= end:
        raise ValueError("start must be smaller than end for stepped_cap_weight")
    if min_weight < 0.0 or min_weight > 1.0:
        raise ValueError("min_weight must be in [0, 1] for stepped_cap_weight")

    values = frame.astype(float)
    raw_bucket = np.ceil((values - float(start)) / ((float(end) - float(start)) / float(steps)))
    bucket = raw_bucket.clip(lower=0.0, upper=float(steps))
    weight = 1.0 - (bucket / float(steps)) * (1.0 - float(min_weight))
    return weight.where(values.notna())


def _linear_ramp_weight_frame(
    frame: pd.DataFrame,
    *,
    start: float,
    end: float,
    min_weight: float,
    max_weight: float,
) -> pd.DataFrame:
    if start >= end:
        raise ValueError("start must be smaller than end for linear_ramp_weight")
    if min_weight < 0.0 or min_weight > 1.0 or max_weight < 0.0 or max_weight > 1.0:
        raise ValueError("min_weight and max_weight must be in [0, 1] for linear_ramp_weight")
    if min_weight > max_weight:
        raise ValueError("min_weight must be <= max_weight for linear_ramp_weight")

    values = frame.astype(float)
    progress = values.sub(float(start)).div(float(end) - float(start)).clip(lower=0.0, upper=1.0)
    weight = float(min_weight) + progress * (float(max_weight) - float(min_weight))
    return weight.where(values.notna())


def _target_vol_weight_frame(
    frame: pd.DataFrame,
    *,
    target_annual_vol: float,
    periods_per_year: int,
    min_weight: float,
    max_weight: float,
) -> pd.DataFrame:
    if target_annual_vol <= 0.0 or not np.isfinite(target_annual_vol):
        raise ValueError("target_annual_vol must be finite and positive for target_vol_weight")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive for target_vol_weight")
    if min_weight < 0.0 or max_weight > 1.0 or min_weight > max_weight:
        raise ValueError("target_vol_weight requires 0 <= min_weight <= max_weight <= 1")

    values = frame.astype(float)
    negative = values.lt(0.0)
    if bool(negative.any().any()):
        raise ValueError("target_vol_weight input volatility must be non-negative")

    target_period_vol = float(target_annual_vol) / np.sqrt(float(periods_per_year))
    denominator = values.where(values.gt(0.0))
    weight = target_period_vol / denominator
    weight = weight.clip(lower=float(min_weight), upper=float(max_weight))
    weight = weight.mask(values.eq(0.0), float(max_weight))
    return weight.where(values.notna())


def _quantize_weight_frame(
    frame: pd.DataFrame,
    *,
    step_size: float,
    rounding: str,
    min_weight: float,
    max_weight: float,
) -> pd.DataFrame:
    if step_size <= 0.0 or not np.isfinite(step_size):
        raise ValueError("step_size must be finite and positive for quantize_weight")
    if min_weight < 0.0 or max_weight > 1.0 or min_weight > max_weight:
        raise ValueError("quantize_weight requires 0 <= min_weight <= max_weight <= 1")

    rounding_mode = str(rounding).strip().lower()
    if rounding_mode not in {"floor", "nearest", "ceil"}:
        raise ValueError("rounding must be 'floor', 'nearest', or 'ceil' for quantize_weight")

    values = frame.astype(float)
    finite_or_nan = np.isfinite(values) | values.isna()
    if not bool(finite_or_nan.all().all()):
        raise ValueError("quantize_weight input must be finite or NaN")

    clipped = values.clip(lower=float(min_weight), upper=float(max_weight))
    scaled = clipped.sub(float(min_weight)).div(float(step_size))
    tolerance = 1e-12
    if rounding_mode == "floor":
        buckets = np.floor(scaled + tolerance)
    elif rounding_mode == "nearest":
        buckets = np.floor(scaled + 0.5 + tolerance)
    else:
        buckets = np.ceil(scaled - tolerance)

    quantized = float(min_weight) + buckets * float(step_size)
    quantized = quantized.clip(lower=float(min_weight), upper=float(max_weight))
    quantized = quantized.mask(clipped.eq(float(max_weight)), float(max_weight))
    return quantized.where(values.notna())


def _hump_step_weight_frame(
    frame: pd.DataFrame,
    *,
    entry: float,
    peak: float,
    exit: float,
    min_weight: float,
    end_weight: float,
    steps: int,
) -> pd.DataFrame:
    if steps <= 0:
        raise ValueError("steps must be positive for hump_step_weight")
    if not entry < peak < exit:
        raise ValueError("entry must be smaller than peak and peak must be smaller than exit for hump_step_weight")
    if min_weight < 0.0 or min_weight > 1.0 or end_weight < 0.0 or end_weight > 1.0:
        raise ValueError("min_weight and end_weight must be in [0, 1] for hump_step_weight")

    values = frame.astype(float)
    weight = pd.DataFrame(0.0, index=values.index, columns=values.columns)

    up_width = (float(peak) - float(entry)) / float(steps)
    down_width = (float(exit) - float(peak)) / float(steps)

    up_mask = values.gt(float(entry)) & values.lt(float(peak))
    up_bucket = np.floor((values - float(entry)) / up_width).clip(lower=0.0, upper=float(steps))
    up_weight = float(min_weight) + (up_bucket / float(steps)) * (1.0 - float(min_weight))
    weight = weight.where(~up_mask, up_weight)

    peak_mask = values.ge(float(peak)) & values.lt(float(exit))
    down_bucket = np.floor((values - float(peak)) / down_width).clip(lower=0.0, upper=float(steps))
    down_weight = 1.0 - (down_bucket / float(steps)) * (1.0 - float(end_weight))
    weight = weight.where(~peak_mask, down_weight)

    exit_mask = values.ge(float(exit))
    weight = weight.where(~exit_mask, float(end_weight))
    return weight.where(values.notna())


def _elementwise_min_frame(frame: pd.DataFrame, reference_frame: pd.DataFrame) -> pd.DataFrame:
    left, right, mask = _reference_aligned(frame, reference_frame)
    values = np.fmin(left.to_numpy(dtype=float), right.to_numpy(dtype=float))
    return pd.DataFrame(values, index=left.index, columns=left.columns).where(mask)


def _elementwise_max_frame(frame: pd.DataFrame, reference_frame: pd.DataFrame) -> pd.DataFrame:
    left, right, mask = _reference_aligned(frame, reference_frame)
    values = np.fmax(left.to_numpy(dtype=float), right.to_numpy(dtype=float))
    return pd.DataFrame(values, index=left.index, columns=left.columns).where(mask)


def _mask_by_feature_frame(frame: pd.DataFrame, mask_frame: pd.DataFrame) -> pd.DataFrame:
    values = frame.astype(float)
    mask = mask_frame.reindex(index=values.index, columns=values.columns).astype(float)
    active = mask.notna() & mask.ne(0.0)
    return values.where(active)


def _select_by_state_feature_map(params: dict[str, int | float | str]) -> dict[float, str]:
    suffix = "_feature"
    prefix = "state_"
    mapping: dict[float, str] = {}
    for key, value in params.items():
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        raw_state = key[len(prefix) : -len(suffix)]
        try:
            state_value = float(raw_state)
        except ValueError as exc:
            raise ValueError(f"select_by_state state key must contain a numeric value: {key}") from exc
        feature_name = str(value).strip()
        if not feature_name:
            raise ValueError(f"select_by_state feature must not be empty: {key}")
        if state_value in mapping:
            raise ValueError(f"select_by_state contains duplicate state value: {state_value:g}")
        mapping[state_value] = feature_name
    if not mapping:
        raise ValueError("select_by_state requires at least one state_<value>_feature parameter")
    return mapping


def _select_by_state_frame(
    state_frame: pd.DataFrame,
    params: dict[str, int | float | str],
    available_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    if available_frames is None:
        raise ValueError("select_by_state requires available feature frames")
    mapping = _select_by_state_feature_map(params)
    default_feature = str(params.get("default_feature", "")).strip()
    required_features = set(mapping.values())
    if default_feature:
        required_features.add(default_feature)
    missing = sorted(feature for feature in required_features if feature not in available_frames)
    if missing:
        raise ValueError(f"Unknown select_by_state feature frame(s): {missing}")

    states = state_frame.astype(float)
    result = pd.DataFrame(np.nan, index=states.index, columns=states.columns, dtype=float)
    matched = pd.DataFrame(False, index=states.index, columns=states.columns)
    for state_value, feature_name in mapping.items():
        selected = available_frames[feature_name].reindex(index=states.index, columns=states.columns).astype(float)
        active = states.eq(state_value)
        result = result.where(~active, selected)
        matched |= active
    if default_feature:
        default = available_frames[default_feature].reindex(index=states.index, columns=states.columns).astype(float)
        use_default = states.notna() & ~matched
        result = result.where(~use_default, default)
    return result.where(states.notna())


def _corr_greedy_allowed_mask_for_period(
    returns: pd.DataFrame,
    liquidity: pd.Series,
    *,
    threshold: float,
    min_periods: int,
) -> pd.Series:
    columns = pd.Index(returns.columns)
    if returns.empty or columns.empty:
        return pd.Series(False, index=columns, dtype=bool)

    corr = returns.corr(min_periods=min_periods)
    order = sorted(
        columns,
        key=lambda column: (
            -float(liquidity.get(column, -np.inf))
            if pd.notna(liquidity.get(column, np.nan))
            else np.inf,
            str(column),
        ),
    )
    kept: list[str] = []
    allowed = pd.Series(False, index=columns, dtype=bool)
    for column in order:
        if not kept:
            kept.append(column)
            allowed.loc[column] = True
            continue
        high_corr = corr.loc[column, kept].dropna().ge(threshold)
        if bool(high_corr.any()):
            continue
        kept.append(column)
        allowed.loc[column] = True
    return allowed


def _corr_greedy_filter_frame(
    frame: pd.DataFrame,
    price_frame: pd.DataFrame,
    liquidity_frame: pd.DataFrame,
    *,
    threshold: float,
    corr_window: int,
    min_periods: int,
    liquidity_window: int,
    freq: str,
    signal_timing: str,
) -> pd.DataFrame:
    if corr_window <= 0:
        raise ValueError("corr_window must be positive for corr_greedy_filter")
    if min_periods <= 0:
        raise ValueError("min_periods must be positive for corr_greedy_filter")
    if min_periods > corr_window:
        raise ValueError("min_periods must be <= corr_window for corr_greedy_filter")
    if liquidity_window <= 0:
        raise ValueError("liquidity_window must be positive for corr_greedy_filter")

    values = frame.astype(float)
    prices = price_frame.reindex(index=values.index, columns=values.columns).astype(float)
    liquidity_values = liquidity_frame.reindex(index=values.index, columns=values.columns).astype(float)
    returns = prices.pct_change(fill_method=None)

    period_freq = _normalize_period_freq(freq)
    row_periods = pd.Index(pd.to_datetime(values.index, utc=False).to_period(period_freq))
    period_positions = pd.Series(np.arange(len(values.index)), index=row_periods).groupby(level=0).max()
    period_masks: list[pd.Series] = []
    period_index: list[pd.Period] = []
    for period, end_pos in period_positions.items():
        end_pos = int(end_pos)
        return_start = max(0, end_pos - corr_window + 1)
        liquidity_start = max(0, end_pos - liquidity_window + 1)
        window_returns = returns.iloc[return_start : end_pos + 1]
        window_liquidity = liquidity_values.iloc[liquidity_start : end_pos + 1].mean(skipna=True)
        allowed = _corr_greedy_allowed_mask_for_period(
            window_returns,
            window_liquidity,
            threshold=threshold,
            min_periods=min_periods,
        ).astype(float)
        period_masks.append(allowed)
        period_index.append(period)

    if not period_masks:
        return values.copy() * np.nan

    mask_by_period = pd.DataFrame(period_masks, index=pd.PeriodIndex(period_index, freq=period_freq))
    daily_mask = _map_period_frame_to_daily(
        values,
        mask_by_period,
        freq=period_freq,
        signal_timing=signal_timing,
    )
    return values.where(daily_mask.eq(1.0))


def _momentum_frame(frame: pd.DataFrame, periods: int) -> pd.DataFrame:
    def _transform(series: pd.Series) -> pd.Series:
        positive = series.where(series > 0.0)
        return np.log(positive) - np.log(positive.shift(periods))

    return _apply_by_market_column(frame, _transform)


def _log_frame(frame: pd.DataFrame, mode: str = "log") -> pd.DataFrame:
    if mode == "log":
        positive = frame.where(frame > 0.0)
        return np.log(positive)
    if mode == "log1p":
        nonnegative = frame.where(frame >= 0.0)
        return np.log1p(nonnegative)
    raise ValueError(f"Unsupported log mode: {mode}")


def _efficiency_ratio_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    if window <= 0:
        raise ValueError("window must be positive")

    def _transform(series: pd.Series) -> pd.Series:
        net_move = series.sub(series.shift(window)).abs()
        path_move = series.diff().abs().rolling(window, min_periods=window).sum()
        nonzero = path_move.ne(0.0)
        return net_move.div(path_move.where(nonzero)).where(nonzero)

    return _apply_by_market_column(frame, _transform)


def _rolling_mean_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _rolling_mean_series_fast(series, window))


def _rolling_sum_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _rolling_sum_series_fast(series, window))


def _rolling_std_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _rolling_std_series_fast(series, window))


def _expanding_std_frame(frame: pd.DataFrame, min_periods: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _expanding_std_series_fast(series, min_periods))


def _rolling_max_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _rolling_max_series_fast(series, window))


def _downside_vol_frame(
    frame: pd.DataFrame,
    window: int,
    *,
    annualize: bool = False,
    annualization_factor: float = 252.0,
) -> pd.DataFrame:
    if window <= 0:
        raise ValueError("window must be positive")
    if annualization_factor <= 0:
        raise ValueError("annualization_factor must be positive")

    def _transform(series: pd.Series) -> pd.Series:
        positive = series.where(series > 0.0).dropna()
        result = pd.Series(np.nan, index=series.index, dtype=float)
        if len(positive) <= window:
            return result
        log_return = np.log(positive).diff()
        downside = log_return.clip(upper=0.0)
        semivariance = downside.pow(2).rolling(window, min_periods=window).mean()
        vol = np.sqrt(semivariance)
        if annualize:
            vol = vol * math.sqrt(annualization_factor)
        result.loc[vol.index] = vol
        return result

    return _apply_by_market_column(frame, _transform)


def _rolling_drawdown_min_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    if window <= 0:
        raise ValueError("window must be positive")

    def _transform(series: pd.Series) -> pd.Series:
        positive = series.where(series > 0.0).dropna()
        result = pd.Series(np.nan, index=series.index, dtype=float)
        if len(positive) < window:
            return result
        values = positive.to_numpy(dtype=float, copy=False)
        windows = np.lib.stride_tricks.sliding_window_view(values, window_shape=window)
        running_highs = np.maximum.accumulate(windows, axis=1)
        drawdowns = (windows / running_highs) - 1.0
        result.loc[positive.index[window - 1 :]] = np.min(drawdowns, axis=1)
        return result

    return _apply_by_market_column(frame, _transform)


def _rolling_sum_series_fast(series: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    if len(series) < window:
        return result
    values = series.to_numpy(dtype=float, copy=False)
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    sums = cumsum[window:] - cumsum[:-window]
    result.iloc[window - 1 :] = sums
    return result


def _rolling_mean_series_fast(series: pd.Series, window: int) -> pd.Series:
    result = _rolling_sum_series_fast(series, window)
    if result.notna().any():
        result = result / float(window)
    return result


def _rolling_std_series_fast(series: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    if len(series) < window:
        return result

    values = series.to_numpy(dtype=float, copy=False)
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    cumsum_sq = np.cumsum(np.insert(values * values, 0, 0.0))
    sums = cumsum[window:] - cumsum[:-window]
    sums_sq = cumsum_sq[window:] - cumsum_sq[:-window]
    means = sums / float(window)
    variances = (sums_sq / float(window)) - (means * means)
    variances = np.maximum(variances, 0.0)
    result.iloc[window - 1 :] = np.sqrt(variances)
    return result


def _expanding_std_series_fast(series: pd.Series, min_periods: int) -> pd.Series:
    if min_periods <= 0:
        raise ValueError("min_periods must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    if len(series) < min_periods:
        return result

    values = series.to_numpy(dtype=float, copy=False)
    counts = np.arange(1, len(values) + 1, dtype=float)
    cumsum = np.cumsum(values)
    cumsum_sq = np.cumsum(values * values)
    means = cumsum / counts
    variances = (cumsum_sq / counts) - (means * means)
    variances = np.maximum(variances, 0.0)
    stds = np.sqrt(variances)
    stds[: min_periods - 1] = np.nan
    result.iloc[:] = stds
    return result


def _rolling_max_series_fast(series: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    if len(series) < window:
        return result
    values = series.to_numpy(dtype=float, copy=False)
    windows = np.lib.stride_tricks.sliding_window_view(values, window_shape=window)
    result.iloc[window - 1 :] = np.max(windows, axis=1)
    return result


def _rolling_zscore_series_fast(series: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    if len(series) < window:
        return result

    values = series.to_numpy(dtype=float, copy=False)
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    cumsum_sq = np.cumsum(np.insert(values * values, 0, 0.0))
    sums = cumsum[window:] - cumsum[:-window]
    sums_sq = cumsum_sq[window:] - cumsum_sq[:-window]
    means = sums / float(window)
    variances = (sums_sq / float(window)) - (means * means)
    variances = np.maximum(variances, 0.0)
    stds = np.sqrt(variances)
    current = values[window - 1 :]
    zscores = np.full_like(current, np.nan, dtype=float)
    nonzero = stds > 0.0
    zscores[nonzero] = (current[nonzero] - means[nonzero]) / stds[nonzero]
    result.iloc[window - 1 :] = zscores
    return result


def _build_hmm_feature_frame(
    price_series: pd.Series,
    *,
    momentum_short_bars: int,
    momentum_long_bars: int,
    vol_bars: int,
) -> pd.DataFrame:
    if momentum_short_bars <= 0 or momentum_long_bars <= 0 or vol_bars <= 0:
        raise ValueError("HMM feature windows must be positive")
    if momentum_short_bars >= momentum_long_bars:
        raise ValueError("momentum_short_bars must be smaller than momentum_long_bars")

    price = price_series.astype(float).where(price_series.astype(float) > 0.0)
    log_price = np.log(price)
    log_return_1 = log_price.diff()
    frame = pd.DataFrame(
        {
            "log_return_1": log_return_1,
            f"momentum_{momentum_short_bars}": log_price.diff(momentum_short_bars),
            f"momentum_{momentum_long_bars}": log_price.diff(momentum_long_bars),
            f"realized_vol_{vol_bars}": log_return_1.rolling(vol_bars).std(),
        }
    )
    return frame.dropna()


def _hmm_filtered_outputs(
    price_series: pd.Series,
    *,
    states: int,
    momentum_short_bars: int,
    momentum_long_bars: int,
    vol_bars: int,
    warmup_bars: int | None,
    refit_every_bars: int | None,
    random_state: int,
    n_iter: int,
) -> pd.DataFrame:
    if states < 2:
        raise ValueError("HMM requires at least two states")
    feature_frame = _build_hmm_feature_frame(
        price_series,
        momentum_short_bars=momentum_short_bars,
        momentum_long_bars=momentum_long_bars,
        vol_bars=vol_bars,
    )
    output_columns = ["state"] + [f"prob_{state}" for state in range(states)]
    result = pd.DataFrame(np.nan, index=price_series.index, columns=output_columns, dtype=float)
    if feature_frame.empty:
        return result

    effective_warmup = warmup_bars if warmup_bars is not None else (2 * momentum_long_bars)
    effective_warmup = max(int(effective_warmup), int(momentum_long_bars))
    if len(feature_frame) <= effective_warmup:
        return result
    effective_refit_every = refit_every_bars if refit_every_bars is not None else momentum_long_bars
    effective_refit_every = max(int(effective_refit_every), 1)

    feature_cols = [
        "log_return_1",
        f"momentum_{momentum_short_bars}",
        f"momentum_{momentum_long_bars}",
        f"realized_vol_{vol_bars}",
    ]

    raw_x = feature_frame[feature_cols].to_numpy(dtype=float, copy=False)

    def _filtered_probabilities(model_x: np.ndarray, model: GaussianHMM) -> np.ndarray:
        log_frameprob = model._compute_log_likelihood(model_x)
        _, fwdlattice = _hmmc.forward_log(model.startprob_, model.transmat_, log_frameprob)
        row_max = np.max(fwdlattice, axis=1, keepdims=True)
        probs = np.exp(fwdlattice - row_max)
        return probs / probs.sum(axis=1, keepdims=True)

    total_rows = len(feature_frame)
    for block_start in range(effective_warmup, total_rows, effective_refit_every):
        train_end = block_start
        block_end = min(block_start + effective_refit_every, total_rows)
        scaler = StandardScaler()
        train_x = scaler.fit_transform(raw_x[:train_end, :])
        prefix_x = scaler.transform(raw_x[:block_end, :])

        model = GaussianHMM(
            n_components=states,
            covariance_type="full",
            n_iter=int(n_iter),
            random_state=int(random_state),
        )
        model.fit(train_x)

        filtered_probs = _filtered_probabilities(prefix_x, model)
        block_probs = filtered_probs[block_start:block_end, :]
        block_states = block_probs.argmax(axis=1).astype(float)
        block_index = feature_frame.index[block_start:block_end]
        result.loc[block_index, "state"] = block_states
        for state in range(states):
            result.loc[block_index, f"prob_{state}"] = block_probs[:, state]
    return result


def _hmm_filtered_state_frame(frame: pd.DataFrame, params: dict[str, int | float | str]) -> pd.DataFrame:
    states = int(params.get("states", 3))
    momentum_short_bars = int(params["momentum_short_bars"])
    momentum_long_bars = int(params["momentum_long_bars"])
    vol_bars = int(params["vol_bars"])
    warmup_bars_raw = params.get("warmup_bars")
    warmup_bars = None if warmup_bars_raw is None else int(warmup_bars_raw)
    refit_every_bars_raw = params.get("refit_every_bars")
    refit_every_bars = None if refit_every_bars_raw is None else int(refit_every_bars_raw)
    random_state = int(params.get("random_state", 42))
    n_iter = int(params.get("n_iter", 500))
    return _apply_by_market_column(
        frame,
        lambda series: _hmm_filtered_outputs(
            series,
            states=states,
            momentum_short_bars=momentum_short_bars,
            momentum_long_bars=momentum_long_bars,
            vol_bars=vol_bars,
            warmup_bars=warmup_bars,
            refit_every_bars=refit_every_bars,
            random_state=random_state,
            n_iter=n_iter,
        )["state"],
    )


def _hmm_filtered_prob_frame(frame: pd.DataFrame, params: dict[str, int | float | str]) -> pd.DataFrame:
    states = int(params.get("states", 3))
    target_state = int(params["target_state"])
    if target_state < 0 or target_state >= states:
        raise ValueError("target_state must be between 0 and states-1")
    momentum_short_bars = int(params["momentum_short_bars"])
    momentum_long_bars = int(params["momentum_long_bars"])
    vol_bars = int(params["vol_bars"])
    warmup_bars_raw = params.get("warmup_bars")
    warmup_bars = None if warmup_bars_raw is None else int(warmup_bars_raw)
    refit_every_bars_raw = params.get("refit_every_bars")
    refit_every_bars = None if refit_every_bars_raw is None else int(refit_every_bars_raw)
    random_state = int(params.get("random_state", 42))
    n_iter = int(params.get("n_iter", 500))
    return _apply_by_market_column(
        frame,
        lambda series: _hmm_filtered_outputs(
            series,
            states=states,
            momentum_short_bars=momentum_short_bars,
            momentum_long_bars=momentum_long_bars,
            vol_bars=vol_bars,
            warmup_bars=warmup_bars,
            refit_every_bars=refit_every_bars,
            random_state=random_state,
            n_iter=n_iter,
        )[f"prob_{target_state}"],
    )


def _ewma_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _ewma_series(series.astype(float), window))


def _wma_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _wma_series(series.astype(float), window))


def _rolling_zscore_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _rolling_zscore_series_fast(series, window))


def _rolling_percentile_frame(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    if window <= 0:
        raise ValueError("window must be positive")
    return _apply_by_market_column(
        frame,
        lambda series: series.rolling(window, min_periods=window).rank(pct=True),
    )


def _expanding_percentile_series(series: pd.Series, min_periods: int) -> pd.Series:
    if min_periods <= 0:
        raise ValueError("min_periods must be positive")
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    result = np.full(len(values), np.nan, dtype=float)
    sorted_values: list[float] = []

    import bisect

    for idx, value in enumerate(values):
        if not np.isfinite(value):
            continue
        left = bisect.bisect_left(sorted_values, value)
        right = bisect.bisect_right(sorted_values, value)
        bisect.insort_right(sorted_values, value)
        count = len(sorted_values)
        if count < min_periods:
            continue
        average_rank = (left + right + 2.0) / 2.0
        result[idx] = average_rank / float(count)
    return pd.Series(result, index=series.index, name=series.name)


def _expanding_percentile_frame(frame: pd.DataFrame, min_periods: int) -> pd.DataFrame:
    return _apply_by_market_column(frame, lambda series: _expanding_percentile_series(series, min_periods))


def _cross_rank_base(frame: pd.DataFrame, descending: bool) -> pd.DataFrame:
    return frame.rank(axis=1, method="first", ascending=not descending).astype(float)


def _cross_rank_frame(frame: pd.DataFrame, descending: bool) -> pd.DataFrame:
    return _cross_rank_base(frame, descending)


def _cross_percentile_frame(frame: pd.DataFrame, descending: bool) -> pd.DataFrame:
    ranks = _cross_rank_base(frame, descending)
    counts = frame.notna().sum(axis=1).astype(float)
    percentile = (ranks.sub(1.0)).div(counts.sub(1.0), axis=0)
    single_mask = counts.eq(1.0)
    if bool(single_mask.any()):
        percentile.loc[single_mask, :] = ranks.loc[single_mask, :].where(ranks.loc[single_mask, :].isna(), 1.0)
    return percentile.where(frame.notna())


def _aligned_group_values(
    frame: pd.DataFrame,
    group_frame: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    values = frame.astype(float).to_numpy(dtype=float, copy=False)
    groups = (
        group_frame.reindex(index=frame.index, columns=frame.columns)
        .astype(float)
        .to_numpy(dtype=float, copy=False)
    )
    return values, groups


def _group_demean_frame(
    frame: pd.DataFrame,
    group_frame: pd.DataFrame,
    *,
    min_group_size: int,
) -> pd.DataFrame:
    if min_group_size <= 0:
        raise ValueError("group_demean min_group_size must be positive")

    values, groups = _aligned_group_values(frame, group_frame)
    output = np.full(values.shape, np.nan, dtype=float)
    for row_idx in range(values.shape[0]):
        valid = np.isfinite(values[row_idx]) & np.isfinite(groups[row_idx])
        if not bool(valid.any()):
            continue
        row_values = values[row_idx, valid]
        _, inverse = np.unique(groups[row_idx, valid], return_inverse=True)
        counts = np.bincount(inverse)
        sums = np.bincount(inverse, weights=row_values)
        eligible = counts[inverse] >= min_group_size
        adjusted = row_values - (sums[inverse] / counts[inverse])
        positions = np.flatnonzero(valid)
        output[row_idx, positions[eligible]] = adjusted[eligible]
    return pd.DataFrame(output, index=frame.index, columns=frame.columns)


def _group_rank_frame(
    frame: pd.DataFrame,
    group_frame: pd.DataFrame,
    *,
    descending: bool,
    min_group_size: int,
) -> pd.DataFrame:
    if min_group_size <= 0:
        raise ValueError("group_rank min_group_size must be positive")

    values, groups = _aligned_group_values(frame, group_frame)
    output = np.full(values.shape, np.nan, dtype=float)
    for row_idx in range(values.shape[0]):
        valid = np.isfinite(values[row_idx]) & np.isfinite(groups[row_idx])
        if not bool(valid.any()):
            continue
        positions = np.flatnonzero(valid)
        row_values = values[row_idx, valid]
        row_groups = groups[row_idx, valid]
        for group in np.unique(row_groups):
            member_mask = row_groups == group
            member_positions = positions[member_mask]
            if len(member_positions) < min_group_size:
                continue
            member_values = row_values[member_mask]
            sort_values = -member_values if descending else member_values
            order = np.argsort(sort_values, kind="stable")
            ranks = np.empty(len(member_positions), dtype=float)
            ranks[order] = np.arange(1, len(member_positions) + 1, dtype=float)
            output[row_idx, member_positions] = ranks
    return pd.DataFrame(output, index=frame.index, columns=frame.columns)


def _group_percentile_frame(
    frame: pd.DataFrame,
    group_frame: pd.DataFrame,
    *,
    descending: bool,
    min_group_size: int,
) -> pd.DataFrame:
    ranks = _group_rank_frame(
        frame,
        group_frame,
        descending=descending,
        min_group_size=min_group_size,
    )
    _, groups = _aligned_group_values(frame, group_frame)
    rank_values = ranks.to_numpy(dtype=float, copy=False)
    output = np.full(rank_values.shape, np.nan, dtype=float)
    for row_idx in range(rank_values.shape[0]):
        valid = np.isfinite(rank_values[row_idx]) & np.isfinite(groups[row_idx])
        if not bool(valid.any()):
            continue
        row_groups = groups[row_idx, valid]
        _, inverse = np.unique(row_groups, return_inverse=True)
        counts = np.bincount(inverse).astype(float)
        row_ranks = rank_values[row_idx, valid]
        percentile = np.ones(len(row_ranks), dtype=float)
        multiple = counts[inverse] > 1.0
        percentile[multiple] = (
            (row_ranks[multiple] - 1.0) / (counts[inverse][multiple] - 1.0)
        )
        output[row_idx, np.flatnonzero(valid)] = percentile
    return pd.DataFrame(output, index=frame.index, columns=frame.columns)


def _gaussian_signed_frame(frame: pd.DataFrame) -> pd.DataFrame:
    values = frame.astype(float)
    return values * np.exp(-0.5 * np.square(values))


def _reference_aligned(
    frame: pd.DataFrame,
    reference_frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    left = frame.astype(float)
    right = reference_frame.reindex(index=left.index, columns=left.columns).astype(float)
    mask = left.notna() & right.notna()
    return left, right, mask


def _subtract_reference_frame(frame: pd.DataFrame, reference_frame: pd.DataFrame) -> pd.DataFrame:
    left, right, mask = _reference_aligned(frame, reference_frame)
    return left.sub(right).where(mask)


def _ratio_to_reference_frame(frame: pd.DataFrame, reference_frame: pd.DataFrame) -> pd.DataFrame:
    left, right, mask = _reference_aligned(frame, reference_frame)
    nonzero = right.ne(0.0)
    return left.div(right.where(nonzero)).where(mask & nonzero)


def _residualize_reference_frame(
    frame: pd.DataFrame,
    reference_frame: pd.DataFrame,
    beta_window: int,
) -> pd.DataFrame:
    if beta_window <= 0:
        raise ValueError("residualize_reference beta_window must be positive")
    left = frame.astype(float)
    right = reference_frame.reindex(index=left.index, columns=left.columns).astype(float)
    result = pd.DataFrame(np.nan, index=left.index, columns=left.columns, dtype=float)
    for column in left.columns:
        pairs = pd.concat([left[column], right[column]], axis=1, keys=["asset", "reference"]).dropna()
        if len(pairs) < beta_window:
            continue
        reference_var = pairs["reference"].rolling(beta_window, min_periods=beta_window).var().replace(0.0, pd.NA)
        beta = pairs["asset"].rolling(beta_window, min_periods=beta_window).cov(pairs["reference"]).div(reference_var)
        residual = pairs["asset"].sub(beta.mul(pairs["reference"]))
        result.loc[pairs.index, column] = residual.where(beta.notna())
    return result


def _clip_frame(frame: pd.DataFrame, lower: float, upper: float) -> pd.DataFrame:
    if lower > upper:
        raise ValueError("clip lower must be <= upper")
    return frame.astype(float).clip(lower=lower, upper=upper)


def _bucket_frame(ranks: pd.DataFrame, counts: pd.Series, quantiles: int) -> pd.DataFrame:
    bucket = 1.0 + np.floor((ranks.sub(1.0)).mul(float(quantiles)).div(counts, axis=0))
    return bucket.where(ranks.notna())


def _apply_transform(
    frame: pd.DataFrame,
    kind: str,
    params: dict[str, int | float | str],
    available_frames: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    if kind not in SUPPORTED_TRANSFORMS:
        raise ValueError(f"Unsupported frame_v2 transform: {kind}")
    if kind == "abs":
        return _abs_frame(frame)
    if kind == "log":
        return _log_frame(frame, str(params.get("mode", "log")))
    if kind == "true_range":
        low_reference = str(params.get("low", "low_price"))
        close_reference = str(params.get("close", "trade_price"))
        if available_frames is None or low_reference not in available_frames:
            raise ValueError(f"Unknown low frame for true_range: {low_reference}")
        if available_frames is None or close_reference not in available_frames:
            raise ValueError(f"Unknown close frame for true_range: {close_reference}")
        return _true_range_frame(frame, available_frames[low_reference], available_frames[close_reference])
    if kind == "stepped_cap_weight":
        return _stepped_cap_weight_frame(
            frame,
            start=float(params["start"]),
            end=float(params["end"]),
            min_weight=float(params["min_weight"]),
            steps=int(params["steps"]),
        )
    if kind == "hump_step_weight":
        return _hump_step_weight_frame(
            frame,
            entry=float(params["entry"]),
            peak=float(params["peak"]),
            exit=float(params["exit"]),
            min_weight=float(params["min_weight"]),
            end_weight=float(params["end_weight"]),
            steps=int(params["steps"]),
        )
    if kind == "elementwise_min":
        reference = str(params["reference"])
        if available_frames is None or reference not in available_frames:
            raise ValueError(f"Unknown reference frame for elementwise_min: {reference}")
        return _elementwise_min_frame(frame, available_frames[reference])
    if kind == "elementwise_max":
        reference = str(params["reference"])
        if available_frames is None or reference not in available_frames:
            raise ValueError(f"Unknown reference frame for elementwise_max: {reference}")
        return _elementwise_max_frame(frame, available_frames[reference])
    if kind == "mask_by_feature":
        feature = str(params["feature"])
        if available_frames is None or feature not in available_frames:
            raise ValueError(f"Unknown mask feature for mask_by_feature: {feature}")
        return _mask_by_feature_frame(frame, available_frames[feature])
    if kind == "select_by_state":
        return _select_by_state_frame(frame, params, available_frames)
    if kind == "corr_greedy_filter":
        price_feature = str(params.get("price_feature", "signal_price"))
        liquidity_feature = str(params.get("liquidity_feature", "candle_acc_trade_price"))
        if available_frames is None or price_feature not in available_frames:
            raise ValueError(f"Unknown price feature for corr_greedy_filter: {price_feature}")
        if available_frames is None or liquidity_feature not in available_frames:
            raise ValueError(f"Unknown liquidity feature for corr_greedy_filter: {liquidity_feature}")
        corr_window = int(params.get("corr_window", 252))
        return _corr_greedy_filter_frame(
            frame,
            available_frames[price_feature],
            available_frames[liquidity_feature],
            threshold=float(params["threshold"]),
            corr_window=corr_window,
            min_periods=int(params.get("min_periods", corr_window)),
            liquidity_window=int(params.get("liquidity_window", 63)),
            freq=str(params.get("freq", "M")),
            signal_timing=str(params.get("signal_timing", "next_period")),
        )
    if kind == "clip":
        return _clip_frame(
            frame,
            lower=float(params.get("lower", -np.inf)),
            upper=float(params.get("upper", np.inf)),
        )
    if kind == "linear_ramp_weight":
        return _linear_ramp_weight_frame(
            frame,
            start=float(params["start"]),
            end=float(params["end"]),
            min_weight=float(params["min_weight"]),
            max_weight=float(params.get("max_weight", 1.0)),
        )
    if kind == "target_vol_weight":
        return _target_vol_weight_frame(
            frame,
            target_annual_vol=float(params["target_annual_vol"]),
            periods_per_year=int(params.get("periods_per_year", 252)),
            min_weight=float(params.get("min_weight", 0.0)),
            max_weight=float(params.get("max_weight", 1.0)),
        )
    if kind == "quantize_weight":
        return _quantize_weight_frame(
            frame,
            step_size=float(params["step_size"]),
            rounding=str(params.get("rounding", "nearest")),
            min_weight=float(params.get("min_weight", 0.0)),
            max_weight=float(params.get("max_weight", 1.0)),
        )
    if kind == "rolling_mean":
        return _rolling_mean_frame(frame, int(params["window"]))
    if kind == "rolling_sum":
        return _rolling_sum_frame(frame, int(params["window"]))
    if kind == "rolling_std":
        return _rolling_std_frame(frame, int(params["window"]))
    if kind == "expanding_std":
        return _expanding_std_frame(frame, int(params["min_periods"]))
    if kind == "expanding_percentile":
        return _expanding_percentile_frame(frame, int(params["min_periods"]))
    if kind == "rolling_max":
        return _rolling_max_frame(frame, int(params["window"]))
    if kind == "rolling_percentile":
        return _rolling_percentile_frame(frame, int(params["window"]))
    if kind == "rolling_zscore":
        return _rolling_zscore_frame(frame, int(params["window"]))
    if kind == "downside_vol":
        return _downside_vol_frame(
            frame,
            int(params["window"]),
            annualize=bool(params.get("annualize", False)),
            annualization_factor=float(params.get("annualization_factor", 252.0)),
        )
    if kind == "rolling_drawdown_min":
        return _rolling_drawdown_min_frame(frame, int(params["window"]))
    if kind == "efficiency_ratio":
        return _efficiency_ratio_frame(frame, int(params["window"]))
    if kind == "hmm_filtered_state":
        return _hmm_filtered_state_frame(frame, params)
    if kind == "hmm_filtered_prob":
        return _hmm_filtered_prob_frame(frame, params)
    if kind == "momentum":
        return _momentum_frame(frame, int(params["window"]))
    if kind == "period_simple_return":
        return _period_simple_return_frame(
            frame,
            freq=str(params.get("freq", "M")),
            periods=int(params["periods"]),
            signal_timing=str(params.get("signal_timing", "next_period")),
        )
    if kind == "simple_return":
        return _simple_return_frame(frame, int(params["window"]))
    if kind == "shift":
        return _shift_frame(frame, int(params.get("periods", 1)))
    if kind == "delta":
        return _delta_frame(frame, int(params.get("periods", 1)))
    if kind == "ewma":
        return _ewma_frame(frame, int(params["window"]))
    if kind == "wma":
        return _wma_frame(frame, int(params["window"]))
    if kind == "age_days":
        return _age_days_frame(frame)
    if kind == "cross_rank":
        return _cross_rank_frame(frame, bool(params.get("descending", True)))
    if kind == "cross_percentile":
        return _cross_percentile_frame(frame, bool(params.get("descending", True)))
    if kind == "group_demean":
        group_feature = str(params["group_feature"])
        if available_frames is None or group_feature not in available_frames:
            raise ValueError(f"Unknown group feature for group_demean: {group_feature}")
        return _group_demean_frame(
            frame,
            available_frames[group_feature],
            min_group_size=int(params.get("min_group_size", 1)),
        )
    if kind == "group_rank":
        group_feature = str(params["group_feature"])
        if available_frames is None or group_feature not in available_frames:
            raise ValueError(f"Unknown group feature for group_rank: {group_feature}")
        return _group_rank_frame(
            frame,
            available_frames[group_feature],
            descending=bool(params.get("descending", True)),
            min_group_size=int(params.get("min_group_size", 1)),
        )
    if kind == "group_percentile":
        group_feature = str(params["group_feature"])
        if available_frames is None or group_feature not in available_frames:
            raise ValueError(f"Unknown group feature for group_percentile: {group_feature}")
        return _group_percentile_frame(
            frame,
            available_frames[group_feature],
            descending=bool(params.get("descending", True)),
            min_group_size=int(params.get("min_group_size", 1)),
        )
    if kind == "calendar_hold":
        return _calendar_hold_frame(
            frame,
            freq=str(params.get("freq", "M")),
            hold_periods=int(params["hold_periods"]),
            signal_timing=str(params.get("signal_timing", "same_period")),
            anchor=str(params.get("anchor", "calendar")),
            broadcast_sparse=bool(params.get("broadcast_sparse", False)),
        )
    if kind == "calendar_mean":
        return _calendar_mean_frame(
            frame,
            freq=str(params.get("freq", "M")),
            signal_timing=str(params.get("signal_timing", "same_period")),
        )
    if kind == "calendar_rolling_robust_mean":
        return _calendar_rolling_robust_mean_frame(
            frame,
            freq=str(params.get("freq", "M")),
            periods=int(params["periods"]),
            skip_periods=int(params.get("skip_periods", 0)),
            signal_timing=str(params.get("signal_timing", "same_period")),
            mode=str(params["mode"]),
            trim_count=int(params.get("trim_count", 1)),
        )
    if kind == "calendar_rolling_sum":
        return _calendar_rolling_sum_frame(
            frame,
            freq=str(params.get("freq", "M")),
            periods=int(params["periods"]),
            skip_periods=int(params.get("skip_periods", 0)),
            signal_timing=str(params.get("signal_timing", "same_period")),
        )
    if kind == "gaussian_signed":
        return _gaussian_signed_frame(frame)
    if kind == "subtract_reference":
        reference = str(params["reference"])
        if available_frames is None or reference not in available_frames:
            raise ValueError(f"Unknown reference frame for subtract_reference: {reference}")
        return _subtract_reference_frame(frame, available_frames[reference])
    if kind == "ratio_to_reference":
        reference = str(params["reference"])
        if available_frames is None or reference not in available_frames:
            raise ValueError(f"Unknown reference frame for ratio_to_reference: {reference}")
        return _ratio_to_reference_frame(frame, available_frames[reference])
    if kind == "residualize_reference":
        reference = str(params["reference"])
        if available_frames is None or reference not in available_frames:
            raise ValueError(f"Unknown reference frame for residualize_reference: {reference}")
        return _residualize_reference_frame(
            frame,
            available_frames[reference],
            int(params["beta_window"]),
        )
    raise ValueError(f"Unsupported frame_v2 transform: {kind}")


def _compare_frames(
    left: pd.DataFrame,
    operator: str,
    right: pd.DataFrame | float,
) -> pd.DataFrame:
    left = left.astype(float)
    if isinstance(right, pd.DataFrame):
        right_frame = right.astype(float)
        mask = left.notna() & right_frame.notna()
        if operator == "gt":
            result = left.gt(right_frame)
        elif operator == "ge":
            result = left.ge(right_frame)
        elif operator == "lt":
            result = left.lt(right_frame)
        elif operator == "le":
            result = left.le(right_frame)
        elif operator == "eq":
            result = left.eq(right_frame)
        elif operator == "ne":
            result = left.ne(right_frame)
        else:
            raise ValueError(f"Unsupported compare operator: {operator}")
        return result.astype(float).where(mask)

    mask = left.notna()
    scalar = float(right)
    if operator == "gt":
        result = left.gt(scalar)
    elif operator == "ge":
        result = left.ge(scalar)
    elif operator == "lt":
        result = left.lt(scalar)
    elif operator == "le":
        result = left.le(scalar)
    elif operator == "eq":
        result = left.eq(scalar)
    elif operator == "ne":
        result = left.ne(scalar)
    else:
        raise ValueError(f"Unsupported compare operator: {operator}")
    return result.astype(float).where(mask)


def build_feature_frames_from_cache(
    cache_dir: Path,
    feature_specs: Sequence[FeatureSpec],
    *,
    market_columns: Sequence[str] | None = None,
    output_market_columns: Sequence[str] | None = None,
    max_markets: int | None = None,
    tail_rows: int | None = None,
    source_frames: dict[str, pd.DataFrame] | None = None,
    frame_cache: dict[tuple[Any, ...], pd.DataFrame] | None = None,
    frame_cache_namespace: tuple[Any, ...] | None = None,
) -> dict[str, pd.DataFrame]:
    from lib.feature_graph_v2 import build_feature_frames_from_cache_graph

    return build_feature_frames_from_cache_graph(
        cache_dir,
        feature_specs,
        market_columns=market_columns,
        output_market_columns=output_market_columns,
        max_markets=max_markets,
        tail_rows=tail_rows,
        source_frames=source_frames,
        frame_cache=frame_cache,
        frame_cache_namespace=frame_cache_namespace,
    )
