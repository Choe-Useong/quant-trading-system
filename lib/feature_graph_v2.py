from __future__ import annotations

from collections import OrderedDict
from collections.abc import MutableMapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from lib.dataframes import CACHE_ELIGIBILITY_FRAME, cache_data_usage_mask, read_wide_frames_from_cache
from lib.specs import FeatureSpec

CUSTOM_SOURCE_PREFIX = "custom:"


class FeatureFrameCache(MutableMapping):
    """Bounded LRU storage for reusable transform results within one grid run."""

    def __init__(self, *, max_bytes: int = 128 * 1024 * 1024, max_entries: int = 128):
        if max_bytes < 0 or max_entries < 0:
            raise ValueError("Feature cache limits must be non-negative")
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.size_bytes = 0
        self._frames = OrderedDict()
        self._sizes = {}

    def __getitem__(self, key):
        value = self._frames[key]
        self._frames.move_to_end(key)
        return value

    def __setitem__(self, key, frame):
        if key in self._frames:
            del self[key]
        size = int(frame.memory_usage(index=True, deep=True).sum())
        if size > self.max_bytes or self.max_entries == 0:
            return
        while self._frames and (
            self.size_bytes + size > self.max_bytes or len(self) >= self.max_entries
        ):
            del self[next(iter(self._frames))]
        self._frames[key] = frame
        self._sizes[key] = size
        self.size_bytes += size

    def __delitem__(self, key):
        del self._frames[key]
        self.size_bytes -= self._sizes.pop(key)

    def __iter__(self):
        return iter(self._frames)

    def __len__(self):
        return len(self._frames)


def _frame_content_signature(frame: pd.DataFrame) -> bytes:
    # Hash current content, not object identity: replacing or mutating an input
    # must invalidate its descendants even if the feature names stay the same.
    digest = sha256()
    digest.update(repr((
        tuple(frame.columns), tuple(frame.columns.names), tuple(map(str, frame.dtypes)),
        str(frame.index.dtype), tuple(frame.index.names), getattr(frame.index, "freqstr", None),
    )).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.digest()


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


def _split_market_source(source: str) -> tuple[str, str]:
    """Keep CCXT settlement suffixes in the symbol and custom: on the column."""
    prefix, separator, body = source.partition(":")
    if prefix != "market" or not separator:
        raise ValueError(f"Unsupported market source prefix: {source}")
    if ":custom:" in body:
        market, stem = body.split(":custom:", 1)
        column = CUSTOM_SOURCE_PREFIX + stem
    else:
        market, separator, column = body.rpartition(":")
        if not separator:
            raise ValueError(f"market source must define a market and column: {source}")
    if not market or not column:
        raise ValueError(f"market source must define a market and column: {source}")
    return market.upper(), column


def _parse_market_source(source: str) -> tuple[str, str]:
    market, column = _split_market_source(source)
    if _is_custom_source(column):
        column = _parse_custom_source(column)
    return market, column


def _custom_source_columns(feature_specs: Sequence[FeatureSpec]) -> set[str]:
    """Cache stems read through custom: sources (directly or via market:...:custom:)."""
    columns: set[str] = set()
    for spec in feature_specs:
        if spec.source is None:
            continue
        if spec.source.startswith("market:"):
            _, raw_column = _split_market_source(spec.source)
            if _is_custom_source(raw_column):
                columns.add(_parse_custom_source(raw_column))
        elif _is_custom_source(spec.source):
            columns.add(_parse_custom_source(spec.source))
    return columns


def _naive_utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tzinfo is not None else stamp


def _check_custom_source_coverage(
    raw_source_frames: dict[str, pd.DataFrame],
    custom_columns: set[str],
    cache_dir: Path,
) -> None:
    """Refuse custom frames that end before the cache's price calendar.

    Custom frames (fundamentals, share-class flags, ...) are built by other
    tools on the price calendar of their build date. If prices are updated
    later without rebuilding them, aligning to the price index would leave
    the new dates silently missing and filters would drop markets (DATA-009).
    """
    if not custom_columns:
        return
    reference = raw_source_frames.get("trade_price")
    if reference is not None and not reference.empty:
        reference_end = _naive_utc(reference.index.max())
    else:
        # Shared feature sets often read only market_cap/flags; compare with the
        # cache's price calendar when it exists (index plus one column).
        price_path = Path(cache_dir) / "trade_price.parquet"
        if not price_path.exists():
            return
        import pyarrow.parquet as pq
        first_column = pq.ParquetFile(price_path).schema_arrow.names[0]
        price_index = pd.read_parquet(price_path, columns=[first_column]).index
        if len(price_index) == 0:
            return
        reference_end = _naive_utc(pd.to_datetime(price_index).max())
    for name in sorted(custom_columns):
        frame = raw_source_frames.get(name)
        if frame is None or frame.empty:
            continue
        frame_end = _naive_utc(frame.index.max())
        if frame_end < reference_end:
            raise ValueError(
                f"custom source '{name}' ends at {frame_end} but trade_price ends at {reference_end} "
                f"in {cache_dir}; rebuild the custom frames after updating prices (for KR stock "
                "fundamentals: scripts/kr_stocks/build_kr_stock_fundamental_cache.py with the "
                "build_args recorded in fundamental_cache_report.json)"
            )


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
            _, raw_column = _split_market_source(spec.source)
            _, column = _parse_market_source(spec.source)
            if column in supported_source_columns or _is_custom_source(raw_column):
                columns.add(column)
        elif _is_custom_source(spec.source):
            columns.add(_parse_custom_source(spec.source))
        elif spec.source in supported_source_columns:
            columns.add(spec.source)
        for step in spec.steps:
            if step.kind in {
                "vwma", "subtract_reference", "ratio_to_reference", "multiply_reference", "residualize_reference", "rolling_beta",
                "elementwise_min", "elementwise_max",
            }:
                references = [str(step.params["reference"])]
            elif step.kind == "true_range":
                references = [
                    str(step.params.get("low", "low_price")),
                    str(step.params.get("close", "trade_price")),
                ]
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


def _holding_state_frame(
    entry_frame: pd.DataFrame, exit_frame: pd.DataFrame, cooldown_bars: int = 0,
    max_holding_bars: int | None = None,
) -> pd.DataFrame:
    if cooldown_bars < 0:
        raise ValueError("state.cooldown_bars must be non-negative")
    if max_holding_bars is not None and (type(max_holding_bars) is not int or max_holding_bars <= 0):
        raise ValueError("state.max_holding_bars must be a positive integer or null")
    entry_frame = entry_frame.astype(float)
    exit_frame = exit_frame.astype(float)
    result = pd.DataFrame(np.nan, index=entry_frame.index, columns=entry_frame.columns, dtype=float)
    for column in entry_frame.columns:
        holding = False
        cooldown_remaining = 0
        entered_at = -1
        entry_reset_required = False
        entry_values = entry_frame[column].to_numpy(dtype=float, copy=False)
        exit_values = exit_frame[column].to_numpy(dtype=float, copy=False)
        out = np.full(len(entry_frame.index), np.nan, dtype=float)
        for idx, (entry_value, exit_value) in enumerate(zip(entry_values, exit_values)):
            # Count source rows, including missing observations, rather than
            # extending the holding window until another valid signal appears.
            if holding and max_holding_bars is not None and idx - entered_at >= max_holding_bars:
                holding = False
                entered_at = -1
                cooldown_remaining = int(cooldown_bars)
                entry_reset_required = np.isnan(entry_value) or entry_value != 0.0
                out[idx] = 0.0
                continue  # Expiry never opens another position on the same row.
            if entry_reset_required and not np.isnan(entry_value) and entry_value == 0.0:
                entry_reset_required = False
            if np.isnan(entry_value) or np.isnan(exit_value):
                continue
            entry_flag = entry_value != 0.0
            exit_flag = exit_value != 0.0
            if holding:
                if exit_flag:
                    holding = False
                    entered_at = -1
                    cooldown_remaining = int(cooldown_bars)
            elif cooldown_remaining > 0:
                cooldown_remaining -= 1
            elif entry_flag and not entry_reset_required:
                holding = True
                entered_at = idx
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


def _spec_dependencies(
    spec: FeatureSpec, supported_source_columns: set[str], *, include_source_columns: bool = False,
) -> list[str]:
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
        if not include_source_columns:
            if column in supported_source_columns:
                return []
            _, raw_column = _split_market_source(spec.source)
            return [] if _is_custom_source(raw_column) else [column]
        source_reference = column
    elif _is_custom_source(spec.source):
        if not include_source_columns:
            return []
        source_reference = _parse_custom_source(spec.source)
    else:
        source_reference = spec.source
    if source_reference in supported_source_columns and not include_source_columns:
        dependencies: list[str] = []
    else:
        dependencies = [source_reference]
    for step in spec.steps:
        if step.kind in {
            "subtract_reference", "ratio_to_reference", "multiply_reference", "residualize_reference", "rolling_beta", "vwma",
            "elementwise_min", "elementwise_max",
        }:
            reference = str(step.params["reference"])
            if include_source_columns or reference not in supported_source_columns:
                dependencies.append(reference)
            continue
        if step.kind == "mask_by_feature":
            feature = str(step.params["feature"])
            if include_source_columns or feature not in supported_source_columns:
                dependencies.append(feature)
            continue
        if step.kind == "select_by_state":
            references = [
                str(value)
                for key, value in step.params.items()
                if (key.startswith("state_") and key.endswith("_feature")) or key == "default_feature"
            ]
            for feature in references:
                if feature and (include_source_columns or feature not in supported_source_columns):
                    dependencies.append(feature)
            continue
        if step.kind in {"group_demean", "group_rank", "group_percentile"}:
            group_feature = str(step.params["group_feature"])
            if include_source_columns or group_feature not in supported_source_columns:
                dependencies.append(group_feature)
            continue
        if step.kind == "corr_greedy_filter":
            for key, default in (
                ("price_feature", "signal_price"),
                ("liquidity_feature", "candle_acc_trade_price"),
            ):
                reference = str(step.params.get(key, default))
                if include_source_columns or reference not in supported_source_columns:
                    dependencies.append(reference)
            continue
        if step.kind == "true_range":
            for key, default in (("low", "low_price"), ("close", "trade_price")):
                reference = str(step.params.get(key, default))
                if include_source_columns or reference not in supported_source_columns:
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


def select_feature_input_frames(
    feature_specs: Sequence[FeatureSpec], available_frames: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """Pass only direct external inputs; the graph resolves declared ancestors.

    Keep references here. The graph owns its working copies, and callers keep
    the full shared dictionary for downstream universe/weight calculations.
    """
    from lib.features_v2 import SUPPORTED_SOURCE_COLUMNS

    required, _ = _required_source_columns(feature_specs, SUPPORTED_SOURCE_COLUMNS)
    required = set(required or {"trade_price"})
    for spec in feature_specs:
        required.update(_spec_dependencies(spec, SUPPORTED_SOURCE_COLUMNS, include_source_columns=True))
    return {name: frame for name, frame in available_frames.items() if name in required}


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
                spec.state.max_holding_bars,
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
    frame_cache: MutableMapping[tuple[Any, ...], pd.DataFrame] | None = None,
    frame_cache_namespace: tuple[Any, ...] | None = None,
    data_usage_starts: dict | None = None,
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
            name: source_frames[name]
            for name in sorted(required_source_columns)
        }
        extra_frames = {
            name: frame
            for name, frame in source_frames.items()
            if name not in raw_source_frames
        }
    raw_source_frames = ops._apply_per_market_tail(raw_source_frames, tail_rows)

    primary_name = "trade_price" if "trade_price" in raw_source_frames else next(iter(raw_source_frames.keys()))
    primary_frame = raw_source_frames[primary_name]
    _check_custom_source_coverage(raw_source_frames, _custom_source_columns(feature_specs), cache_dir)
    if output_market_columns is not None:
        output_columns = pd.Index([str(market).upper() for market in output_market_columns])
    else:
        output_columns = primary_frame.columns if max_markets is None else primary_frame.columns[:max_markets]
    source_columns = primary_frame.columns if max_markets is None else primary_frame.columns[:max_markets]
    output_index = primary_frame.index
    usage = cache_data_usage_mask(cache_dir, output_index, primary_frame.columns.union(output_columns),
                                 data_usage_starts=data_usage_starts)

    def coverage(frame):
        return frame if usage is None else frame.where(usage.reindex(index=frame.index, columns=frame.columns).eq(True))

    frames: dict[str, pd.DataFrame] = {}
    def prepare_input(frame, columns):
        aligned = frame if frame.index.identical(output_index) and frame.columns.identical(columns) else frame.reindex(
            index=output_index, columns=columns, copy=False)
        # One owned copy, either from where() or copy(). Never mutate caller data.
        return aligned.copy() if usage is None else coverage(aligned)

    for name, frame in raw_source_frames.items():
        frame_columns = source_columns
        if uses_market_source:
            frame_columns = source_columns.union(frame.columns)
        frames[name] = prepare_input(frame, frame_columns)
    for name, frame in extra_frames.items():
        frames[name] = prepare_input(frame, output_columns)

    specs_by_name: dict[str, FeatureSpec] = {}
    for spec in feature_specs:
        column_name = spec.resolved_column_name()
        if column_name == CACHE_ELIGIBILITY_FRAME:
            raise ValueError(f"Reserved cache eligibility feature name: {column_name}")
        if column_name in specs_by_name:
            raise ValueError(f"Duplicate frame_v2 feature column name: {column_name}")
        specs_by_name[column_name] = spec

    evaluating: set[str] = set()
    input_names = set(frames)
    covered_names = set(frames)
    signatures: dict[str, bytes] = {}
    signing: set[str] = set()

    def _signature(name: str) -> bytes:
        if name in signatures:
            return signatures[name]
        if name in input_names:
            signatures[name] = _frame_content_signature(frames[name])
            return signatures[name]
        spec = specs_by_name.get(name)
        if spec is None:
            raise ValueError(f"Unknown frame_v2 graph dependency: {name}")
        if name in signing:
            raise ValueError(f"Cyclic frame_v2 feature dependency detected at: {name}")
        signing.add(name)
        try:
            dependencies = tuple(
                (dependency, _signature(dependency))
                for dependency in _spec_dependencies(
                    spec, ops.SUPPORTED_SOURCE_COLUMNS, include_source_columns=True,
                )
            )
            signatures[name] = sha256(repr((
                _feature_spec_signature(spec), dependencies,
            )).encode()).digest()
            return signatures[name]
        finally:
            signing.remove(name)

    cache_context = None
    if frame_cache is not None:
        index_digest = sha256(pd.util.hash_pandas_object(output_index).to_numpy().tobytes()).digest()
        cache_context = (
            tuple(output_columns), tuple(output_columns.names), index_digest,
            str(output_index.dtype), tuple(output_index.names), getattr(output_index, "freqstr", None),
        )
        if usage is not None:
            cache_context += (usage.attrs["data_usage_signature"],)

    def _ensure_frame(name: str) -> pd.DataFrame:
        # Mask every dependency, including negated conditions, before another
        # stateful feature can consume it and enter during excluded history.
        frame = _evaluate_frame(name)
        if name not in covered_names:
            frames[name] = coverage(frame)
            covered_names.add(name)
        return frames[name]

    def _evaluate_frame(name: str) -> pd.DataFrame:
        if name in frames:
            return frames[name]
        spec = specs_by_name.get(name)
        if spec is None:
            raise ValueError(f"Unknown frame_v2 graph dependency: {name}")
        cache_key = None
        # Keep the budget for transforms (EMA, rolling statistics, ratios, ...),
        # rather than filling it with inexpensive Boolean and holding outputs.
        # Numeric transform outputs are float64 on the declared output grid.
        # Oversized frames cannot enter the bounded cache; avoid hashing every
        # large ancestor merely to construct a key that will never be retained.
        oversized = isinstance(frame_cache, FeatureFrameCache) and (
            len(output_index) * len(output_columns) * 8 > frame_cache.max_bytes)
        if frame_cache is not None and spec.steps and not oversized:
            cache_key = (
                frame_cache_namespace or (),
                cache_context,
                _signature(name),
            )
            cached = frame_cache.get(cache_key)
            if cached is not None:
                frames[name] = cached
                covered_names.add(name)
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
                    max_holding_bars=spec.state.max_holding_bars,
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
                # Reference-market history must also obey each output's history.
                current = coverage(current)
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
                current = coverage(ops._apply_transform(current, step.kind, step.params, frames))
            if not current.index.identical(output_index) or not current.columns.identical(output_columns):
                current = current.reindex(index=output_index, columns=output_columns)
            if not current.columns.is_monotonic_increasing:
                current = current.sort_index(axis=1)
            frames[name] = current
            covered_names.add(name)
            if cache_key is not None:
                frame_cache[cache_key] = frames[name]
            return frames[name]
        finally:
            evaluating.remove(name)

    try:
        for spec in feature_specs:
            _ensure_frame(spec.resolved_column_name())

        if usage is not None:
            frames[CACHE_ELIGIBILITY_FRAME] = usage
        return frames
    finally:
        # Recursive local functions form reference cycles through their closure
        # cells. Break them explicitly so discarded NumPy buffers are released
        # now, rather than waiting for Python's object-count-based cyclic GC.
        del _ensure_frame, _evaluate_frame, _signature
