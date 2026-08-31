from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from lib.dataframes import read_wide_frames_from_cache
from lib.specs import FeatureSpec

CUSTOM_SOURCE_PREFIX = "custom:"


def _is_custom_source(source: str) -> bool:
    return source.startswith(CUSTOM_SOURCE_PREFIX)


def _parse_custom_source(source: str) -> str:
    if not _is_custom_source(source):
        raise ValueError(f"Unsupported custom source prefix: {source}")
    column = source[len(CUSTOM_SOURCE_PREFIX) :].strip()
    if not column:
        raise ValueError("custom source column must not be empty")
    if "/" in column or "\\" in column or column in {".", ".."}:
        raise ValueError(f"custom source column must be a parquet stem, not a path: {column}")
    return column


def _parse_market_source(source: str) -> tuple[str, str]:
    prefix, market, column = source.split(":", 2)
    if prefix != "market":
        raise ValueError(f"Unsupported market source prefix: {source}")
    if _is_custom_source(column):
        column = _parse_custom_source(column)
    return market.upper(), column


def _required_source_columns(
    feature_specs: Sequence[FeatureSpec],
    supported_source_columns: set[str],
) -> tuple[set[str], bool]:
    columns: set[str] = set()
    uses_market_source = False
    for spec in feature_specs:
        if spec.source is None:
            continue
        if spec.source.startswith("market:"):
            uses_market_source = True
            raw_column = spec.source.split(":", 2)[2]
            _, column = _parse_market_source(spec.source)
            if column in supported_source_columns or _is_custom_source(raw_column):
                columns.add(column)
        elif _is_custom_source(spec.source):
            columns.add(_parse_custom_source(spec.source))
        elif spec.source in supported_source_columns:
            columns.add(spec.source)
        for step in spec.steps:
            if step.kind == "true_range":
                keys = ("low", "close")
                references = [str(step.params.get(key, f"{key}_price")) for key in keys]
            elif step.kind == "corr_greedy_filter":
                references = [
                    str(step.params.get("price_feature", "signal_price")),
                    str(step.params.get("liquidity_feature", "candle_acc_trade_price")),
                ]
            elif step.kind == "select_by_state":
                references = [
                    str(value)
                    for key, value in step.params.items()
                    if (key.startswith("state_") and key.endswith("_feature")) or key == "default_feature"
                ]
            else:
                references = []
            for reference in references:
                if reference in supported_source_columns:
                    columns.add(reference)
    return columns, uses_market_source


def required_source_columns_for_feature_specs(
    feature_specs: Sequence[FeatureSpec],
    supported_source_columns: set[str],
) -> tuple[set[str], bool]:
    return _required_source_columns(feature_specs, supported_source_columns)


def referenced_markets_for_feature_specs(feature_specs: Sequence[FeatureSpec]) -> set[str]:
    markets: set[str] = set()
    for spec in feature_specs:
        if spec.source is None or not spec.source.startswith("market:"):
            continue
        market, _ = _parse_market_source(spec.source)
        markets.add(market)
    return markets


def _logical_frame(operator: str, inputs: list[pd.DataFrame]) -> pd.DataFrame:
    if not inputs:
        raise ValueError("Logical operation requires at least one input frame")
    mask = inputs[0].notna()
    for frame in inputs[1:]:
        mask &= frame.notna()
    flags = [frame.ne(0.0) for frame in inputs]
    if operator == "and":
        result = flags[0]
        for flag in flags[1:]:
            result = result & flag
    elif operator == "or":
        result = flags[0]
        for flag in flags[1:]:
            result = result | flag
    elif operator == "not":
        if len(flags) != 1:
            raise ValueError("LogicalSpec with operator 'not' must contain exactly one feature")
        result = ~flags[0]
    else:
        raise ValueError(f"Unsupported logical operator for frame_v2 graph: {operator}")
    return result.astype(float).where(mask)


def _holding_state_frame(entry_frame: pd.DataFrame, exit_frame: pd.DataFrame, cooldown_bars: int = 0) -> pd.DataFrame:
    if cooldown_bars < 0:
        raise ValueError("state.cooldown_bars must be non-negative")
    entry_frame = entry_frame.astype(float)
    exit_frame = exit_frame.astype(float)
    result = pd.DataFrame(np.nan, index=entry_frame.index, columns=entry_frame.columns, dtype=float)
    for column in entry_frame.columns:
        holding = False
        cooldown_remaining = 0
        entry_values = entry_frame[column].to_numpy(dtype=float, copy=False)
        exit_values = exit_frame[column].to_numpy(dtype=float, copy=False)
        out = np.full(len(entry_frame.index), np.nan, dtype=float)
        for idx, (entry_value, exit_value) in enumerate(zip(entry_values, exit_values)):
            if np.isnan(entry_value) or np.isnan(exit_value):
                continue
            entry_flag = entry_value != 0.0
            exit_flag = exit_value != 0.0
            if holding:
                if exit_flag:
                    holding = False
                    cooldown_remaining = int(cooldown_bars)
            elif cooldown_remaining > 0:
                cooldown_remaining -= 1
            elif entry_flag:
                holding = True
            out[idx] = 1.0 if holding else 0.0
        result[column] = out
    return result


def _breadth_frame(
    driver_frame: pd.DataFrame,
    signal_frame: pd.DataFrame,
    *,
    mode: str,
    top_n: int,
    quantiles: int,
    bucket_values: tuple[int, ...],
    ascending: bool,
) -> pd.DataFrame:
    driver = driver_frame.astype(float)
    signal = signal_frame.astype(float).reindex(index=driver.index, columns=driver.columns)
    result = pd.DataFrame(np.nan, index=driver.index, columns=driver.columns, dtype=float)

    for timestamp in driver.index:
        row_driver = driver.loc[timestamp]
        row_signal = signal.loc[timestamp]
        valid_mask = row_driver.notna() & row_signal.notna()
        if not bool(valid_mask.any()):
            continue

        row_driver = row_driver[valid_mask]
        row_signal = row_signal[valid_mask]
        if row_driver.empty:
            continue

        if mode == "top_n":
            if top_n <= 0:
                raise ValueError("breadth.top_n must be positive")
            selected_index = row_driver.sort_values(ascending=ascending).index[:top_n]
            selected_signal = row_signal.reindex(selected_index)
        elif mode == "quantile":
            if quantiles <= 0:
                raise ValueError("breadth.quantiles must be positive")
            ranks = row_driver.rank(method="first", ascending=ascending).astype(float)
            counts = float(len(ranks))
            buckets = 1.0 + np.floor((ranks - 1.0) * float(quantiles) / counts)
            selected_mask = buckets.isin([float(value) for value in bucket_values])
            selected_signal = row_signal[selected_mask]
        else:
            raise ValueError(f"Unsupported breadth mode for frame_v2 graph: {mode}")

        if selected_signal.empty:
            breadth_value = 0.0
        else:
            breadth_value = float(selected_signal.ne(0.0).mean())

        result.loc[timestamp, :] = breadth_value

    return result


def _weighted_sum_frame(
    inputs: list[tuple[pd.DataFrame, float]],
    combine: str | None,
) -> pd.DataFrame:
    mode = combine or "weighted_sum"
    if mode != "weighted_sum":
        raise ValueError(f"Unsupported score combine mode for frame_v2 graph: {mode}")
    if not inputs:
        raise ValueError("Component combination requires at least one input frame")
    mask = inputs[0][0].notna()
    weighted = inputs[0][0].astype(float) * float(inputs[0][1])
    for frame, weight in inputs[1:]:
        mask &= frame.notna()
        weighted = weighted + (frame.astype(float) * float(weight))
    return weighted.where(mask)


def _broadcast_market_source_frame(
    source: str,
    available_frames: dict[str, pd.DataFrame],
    output_index: pd.Index,
    output_columns: pd.Index,
) -> pd.DataFrame:
    market, column = _parse_market_source(source)
    if column not in available_frames:
        raise ValueError(f"Unknown source column for frame_v2 market source: {column}")
    base_frame = available_frames[column]
    if market not in base_frame.columns:
        raise ValueError(f"Reference market not found for frame_v2 market source: {market}")
    series = base_frame[market].reindex(output_index)
    data = {target_column: series for target_column in output_columns}
    return pd.DataFrame(data, index=output_index, columns=output_columns)


def _spec_dependencies(spec: FeatureSpec, supported_source_columns: set[str]) -> list[str]:
    if spec.compare is not None:
        dependencies = [spec.compare.left_feature]
        if spec.compare.right_feature is not None:
            dependencies.append(spec.compare.right_feature)
        return dependencies
    if spec.logical is not None:
        return list(spec.logical.features)
    if spec.state is not None:
        return [spec.state.entry_feature, spec.state.exit_feature]
    if spec.breadth is not None:
        return [spec.breadth.driver_feature, spec.breadth.signal_feature]
    if spec.components:
        return [component.feature_column for component in spec.components]
    if spec.source is None:
        return []
    if spec.source.startswith("market:"):
        _, column = _parse_market_source(spec.source)
        if column in supported_source_columns:
            return []
        raw_column = spec.source.split(":", 2)[2]
        if _is_custom_source(raw_column):
            return []
        return [column]
    if _is_custom_source(spec.source):
        return []
    if spec.source in supported_source_columns:
        dependencies: list[str] = []
    else:
        dependencies = [spec.source]
    for step in spec.steps:
        if step.kind in {"subtract_reference", "ratio_to_reference", "residualize_reference"}:
            reference = str(step.params["reference"])
            if reference not in supported_source_columns:
                dependencies.append(reference)
            continue
        if step.kind == "mask_by_feature":
            feature = str(step.params["feature"])
            if feature not in supported_source_columns:
                dependencies.append(feature)
            continue
        if step.kind == "select_by_state":
            references = [
                str(value)
                for key, value in step.params.items()
                if (key.startswith("state_") and key.endswith("_feature")) or key == "default_feature"
            ]
            for feature in references:
                if feature and feature not in supported_source_columns:
                    dependencies.append(feature)
            continue
        if step.kind in {"group_demean", "group_rank", "group_percentile"}:
            group_feature = str(step.params["group_feature"])
            if group_feature not in supported_source_columns:
                dependencies.append(group_feature)
            continue
        if step.kind == "corr_greedy_filter":
            for key, default in (
                ("price_feature", "signal_price"),
                ("liquidity_feature", "candle_acc_trade_price"),
            ):
                reference = str(step.params.get(key, default))
                if reference not in supported_source_columns:
                    dependencies.append(reference)
            continue
        if step.kind == "true_range":
            for key in ("low", "close"):
                reference = str(step.params.get(key, f"{key}_price"))
                if reference not in supported_source_columns:
                    dependencies.append(reference)
    return dependencies


def _step_signature(spec: FeatureSpec) -> tuple[Any, ...]:
    return tuple(
        (
            step.kind,
            tuple(sorted((str(key), step.params[key]) for key in step.params)),
        )
        for step in spec.steps
    )


def _feature_spec_signature(spec: FeatureSpec) -> tuple[Any, ...]:
    return (
        spec.source,
        _step_signature(spec),
        tuple((component.feature_column, float(component.weight)) for component in spec.components),
        spec.combine,
        (
            None
            if spec.compare is None
            else (
                spec.compare.left_feature,
                spec.compare.operator,
                spec.compare.right_feature,
                spec.compare.right_value,
            )
        ),
        (
            None
            if spec.logical is None
            else (
                spec.logical.operator,
                tuple(spec.logical.features),
            )
        ),
        (
            None
            if spec.state is None
            else (
                spec.state.entry_feature,
                spec.state.exit_feature,
                spec.state.cooldown_bars,
            )
        ),
        (
            None
            if spec.breadth is None
            else (
                spec.breadth.driver_feature,
                spec.breadth.signal_feature,
                spec.breadth.mode,
                spec.breadth.top_n,
                spec.breadth.quantiles,
                tuple(spec.breadth.bucket_values),
                spec.breadth.ascending,
            )
        ),
        spec.column_name,
        spec.resolved_column_name(),
    )


def build_feature_frames_from_cache_graph(
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
    from lib import features_v2 as ops

    required_source_columns, uses_market_source = _required_source_columns(feature_specs, ops.SUPPORTED_SOURCE_COLUMNS)
    if not required_source_columns:
        required_source_columns = {"trade_price"}

    if source_frames is None:
        raw_source_frames = read_wide_frames_from_cache(
            cache_dir,
            sorted(required_source_columns),
            market_columns=market_columns,
            max_markets=None if uses_market_source else max_markets,
        )
        extra_frames: dict[str, pd.DataFrame] = {}
    else:
        missing_columns = sorted(required_source_columns.difference(source_frames.keys()))
        if missing_columns:
            raise ValueError(f"Missing source frames for frame_v2 graph: {missing_columns}")
        raw_source_frames = {
            name: source_frames[name].copy()
            for name in sorted(required_source_columns)
        }
        extra_frames = {
            name: frame.copy()
            for name, frame in source_frames.items()
            if name not in raw_source_frames
        }
    raw_source_frames = ops._apply_per_market_tail(raw_source_frames, tail_rows)

    primary_name = "trade_price" if "trade_price" in raw_source_frames else next(iter(raw_source_frames.keys()))
    primary_frame = raw_source_frames[primary_name]
    if output_market_columns is not None:
        output_columns = pd.Index([str(market).upper() for market in output_market_columns])
    else:
        output_columns = primary_frame.columns if max_markets is None else primary_frame.columns[:max_markets]
    source_columns = primary_frame.columns if max_markets is None else primary_frame.columns[:max_markets]
    output_index = primary_frame.index

    frames: dict[str, pd.DataFrame] = {}
    for name, frame in raw_source_frames.items():
        frame_columns = source_columns
        if uses_market_source:
            frame_columns = source_columns.union(frame.columns)
        frames[name] = frame.reindex(index=output_index, columns=frame_columns).copy()
    for name, frame in extra_frames.items():
        frames[name] = frame.reindex(index=output_index, columns=output_columns).copy()

    specs_by_name: dict[str, FeatureSpec] = {}
    for spec in feature_specs:
        column_name = spec.resolved_column_name()
        if column_name in specs_by_name:
            raise ValueError(f"Duplicate frame_v2 feature column name: {column_name}")
        specs_by_name[column_name] = spec

    evaluating: set[str] = set()

    def _ensure_frame(name: str) -> pd.DataFrame:
        if name in frames:
            return frames[name]
        spec = specs_by_name.get(name)
        if spec is None:
            raise ValueError(f"Unknown frame_v2 graph dependency: {name}")
        cache_key = None
        if frame_cache is not None:
            cache_key = (
                frame_cache_namespace or (),
                _feature_spec_signature(spec),
            )
            cached = frame_cache.get(cache_key)
            if cached is not None:
                frames[name] = cached
                return frames[name]
        if name in evaluating:
            raise ValueError(f"Cyclic frame_v2 feature dependency detected at: {name}")
        evaluating.add(name)
        try:
            for dependency in _spec_dependencies(spec, ops.SUPPORTED_SOURCE_COLUMNS):
                _ensure_frame(dependency)

            if spec.compare is not None:
                compare = spec.compare
                left = frames[compare.left_feature]
                if compare.right_feature is not None:
                    frames[name] = ops._compare_frames(left, compare.operator, frames[compare.right_feature])
                else:
                    frames[name] = ops._compare_frames(left, compare.operator, float(compare.right_value))
                if cache_key is not None:
                    frame_cache[cache_key] = frames[name]
                return frames[name]

            if spec.logical is not None:
                inputs = [frames[feature_name] for feature_name in spec.logical.features]
                frames[name] = _logical_frame(spec.logical.operator, inputs)
                if cache_key is not None:
                    frame_cache[cache_key] = frames[name]
                return frames[name]

            if spec.state is not None:
                entry_frame = frames[spec.state.entry_feature]
                exit_frame = frames[spec.state.exit_feature]
                frames[name] = _holding_state_frame(
                    entry_frame,
                    exit_frame,
                    cooldown_bars=int(spec.state.cooldown_bars),
                )
                if cache_key is not None:
                    frame_cache[cache_key] = frames[name]
                return frames[name]

            if spec.breadth is not None:
                breadth = spec.breadth
                driver_frame = frames[breadth.driver_feature]
                signal_frame = frames[breadth.signal_feature]
                frames[name] = _breadth_frame(
                    driver_frame,
                    signal_frame,
                    mode=breadth.mode,
                    top_n=int(breadth.top_n),
                    quantiles=int(breadth.quantiles),
                    bucket_values=tuple(int(value) for value in breadth.bucket_values),
                    ascending=bool(breadth.ascending),
                )
                if cache_key is not None:
                    frame_cache[cache_key] = frames[name]
                return frames[name]

            if spec.components:
                component_inputs = [
                    (frames[component.feature_column], float(component.weight))
                    for component in spec.components
                ]
                frames[name] = _weighted_sum_frame(component_inputs, spec.combine)
                if cache_key is not None:
                    frame_cache[cache_key] = frames[name]
                return frames[name]

            if spec.source is None:
                raise ValueError("frame_v2 feature graph spec must define source, compare, logical, state, breadth, or components")

            if spec.source.startswith("market:"):
                _, column = _parse_market_source(spec.source)
                if column not in ops.SUPPORTED_SOURCE_COLUMNS:
                    _ensure_frame(column)
                current = _broadcast_market_source_frame(
                    spec.source,
                    frames,
                    output_index,
                    output_columns,
                )
            elif _is_custom_source(spec.source):
                column = _parse_custom_source(spec.source)
                if column not in frames:
                    raise ValueError(f"Unknown custom source column for frame_v2 graph: {column}")
                current = frames[column].copy()
            else:
                if spec.source not in frames:
                    raise ValueError(f"Unknown source column for frame_v2 graph: {spec.source}")
                current = frames[spec.source].copy()

            for step in spec.steps:
                current = ops._apply_transform(current, step.kind, step.params, frames)
            frames[name] = current.reindex(index=output_index, columns=output_columns).sort_index(axis=1)
            if cache_key is not None:
                frame_cache[cache_key] = frames[name]
            return frames[name]
        finally:
            evaluating.remove(name)

    for spec in feature_specs:
        _ensure_frame(spec.resolved_column_name())

    return frames
