from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lib.specs import FilterStageSpec, RankFilterSpec, UniverseSpec, ValueFilterSpec


@dataclass(frozen=True)
class UniverseV2Result:
    selection_mask: pd.DataFrame
    base_eligible_mask: pd.DataFrame


RankCache = dict[tuple[object, ...], pd.DataFrame]


def _age_days_from_frame(frame: pd.DataFrame) -> pd.DataFrame:
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


def _bucket_for_rank(rank: int, size: int, quantiles: int) -> int:
    if size <= 0:
        raise ValueError("Cross-sectional size must be positive")
    return 1 + (((rank - 1) * quantiles) // size)


def _compare_frame(operator: str, left: pd.DataFrame, right: float) -> pd.DataFrame:
    if operator == "gt":
        return left.gt(right)
    if operator == "ge":
        return left.ge(right)
    if operator == "lt":
        return left.lt(right)
    if operator == "le":
        return left.le(right)
    if operator == "eq":
        return left.eq(right)
    if operator == "ne":
        return left.ne(right)
    raise ValueError(f"Unsupported filter operator: {operator}")


def _effective_universe_spec(spec: UniverseSpec) -> UniverseSpec:
    if int(spec.signal_lag or 0) < 0:
        raise ValueError("signal_lag must be >= 0 for frame_v2 universe")
    effective_rank_filters = tuple(
        RankFilterSpec(
            feature_column=item.feature_column,
            mode=item.mode,
            lag=int(item.lag or 0) + int(spec.signal_lag or 0),
            top_n=item.top_n,
            quantiles=item.quantiles,
            bucket_values=item.bucket_values,
            ascending=item.ascending,
            scope=item.scope,
        )
        for item in spec.rank_filters
    )
    effective_filter_stages = tuple(
        FilterStageSpec(
            mode=item.mode,
            filters=tuple(
                RankFilterSpec(
                    feature_column=rank_filter.feature_column,
                    mode=rank_filter.mode,
                    lag=int(rank_filter.lag or 0) + int(spec.signal_lag or 0),
                    top_n=rank_filter.top_n,
                    quantiles=rank_filter.quantiles,
                    bucket_values=rank_filter.bucket_values,
                    ascending=rank_filter.ascending,
                    scope=rank_filter.scope,
                )
                for rank_filter in item.filters
            ),
        )
        for item in spec.filter_stages
    )
    if not effective_filter_stages and effective_rank_filters:
        effective_filter_stages = (
            FilterStageSpec(
                mode="sequential",
                filters=effective_rank_filters,
            ),
        )
    return UniverseSpec(
        feature_column=spec.feature_column,
        sort_column=spec.sort_column,
        lag=int(spec.lag or 0) + int(spec.signal_lag or 0),
        signal_lag=spec.signal_lag,
        start_min_cross_section_size=spec.start_min_cross_section_size,
        mode=spec.mode,
        top_n=spec.top_n,
        quantiles=spec.quantiles,
        bucket_values=spec.bucket_values,
        ascending=spec.ascending,
        scope=spec.scope,
        exclude_warnings=spec.exclude_warnings,
        min_age_days=spec.min_age_days,
        allowed_markets=spec.allowed_markets,
        excluded_markets=spec.excluded_markets,
        value_filters=tuple(
            ValueFilterSpec(
                feature_column=item.feature_column,
                operator=item.operator,
                value=item.value,
                lag=int(item.lag or 0) + int(spec.signal_lag or 0),
            )
            for item in spec.value_filters
        ),
        rank_filters=effective_rank_filters,
        filter_stages=effective_filter_stages,
        name=spec.name,
    )


def _shift_frame(frame: pd.DataFrame, lag: int) -> pd.DataFrame:
    if lag <= 0:
        return frame.copy()
    # Use calendar-row shift semantics.
    # If the immediately prior bar is missing, the lagged value should remain NaN
    # instead of skipping backward to the last valid observation.
    return frame.shift(lag).reindex(index=frame.index, columns=frame.columns).sort_index(axis=1)


def _allowed_market_mask(columns: pd.Index, spec: UniverseSpec) -> pd.Series:
    mask = pd.Series(True, index=columns)
    if spec.allowed_markets:
        mask &= columns.to_series().isin(set(spec.allowed_markets))
    if spec.excluded_markets:
        mask &= ~columns.to_series().isin(set(spec.excluded_markets))
    return mask


def _rank_filter_signature(rank_filter: RankFilterSpec, *, include_cutoff: bool) -> tuple[object, ...]:
    signature: list[object] = [
        rank_filter.feature_column,
        rank_filter.mode,
        int(rank_filter.lag or 0),
        bool(rank_filter.ascending),
        rank_filter.scope or "filtered",
    ]
    if include_cutoff:
        signature.extend(
            [
                rank_filter.top_n,
                rank_filter.quantiles,
                tuple(rank_filter.bucket_values),
            ]
        )
    return tuple(signature)


def _value_filter_signature(value_filter: ValueFilterSpec) -> tuple[object, ...]:
    return (
        value_filter.feature_column,
        value_filter.operator,
        float(value_filter.value),
        int(value_filter.lag or 0),
    )


def _rank_frame(
    scoped: pd.DataFrame,
    *,
    ascending: bool,
    rank_cache: RankCache | None,
    cache_key: tuple[object, ...] | None,
) -> pd.DataFrame:
    if rank_cache is not None and cache_key is not None:
        cached = rank_cache.get(cache_key)
        if cached is not None:
            return cached
    ranks = scoped.rank(axis=1, method="first", ascending=ascending)
    if rank_cache is not None and cache_key is not None:
        rank_cache[cache_key] = ranks
    return ranks


def _apply_rank_filter(
    candidates: pd.DataFrame,
    base: pd.DataFrame,
    feature_frame: pd.DataFrame,
    rank_filter: RankFilterSpec,
    *,
    rank_cache: RankCache | None = None,
    rank_cache_namespace: tuple[object, ...] | None = None,
    candidates_key: tuple[object, ...] | None = None,
    base_key: tuple[object, ...] | None = None,
) -> pd.DataFrame:
    values = _shift_frame(feature_frame, rank_filter.lag)
    scope = (rank_filter.scope or "filtered").lower()
    if scope in {"filtered", "candidates"}:
        scoped = values.where(candidates)
        scoped_key = candidates_key
    elif scope in {"global", "base"}:
        scoped = values.where(base)
        scoped_key = base_key
    else:
        raise ValueError(f"Unsupported rank filter scope: {rank_filter.scope}")
    cache_key = None
    if rank_cache_namespace is not None and scoped_key is not None:
        cache_key = (
            *rank_cache_namespace,
            "rank_filter",
            _rank_filter_signature(rank_filter, include_cutoff=False),
            scoped_key,
        )
    ranks = _rank_frame(
        scoped,
        ascending=rank_filter.ascending,
        rank_cache=rank_cache,
        cache_key=cache_key,
    )
    if rank_filter.mode == "top_n":
        selected = ranks.le(float(rank_filter.top_n)).fillna(False)
        return candidates & selected
    if rank_filter.mode == "quantile":
        counts = scoped.notna().sum(axis=1).astype(float)
        bucket = 1.0 + np.floor((ranks.sub(1.0)).mul(float(rank_filter.quantiles)).div(counts, axis=0))
        selected = bucket.isin([float(value) for value in rank_filter.bucket_values]).fillna(False)
        return candidates & selected
    raise ValueError(f"Unsupported rank filter mode: {rank_filter.mode}")


def _apply_final_selection(
    candidates: pd.DataFrame,
    base: pd.DataFrame,
    sort_frame: pd.DataFrame,
    spec: UniverseSpec,
    *,
    rank_cache: RankCache | None = None,
    rank_cache_namespace: tuple[object, ...] | None = None,
    candidates_key: tuple[object, ...] | None = None,
    base_key: tuple[object, ...] | None = None,
) -> pd.DataFrame:
    if spec.mode == "all":
        return candidates
    scope = (spec.scope or "filtered").lower()
    if scope in {"filtered", "candidates"}:
        scoped = sort_frame.where(candidates)
        scoped_key = candidates_key
    elif scope in {"global", "base"}:
        scoped = sort_frame.where(base)
        scoped_key = base_key
    else:
        raise ValueError(f"Unsupported universe scope: {spec.scope}")
    cache_key = None
    if rank_cache_namespace is not None and scoped_key is not None:
        cache_key = (
            *rank_cache_namespace,
            "final_selection",
            spec.sort_column or spec.feature_column,
            int(spec.lag or 0),
            scope,
            bool(spec.ascending),
            scoped_key,
        )
    ranks = _rank_frame(
        scoped,
        ascending=spec.ascending,
        rank_cache=rank_cache,
        cache_key=cache_key,
    )
    if spec.mode == "top_n":
        selected = ranks.le(float(spec.top_n)).fillna(False)
        return candidates & selected
    if spec.mode == "quantile":
        counts = scoped.notna().sum(axis=1).astype(float)
        bucket = 1.0 + np.floor((ranks.sub(1.0)).mul(float(spec.quantiles)).div(counts, axis=0))
        selected = bucket.isin([float(value) for value in spec.bucket_values]).fillna(False)
        return candidates & selected
    raise ValueError(f"Unsupported universe mode: {spec.mode}")


def _apply_filter_stage(
    candidates: pd.DataFrame,
    candidates_key: tuple[object, ...],
    base: pd.DataFrame,
    base_key: tuple[object, ...],
    feature_frames: dict[str, pd.DataFrame],
    stage: FilterStageSpec,
    *,
    rank_cache: RankCache | None = None,
    rank_cache_namespace: tuple[object, ...] | None = None,
) -> tuple[pd.DataFrame, tuple[object, ...]]:
    mode = (stage.mode or "sequential").lower()
    if mode == "sequential":
        result = candidates
        result_key = candidates_key
        for rank_filter in stage.filters:
            if rank_filter.feature_column not in feature_frames:
                raise ValueError(f"Missing rank filter feature for frame_v2 universe: {rank_filter.feature_column}")
            result = _apply_rank_filter(
                result,
                base,
                feature_frames[rank_filter.feature_column],
                rank_filter,
                rank_cache=rank_cache,
                rank_cache_namespace=rank_cache_namespace,
                candidates_key=result_key,
                base_key=base_key,
            )
            result_key = (
                "rank_filter",
                result_key,
                _rank_filter_signature(rank_filter, include_cutoff=True),
            )
        return result, result_key
    if mode == "and":
        frozen = candidates.copy()
        frozen_key = candidates_key
        result = frozen.copy()
        filter_keys: list[tuple[object, ...]] = []
        for rank_filter in stage.filters:
            if rank_filter.feature_column not in feature_frames:
                raise ValueError(f"Missing rank filter feature for frame_v2 universe: {rank_filter.feature_column}")
            selected = _apply_rank_filter(
                frozen,
                base,
                feature_frames[rank_filter.feature_column],
                rank_filter,
                rank_cache=rank_cache,
                rank_cache_namespace=rank_cache_namespace,
                candidates_key=frozen_key,
                base_key=base_key,
            )
            result &= selected
            filter_keys.append(_rank_filter_signature(rank_filter, include_cutoff=True))
        return result, ("rank_filter_stage_and", frozen_key, tuple(filter_keys))
    raise ValueError(f"Unsupported filter stage mode: {stage.mode}")


def build_universe_mask_v2(
    feature_frames: dict[str, pd.DataFrame],
    market_warning_frame: pd.DataFrame,
    spec: UniverseSpec,
    *,
    rank_cache: RankCache | None = None,
    rank_cache_namespace: tuple[object, ...] | None = None,
) -> UniverseV2Result:
    effective_spec = _effective_universe_spec(spec)
    sort_column = effective_spec.sort_column or effective_spec.feature_column
    if sort_column not in feature_frames:
        raise ValueError(f"Missing sort feature for frame_v2 universe: {sort_column}")
    sort_frame = _shift_frame(feature_frames[sort_column], effective_spec.lag)
    columns = sort_frame.columns
    index = sort_frame.index

    market_mask = _allowed_market_mask(columns, effective_spec)
    market_mask_frame = pd.DataFrame([market_mask.to_numpy(dtype=bool)] * len(index), index=index, columns=columns)
    base = sort_frame.notna() & market_mask_frame

    if effective_spec.exclude_warnings and not market_warning_frame.empty:
        warning_frame = market_warning_frame.reindex(index=index, columns=columns)
        base &= warning_frame.fillna("NONE").eq("NONE")

    if effective_spec.min_age_days is not None:
        if "trade_price" not in feature_frames:
            raise ValueError("trade_price frame is required for min_age_days in frame_v2")
        age_frame = _age_days_from_frame(feature_frames["trade_price"])
        base &= age_frame.ge(float(effective_spec.min_age_days)).fillna(False)

    started = effective_spec.start_min_cross_section_size <= 0
    base_start_key: object = None
    if not started:
        counts = base.sum(axis=1)
        if (counts >= effective_spec.start_min_cross_section_size).any():
            first_start = counts[counts >= effective_spec.start_min_cross_section_size].index[0]
            base.loc[base.index < first_start, :] = False
            started = True
            base_start_key = str(first_start)
        else:
            base.loc[:, :] = False
            base_start_key = "never"

    candidates = base.copy()
    base_key: tuple[object, ...] = (
        "base",
        sort_column,
        int(effective_spec.lag or 0),
        base_start_key,
        bool(effective_spec.exclude_warnings),
        None if effective_spec.min_age_days is None else float(effective_spec.min_age_days),
        tuple(effective_spec.allowed_markets),
        tuple(effective_spec.excluded_markets),
    )
    candidates_key: tuple[object, ...] = ("candidates", base_key)
    for value_filter in effective_spec.value_filters:
        if value_filter.feature_column not in feature_frames:
            raise ValueError(f"Missing value filter feature for frame_v2 universe: {value_filter.feature_column}")
        value_frame = _shift_frame(feature_frames[value_filter.feature_column], value_filter.lag)
        candidates &= _compare_frame(value_filter.operator, value_frame, float(value_filter.value)).fillna(False)
        candidates_key = ("value_filter", candidates_key, _value_filter_signature(value_filter))

    for stage in effective_spec.filter_stages:
        candidates, candidates_key = _apply_filter_stage(
            candidates,
            candidates_key,
            base,
            base_key,
            feature_frames,
            stage,
            rank_cache=rank_cache,
            rank_cache_namespace=rank_cache_namespace,
        )

    selection = _apply_final_selection(
        candidates,
        base,
        sort_frame,
        effective_spec,
        rank_cache=rank_cache,
        rank_cache_namespace=rank_cache_namespace,
        candidates_key=candidates_key,
        base_key=base_key,
    )
    return UniverseV2Result(selection_mask=selection, base_eligible_mask=base)
