#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze market weakness onsets, rebound failures, and durable recoveries "
            "from an existing v2 wide source cache."
        )
    )
    parser.add_argument("--source-cache-dir", required=True, help="v2 wide source cache directory")
    parser.add_argument("--out-dir", required=True, help="Research output directory")
    parser.add_argument(
        "--strategy-equity-csv",
        default="",
        help="Optional strategy equity curve CSV for strategy-specific onset analysis",
    )
    parser.add_argument(
        "--strategy-value-column",
        default="",
        help="Optional value column when strategy equity CSV contains multiple value columns",
    )
    parser.add_argument(
        "--strategy-name",
        default="strategy",
        help="Label stored in strategy analysis metadata",
    )
    parser.add_argument("--price-column", default="signal_price", help="Wide price frame name")
    parser.add_argument("--benchmark-market", default="SPY-KRW", help="Market state benchmark")
    parser.add_argument(
        "--market-columns",
        default="",
        help="Optional comma-separated breadth markets; defaults to all except benchmark",
    )
    parser.add_argument("--start-date", default="", help="Optional inclusive analysis start")
    parser.add_argument("--end-date", default="", help="Optional inclusive analysis end")
    parser.add_argument(
        "--forward-horizons",
        default="21,42,63",
        help="Comma-separated forward outcome horizons in trading bars",
    )
    parser.add_argument(
        "--label-horizon",
        type=int,
        default=42,
        help="Forward horizon used for the descriptive state label",
    )
    parser.add_argument(
        "--return-windows",
        default="5,10,21,63",
        help="Comma-separated benchmark and breadth return windows",
    )
    parser.add_argument(
        "--vol-windows",
        default="10,21,63",
        help="Comma-separated benchmark realized-volatility windows",
    )
    parser.add_argument(
        "--drawdown-windows",
        default="63,126,252",
        help="Comma-separated rolling-high drawdown windows",
    )
    parser.add_argument("--correlation-window", type=int, default=21)
    parser.add_argument("--min-breadth-markets", type=int, default=8)
    parser.add_argument("--quantiles", type=int, default=5)
    parser.add_argument(
        "--event-onset-drawdown-pct",
        type=float,
        default=5.0,
        help="First drawdown magnitude treated as an event onset",
    )
    parser.add_argument(
        "--event-qualifying-drawdown-pct",
        type=float,
        default=10.0,
        help="Minimum eventual drawdown magnitude for a qualified bear event",
    )
    parser.add_argument("--pre-event-bars", type=int, default=63)
    parser.add_argument(
        "--rebound-drawdown-pct",
        type=float,
        default=10.0,
        help="Current drawdown magnitude required for a rebound candidate",
    )
    parser.add_argument("--rebound-return-window", type=int, default=10)
    parser.add_argument("--rebound-breadth-window", type=int, default=10)
    parser.add_argument(
        "--rebound-breadth-threshold",
        type=float,
        default=0.60,
    )
    parser.add_argument(
        "--rebound-failure-drawdown-pct",
        type=float,
        default=5.0,
        help="Future loss magnitude that marks a failed rebound",
    )
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _parse_ints(value: str, *, label: str) -> list[int]:
    parsed = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    if not parsed or any(item <= 0 for item in parsed):
        raise ValueError(f"{label} must contain positive integers")
    return parsed


def _parse_markets(value: str) -> list[str] | None:
    markets = sorted({item.strip().upper() for item in value.split(",") if item.strip()})
    return markets or None


def _read_price_frame(
    cache_dir: Path,
    *,
    frame_name: str,
    benchmark_market: str,
    market_columns: list[str] | None,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, str, list[str]]:
    path = cache_dir / f"{frame_name}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing price frame: {path}")
    frame = pd.read_parquet(path)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"Price frame is empty: {path}")

    frame.index = pd.to_datetime(frame.index, utc=False).normalize()
    frame.columns = [str(column).strip().upper() for column in frame.columns]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.loc[:, ~pd.Index(frame.columns).duplicated()]
    frame = frame.apply(pd.to_numeric, errors="coerce")

    benchmark = benchmark_market.strip().upper()
    if benchmark not in frame.columns:
        raise ValueError(f"Benchmark market {benchmark!r} is missing from {path}")
    selected = market_columns or [column for column in frame.columns if column != benchmark]
    missing = sorted(set(selected) - set(frame.columns))
    if missing:
        raise ValueError(f"Breadth markets are missing from price frame: {missing}")
    selected = [column for column in selected if column != benchmark]
    if not selected:
        raise ValueError("At least one non-benchmark market is required")

    frame = frame.reindex(columns=[benchmark, *selected])
    frame = frame.loc[frame[benchmark].notna()]
    if start_date:
        frame = frame.loc[frame.index >= pd.Timestamp(start_date).normalize()]
    if end_date:
        frame = frame.loc[frame.index <= pd.Timestamp(end_date).normalize()]
    if len(frame) < 300:
        raise ValueError("Analysis requires at least 300 benchmark observations")
    return frame, benchmark, selected


def _read_equity_curve(path: Path, *, value_column: str) -> pd.Series:
    if not path.exists():
        raise FileNotFoundError(f"Missing strategy equity curve: {path}")
    frame = pd.read_csv(path)
    if frame.empty or frame.shape[1] < 2:
        raise ValueError(f"Strategy equity curve needs date and value columns: {path}")
    date_column = "date" if "date" in frame.columns else str(frame.columns[0])
    candidates = [str(column) for column in frame.columns if str(column) != date_column]
    selected_value = value_column.strip() if value_column else ""
    if not selected_value:
        if len(candidates) != 1:
            raise ValueError(
                f"Specify --strategy-value-column for a multi-value equity curve: {path}"
            )
        selected_value = candidates[0]
    if selected_value not in frame.columns:
        raise ValueError(f"Missing strategy value column {selected_value!r}: {path}")

    dates = pd.to_datetime(frame[date_column], errors="coerce", utc=False).dt.normalize()
    values = pd.to_numeric(frame[selected_value], errors="coerce")
    curve = pd.Series(values.to_numpy(dtype=float), index=dates, name="strategy_equity")
    curve = curve.loc[curve.index.notna()]
    curve = curve[~curve.index.duplicated(keep="last")].sort_index().dropna()
    if len(curve) < 300:
        raise ValueError("Strategy equity curve requires at least 300 observations")
    if (curve <= 0.0).any() or not np.isfinite(curve.to_numpy(dtype=float)).all():
        raise ValueError("Strategy equity curve must contain finite positive values")
    return curve


def _rolling_last_percentile(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    def rank_last(values: np.ndarray) -> float:
        last = values[-1]
        finite = values[np.isfinite(values)]
        if not np.isfinite(last) or len(finite) == 0:
            return float("nan")
        less = float(np.sum(finite < last))
        equal = float(np.sum(finite == last))
        return (less + 0.5 * equal) / float(len(finite))

    return series.rolling(window, min_periods=min_periods).apply(rank_last, raw=True)


def _average_pairwise_correlation(
    returns: pd.DataFrame,
    *,
    window: int,
    min_markets: int,
) -> pd.Series:
    values = returns.to_numpy(dtype=float)
    output = np.full(len(returns), np.nan, dtype=float)
    for end in range(window - 1, len(returns)):
        block = values[end - window + 1 : end + 1]
        complete = np.isfinite(block).all(axis=0)
        selected = block[:, complete]
        if selected.shape[1] < min_markets:
            continue
        std = selected.std(axis=0, ddof=1)
        selected = selected[:, np.isfinite(std) & (std > 0.0)]
        if selected.shape[1] < min_markets:
            continue
        corr = np.corrcoef(selected, rowvar=False)
        upper = corr[np.triu_indices(corr.shape[0], k=1)]
        upper = upper[np.isfinite(upper)]
        if len(upper):
            output[end] = float(upper.mean())
    return pd.Series(output, index=returns.index, name=f"avg_pair_corr_{window}")


def _build_predictors(
    prices: pd.DataFrame,
    *,
    benchmark: str,
    markets: list[str],
    return_windows: list[int],
    vol_windows: list[int],
    drawdown_windows: list[int],
    correlation_window: int,
    min_breadth_markets: int,
) -> tuple[pd.DataFrame, list[str]]:
    benchmark_price = prices[benchmark]
    benchmark_return = benchmark_price.pct_change(fill_method=None)
    market_prices = prices.reindex(columns=markets)
    market_daily_return = market_prices.pct_change(fill_method=None)
    predictors = pd.DataFrame(index=prices.index)

    for window in return_windows:
        benchmark_window_return = benchmark_price.pct_change(window, fill_method=None)
        market_window_return = market_prices.pct_change(window, fill_method=None)
        valid_count = market_window_return.notna().sum(axis=1)
        predictors[f"benchmark_return_{window}"] = benchmark_window_return
        predictors[f"breadth_positive_{window}"] = (
            market_window_return.gt(0.0).sum(axis=1).div(valid_count)
        ).where(valid_count >= min_breadth_markets)
        predictors[f"cross_median_return_{window}"] = market_window_return.median(
            axis=1,
            skipna=True,
        ).where(valid_count >= min_breadth_markets)
        predictors[f"cross_dispersion_{window}"] = market_window_return.std(
            axis=1,
            skipna=True,
        ).where(valid_count >= min_breadth_markets)

    for window in vol_windows:
        realized_vol = benchmark_return.rolling(window, min_periods=window).std(ddof=1)
        predictors[f"benchmark_rv_{window}"] = realized_vol * math.sqrt(252.0)

    for window in drawdown_windows:
        rolling_high = benchmark_price.rolling(window, min_periods=window).max()
        predictors[f"benchmark_drawdown_{window}"] = benchmark_price.div(rolling_high).sub(1.0)
        moving_average = benchmark_price.rolling(window, min_periods=window).mean()
        predictors[f"benchmark_sma_gap_{window}"] = benchmark_price.div(moving_average).sub(1.0)

    if {5, 21}.issubset(return_windows):
        predictors["return_acceleration_5_minus_21"] = (
            predictors["benchmark_return_5"] - predictors["benchmark_return_21"]
        )
        predictors["breadth_acceleration_5_minus_21"] = (
            predictors["breadth_positive_5"] - predictors["breadth_positive_21"]
        )
    if {10, 21}.issubset(return_windows):
        predictors["return_acceleration_10_minus_21"] = (
            predictors["benchmark_return_10"] - predictors["benchmark_return_21"]
        )
        predictors["breadth_acceleration_10_minus_21"] = (
            predictors["breadth_positive_10"] - predictors["breadth_positive_21"]
        )
        predictors["breadth_positive_10_change_5"] = predictors[
            "breadth_positive_10"
        ].diff(5)
    if {10, 63}.issubset(vol_windows):
        predictors["rv_ratio_10_to_63"] = predictors["benchmark_rv_10"].div(
            predictors["benchmark_rv_63"]
        )
    if 21 in vol_windows:
        predictors["benchmark_rv_21_pct_252"] = _rolling_last_percentile(
            predictors["benchmark_rv_21"],
            window=252,
            min_periods=126,
        )

    predictors[f"avg_pair_corr_{correlation_window}"] = _average_pairwise_correlation(
        market_daily_return,
        window=correlation_window,
        min_markets=min_breadth_markets,
    )
    predictors["valid_market_count"] = market_prices.notna().sum(axis=1).astype(float)

    for defensive in ("TLT-KRW", "GLD-KRW"):
        if defensive not in prices.columns:
            continue
        for window in (21, 63):
            if window not in return_windows:
                continue
            predictors[f"{defensive.lower()}_relative_return_{window}"] = (
                prices[defensive].pct_change(window, fill_method=None)
                - predictors[f"benchmark_return_{window}"]
            )

    predictor_columns = [
        column for column in predictors.columns if column != "valid_market_count"
    ]
    return predictors, predictor_columns


def _build_strategy_predictors(
    strategy_equity: pd.Series,
    benchmark_price: pd.Series,
    *,
    return_windows: list[int],
    vol_windows: list[int],
    drawdown_windows: list[int],
) -> tuple[pd.DataFrame, list[str]]:
    strategy_return = strategy_equity.pct_change(fill_method=None)
    benchmark_return = benchmark_price.pct_change(fill_method=None)
    predictors = pd.DataFrame(index=strategy_equity.index)

    for window in return_windows:
        predictors[f"strategy_return_{window}"] = strategy_equity.pct_change(
            window,
            fill_method=None,
        )
        if window in {21, 63}:
            predictors[f"strategy_relative_return_{window}"] = (
                predictors[f"strategy_return_{window}"]
                - benchmark_price.pct_change(window, fill_method=None)
            )

    for window in vol_windows:
        predictors[f"strategy_rv_{window}"] = (
            strategy_return.rolling(window, min_periods=window).std(ddof=1)
            * math.sqrt(252.0)
        )

    for window in drawdown_windows:
        rolling_high = strategy_equity.rolling(window, min_periods=window).max()
        predictors[f"strategy_drawdown_{window}"] = strategy_equity.div(
            rolling_high
        ).sub(1.0)

    for window in (21, 63):
        covariance = strategy_return.rolling(window, min_periods=window).cov(benchmark_return)
        benchmark_variance = benchmark_return.rolling(window, min_periods=window).var(ddof=1)
        predictors[f"strategy_market_beta_{window}"] = covariance.div(benchmark_variance)
        predictors[f"strategy_market_corr_{window}"] = strategy_return.rolling(
            window,
            min_periods=window,
        ).corr(benchmark_return)

    predictor_columns = list(predictors.columns)
    return predictors, predictor_columns


def _future_path_stats(
    price: pd.Series,
    horizon: int,
    *,
    prefix: str = "future",
) -> pd.DataFrame:
    values = price.to_numpy(dtype=float)
    output = pd.DataFrame(
        {
            f"{prefix}_return_{horizon}": np.nan,
            f"{prefix}_min_return_{horizon}": np.nan,
            f"{prefix}_max_return_{horizon}": np.nan,
            f"{prefix}_min_price_{horizon}": np.nan,
        },
        index=price.index,
    )
    if len(values) <= horizon:
        return output

    windows = np.lib.stride_tricks.sliding_window_view(values, horizon + 1)
    current = windows[:, 0]
    future = windows[:, 1:]
    valid = np.isfinite(current) & np.isfinite(future).all(axis=1) & (current > 0.0)
    positions = np.arange(len(windows))[valid]
    current_valid = current[valid]
    future_valid = future[valid]
    output.iloc[positions, 0] = future_valid[:, -1] / current_valid - 1.0
    output.iloc[positions, 1] = np.min(future_valid, axis=1) / current_valid - 1.0
    output.iloc[positions, 2] = np.max(future_valid, axis=1) / current_valid - 1.0
    output.iloc[positions, 3] = np.min(future_valid, axis=1)
    return output


def _attach_outcomes_and_labels(
    daily: pd.DataFrame,
    benchmark_price: pd.Series,
    *,
    horizons: list[int],
    label_horizon: int,
    rebound_drawdown_pct: float,
    rebound_return_window: int,
    rebound_breadth_window: int,
    rebound_breadth_threshold: float,
    rebound_failure_drawdown_pct: float,
) -> pd.DataFrame:
    result = daily.copy()
    trailing_low_63 = benchmark_price.rolling(63, min_periods=63).min()
    current_drawdown = result["benchmark_drawdown_252"]
    rebound_return = result[f"benchmark_return_{rebound_return_window}"]
    rebound_breadth = result[f"breadth_positive_{rebound_breadth_window}"]
    rebound_candidate = (
        current_drawdown.le(-rebound_drawdown_pct / 100.0)
        & rebound_return.gt(0.0)
        & rebound_breadth.ge(rebound_breadth_threshold)
    )
    result["rebound_candidate"] = rebound_candidate

    for horizon in horizons:
        path_stats = _future_path_stats(benchmark_price, horizon)
        result = result.join(path_stats)
        future_min = result[f"future_min_return_{horizon}"]
        future_return = result[f"future_return_{horizon}"]
        future_min_price = result[f"future_min_price_{horizon}"]
        new_low = future_min_price.lt(trailing_low_63)
        future_loss_10 = future_min.le(-0.10)

        result[f"future_new_63d_low_{horizon}"] = new_low
        result[f"future_loss_10pct_{horizon}"] = future_loss_10
        result[f"pre_bear_{horizon}"] = current_drawdown.gt(-0.05) & future_loss_10
        result[f"early_bear_{horizon}"] = (
            current_drawdown.le(-0.05) & current_drawdown.gt(-0.10) & future_loss_10
        )
        result[f"failed_rebound_{horizon}"] = rebound_candidate & (
            future_min.le(-rebound_failure_drawdown_pct / 100.0) | new_low
        )
        result[f"durable_recovery_{horizon}"] = (
            rebound_candidate
            & future_min.gt(-rebound_failure_drawdown_pct / 100.0)
            & future_return.gt(0.0)
            & ~new_low
        )

    state = pd.Series("other", index=result.index, dtype="object")
    pre_bear = result[f"pre_bear_{label_horizon}"].fillna(False)
    early_bear = result[f"early_bear_{label_horizon}"].fillna(False)
    failed_rebound = result[f"failed_rebound_{label_horizon}"].fillna(False)
    durable_recovery = result[f"durable_recovery_{label_horizon}"].fillna(False)
    ongoing_weakness = (
        current_drawdown.le(-rebound_drawdown_pct / 100.0)
        & rebound_return.le(0.0)
        & result[f"future_min_return_{label_horizon}"].le(
            -rebound_failure_drawdown_pct / 100.0
        )
    )
    normal_risk_on = (
        current_drawdown.gt(-0.05)
        & result[f"future_min_return_{label_horizon}"].gt(-0.05)
        & result[f"future_return_{label_horizon}"].gt(0.0)
    )
    state.loc[normal_risk_on.fillna(False)] = "normal_risk_on"
    state.loc[ongoing_weakness.fillna(False)] = "ongoing_weakness"
    state.loc[durable_recovery] = "durable_recovery"
    state.loc[failed_rebound] = "failed_rebound"
    state.loc[early_bear] = "early_bear"
    state.loc[pre_bear] = "pre_bear"
    result[f"state_label_{label_horizon}"] = state
    return result


def _attach_strategy_outcomes_and_labels(
    daily: pd.DataFrame,
    strategy_equity: pd.Series,
    *,
    horizons: list[int],
    label_horizon: int,
    rebound_drawdown_pct: float,
    rebound_return_window: int,
    rebound_failure_drawdown_pct: float,
) -> pd.DataFrame:
    result = daily.copy()
    trailing_low_63 = strategy_equity.rolling(63, min_periods=63).min()
    current_drawdown = result["strategy_drawdown_252"]
    rebound_return = result[f"strategy_return_{rebound_return_window}"]
    rebound_candidate = current_drawdown.le(-rebound_drawdown_pct / 100.0) & rebound_return.gt(
        0.0
    )
    result["strategy_rebound_candidate"] = rebound_candidate

    for horizon in horizons:
        path_stats = _future_path_stats(
            strategy_equity,
            horizon,
            prefix="strategy_future",
        )
        result = result.join(path_stats)
        future_min = result[f"strategy_future_min_return_{horizon}"]
        future_return = result[f"strategy_future_return_{horizon}"]
        future_min_price = result[f"strategy_future_min_price_{horizon}"]
        new_low = future_min_price.lt(trailing_low_63)
        future_loss_10 = future_min.le(-0.10)

        result[f"strategy_future_new_63d_low_{horizon}"] = new_low
        result[f"strategy_future_loss_10pct_{horizon}"] = future_loss_10
        result[f"strategy_pre_bear_{horizon}"] = current_drawdown.gt(-0.05) & future_loss_10
        result[f"strategy_early_bear_{horizon}"] = (
            current_drawdown.le(-0.05) & current_drawdown.gt(-0.10) & future_loss_10
        )
        result[f"strategy_failed_rebound_{horizon}"] = rebound_candidate & (
            future_min.le(-rebound_failure_drawdown_pct / 100.0) | new_low
        )
        result[f"strategy_durable_recovery_{horizon}"] = (
            rebound_candidate
            & future_min.gt(-rebound_failure_drawdown_pct / 100.0)
            & future_return.gt(0.0)
            & ~new_low
        )

    state = pd.Series("other", index=result.index, dtype="object")
    future_min = result[f"strategy_future_min_return_{label_horizon}"]
    future_return = result[f"strategy_future_return_{label_horizon}"]
    normal = current_drawdown.gt(-0.05) & future_min.gt(-0.05) & future_return.gt(0.0)
    ongoing = (
        current_drawdown.le(-rebound_drawdown_pct / 100.0)
        & rebound_return.le(0.0)
        & future_min.le(-rebound_failure_drawdown_pct / 100.0)
    )
    state.loc[normal.fillna(False)] = "normal_risk_on"
    state.loc[ongoing.fillna(False)] = "ongoing_weakness"
    state.loc[result[f"strategy_durable_recovery_{label_horizon}"].fillna(False)] = (
        "durable_recovery"
    )
    state.loc[result[f"strategy_failed_rebound_{label_horizon}"].fillna(False)] = (
        "failed_rebound"
    )
    state.loc[result[f"strategy_early_bear_{label_horizon}"].fillna(False)] = "early_bear"
    state.loc[result[f"strategy_pre_bear_{label_horizon}"].fillna(False)] = "pre_bear"
    result[f"strategy_state_label_{label_horizon}"] = state
    return result


def _detect_drawdown_events(
    price: pd.Series,
    *,
    onset_drawdown_pct: float,
    qualifying_drawdown_pct: float,
) -> pd.DataFrame:
    values = price.dropna()
    rows: list[dict[str, Any]] = []
    peak_value = float(values.iloc[0])
    peak_date = values.index[0]
    onset_date: pd.Timestamp | None = None
    qualifying_date: pd.Timestamp | None = None
    trough_value = peak_value
    trough_date = peak_date

    def append_event(recovery_date: pd.Timestamp | None) -> None:
        if qualifying_date is None or onset_date is None:
            return
        rows.append(
            {
                "event_id": f"bear_{len(rows) + 1:02d}",
                "peak_date": peak_date,
                "onset_date": onset_date,
                "qualifying_date": qualifying_date,
                "trough_date": trough_date,
                "recovery_date": recovery_date,
                "max_drawdown": trough_value / peak_value - 1.0,
                "peak_to_onset_bars": int(values.index.get_loc(onset_date) - values.index.get_loc(peak_date)),
                "peak_to_trough_bars": int(values.index.get_loc(trough_date) - values.index.get_loc(peak_date)),
                "peak_to_recovery_bars": (
                    int(values.index.get_loc(recovery_date) - values.index.get_loc(peak_date))
                    if recovery_date is not None
                    else np.nan
                ),
                "resolved": recovery_date is not None,
            }
        )

    for date, raw_value in values.iloc[1:].items():
        value = float(raw_value)
        if value >= peak_value:
            append_event(pd.Timestamp(date))
            peak_value = value
            peak_date = pd.Timestamp(date)
            onset_date = None
            qualifying_date = None
            trough_value = value
            trough_date = pd.Timestamp(date)
            continue

        if value < trough_value:
            trough_value = value
            trough_date = pd.Timestamp(date)
        drawdown_pct = (value / peak_value - 1.0) * 100.0
        if onset_date is None and drawdown_pct <= -onset_drawdown_pct:
            onset_date = pd.Timestamp(date)
        if qualifying_date is None and drawdown_pct <= -qualifying_drawdown_pct:
            qualifying_date = pd.Timestamp(date)

    append_event(None)
    return pd.DataFrame(rows)


def _event_relative_outputs(
    daily: pd.DataFrame,
    events: pd.DataFrame,
    *,
    predictor_columns: list[str],
    pre_event_bars: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    raw_rows: list[dict[str, Any]] = []
    selected_offsets = {-42, -21, -10, -5, 0}
    shift_rows: list[dict[str, Any]] = []

    for event in events.itertuples(index=False):
        onset_date = pd.Timestamp(event.onset_date)
        if onset_date not in daily.index:
            continue
        onset_position = int(daily.index.get_loc(onset_date))
        baseline_start = max(0, onset_position - 252)
        baseline_end = max(0, onset_position - pre_event_bars)
        baseline = daily.iloc[baseline_start:baseline_end]

        for offset in range(-pre_event_bars, 1):
            position = onset_position + offset
            if position < 0:
                continue
            row: dict[str, Any] = {
                "event_id": event.event_id,
                "onset_date": onset_date,
                "offset_bars": offset,
                "date": daily.index[position],
            }
            for feature in predictor_columns:
                row[feature] = daily.iloc[position][feature]
            raw_rows.append(row)

            if offset not in selected_offsets:
                continue
            for feature in predictor_columns:
                value = daily.iloc[position][feature]
                base_values = pd.to_numeric(baseline[feature], errors="coerce").dropna()
                history = pd.to_numeric(
                    daily.iloc[max(0, position - 756) : position + 1][feature],
                    errors="coerce",
                ).dropna()
                historical_percentile = float("nan")
                if np.isfinite(value) and len(history) >= 126:
                    historical_percentile = float(
                        ((history < value).sum() + 0.5 * (history == value).sum()) / len(history)
                    )
                shift_rows.append(
                    {
                        "event_id": event.event_id,
                        "onset_date": onset_date,
                        "offset_bars": offset,
                        "feature": feature,
                        "value": value,
                        "pre_event_median": (
                            float(base_values.median()) if len(base_values) else float("nan")
                        ),
                        "change_from_pre_event_median": (
                            float(value - base_values.median())
                            if np.isfinite(value) and len(base_values)
                            else float("nan")
                        ),
                        "historical_percentile": historical_percentile,
                    }
                )

    raw = pd.DataFrame(raw_rows)
    if raw.empty:
        return raw, pd.DataFrame(), pd.DataFrame()

    profile_rows: list[dict[str, Any]] = []
    for offset, group in raw.groupby("offset_bars", sort=True):
        for feature in predictor_columns:
            values = pd.to_numeric(group[feature], errors="coerce").dropna()
            profile_rows.append(
                {
                    "offset_bars": int(offset),
                    "feature": feature,
                    "event_count": int(len(values)),
                    "mean": float(values.mean()) if len(values) else float("nan"),
                    "median": float(values.median()) if len(values) else float("nan"),
                    "q25": float(values.quantile(0.25)) if len(values) else float("nan"),
                    "q75": float(values.quantile(0.75)) if len(values) else float("nan"),
                }
            )

    shifts = pd.DataFrame(shift_rows)
    consistency_rows: list[dict[str, Any]] = []
    if not shifts.empty:
        for (offset, feature), group in shifts.groupby(["offset_bars", "feature"], sort=True):
            percentile = pd.to_numeric(group["historical_percentile"], errors="coerce").dropna()
            change = pd.to_numeric(
                group["change_from_pre_event_median"],
                errors="coerce",
            ).dropna()
            consistency_rows.append(
                {
                    "offset_bars": int(offset),
                    "feature": feature,
                    "event_count": int(group["event_id"].nunique()),
                    "median_historical_percentile": (
                        float(percentile.median()) if len(percentile) else float("nan")
                    ),
                    "historical_percentile_ge_80_ratio": (
                        float(percentile.ge(0.80).mean()) if len(percentile) else float("nan")
                    ),
                    "historical_percentile_le_20_ratio": (
                        float(percentile.le(0.20).mean()) if len(percentile) else float("nan")
                    ),
                    "median_change_from_pre_event": (
                        float(change.median()) if len(change) else float("nan")
                    ),
                    "same_sign_change_ratio": (
                        float(max(change.gt(0.0).mean(), change.lt(0.0).mean()))
                        if len(change)
                        else float("nan")
                    ),
                }
            )
    return raw, pd.DataFrame(profile_rows), pd.DataFrame(consistency_rows)


def _auc_score(target: pd.Series, score: pd.Series) -> float:
    aligned = pd.concat([target.rename("target"), score.rename("score")], axis=1).dropna()
    if aligned.empty:
        return float("nan")
    y = aligned["target"].astype(bool)
    positives = int(y.sum())
    negatives = int((~y).sum())
    if positives == 0 or negatives == 0:
        return float("nan")
    ranks = aligned["score"].rank(method="average")
    rank_sum = float(ranks.loc[y].sum())
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def _binary_separation_summary(
    daily: pd.DataFrame,
    *,
    predictor_columns: list[str],
    horizons: list[int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    current_drawdown = daily["benchmark_drawdown_252"]
    for horizon in horizons:
        target_specs: list[tuple[str, pd.Series, pd.Series]] = [
            (
                "future_loss_10pct",
                daily[f"future_loss_10pct_{horizon}"],
                daily[f"future_min_return_{horizon}"].notna(),
            ),
            (
                "future_loss_10pct_while_drawdown_gt_minus10",
                daily[f"future_loss_10pct_{horizon}"],
                daily[f"future_min_return_{horizon}"].notna() & current_drawdown.gt(-0.10),
            ),
        ]
        rebound_known = (
            daily[f"failed_rebound_{horizon}"] | daily[f"durable_recovery_{horizon}"]
        )
        target_specs.append(
            (
                "failed_vs_durable_rebound",
                daily[f"failed_rebound_{horizon}"],
                rebound_known,
            )
        )

        for target_name, target, eligible in target_specs:
            y = target.where(eligible)
            for feature in predictor_columns:
                x = daily[feature].where(eligible)
                aligned = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
                auc = _auc_score(aligned["y"], aligned["x"])
                rows.append(
                    {
                        "horizon": horizon,
                        "target": target_name,
                        "feature": feature,
                        "observations": int(len(aligned)),
                        "positives": int(aligned["y"].astype(bool).sum()) if len(aligned) else 0,
                        "auc": auc,
                        "discrimination_auc": (
                            max(auc, 1.0 - auc) if np.isfinite(auc) else float("nan")
                        ),
                        "risk_direction": (
                            "high"
                            if np.isfinite(auc) and auc >= 0.5
                            else "low" if np.isfinite(auc) else ""
                        ),
                    }
                )
    return pd.DataFrame(rows)


def _strategy_binary_separation_summary(
    daily: pd.DataFrame,
    *,
    predictor_columns: list[str],
    horizons: list[int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    current_drawdown = daily["strategy_drawdown_252"]
    for horizon in horizons:
        target_specs: list[tuple[str, pd.Series, pd.Series]] = [
            (
                "strategy_future_loss_10pct",
                daily[f"strategy_future_loss_10pct_{horizon}"],
                daily[f"strategy_future_min_return_{horizon}"].notna(),
            ),
            (
                "strategy_future_loss_10pct_while_drawdown_gt_minus10",
                daily[f"strategy_future_loss_10pct_{horizon}"],
                daily[f"strategy_future_min_return_{horizon}"].notna()
                & current_drawdown.gt(-0.10),
            ),
        ]
        rebound_known = (
            daily[f"strategy_failed_rebound_{horizon}"]
            | daily[f"strategy_durable_recovery_{horizon}"]
        )
        target_specs.append(
            (
                "strategy_failed_vs_durable_rebound",
                daily[f"strategy_failed_rebound_{horizon}"],
                rebound_known,
            )
        )

        for target_name, target, eligible in target_specs:
            y = target.where(eligible)
            for feature in predictor_columns:
                x = daily[feature].where(eligible)
                aligned = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
                auc = _auc_score(aligned["y"], aligned["x"])
                rows.append(
                    {
                        "horizon": horizon,
                        "target": target_name,
                        "feature": feature,
                        "observations": int(len(aligned)),
                        "positives": int(aligned["y"].astype(bool).sum()) if len(aligned) else 0,
                        "auc": auc,
                        "discrimination_auc": (
                            max(auc, 1.0 - auc) if np.isfinite(auc) else float("nan")
                        ),
                        "risk_direction": (
                            "high"
                            if np.isfinite(auc) and auc >= 0.5
                            else "low" if np.isfinite(auc) else ""
                        ),
                    }
                )
    return pd.DataFrame(rows)


def _strategy_event_market_context(
    daily: pd.DataFrame,
    strategy_events: pd.DataFrame,
    market_events: pd.DataFrame,
    *,
    label_horizon: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for event in strategy_events.itertuples(index=False):
        onset = pd.Timestamp(event.onset_date)
        if onset not in daily.index:
            continue
        overlapping = None
        for market_event in market_events.itertuples(index=False):
            market_peak = pd.Timestamp(market_event.peak_date)
            market_recovery = (
                pd.Timestamp(market_event.recovery_date)
                if pd.notna(market_event.recovery_date)
                else daily.index.max()
            )
            if market_peak <= onset <= market_recovery:
                overlapping = market_event
                break

        nearest_event_id = ""
        nearest_onset_bars = float("nan")
        if len(market_events):
            onset_position = int(daily.index.get_loc(onset))
            distances: list[tuple[int, Any]] = []
            for market_event in market_events.itertuples(index=False):
                market_onset = pd.Timestamp(market_event.onset_date)
                if market_onset not in daily.index:
                    continue
                distances.append(
                    (
                        int(daily.index.get_loc(market_onset)) - onset_position,
                        market_event,
                    )
                )
            if distances:
                nearest_onset_bars, nearest = min(distances, key=lambda item: abs(item[0]))
                nearest_event_id = str(nearest.event_id)

        row = daily.loc[onset]
        rows.append(
            {
                "strategy_event_id": event.event_id,
                "strategy_peak_date": event.peak_date,
                "strategy_onset_date": event.onset_date,
                "strategy_trough_date": event.trough_date,
                "strategy_recovery_date": event.recovery_date,
                "strategy_max_drawdown": event.max_drawdown,
                "strategy_peak_to_recovery_bars": event.peak_to_recovery_bars,
                "market_overlap": overlapping is not None,
                "overlapping_market_event_id": (
                    str(overlapping.event_id) if overlapping is not None else ""
                ),
                "nearest_market_event_id": nearest_event_id,
                "nearest_market_onset_minus_strategy_onset_bars": nearest_onset_bars,
                "market_state_at_strategy_onset": row[f"state_label_{label_horizon}"],
                "market_drawdown_252_at_strategy_onset": row["benchmark_drawdown_252"],
                "market_rv_10_at_strategy_onset": row["benchmark_rv_10"],
                "market_breadth_10_at_strategy_onset": row["breadth_positive_10"],
                "market_breadth_63_at_strategy_onset": row["breadth_positive_63"],
                "strategy_drawdown_252_at_onset": row["strategy_drawdown_252"],
                "strategy_relative_return_21_at_onset": row["strategy_relative_return_21"],
                "strategy_market_beta_21_at_onset": row["strategy_market_beta_21"],
                "strategy_future_min_return": row[
                    f"strategy_future_min_return_{label_horizon}"
                ],
                "market_future_min_return": row[f"future_min_return_{label_horizon}"],
            }
        )
    return pd.DataFrame(rows)


def _quantile_summary(
    daily: pd.DataFrame,
    *,
    predictor_columns: list[str],
    horizons: list[int],
    quantiles: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        outcome_columns = [
            f"future_return_{horizon}",
            f"future_min_return_{horizon}",
            f"future_new_63d_low_{horizon}",
        ]
        for feature in predictor_columns:
            source = pd.concat(
                [daily[feature], daily[outcome_columns]],
                axis=1,
            ).dropna(subset=[feature, f"future_min_return_{horizon}"])
            if len(source) < quantiles * 10:
                continue
            bucket = pd.qcut(
                source[feature],
                q=quantiles,
                labels=False,
                duplicates="drop",
            )
            source = source.assign(bucket=bucket)
            for bucket_value, group in source.groupby("bucket", sort=True):
                rows.append(
                    {
                        "horizon": horizon,
                        "feature": feature,
                        "quantile_low_to_high": int(bucket_value) + 1,
                        "count": int(len(group)),
                        "feature_min": float(group[feature].min()),
                        "feature_max": float(group[feature].max()),
                        "mean_future_return": float(group[f"future_return_{horizon}"].mean()),
                        "median_future_return": float(group[f"future_return_{horizon}"].median()),
                        "mean_future_min_return": float(
                            group[f"future_min_return_{horizon}"].mean()
                        ),
                        "future_loss_10pct_ratio": float(
                            group[f"future_min_return_{horizon}"].le(-0.10).mean()
                        ),
                        "future_new_63d_low_ratio": float(
                            group[f"future_new_63d_low_{horizon}"].mean()
                        ),
                    }
                )
    return pd.DataFrame(rows)


def _strategy_quantile_summary(
    daily: pd.DataFrame,
    *,
    predictor_columns: list[str],
    horizons: list[int],
    quantiles: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        outcome_columns = [
            f"strategy_future_return_{horizon}",
            f"strategy_future_min_return_{horizon}",
            f"strategy_future_new_63d_low_{horizon}",
        ]
        for feature in predictor_columns:
            source = pd.concat(
                [daily[feature], daily[outcome_columns]],
                axis=1,
            ).dropna(subset=[feature, f"strategy_future_min_return_{horizon}"])
            if len(source) < quantiles * 10:
                continue
            bucket = pd.qcut(
                source[feature],
                q=quantiles,
                labels=False,
                duplicates="drop",
            )
            source = source.assign(bucket=bucket)
            for bucket_value, group in source.groupby("bucket", sort=True):
                rows.append(
                    {
                        "horizon": horizon,
                        "feature": feature,
                        "quantile_low_to_high": int(bucket_value) + 1,
                        "count": int(len(group)),
                        "feature_min": float(group[feature].min()),
                        "feature_max": float(group[feature].max()),
                        "mean_strategy_future_return": float(
                            group[f"strategy_future_return_{horizon}"].mean()
                        ),
                        "median_strategy_future_return": float(
                            group[f"strategy_future_return_{horizon}"].median()
                        ),
                        "mean_strategy_future_min_return": float(
                            group[f"strategy_future_min_return_{horizon}"].mean()
                        ),
                        "strategy_future_loss_10pct_ratio": float(
                            group[f"strategy_future_min_return_{horizon}"].le(-0.10).mean()
                        ),
                        "strategy_future_new_63d_low_ratio": float(
                            group[f"strategy_future_new_63d_low_{horizon}"].mean()
                        ),
                    }
                )
    return pd.DataFrame(rows)


def _state_map(daily: pd.DataFrame, *, horizons: Iterable[int]) -> pd.DataFrame:
    drawdown_band = pd.cut(
        daily["benchmark_drawdown_252"],
        bins=[-np.inf, -0.20, -0.10, -0.05, np.inf],
        labels=["dd_le_20", "dd_10_to_20", "dd_5_to_10", "dd_lt_5"],
    )
    breadth_band = pd.cut(
        daily["breadth_positive_10"],
        bins=[-np.inf, 0.40, 0.60, np.inf],
        labels=["breadth_le_40", "breadth_40_to_60", "breadth_gt_60"],
    )
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        frame = pd.DataFrame(
            {
                "drawdown_band": drawdown_band,
                "breadth_band": breadth_band,
                "future_return": daily[f"future_return_{horizon}"],
                "future_min_return": daily[f"future_min_return_{horizon}"],
                "future_new_low": daily[f"future_new_63d_low_{horizon}"],
            }
        ).dropna()
        for (drawdown, breadth), group in frame.groupby(
            ["drawdown_band", "breadth_band"],
            observed=True,
            sort=False,
        ):
            rows.append(
                {
                    "horizon": horizon,
                    "drawdown_band": str(drawdown),
                    "breadth_band": str(breadth),
                    "count": int(len(group)),
                    "mean_future_return": float(group["future_return"].mean()),
                    "median_future_return": float(group["future_return"].median()),
                    "mean_future_min_return": float(group["future_min_return"].mean()),
                    "future_loss_10pct_ratio": float(
                        group["future_min_return"].le(-0.10).mean()
                    ),
                    "future_new_63d_low_ratio": float(group["future_new_low"].mean()),
                }
            )
    return pd.DataFrame(rows)


def _validate_args(args: argparse.Namespace) -> None:
    positive_ints = {
        "label-horizon": args.label_horizon,
        "correlation-window": args.correlation_window,
        "min-breadth-markets": args.min_breadth_markets,
        "quantiles": args.quantiles,
        "pre-event-bars": args.pre_event_bars,
        "rebound-return-window": args.rebound_return_window,
        "rebound-breadth-window": args.rebound_breadth_window,
    }
    invalid = [name for name, value in positive_ints.items() if value <= 0]
    if invalid:
        raise ValueError(f"These arguments must be positive: {invalid}")
    if args.event_onset_drawdown_pct <= 0.0:
        raise ValueError("event-onset-drawdown-pct must be positive")
    if args.event_qualifying_drawdown_pct < args.event_onset_drawdown_pct:
        raise ValueError(
            "event-qualifying-drawdown-pct must not be below event-onset-drawdown-pct"
        )
    if not 0.0 <= args.rebound_breadth_threshold <= 1.0:
        raise ValueError("rebound-breadth-threshold must be between 0 and 1")


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    horizons = _parse_ints(args.forward_horizons, label="forward-horizons")
    return_windows = _parse_ints(args.return_windows, label="return-windows")
    vol_windows = _parse_ints(args.vol_windows, label="vol-windows")
    drawdown_windows = _parse_ints(args.drawdown_windows, label="drawdown-windows")
    if args.label_horizon not in horizons:
        horizons = sorted({*horizons, int(args.label_horizon)})
    required_return_windows = {
        5,
        10,
        21,
        63,
        int(args.rebound_return_window),
        int(args.rebound_breadth_window),
    }
    return_windows = sorted(set(return_windows) | required_return_windows)
    drawdown_windows = sorted(set(drawdown_windows) | {252})

    cache_dir = _resolve_path(args.source_cache_dir)
    out_dir = _resolve_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prices, benchmark, markets = _read_price_frame(
        cache_dir,
        frame_name=args.price_column,
        benchmark_market=args.benchmark_market,
        market_columns=_parse_markets(args.market_columns),
        start_date=args.start_date,
        end_date=args.end_date,
    )

    daily, predictor_columns = _build_predictors(
        prices,
        benchmark=benchmark,
        markets=markets,
        return_windows=return_windows,
        vol_windows=vol_windows,
        drawdown_windows=drawdown_windows,
        correlation_window=int(args.correlation_window),
        min_breadth_markets=int(args.min_breadth_markets),
    )
    daily = _attach_outcomes_and_labels(
        daily,
        prices[benchmark],
        horizons=horizons,
        label_horizon=int(args.label_horizon),
        rebound_drawdown_pct=float(args.rebound_drawdown_pct),
        rebound_return_window=int(args.rebound_return_window),
        rebound_breadth_window=int(args.rebound_breadth_window),
        rebound_breadth_threshold=float(args.rebound_breadth_threshold),
        rebound_failure_drawdown_pct=float(args.rebound_failure_drawdown_pct),
    )
    events = _detect_drawdown_events(
        prices[benchmark],
        onset_drawdown_pct=float(args.event_onset_drawdown_pct),
        qualifying_drawdown_pct=float(args.event_qualifying_drawdown_pct),
    )
    event_raw, event_profile, event_consistency = _event_relative_outputs(
        daily,
        events,
        predictor_columns=predictor_columns,
        pre_event_bars=int(args.pre_event_bars),
    )
    binary_summary = _binary_separation_summary(
        daily,
        predictor_columns=predictor_columns,
        horizons=horizons,
    )
    quantile_summary = _quantile_summary(
        daily,
        predictor_columns=predictor_columns,
        horizons=horizons,
        quantiles=int(args.quantiles),
    )
    state_map = _state_map(daily, horizons=horizons)

    strategy_metadata: dict[str, Any] | None = None
    strategy_events = pd.DataFrame()
    strategy_event_raw = pd.DataFrame()
    strategy_event_profile = pd.DataFrame()
    strategy_event_consistency = pd.DataFrame()
    strategy_binary_summary = pd.DataFrame()
    strategy_quantile_summary = pd.DataFrame()
    strategy_event_context = pd.DataFrame()
    if args.strategy_equity_csv:
        strategy_equity_path = _resolve_path(args.strategy_equity_csv)
        source_strategy_equity = _read_equity_curve(
            strategy_equity_path,
            value_column=args.strategy_value_column,
        )
        common_dates = prices.index.intersection(source_strategy_equity.index)
        if len(common_dates) < 300:
            raise ValueError("Strategy equity and market cache have fewer than 300 common dates")
        strategy_equity = source_strategy_equity.reindex(prices.index)
        strategy_start = common_dates.min()
        strategy_end = common_dates.max()
        internal_missing = strategy_equity.loc[strategy_start:strategy_end].isna()
        if internal_missing.any():
            missing_dates = strategy_equity.loc[strategy_start:strategy_end].index[internal_missing]
            raise ValueError(
                "Strategy equity is missing market-cache dates inside its analysis period; "
                f"first missing date: {missing_dates[0].date()}"
            )

        strategy_predictors, strategy_predictor_columns = _build_strategy_predictors(
            strategy_equity,
            prices[benchmark],
            return_windows=return_windows,
            vol_windows=vol_windows,
            drawdown_windows=drawdown_windows,
        )
        daily = daily.join(strategy_predictors)
        all_strategy_predictors = [*predictor_columns, *strategy_predictor_columns]
        daily = _attach_strategy_outcomes_and_labels(
            daily,
            strategy_equity,
            horizons=horizons,
            label_horizon=int(args.label_horizon),
            rebound_drawdown_pct=float(args.rebound_drawdown_pct),
            rebound_return_window=int(args.rebound_return_window),
            rebound_failure_drawdown_pct=float(args.rebound_failure_drawdown_pct),
        )
        strategy_events = _detect_drawdown_events(
            strategy_equity,
            onset_drawdown_pct=float(args.event_onset_drawdown_pct),
            qualifying_drawdown_pct=float(args.event_qualifying_drawdown_pct),
        )
        (
            strategy_event_raw,
            strategy_event_profile,
            strategy_event_consistency,
        ) = _event_relative_outputs(
            daily,
            strategy_events,
            predictor_columns=all_strategy_predictors,
            pre_event_bars=int(args.pre_event_bars),
        )
        strategy_binary_summary = _strategy_binary_separation_summary(
            daily,
            predictor_columns=all_strategy_predictors,
            horizons=horizons,
        )
        strategy_quantile_summary = _strategy_quantile_summary(
            daily,
            predictor_columns=all_strategy_predictors,
            horizons=horizons,
            quantiles=int(args.quantiles),
        )
        strategy_event_context = _strategy_event_market_context(
            daily,
            strategy_events,
            events,
            label_horizon=int(args.label_horizon),
        )
        strategy_state_column = f"strategy_state_label_{args.label_horizon}"
        strategy_metadata = {
            "name": str(args.strategy_name),
            "equity_csv": str(strategy_equity_path),
            "value_column": str(args.strategy_value_column),
            "start": strategy_start.date().isoformat(),
            "end": strategy_end.date().isoformat(),
            "observations": int(len(common_dates)),
            "predictor_columns": strategy_predictor_columns,
            "qualified_drawdown_events": int(len(strategy_events)),
            "state_counts": {
                str(key): int(value)
                for key, value in daily.loc[
                    strategy_start:strategy_end,
                    strategy_state_column,
                ]
                .value_counts(dropna=False)
                .items()
            },
        }

    daily.to_parquet(out_dir / "daily_features_and_labels.parquet")
    events.to_csv(out_dir / "bear_event_catalog.csv", index=False, encoding="utf-8-sig")
    event_raw.to_parquet(out_dir / "bear_onset_event_features.parquet", index=False)
    event_profile.to_csv(
        out_dir / "bear_onset_relative_profile.csv",
        index=False,
        encoding="utf-8-sig",
    )
    event_consistency.to_csv(
        out_dir / "bear_onset_feature_consistency.csv",
        index=False,
        encoding="utf-8-sig",
    )
    binary_summary.to_csv(
        out_dir / "feature_binary_separation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    quantile_summary.to_csv(
        out_dir / "feature_quantile_outcomes.csv",
        index=False,
        encoding="utf-8-sig",
    )
    state_map.to_csv(out_dir / "drawdown_breadth_state_map.csv", index=False, encoding="utf-8-sig")
    if strategy_metadata is not None:
        strategy_events.to_csv(
            out_dir / "strategy_drawdown_event_catalog.csv",
            index=False,
            encoding="utf-8-sig",
        )
        strategy_event_raw.to_parquet(
            out_dir / "strategy_onset_event_features.parquet",
            index=False,
        )
        strategy_event_profile.to_csv(
            out_dir / "strategy_onset_relative_profile.csv",
            index=False,
            encoding="utf-8-sig",
        )
        strategy_event_consistency.to_csv(
            out_dir / "strategy_onset_feature_consistency.csv",
            index=False,
            encoding="utf-8-sig",
        )
        strategy_binary_summary.to_csv(
            out_dir / "strategy_feature_binary_separation.csv",
            index=False,
            encoding="utf-8-sig",
        )
        strategy_quantile_summary.to_csv(
            out_dir / "strategy_feature_quantile_outcomes.csv",
            index=False,
            encoding="utf-8-sig",
        )
        strategy_event_context.to_csv(
            out_dir / "strategy_event_market_context.csv",
            index=False,
            encoding="utf-8-sig",
        )

    state_column = f"state_label_{args.label_horizon}"
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "descriptive_market_regime_transition_event_study_v2",
        "source_cache_dir": str(cache_dir),
        "price_column": str(args.price_column),
        "benchmark_market": benchmark,
        "breadth_markets": markets,
        "market_count": len(markets),
        "start": daily.index.min().date().isoformat(),
        "end": daily.index.max().date().isoformat(),
        "observations": int(len(daily)),
        "forward_horizons": horizons,
        "predictor_columns": predictor_columns,
        "qualified_bear_events": int(len(events)),
        "strategy_analysis": strategy_metadata,
        "state_counts": {
            str(key): int(value)
            for key, value in daily[state_column].value_counts(dropna=False).items()
        },
        "definitions": {
            "event_onset": (
                f"First expanding-peak drawdown at or below -{args.event_onset_drawdown_pct:g}%"
            ),
            "qualified_bear_event": (
                f"Drawdown episode eventually reaching -{args.event_qualifying_drawdown_pct:g}%"
            ),
            "pre_bear": (
                f"Current 252-bar drawdown above -5% and next {args.label_horizon} bars "
                "contain a loss of at least 10% from the current price"
            ),
            "early_bear": (
                f"Current 252-bar drawdown between -5% and -10% and next "
                f"{args.label_horizon} bars contain a loss of at least 10%"
            ),
            "rebound_candidate": (
                f"Current drawdown at or below -{args.rebound_drawdown_pct:g}%, "
                f"{args.rebound_return_window}-bar benchmark return positive, and "
                f"{args.rebound_breadth_window}-bar positive breadth at least "
                f"{args.rebound_breadth_threshold:g}"
            ),
            "failed_rebound": (
                f"Rebound candidate followed by a loss of at least "
                f"{args.rebound_failure_drawdown_pct:g}% or a new trailing-63-bar low"
            ),
            "durable_recovery": (
                f"Rebound candidate with positive terminal return, no loss beyond "
                f"{args.rebound_failure_drawdown_pct:g}%, and no new trailing-63-bar low"
            ),
            "warning": (
                "Labels use future prices for research outcomes only. They must never be "
                "used directly as live features. Daily rows and forward labels overlap; "
                "event-level consistency is the primary robustness view."
            ),
        },
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Wrote market regime transition analysis to {out_dir} "
        f"(rows={len(daily)}, bear_events={len(events)}, markets={len(markets)}, "
        f"strategy_events={len(strategy_events) if strategy_metadata is not None else 0})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
