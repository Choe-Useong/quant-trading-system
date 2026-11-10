from __future__ import annotations

from dataclasses import replace
import re

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform

from lib.specs import DefensiveSleeveSpec, FilterStageSpec, PortfolioVolTargetSpec, WeightSpec
from lib.dataframes import CACHE_ELIGIBILITY_FRAME


# Tolerance for "same value in every market column" checks. Broadcast features
# computed per column (e.g. market:BTC rolling statistics with per-market start
# points) differ by floating-point error of about 1e-12 relative.
BROADCAST_ATOL = 1e-12
BROADCAST_RTOL = 1e-9


def _rebalance_mask(index: pd.Index, frequency: str) -> pd.Series:
    frequency = str(frequency).strip().lower()
    timestamps = pd.to_datetime(index)
    if frequency == "every_bar":
        return pd.Series(True, index=index)
    nday = re.fullmatch(r"([1-9][0-9]*)d(?:\+([1-9][0-9]*))?", frequency)
    if nday:
        # Global UTC calendar buckets keep the phase independent of the input
        # start (including WF slices). A Monday anchor also makes 7d == weekly.
        # "Nd+K" shifts bucket starts by K days (7d+3: Thursdays 00 UTC).
        days = int(nday.group(1))
        offset = int(nday.group(2) or 0)
        if offset >= days:
            raise ValueError(f"N-day rebalance offset must be 1..N-1: {frequency}")
        utc = timestamps.tz_localize("UTC") if timestamps.tz is None else timestamps.tz_convert("UTC")
        if utc.hasnans:
            raise ValueError("N-day rebalance timestamps must not contain NaT")
        elapsed_days = (utc.normalize() - pd.Timestamp("1970-01-05", tz="UTC")).days
        keys = pd.Index((elapsed_days - offset) // days)
    elif frequency == "daily":
        keys = pd.Index([ts.strftime("%Y-%m-%d") for ts in timestamps], dtype="object")
    elif frequency == "weekly":
        keys = pd.Index([f"{ts.isocalendar().year}-{ts.isocalendar().week}" for ts in timestamps], dtype="object")
    elif frequency == "monthly":
        keys = pd.Index([f"{ts.year}-{ts.month:02d}" for ts in timestamps], dtype="object")
    elif frequency in {"quarterly", "quarter", "q"}:
        keys = pd.Index([f"{ts.year}-Q{((ts.month - 1) // 3) + 1}" for ts in timestamps], dtype="object")
    elif frequency in {"semiannual", "semi_annually", "half_year", "half-year"}:
        keys = pd.Index([f"{ts.year}-H{1 if ts.month <= 6 else 2}" for ts in timestamps], dtype="object")
    else:
        raise ValueError(f"Unsupported rebalance frequency for frame_v2: {frequency}")
    return pd.Series(~keys.duplicated(), index=index)


def is_change_only_rebalance_frequency(frequency: str) -> bool:
    return str(frequency).strip().lower() in {"change_only", "change-only"}


def _is_change_only_output_mode(output_mode: str) -> bool:
    return str(output_mode or "").strip().lower() in {"change_only", "change-only"}


def is_change_only_weight_output(spec: WeightSpec) -> bool:
    return is_change_only_rebalance_frequency(spec.rebalance_frequency) or _is_change_only_output_mode(spec.output_mode)


def to_change_only_target_frame(target: pd.DataFrame, *, scope: str = "portfolio") -> pd.DataFrame:
    """Emit requests from dense targets; callers resolve hold NaNs before slicing.

    Portfolio scope preserves historical whole-row requests. Asset scope emits
    only changed cells, including explicit zero exits. Every first-row target
    is emitted so a fresh simulation can establish its initial positions.
    """
    if scope not in ("portfolio", "asset"):
        raise ValueError("change_only_scope must be 'portfolio' or 'asset'")
    dense = target.fillna(0.0)
    if scope == "asset":
        changed = dense.ne(dense.shift(1))
        if len(changed.index):
            changed.iloc[0, :] = True
        return dense.where(changed)
    change_mask = dense.ne(dense.shift(1)).any(axis=1)
    if not change_mask.empty:
        change_mask.iloc[0] = True

    sparse = pd.DataFrame(float("nan"), index=target.index, columns=target.columns)
    sparse.loc[change_mask, :] = dense.loc[change_mask, :]
    return sparse


def combine_strategy_sleeve_frames(frames: list[pd.DataFrame], capitals: list[float]) -> pd.DataFrame:
    """Combine final per-sleeve request frames into one account request frame.

    Inputs are execution-ready frames (NaN = hold, value = account-equity target)
    on one common index. A market is requested at t when any sleeve holding that
    column requests it; the request is sum(capital * latest sleeve target), with
    sleeves before their first request contributing zero. Other cells stay NaN.
    Disjoint markets therefore reproduce the scaled sleeve frames exactly. On a
    shared market, a sleeve that does not request at t contributes its latest
    target weight, not its price-drifted holding.
    """
    if not frames or len(frames) != len(capitals):
        raise ValueError("strategy sleeves require one capital per non-empty frame list")
    index = frames[0].index
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("strategy sleeve frames require unique chronological timestamps")
    columns: list = []
    for frame in frames:
        if not frame.index.equals(index):
            raise ValueError("strategy sleeve frames must share the same timestamps")
        if not frame.columns.is_unique:
            raise ValueError("strategy sleeve frames require unique markets")
        columns.extend(column for column in frame.columns if column not in columns)
    values = np.zeros((len(index), len(columns)))
    requested = np.zeros((len(index), len(columns)), dtype=bool)
    for frame, capital in zip(frames, capitals):
        capital = float(capital)
        if not np.isfinite(capital) or capital < 0:
            raise ValueError("strategy sleeve capital must be finite and non-negative")
        sleeve = frame.astype(float)
        cells = sleeve.to_numpy()
        if np.isinf(cells).any():
            raise ValueError("strategy sleeve targets must be finite or NaN")
        positions = [columns.index(column) for column in sleeve.columns]
        requested[:, positions] |= ~np.isnan(cells)
        values[:, positions] += capital * sleeve.ffill().fillna(0.0).to_numpy()
    return pd.DataFrame(np.where(requested, values, np.nan), index=index, columns=pd.Index(columns))


def _normalize_frequency(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _exposure_scale_series(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
    *,
    side: str | None = None,
) -> pd.Series | None:
    prefix = "gross" if side is None else side
    field = f"{prefix}_exposure_feature"
    feature_name = getattr(spec, field)
    if not feature_name:
        return None
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing {prefix} exposure feature for weights_v2: {feature_name}")

    frame = feature_frames[feature_name].copy()
    frame = frame.reindex(selection_mask.index)

    if frame.shape[1] == 0:
        raise ValueError(f"{prefix.capitalize()} exposure feature has no columns for weights_v2: {feature_name}")

    if frame.shape[1] == 1:
        scalar = frame.iloc[:, 0].astype(float)
    else:
        values = frame.astype(float)
        row_min = values.min(axis=1, skipna=True)
        row_max = values.max(axis=1, skipna=True)
        inconsistent = row_min.notna() & row_max.notna() & ~np.isclose(row_max, row_min, atol=BROADCAST_ATOL, rtol=BROADCAST_RTOL)
        if bool(inconsistent.any()):
            first_bad = str(inconsistent[inconsistent].index[0])
            raise ValueError(
                f"{field} must be scalar/broadcast across markets for weights_v2: "
                f"{feature_name} at {first_bad}"
            )
        scalar = values.bfill(axis=1).iloc[:, 0]

    lag = int(getattr(spec, f"{prefix}_exposure_lag"))
    if lag > 0:
        scalar = scalar.shift(lag)
    elif lag < 0:
        raise ValueError(f"{prefix}_exposure_lag must be non-negative for weights_v2")

    if side is not None:
        # Side features are multipliers, not target exposures. Missing history
        # is neutral; never borrow a later observation to fill the prefix.
        values = frame.to_numpy(dtype=float, copy=False)
        if np.isinf(values).any() or (values < 0).any():
            raise ValueError(f"{field} must contain finite non-negative multipliers or NaN")
        return scalar.fillna(1.0)

    clip_min = float(spec.gross_exposure_clip_min)
    clip_max = float(spec.gross_exposure_clip_max)
    if clip_min > clip_max:
        raise ValueError("gross_exposure_clip_min must be <= gross_exposure_clip_max")

    return scalar.astype(float).clip(lower=clip_min, upper=clip_max).fillna(0.0)


def _defensive_risk_on_series(
    index: pd.Index,
    sleeve: DefensiveSleeveSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.Series:
    feature_name = str(sleeve.risk_on_feature)
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing defensive_sleeve risk_on_feature for weights_v2: {feature_name}")

    frame = feature_frames[feature_name].copy()
    frame = frame.reindex(index)

    if frame.shape[1] == 0:
        raise ValueError(f"defensive_sleeve risk_on_feature has no columns for weights_v2: {feature_name}")

    if frame.shape[1] == 1:
        scalar = frame.iloc[:, 0].astype(float)
    else:
        values = frame.astype(float)
        row_min = values.min(axis=1, skipna=True)
        row_max = values.max(axis=1, skipna=True)
        inconsistent = row_min.notna() & row_max.notna() & ~np.isclose(row_max, row_min, atol=BROADCAST_ATOL, rtol=BROADCAST_RTOL)
        if bool(inconsistent.any()):
            first_bad = str(inconsistent[inconsistent].index[0])
            raise ValueError(
                f"defensive_sleeve risk_on_feature must be scalar/broadcast across markets for weights_v2: "
                f"{feature_name} at {first_bad}"
            )
        scalar = values.bfill(axis=1).iloc[:, 0]

    lag = int(sleeve.risk_on_lag)
    if lag > 0:
        scalar = scalar.shift(lag)
    elif lag < 0:
        raise ValueError("defensive_sleeve risk_on_lag must be non-negative for weights_v2")

    return scalar.astype(float)


def _defensive_listed_mask_frame(
    target: pd.DataFrame,
    sleeve: DefensiveSleeveSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
    markets: tuple[str, ...],
) -> pd.DataFrame:
    feature_name = str(sleeve.listed_feature or "trade_price")
    if feature_frames is None or feature_name not in feature_frames:
        return pd.DataFrame(True, index=target.index, columns=markets)
    frame = feature_frames[feature_name].reindex(index=target.index, columns=markets)
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    return numeric.notna().cummax()


def _apply_defensive_sleeve_target_frame(
    target: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    sleeve = spec.defensive_sleeve
    if sleeve is None:
        return target

    weighting = str(sleeve.weighting or "equal").strip().lower()
    if weighting != "equal":
        raise ValueError("defensive_sleeve currently supports only equal weighting")

    markets = tuple(dict.fromkeys(str(market).upper() for market in sleeve.markets if str(market).strip()))
    if not markets:
        raise ValueError("defensive_sleeve markets must not be empty")
    missing_markets = [market for market in markets if market not in target.columns]
    if missing_markets:
        raise ValueError(f"defensive_sleeve markets missing from target columns: {missing_markets}")

    gross_exposure = float(sleeve.gross_exposure)
    if gross_exposure < 0.0 or not np.isfinite(gross_exposure):
        raise ValueError("defensive_sleeve gross_exposure must be finite and non-negative")

    risk_on = _defensive_risk_on_series(target.index, sleeve, feature_frames)
    risk_on_flag = risk_on.notna() & risk_on.ne(0.0)
    explicit_rows = target.notna().any(axis=1)
    defensive_rows = explicit_rows & ~risk_on_flag
    if not bool(defensive_rows.any()):
        return target

    result = target.copy()
    listed = _defensive_listed_mask_frame(result, sleeve, feature_frames, markets)
    for timestamp in result.index[defensive_rows]:
        active_markets = [market for market in markets if bool(listed.loc[timestamp, market])]
        row = pd.Series(0.0, index=result.columns, dtype=float)
        if active_markets:
            row.loc[active_markets] = gross_exposure / float(len(active_markets))
        result.loc[timestamp, :] = row
    return result


def _position_score_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame | None:
    feature_name = spec.position_score_feature
    if not feature_name:
        return None
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing position score feature for weights_v2: {feature_name}")

    frame = feature_frames[feature_name].copy()
    frame = frame.reindex(index=selection_mask.index, columns=selection_mask.columns)

    lag = int(spec.position_score_lag)
    if lag > 0:
        frame = frame.shift(lag)
    elif lag < 0:
        raise ValueError("position_score_lag must be non-negative for weights_v2")

    return frame.astype(float).fillna(0.0)


def _feature_value_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    position_scores: pd.DataFrame | None,
    exposure_scale: pd.Series | None,
    rebalance_dates: pd.Series,
) -> pd.DataFrame:
    if position_scores is None:
        raise ValueError("position_score_feature must be provided for feature_value weighting in weights_v2")

    scale = float(spec.feature_value_scale)
    if scale <= 0.0:
        raise ValueError("feature_value_scale must be positive for feature_value weighting in weights_v2")

    clip_min = float(spec.feature_value_clip_min)
    clip_max = float(spec.feature_value_clip_max)
    if clip_min > clip_max:
        raise ValueError("feature_value_clip_min must be <= feature_value_clip_max for weights_v2")

    gross_exposure = float(spec.gross_exposure)
    selected = selection_mask.fillna(False).astype(bool)
    scores = position_scores.div(scale).clip(lower=clip_min, upper=clip_max)
    scores = scores.where(selected, 0.0)

    if spec.rebalance_frequency == "every_bar":
        active = scores.gt(0.0)
        active_count = active.sum(axis=1).replace(0, np.nan)
        target = scores.mul(gross_exposure).div(active_count, axis=0)
        if exposure_scale is not None:
            target = target.mul(exposure_scale, axis=0)
        return target.fillna(0.0)

    rebalance_index = selection_mask.index[rebalance_dates]
    target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
    if len(rebalance_index) == 0:
        return target

    rebalance_scores = scores.loc[rebalance_index]
    active = rebalance_scores.gt(0.0)
    active_count = active.sum(axis=1).replace(0, np.nan)
    rebalance_target = rebalance_scores.mul(gross_exposure).div(active_count, axis=0)
    if exposure_scale is not None:
        rebalance_target = rebalance_target.mul(exposure_scale.loc[rebalance_index], axis=0)
    target.loc[rebalance_index, :] = rebalance_target.fillna(0.0)
    return target


def _long_short_signals(selection, spec, frames, base_eligible_mask):
    """Resolve optional rank conditions with the universe's original population."""
    for name in ("long_exposure", "short_exposure"):
        value = getattr(spec, name)
        if (isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating))
                or not np.isfinite(value) or value < 0):
            raise ValueError(f"long_short_equal {name} must be finite and non-negative")
    if spec.gross_exposure != 1.0:
        raise ValueError("long_short_equal uses long_exposure/short_exposure; gross_exposure must be 1")
    filters = (spec.long_filter, spec.short_filter)
    if all(item is None for item in filters):
        if not spec.position_score_feature:
            raise ValueError("long_short_equal requires position_score_feature or long_filter/short_filter")
        return spec, frames
    if any(item is None for item in filters) or spec.position_score_feature:
        raise ValueError("long_short_equal requires both filters and no position_score_feature")
    if spec.position_score_lag != 0:
        raise ValueError("long_short_equal rank filters use their own lag; position_score_lag must be 0")
    from lib.universe_v2 import _apply_filter_stage
    if frames is None:
        raise ValueError("long_short_equal rank filters require feature frames")
    base = (None if base_eligible_mask is None else
            base_eligible_mask.reindex(index=selection.index, columns=selection.columns).eq(True))
    usage = frames.get(CACHE_ELIGIBILITY_FRAME)
    candidates = selection.fillna(False).astype(bool)
    if usage is not None:
        eligible = usage.reindex(index=selection.index, columns=selection.columns).eq(True)
        candidates = candidates & eligible
        if base is not None:
            base = base & eligible
    masks = []
    rank_cache = {}
    aligned_frames = {}
    for side in filters:
        stage = side if isinstance(side, FilterStageSpec) else FilterStageSpec(mode="and", filters=(side,))
        if stage.mode not in ("and", "sequential") or not stage.filters:
            raise ValueError("long_short_equal side groups require mode='and' or 'sequential' and non-empty filters")
        for item in stage.filters:
            if type(item.lag) is not int or item.lag < 0:
                raise ValueError("long_short_equal filter lag must be a non-negative integer")
            if item.mode == "quantile":
                if (type(item.quantiles) is not int or item.quantiles <= 0 or not item.bucket_values
                        or any(type(b) is not int or not 1 <= b <= item.quantiles for b in item.bucket_values)):
                    raise ValueError("long_short_equal filter requires valid quantiles/bucket_values")
            elif item.mode == "top_n":
                if type(item.top_n) is not int or item.top_n <= 0:
                    raise ValueError("long_short_equal filter top_n must be a positive integer")
            else:
                raise ValueError("long_short_equal filter mode must be top_n or quantile")
            if item.feature_column not in frames:
                raise ValueError(f"Missing long_short_equal filter feature: {item.feature_column}")
            if (item.scope or "filtered").lower() in {"global", "base"} and base is None:
                raise ValueError("long_short_equal global filters require base_eligible_mask from the universe")
            if item.feature_column not in aligned_frames:
                values = frames[item.feature_column]
                if not values.index.equals(selection.index) or not values.columns.equals(selection.columns):
                    values = values.reindex(index=selection.index, columns=selection.columns)
                if np.isinf(values.to_numpy(dtype=float)).any():
                    raise ValueError("long_short_equal filter feature must not contain infinity")
                aligned_frames[item.feature_column] = values
        mask, _ = _apply_filter_stage(
            candidates, ("selection",), candidates if base is None else base, ("base",),
            aligned_frames, stage, rank_cache=rank_cache, rank_cache_namespace=("long_short",))
        masks.append(mask)
    if (masks[0] & masks[1]).any().any():
        raise ValueError("long_short_equal long and short conditions overlap")
    signals = masks[0].astype(float) - masks[1].astype(float)
    resolved_frames = dict(frames)
    name = "__long_short_equal_signal__"
    resolved_frames[name] = signals
    return replace(spec, position_score_feature=name, position_score_lag=0,
                   long_filter=None, short_filter=None), resolved_frames


def _long_short_equal_target_frame(selection, spec, scores, scale, rebalance_dates, frames=None):
    if scores is None:
        raise ValueError("long_short_equal requires position scores")
    if np.isinf(scores.to_numpy(dtype=float)).any():
        raise ValueError("long_short_equal position scores must not contain infinity")
    index = selection.index[rebalance_dates]
    selected = selection.loc[index].fillna(False).astype(bool)
    values = scores.loc[index]
    longs = selected & values.gt(0)
    shorts = selected & values.lt(0)
    long_target = longs.astype(float).mul(float(spec.long_exposure)).div(
        longs.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    short_target = shorts.astype(float).mul(float(spec.short_exposure)).div(
        shorts.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    for side, target_side in (("long", long_target), ("short", short_target)):
        side_scale = _exposure_scale_series(selection, spec, frames, side=side)
        if side_scale is not None:
            target_side *= side_scale.loc[index].to_numpy()[:, None]
    target = long_target - short_target
    if scale is not None:
        target = target.mul(scale.loc[index], axis=0)
    result = pd.DataFrame(np.nan, index=selection.index, columns=selection.columns)
    result.loc[index] = target
    return result


def _listed_mask_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    feature_name = str(spec.listed_feature or "trade_price")
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing listed feature for listed_fixed weights_v2: {feature_name}")
    frame = feature_frames[feature_name].reindex(index=selection_mask.index, columns=selection_mask.columns)
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    return numeric.notna().cummax()


def _listed_fixed_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
    exposure_scale: pd.Series | None,
    rebalance_dates: pd.Series,
) -> pd.DataFrame:
    gross_exposure = float(spec.gross_exposure)
    selected = selection_mask.fillna(False).astype(bool)
    listed = _listed_mask_frame(selection_mask, spec, feature_frames)
    selected &= listed

    if spec.rebalance_frequency == "every_bar":
        listed_count = listed.sum(axis=1).replace(0, np.nan)
        target = selected.astype(float).mul(gross_exposure).div(listed_count, axis=0)
        if exposure_scale is not None:
            target = target.mul(exposure_scale, axis=0)
        return target.fillna(0.0)

    rebalance_index = selection_mask.index[rebalance_dates]
    target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
    if len(rebalance_index) == 0:
        return target

    rebalance_selected = selected.loc[rebalance_index]
    rebalance_listed = listed.loc[rebalance_index]
    listed_count = rebalance_listed.sum(axis=1).replace(0, np.nan)
    rebalance_target = rebalance_selected.astype(float).mul(gross_exposure).div(listed_count, axis=0).fillna(0.0)
    if exposure_scale is not None:
        rebalance_target = rebalance_target.mul(exposure_scale.loc[rebalance_index], axis=0)
    target.loc[rebalance_index, :] = rebalance_target
    return target


def _rank_fixed_weights_for_row(
    selected_columns: pd.Index,
    score_row: pd.Series,
    spec: WeightSpec,
) -> pd.Series:
    if len(selected_columns) == 0:
        return pd.Series(dtype=float)

    rank_weights = np.asarray(spec.rank_weights, dtype=float)
    if rank_weights.size == 0:
        raise ValueError("rank_weights must be provided for rank_fixed weighting")
    if not np.isfinite(rank_weights).all() or bool((rank_weights < 0.0).any()):
        raise ValueError("rank_weights must be finite and non-negative for rank_fixed weighting")
    if float(rank_weights.sum()) <= 0.0:
        raise ValueError("rank_weights must sum to a positive value for rank_fixed weighting")
    if len(selected_columns) > rank_weights.size:
        raise ValueError("rank_weights must cover every selected position for rank_fixed weighting")

    selected_scores = score_row.reindex(selected_columns)
    if bool(selected_scores.isna().any()):
        raise ValueError("rank_weight_feature contains NaN for a selected position")

    ordered_columns = selected_scores.sort_values(
        ascending=bool(spec.rank_weight_ascending),
        kind="mergesort",
    ).index
    active_weights = pd.Series(rank_weights[: len(ordered_columns)], index=ordered_columns, dtype=float)
    active_weights = active_weights / float(active_weights.sum())
    return active_weights.mul(float(spec.gross_exposure))


def _rank_fixed_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
    position_scores: pd.DataFrame | None,
    exposure_scale: pd.Series | None,
    rebalance_dates: pd.Series,
) -> pd.DataFrame:
    feature_name = spec.rank_weight_feature
    if not feature_name:
        raise ValueError("rank_weight_feature must be provided for rank_fixed weighting in weights_v2")
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing rank weight feature for weights_v2: {feature_name}")

    selected = selection_mask.fillna(False).astype(bool)
    score_frame = feature_frames[feature_name].reindex(index=selection_mask.index, columns=selection_mask.columns)
    lag = int(spec.rank_weight_lag)
    if lag > 0:
        score_frame = score_frame.shift(lag)
    elif lag < 0:
        raise ValueError("rank_weight_lag must be non-negative for weights_v2")

    if spec.rebalance_frequency == "every_bar":
        target = pd.DataFrame(0.0, index=selection_mask.index, columns=selection_mask.columns)
        for timestamp in selection_mask.index:
            selected_columns = selected.columns[selected.loc[timestamp]]
            if len(selected_columns) == 0:
                continue
            weights = _rank_fixed_weights_for_row(selected_columns, score_frame.loc[timestamp], spec)
            if position_scores is not None:
                weights = weights.mul(position_scores.loc[timestamp, weights.index])
            if exposure_scale is not None:
                weights = weights.mul(float(exposure_scale.loc[timestamp]))
            target.loc[timestamp, weights.index] = weights.astype(float)
        return target

    rebalance_index = selection_mask.index[rebalance_dates]
    target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
    if len(rebalance_index) == 0:
        return target

    for timestamp in rebalance_index:
        selected_columns = selected.columns[selected.loc[timestamp]]
        row = pd.Series(0.0, index=selection_mask.columns, dtype=float)
        if len(selected_columns) > 0:
            weights = _rank_fixed_weights_for_row(selected_columns, score_frame.loc[timestamp], spec)
            if position_scores is not None:
                weights = weights.mul(position_scores.loc[timestamp, weights.index])
            if exposure_scale is not None:
                weights = weights.mul(float(exposure_scale.loc[timestamp]))
            row.loc[weights.index] = weights.astype(float)
        target.loc[timestamp, :] = row
    return target


def _equal_rebalance_weights(columns: pd.Index, gross_exposure: float) -> pd.Series:
    if len(columns) == 0:
        return pd.Series(dtype=float)
    return pd.Series(gross_exposure / float(len(columns)), index=columns, dtype=float)


def _pairwise_observation_counts(returns: pd.DataFrame) -> pd.DataFrame:
    valid = returns.notna().astype(int)
    return valid.T.dot(valid)


def _quasi_diag_order(linkage_matrix: np.ndarray, item_count: int) -> list[int]:
    tree = linkage_matrix.astype(int, copy=False)

    def _order(node_id: int) -> list[int]:
        if node_id < item_count:
            return [node_id]
        left, right = tree[node_id - item_count, :2]
        return _order(int(left)) + _order(int(right))

    if item_count <= 1:
        return list(range(item_count))
    return _order(2 * item_count - 2)


def _inverse_variance_cluster_variance(covariance: np.ndarray, item_positions: list[int]) -> float:
    cluster_cov = covariance[np.ix_(item_positions, item_positions)]
    diagonal = np.diag(cluster_cov)
    if not np.isfinite(diagonal).all() or bool((diagonal <= 0.0).any()):
        raise ValueError("HRP covariance diagonal must be positive and finite")
    inv_diag = 1.0 / diagonal
    weights = inv_diag / inv_diag.sum()
    variance = float(weights @ cluster_cov @ weights)
    if not np.isfinite(variance) or variance < 0.0:
        raise ValueError("HRP cluster variance must be finite and non-negative")
    return variance


def _hrp_weights_from_returns(
    returns: pd.DataFrame,
    *,
    linkage_method: str,
) -> pd.Series:
    asset_count = returns.shape[1]
    if asset_count == 0:
        return pd.Series(dtype=float)
    if asset_count == 1:
        return pd.Series(1.0, index=returns.columns, dtype=float)

    corr = returns.corr()
    cov = returns.cov()
    if corr.shape != (asset_count, asset_count) or cov.shape != (asset_count, asset_count):
        raise ValueError("HRP correlation/covariance matrix shape mismatch")
    if not np.isfinite(corr.to_numpy(dtype=float)).all():
        raise ValueError("HRP correlation matrix contains non-finite values")
    if not np.isfinite(cov.to_numpy(dtype=float)).all():
        raise ValueError("HRP covariance matrix contains non-finite values")

    corr_values = corr.to_numpy(dtype=float)
    corr_values = np.clip(corr_values, -1.0, 1.0)
    np.fill_diagonal(corr_values, 1.0)
    distance = np.sqrt(np.maximum((1.0 - corr_values) / 2.0, 0.0))
    np.fill_diagonal(distance, 0.0)

    condensed_distance = squareform(distance, checks=False)
    link = linkage(condensed_distance, method=str(linkage_method or "single"))
    order = _quasi_diag_order(link, asset_count)
    cov_values = cov.to_numpy(dtype=float)

    ordered_clusters: list[list[int]] = [order]
    weights = pd.Series(1.0, index=order, dtype=float)
    while ordered_clusters:
        next_clusters: list[list[int]] = []
        for cluster in ordered_clusters:
            if len(cluster) <= 1:
                continue
            split = len(cluster) // 2
            left = cluster[:split]
            right = cluster[split:]
            left_var = _inverse_variance_cluster_variance(cov_values, left)
            right_var = _inverse_variance_cluster_variance(cov_values, right)
            denom = left_var + right_var
            if denom <= 0.0 or not np.isfinite(denom):
                raise ValueError("HRP cluster variance denominator must be positive")
            left_weight = 1.0 - left_var / denom
            weights.loc[left] *= left_weight
            weights.loc[right] *= 1.0 - left_weight
            next_clusters.extend([left, right])
        ordered_clusters = next_clusters

    weights.index = returns.columns[weights.index]
    weights = weights.reindex(returns.columns).astype(float)
    total = float(weights.sum())
    if total <= 0.0 or not np.isfinite(total) or bool(weights.lt(-1e-12).any()):
        raise ValueError("HRP weights must be non-negative and sum to a positive value")
    return weights.clip(lower=0.0) / total


def _hrp_rebalance_weights(
    prices: pd.DataFrame,
    timestamp,
    selected_columns: pd.Index,
    spec: WeightSpec,
) -> pd.Series:
    gross_exposure = float(spec.gross_exposure)
    equal_weights = _equal_rebalance_weights(selected_columns, gross_exposure)
    if len(selected_columns) <= 1:
        return equal_weights

    window = int(spec.hrp_window)
    if window <= 1:
        raise ValueError("hrp_window must be greater than 1")

    min_valid_ratio = float(spec.hrp_min_valid_ratio)
    min_pair_ratio = float(spec.hrp_min_pair_obs_ratio)
    if not (0.0 < min_valid_ratio <= 1.0):
        raise ValueError("hrp_min_valid_ratio must be in (0, 1]")
    if not (0.0 < min_pair_ratio <= 1.0):
        raise ValueError("hrp_min_pair_obs_ratio must be in (0, 1]")

    position = prices.index.get_loc(timestamp)
    if isinstance(position, slice) or isinstance(position, np.ndarray):
        raise ValueError("HRP price index must be unique")
    if int(position) < window + 1:
        return equal_weights

    price_window = prices.iloc[int(position) - window - 1 : int(position), :].loc[:, selected_columns]
    returns = price_window.astype(float).pct_change(fill_method=None).iloc[1:]
    if returns.shape[0] < window:
        return equal_weights

    min_valid_count = int(np.ceil(window * min_valid_ratio))
    valid_counts = returns.notna().sum(axis=0)
    if bool(valid_counts.lt(min_valid_count).any()):
        return equal_weights

    pair_counts = _pairwise_observation_counts(returns)
    min_pair_count = int(np.ceil(window * min_pair_ratio))
    pair_values = pair_counts.to_numpy(dtype=int)
    off_diagonal = ~np.eye(len(selected_columns), dtype=bool)
    if bool((pair_values[off_diagonal] < min_pair_count).any()):
        return equal_weights

    try:
        hrp_weights = _hrp_weights_from_returns(
            returns,
            linkage_method=spec.hrp_linkage_method,
        )
    except (ValueError, FloatingPointError, np.linalg.LinAlgError):
        return equal_weights

    return hrp_weights.mul(gross_exposure)


def _hrp_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
    exposure_scale: pd.Series | None,
    rebalance_dates: pd.Series,
) -> pd.DataFrame:
    feature_name = str(spec.hrp_price_feature or "trade_price")
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing HRP price feature for weights_v2: {feature_name}")

    selected = selection_mask.fillna(False).astype(bool)
    prices = feature_frames[feature_name].reindex(index=selection_mask.index, columns=selection_mask.columns)
    if prices.index.has_duplicates:
        raise ValueError("HRP price feature index must be unique")

    rebalance_index = selection_mask.index[rebalance_dates]
    target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
    if len(rebalance_index) == 0:
        return target

    for timestamp in rebalance_index:
        selected_columns = selected.columns[selected.loc[timestamp]]
        row = pd.Series(0.0, index=selection_mask.columns, dtype=float)
        if len(selected_columns) > 0:
            weights = _hrp_rebalance_weights(prices, timestamp, selected_columns, spec)
            if exposure_scale is not None:
                weights = weights.mul(float(exposure_scale.loc[timestamp]))
            row.loc[weights.index] = weights.astype(float)
        target.loc[timestamp, :] = row
    return target


def _cluster_equal_weights_from_returns(
    returns: pd.DataFrame,
    *,
    corr_threshold: float,
) -> pd.Series:
    asset_count = returns.shape[1]
    if asset_count == 0:
        return pd.Series(dtype=float)
    if asset_count == 1:
        return pd.Series(1.0, index=returns.columns, dtype=float)

    corr = returns.corr()
    if corr.shape != (asset_count, asset_count):
        raise ValueError("Cluster correlation matrix shape mismatch")
    if not np.isfinite(corr.to_numpy(dtype=float)).all():
        raise ValueError("Cluster correlation matrix contains non-finite values")

    corr_values = np.clip(corr.to_numpy(dtype=float), -1.0, 1.0)
    np.fill_diagonal(corr_values, 1.0)
    adjacency = corr_values >= float(corr_threshold)

    visited = np.zeros(asset_count, dtype=bool)
    clusters: list[list[int]] = []
    for start in range(asset_count):
        if visited[start]:
            continue
        stack = [start]
        visited[start] = True
        cluster: list[int] = []
        while stack:
            current = stack.pop()
            cluster.append(current)
            neighbors = np.flatnonzero(adjacency[current])
            for neighbor in neighbors:
                neighbor_id = int(neighbor)
                if not visited[neighbor_id]:
                    visited[neighbor_id] = True
                    stack.append(neighbor_id)
        clusters.append(cluster)

    if not clusters:
        raise ValueError("Cluster equal weighting produced no clusters")

    weights = pd.Series(0.0, index=returns.columns, dtype=float)
    cluster_weight = 1.0 / float(len(clusters))
    for cluster in clusters:
        per_asset_weight = cluster_weight / float(len(cluster))
        weights.iloc[cluster] = per_asset_weight

    total = float(weights.sum())
    if total <= 0.0 or not np.isfinite(total):
        raise ValueError("Cluster equal weights must sum to a positive finite value")
    return weights / total


def _cluster_equal_rebalance_weights(
    prices: pd.DataFrame,
    timestamp,
    selected_columns: pd.Index,
    spec: WeightSpec,
) -> pd.Series:
    gross_exposure = float(spec.gross_exposure)
    equal_weights = _equal_rebalance_weights(selected_columns, gross_exposure)
    if len(selected_columns) <= 1:
        return equal_weights

    window = int(spec.cluster_corr_window)
    if window <= 1:
        raise ValueError("cluster_corr_window must be greater than 1")

    corr_threshold = float(spec.cluster_corr_threshold)
    if not (-1.0 <= corr_threshold <= 1.0):
        raise ValueError("cluster_corr_threshold must be in [-1, 1]")

    min_valid_ratio = float(spec.cluster_min_valid_ratio)
    min_pair_ratio = float(spec.cluster_min_pair_obs_ratio)
    if not (0.0 < min_valid_ratio <= 1.0):
        raise ValueError("cluster_min_valid_ratio must be in (0, 1]")
    if not (0.0 < min_pair_ratio <= 1.0):
        raise ValueError("cluster_min_pair_obs_ratio must be in (0, 1]")

    position = prices.index.get_loc(timestamp)
    if isinstance(position, slice) or isinstance(position, np.ndarray):
        raise ValueError("Cluster price index must be unique")
    if int(position) < window + 1:
        return equal_weights

    price_window = prices.iloc[int(position) - window - 1 : int(position), :].loc[:, selected_columns]
    returns = price_window.astype(float).pct_change(fill_method=None).iloc[1:]
    if returns.shape[0] < window:
        return equal_weights

    min_valid_count = int(np.ceil(window * min_valid_ratio))
    valid_counts = returns.notna().sum(axis=0)
    if bool(valid_counts.lt(min_valid_count).any()):
        return equal_weights

    pair_counts = _pairwise_observation_counts(returns)
    min_pair_count = int(np.ceil(window * min_pair_ratio))
    pair_values = pair_counts.to_numpy(dtype=int)
    off_diagonal = ~np.eye(len(selected_columns), dtype=bool)
    if bool((pair_values[off_diagonal] < min_pair_count).any()):
        return equal_weights

    try:
        cluster_weights = _cluster_equal_weights_from_returns(
            returns,
            corr_threshold=corr_threshold,
        )
    except (ValueError, FloatingPointError, np.linalg.LinAlgError):
        return equal_weights

    return cluster_weights.mul(gross_exposure)


def _cluster_equal_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
    exposure_scale: pd.Series | None,
    rebalance_dates: pd.Series,
) -> pd.DataFrame:
    feature_name = str(spec.cluster_price_feature or "trade_price")
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing cluster price feature for weights_v2: {feature_name}")

    selected = selection_mask.fillna(False).astype(bool)
    prices = feature_frames[feature_name].reindex(index=selection_mask.index, columns=selection_mask.columns)
    if prices.index.has_duplicates:
        raise ValueError("Cluster price feature index must be unique")

    rebalance_index = selection_mask.index[rebalance_dates]
    target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
    if len(rebalance_index) == 0:
        return target

    for timestamp in rebalance_index:
        selected_columns = selected.columns[selected.loc[timestamp]]
        row = pd.Series(0.0, index=selection_mask.columns, dtype=float)
        if len(selected_columns) > 0:
            weights = _cluster_equal_rebalance_weights(prices, timestamp, selected_columns, spec)
            if exposure_scale is not None:
                weights = weights.mul(float(exposure_scale.loc[timestamp]))
            row.loc[weights.index] = weights.astype(float)
        target.loc[timestamp, :] = row
    return target


def _validate_portfolio_vol_target_spec(spec: PortfolioVolTargetSpec) -> None:
    if not str(spec.price_feature).strip():
        raise ValueError("portfolio_vol_target price_feature must not be empty")
    if int(spec.window) <= 1:
        raise ValueError("portfolio_vol_target window must be greater than 1")
    if not np.isfinite(spec.target_annual_vol) or float(spec.target_annual_vol) <= 0.0:
        raise ValueError("portfolio_vol_target target_annual_vol must be finite and positive")
    if int(spec.periods_per_year) <= 0:
        raise ValueError("portfolio_vol_target periods_per_year must be positive")

    min_multiplier = float(spec.min_multiplier)
    max_multiplier = float(spec.max_multiplier)
    if not (0.0 <= min_multiplier <= max_multiplier <= 1.0):
        raise ValueError("portfolio_vol_target requires 0 <= min_multiplier <= max_multiplier <= 1")

    step_size = float(spec.step_size)
    if not np.isfinite(step_size) or step_size < 0.0:
        raise ValueError("portfolio_vol_target step_size must be finite and non-negative")
    rounding = str(spec.rounding).strip().lower()
    if rounding not in {"floor", "nearest", "ceil"}:
        raise ValueError("portfolio_vol_target rounding must be 'floor', 'nearest', or 'ceil'")

    min_valid_ratio = float(spec.min_valid_ratio)
    min_pair_ratio = float(spec.min_pair_obs_ratio)
    if not (0.0 < min_valid_ratio <= 1.0):
        raise ValueError("portfolio_vol_target min_valid_ratio must be in (0, 1]")
    if not (0.0 < min_pair_ratio <= 1.0):
        raise ValueError("portfolio_vol_target min_pair_obs_ratio must be in (0, 1]")


def _quantize_portfolio_vol_multiplier(value: float, spec: PortfolioVolTargetSpec) -> float:
    min_multiplier = float(spec.min_multiplier)
    max_multiplier = float(spec.max_multiplier)
    clipped = float(np.clip(value, min_multiplier, max_multiplier))
    step_size = float(spec.step_size)
    if step_size == 0.0:
        return clipped
    if np.isclose(clipped, max_multiplier, atol=1e-12, rtol=0.0):
        return max_multiplier

    scaled = (clipped - min_multiplier) / step_size
    rounding = str(spec.rounding).strip().lower()
    tolerance = 1e-12
    if rounding == "floor":
        bucket = np.floor(scaled + tolerance)
    elif rounding == "nearest":
        bucket = np.floor(scaled + 0.5 + tolerance)
    else:
        bucket = np.ceil(scaled - tolerance)
    quantized = min_multiplier + float(bucket) * step_size
    return float(np.clip(quantized, min_multiplier, max_multiplier))


def _pairwise_covariance_values(
    values: np.ndarray,
    *,
    min_valid_count: int,
    min_pair_count: int,
) -> np.ndarray | None:
    asset_count = int(values.shape[1])
    covariance = np.empty((asset_count, asset_count), dtype=float)
    for left in range(asset_count):
        left_values = values[:, left]
        left_valid = np.isfinite(left_values)
        if int(left_valid.sum()) < max(2, min_valid_count):
            return None
        covariance[left, left] = float(np.var(left_values[left_valid], ddof=1))

        for right in range(left + 1, asset_count):
            right_values = values[:, right]
            pair_valid = left_valid & np.isfinite(right_values)
            if int(pair_valid.sum()) < max(2, min_pair_count):
                return None
            left_pair = left_values[pair_valid]
            right_pair = right_values[pair_valid]
            pair_covariance = float(
                np.dot(left_pair - left_pair.mean(), right_pair - right_pair.mean())
                / float(len(left_pair) - 1)
            )
            covariance[left, right] = pair_covariance
            covariance[right, left] = pair_covariance
    return covariance


def _portfolio_vol_multiplier_series(
    base_target: pd.DataFrame,
    prices: pd.DataFrame,
    spec: PortfolioVolTargetSpec,
) -> pd.Series:
    _validate_portfolio_vol_target_spec(spec)
    window = int(spec.window)
    periods_per_year = int(spec.periods_per_year)
    min_valid_count = int(np.ceil(window * float(spec.min_valid_ratio)))
    min_pair_count = int(np.ceil(window * float(spec.min_pair_obs_ratio)))
    fallback = _quantize_portfolio_vol_multiplier(float(spec.max_multiplier), spec)

    numeric_prices = prices.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    returns = np.full_like(numeric_prices, np.nan, dtype=float)
    previous = numeric_prices[:-1]
    current = numeric_prices[1:]
    valid_returns = np.isfinite(previous) & np.isfinite(current) & (previous != 0.0)
    return_values = np.full_like(previous, np.nan, dtype=float)
    np.divide(current, previous, out=return_values, where=valid_returns)
    return_values[valid_returns] -= 1.0
    returns[1:] = return_values

    base_values = base_target.to_numpy(dtype=float)
    multipliers = np.full(len(base_target.index), fallback, dtype=float)

    for position in range(len(base_target.index)):
        if position < window + 1:
            continue

        weight_row = base_values[position]
        active_positions = np.flatnonzero(np.abs(weight_row) > 1e-15)
        if active_positions.size == 0:
            continue

        return_window = returns[position - window : position, active_positions]
        covariance_values = _pairwise_covariance_values(
            return_window,
            min_valid_count=min_valid_count,
            min_pair_count=min_pair_count,
        )
        if covariance_values is None:
            continue

        covariance_values = (covariance_values + covariance_values.T) / 2.0
        try:
            eigenvalues, eigenvectors = np.linalg.eigh(covariance_values)
        except np.linalg.LinAlgError:
            continue
        if not np.isfinite(eigenvalues).all() or not np.isfinite(eigenvectors).all():
            continue
        covariance_values = (eigenvectors * np.clip(eigenvalues, 0.0, None)) @ eigenvectors.T

        weights = weight_row[active_positions]
        variance = float(weights @ covariance_values @ weights)
        if not np.isfinite(variance) or variance < 0.0:
            continue
        if variance == 0.0:
            raw_multiplier = float(spec.max_multiplier)
        else:
            annual_vol = np.sqrt(variance * periods_per_year)
            raw_multiplier = float(spec.target_annual_vol) / float(annual_vol)
        multipliers[position] = _quantize_portfolio_vol_multiplier(raw_multiplier, spec)

    return pd.Series(multipliers, index=base_target.index, dtype=float)


def _build_portfolio_vol_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    target_spec = spec.portfolio_vol_target
    if target_spec is None:
        raise ValueError("portfolio_vol_target configuration is missing")
    _validate_portfolio_vol_target_spec(target_spec)

    feature_name = str(target_spec.price_feature)
    if feature_frames is None or feature_name not in feature_frames:
        raise ValueError(f"Missing portfolio volatility price feature for weights_v2: {feature_name}")

    prices = feature_frames[feature_name].reindex(index=selection_mask.index)
    if prices.index.has_duplicates:
        raise ValueError("portfolio_vol_target price feature index must be unique")

    base_spec = replace(spec, portfolio_vol_target=None, output_mode="")
    base_target = build_weight_frame_v2(selection_mask, base_spec, feature_frames).ffill().fillna(0.0)
    if spec.hedge is not None and spec.hedge.market not in prices.columns:
        raise ValueError("portfolio_vol_target price feature must include the hedge market")
    prices = prices.reindex(columns=base_target.columns)
    if not np.isfinite(base_target.to_numpy(dtype=float)).all():
        raise ValueError("portfolio_vol_target base weights must be finite")
    multipliers = _portfolio_vol_multiplier_series(base_target, prices, target_spec)
    return base_target.mul(multipliers, axis=0).fillna(0.0)


def _build_hedged_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    hedge = spec.hedge
    market = str(hedge.market).strip().upper()
    if not market or market != hedge.market:
        raise ValueError("hedge market must be a non-empty uppercase market identifier")
    if hedge.mode not in {"beta", "notional"}:
        raise ValueError("hedge mode must be 'beta' or 'notional'")
    if not np.isfinite(hedge.ratio) or hedge.ratio < 0.0:
        raise ValueError("hedge ratio must be finite and non-negative")
    if (isinstance(hedge.beta_lag, bool) or not isinstance(hedge.beta_lag, (int, float, np.integer, np.floating))
            or not np.isfinite(hedge.beta_lag) or int(hedge.beta_lag) != hedge.beta_lag or hedge.beta_lag < 0):
        raise ValueError("hedge beta_lag must be a non-negative integer")
    if spec.defensive_sleeve is not None:
        raise ValueError("hedge and defensive_sleeve cannot be combined")
    if market in selection_mask and bool(selection_mask[market].fillna(False).any()):
        raise ValueError(f"Hedge market must be excluded from the strategy universe: {market}")

    # Let the existing allocation logic set actual primary target weights first.
    # Apply a common exposure multiplier only after adding the hedge leg.
    base_spec = replace(spec, hedge=None, gross_exposure_feature=None, output_mode="")
    # The public wrapper already filtered selection by cache eligibility. Read
    # the allocation before its final forced zeros: those zeros also appear on
    # non-rebalance rows and must not be mistaken for primary order requests.
    # The wrapper still applies the same eligibility mask to the combined result.
    target = _build_weight_frame_v2(selection_mask, base_spec, feature_frames)
    if not target.index.is_unique or not target.columns.is_unique:
        raise ValueError("hedge requires unique timestamps and columns")
    requested = target.notna().any(axis=1)
    primary = target.drop(columns=market, errors="ignore")
    weights = primary.loc[requested].to_numpy(dtype=float)
    if not np.isfinite(weights).all():
        raise ValueError("hedge requires complete finite target rows at rebalance timestamps")
    if hedge.mode == "beta":
        if not hedge.beta_feature or feature_frames is None or hedge.beta_feature not in feature_frames:
            raise ValueError(f"Missing hedge beta feature: {hedge.beta_feature}")
        beta_frame = feature_frames[hedge.beta_feature]
        if not beta_frame.index.is_unique or not beta_frame.columns.is_unique:
            raise ValueError("hedge beta feature must have unique timestamps and columns")
        # Shift on the complete source timeline before selecting rebalance rows.
        betas = beta_frame.reindex(index=target.index, columns=primary.columns).shift(int(hedge.beta_lag))
        beta_values = betas.loc[requested].to_numpy(dtype=float)
        active = (weights != 0.0) & (hedge.ratio != 0.0)
        invalid = active & ~np.isfinite(beta_values)
        if invalid.any():
            row, column = np.argwhere(invalid)[0]
            raise ValueError(
                f"Missing/non-finite hedge beta for active position: {primary.columns[column]} "
                f"at {target.index[requested][row]}; filter beta warmup/missing observations before entry"
            )
        exposure = np.sum(weights * np.where(active, beta_values, 0.0), axis=1)
    else:
        exposure = weights.sum(axis=1)
    hedge_weights = -exposure * hedge.ratio
    if not np.isfinite(hedge_weights).all():
        raise ValueError("Non-finite calculated hedge weights")
    result = target.copy()
    result[market] = np.nan
    result.loc[requested, market] = hedge_weights
    scale = _exposure_scale_series(selection_mask, spec, feature_frames)
    if scale is not None:
        result = result.mul(scale, axis=0)
    return result


def build_weight_frame_v2(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None = None,
    *,
    base_eligible_mask: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if spec.change_only_scope not in ("portfolio", "asset"):
        raise ValueError("change_only_scope must be 'portfolio' or 'asset'")
    for side in ("long", "short"):
        feature = getattr(spec, f"{side}_exposure_feature")
        lag = getattr(spec, f"{side}_exposure_lag")
        if type(lag) is not int or lag < 0:
            raise ValueError(f"{side}_exposure_lag must be a non-negative integer")
        if feature is not None and (not isinstance(feature, str) or not feature.strip()):
            raise ValueError(f"{side}_exposure_feature must be a non-empty feature name")
        if lag and feature is None:
            raise ValueError(f"{side}_exposure_lag requires {side}_exposure_feature")
        if feature is not None and spec.weighting != "long_short_equal":
            raise ValueError("Side exposure features require weighting='long_short_equal'")
    if spec.weighting == "long_short_equal":
        spec, feature_frames = _long_short_signals(selection_mask, spec, feature_frames, base_eligible_mask)
    usage = feature_frames.get(CACHE_ELIGIBILITY_FRAME) if feature_frames is not None else None
    if usage is None:
        return _build_weight_frame_v2(selection_mask, spec, feature_frames)
    eligible = usage.reindex(index=selection_mask.index, columns=selection_mask.columns).eq(True)
    target = _build_weight_frame_v2(selection_mask & eligible, spec, feature_frames)
    target_eligible = usage.reindex(index=target.index, columns=target.columns).eq(True)
    if spec.hedge is not None:
        market = spec.hedge.market
        if market in target and (target[market].fillna(0).ne(0) & ~target_eligible[market]).any():
            raise ValueError(f"Hedge market is outside cache data usage history: {market}")
    return target.where(target_eligible, 0.0)


def _build_weight_frame_v2(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    rebalance_frequency = str(spec.rebalance_frequency).strip().lower()
    if is_change_only_rebalance_frequency(rebalance_frequency):
        dense_spec = replace(spec, rebalance_frequency="every_bar", gross_exposure_update_frequency="", output_mode="")
        dense_target = build_weight_frame_v2(selection_mask, dense_spec, feature_frames)
        return to_change_only_target_frame(dense_target, scope=spec.change_only_scope)

    output_mode = _normalize_frequency(spec.output_mode)
    if output_mode not in {"", "dense", "change_only"}:
        raise ValueError("output_mode must be empty, 'dense', or 'change_only'")

    if spec.portfolio_vol_target is not None:
        dense_target = _build_portfolio_vol_target_frame(selection_mask, spec, feature_frames)
        if output_mode == "change_only":
            return to_change_only_target_frame(dense_target, scope=spec.change_only_scope)
        return dense_target

    if spec.gross_exposure_update_frequency:
        dense_target = _build_exposure_overlay_target_frame(selection_mask, spec, feature_frames)
        if output_mode == "change_only":
            return to_change_only_target_frame(dense_target, scope=spec.change_only_scope)
        return dense_target

    if output_mode == "change_only":
        base_spec = replace(spec, output_mode="")
        dense_target = build_weight_frame_v2(selection_mask, base_spec, feature_frames).ffill().fillna(0.0)
        return to_change_only_target_frame(dense_target, scope=spec.change_only_scope)

    if spec.hedge is not None:
        return _build_hedged_target_frame(selection_mask, spec, feature_frames)

    rebalance_dates = _rebalance_mask(selection_mask.index, rebalance_frequency)
    selected = selection_mask.fillna(False).astype(bool)
    exposure_scale = _exposure_scale_series(selection_mask, spec, feature_frames)
    position_scores = _position_score_frame(selection_mask, spec, feature_frames)

    if spec.weighting == "long_short_equal":
        return _apply_defensive_sleeve_target_frame(
            _long_short_equal_target_frame(selection_mask, spec, position_scores, exposure_scale, rebalance_dates,
                                           feature_frames),
            spec, feature_frames)

    if spec.weighting == "equal":
        gross_exposure = float(spec.gross_exposure)
        if spec.rebalance_frequency == "every_bar":
            counts = selected.sum(axis=1).replace(0, np.nan)
            target = selected.astype(float).mul(gross_exposure).div(counts, axis=0)
            if exposure_scale is not None:
                target = target.mul(exposure_scale, axis=0)
            return _apply_defensive_sleeve_target_frame(target.fillna(0.0), spec, feature_frames)

        rebalance_index = selection_mask.index[rebalance_dates]
        target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
        if len(rebalance_index) == 0:
            return target

        rebalance_selected = selected.loc[rebalance_index]
        counts = rebalance_selected.sum(axis=1).replace(0, np.nan)
        rebalance_target = rebalance_selected.astype(float).mul(gross_exposure).div(counts, axis=0).fillna(0.0)
        if exposure_scale is not None:
            rebalance_target = rebalance_target.mul(exposure_scale.loc[rebalance_index], axis=0)
        target.loc[rebalance_index, :] = rebalance_target
        return _apply_defensive_sleeve_target_frame(target, spec, feature_frames)

    if spec.weighting == "fixed":
        if spec.fixed_weight is None:
            raise ValueError("fixed_weight must be provided for fixed weighting")
        fixed_weight = float(spec.fixed_weight)
        if spec.rebalance_frequency == "every_bar":
            target = selected.astype(float).mul(fixed_weight)
            if position_scores is not None:
                target = target.mul(position_scores)
            if exposure_scale is not None:
                target = target.mul(exposure_scale, axis=0)
            return _apply_defensive_sleeve_target_frame(target, spec, feature_frames)

        rebalance_index = selection_mask.index[rebalance_dates]
        target = pd.DataFrame(float("nan"), index=selection_mask.index, columns=selection_mask.columns)
        if len(rebalance_index) == 0:
            return target

        rebalance_selected = selected.loc[rebalance_index]
        rebalance_target = rebalance_selected.astype(float).mul(fixed_weight)
        if position_scores is not None:
            rebalance_target = rebalance_target.mul(position_scores.loc[rebalance_index])
        if exposure_scale is not None:
            rebalance_target = rebalance_target.mul(exposure_scale.loc[rebalance_index], axis=0)
        target.loc[rebalance_index, :] = rebalance_target
        return _apply_defensive_sleeve_target_frame(target, spec, feature_frames)

    if spec.weighting == "rank_fixed":
        return _apply_defensive_sleeve_target_frame(
            _rank_fixed_target_frame(
                selection_mask=selection_mask,
                spec=spec,
                feature_frames=feature_frames,
                position_scores=position_scores,
                exposure_scale=exposure_scale,
                rebalance_dates=rebalance_dates,
            ),
            spec,
            feature_frames,
        )

    if spec.weighting == "feature_value":
        return _apply_defensive_sleeve_target_frame(
            _feature_value_target_frame(
                selection_mask=selection_mask,
                spec=spec,
                position_scores=position_scores,
                exposure_scale=exposure_scale,
                rebalance_dates=rebalance_dates,
            ),
            spec,
            feature_frames,
        )

    if spec.weighting == "listed_fixed":
        return _apply_defensive_sleeve_target_frame(
            _listed_fixed_target_frame(
                selection_mask=selection_mask,
                spec=spec,
                feature_frames=feature_frames,
                exposure_scale=exposure_scale,
                rebalance_dates=rebalance_dates,
            ),
            spec,
            feature_frames,
        )

    if spec.weighting == "hrp":
        return _apply_defensive_sleeve_target_frame(
            _hrp_target_frame(
                selection_mask=selection_mask,
                spec=spec,
                feature_frames=feature_frames,
                exposure_scale=exposure_scale,
                rebalance_dates=rebalance_dates,
            ),
            spec,
            feature_frames,
        )

    if spec.weighting == "cluster_equal":
        return _apply_defensive_sleeve_target_frame(
            _cluster_equal_target_frame(
                selection_mask=selection_mask,
                spec=spec,
                feature_frames=feature_frames,
                exposure_scale=exposure_scale,
                rebalance_dates=rebalance_dates,
            ),
            spec,
            feature_frames,
        )

    raise ValueError(
        "frame_v2 currently supports equal, long_short_equal, fixed, rank_fixed, listed_fixed, feature_value, hrp, "
        "and cluster_equal weighting only"
    )


def _build_exposure_overlay_target_frame(
    selection_mask: pd.DataFrame,
    spec: WeightSpec,
    feature_frames: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    update_frequency = _normalize_frequency(spec.gross_exposure_update_frequency)
    if update_frequency != "every_bar":
        raise ValueError("gross_exposure_update_frequency currently supports only 'every_bar'")
    if not spec.gross_exposure_feature:
        raise ValueError("gross_exposure_update_frequency requires gross_exposure_feature")

    base_spec = replace(
        spec,
        gross_exposure_feature=None,
        gross_exposure_update_frequency="",
        output_mode="",
    )
    base_target = build_weight_frame_v2(selection_mask, base_spec, feature_frames).ffill().fillna(0.0)
    exposure_scale = _exposure_scale_series(selection_mask, spec, feature_frames)
    if exposure_scale is None:
        raise ValueError("gross_exposure_feature did not produce an exposure scale")
    return base_target.mul(exposure_scale, axis=0).fillna(0.0)
