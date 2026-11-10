#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import (
    CACHE_ELIGIBILITY_FRAME, build_long_frame_from_candle_dir, build_wide_frames_from_candle_dir,
    read_wide_frames_from_cache,
)
from lib.feature_graph_v2 import (
    FeatureFrameCache, referenced_markets_for_feature_specs, required_source_columns_for_feature_specs,
    select_feature_input_frames, _spec_dependencies,
)
from lib.features_v2 import SUPPORTED_SOURCE_COLUMNS, build_feature_frames_from_cache
from lib.market_scores_v2 import build_market_score_frame, required_markets_for_market_score_spec
from lib.storage import replace_file
from lib.spec_io import (
    load_feature_specs_from_payload,
    load_market_score_spec,
    load_market_score_spec_from_payload,
    load_strategy_sleeves_from_payload,
    load_universe_spec_from_payload,
    load_weight_spec_from_payload,
)
from lib.universe_v2 import (
    ENTRY_ELIGIBILITY_FRAME, build_universe_mask_v2, holding_selection_start, validate_holding_selection_config,
)
from lib.vectorbt_adapter import VectorBTSpec
from lib.backtest_engine_v2 import resolve_benchmark_curve_v2 as resolve_benchmark_curve
from lib.backtest_engine_v2 import (
    DEFAULT_ARTIFACT_FORMAT, BacktestResultV2, FuturesBankruptcyError,
    normalize_artifact_format, run_backtest_v2, write_backtest_frame,
    write_backtest_analysis_v2,
    apply_observed_universe_policy_v2, record_data_policy_audit_v2, record_execution_availability_summary_v2,
    funding_usage_starts_v2,
)
from lib.weights_v2 import (
    build_weight_frame_v2, combine_strategy_sleeve_frames, is_change_only_weight_output, to_change_only_target_frame,
    _rebalance_mask,
)
from scripts.run_vectorbt import (
    benchmark_comparison_period_summary,
    benchmark_summary,
    build_benchmark_comparison_curves,
    compute_benchmark_regression_stats,
    compute_drawdown_recovery_stats,
    compute_excess_curves,
    compute_information_ratio,
    compute_recent_1y_stats,
    compute_recent_2y_stats,
    compute_return_series,
    compute_rolling_information_ratio,
    infer_timeframe,
    load_price_frame,
    periods_per_day_for_timeframe,
    print_summary,
    resolve_periods_per_year,
    summarize_rolling_information_ratio,
    strategy_performance_summary,
    timeframe_to_pandas_freq,
    trim_frames_to_first_weight,
    write_summary_csv,
)


DEFAULT_CACHE_VALUE_COLUMNS = (
    "trade_price",
    "opening_price",
    "high_price",
    "low_price",
    "candle_acc_trade_volume",
    "candle_acc_trade_price",
    "timestamp",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run frame-native v2 cross-sectional grid from config JSON."
    )
    parser.add_argument("--config-json", required=True, help="Cross-sectional grid configuration JSON path")
    return parser


def _render_value(template: Any, context: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {key: _render_value(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_value(value, context) for value in template]
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template.count("{") == 1 and template.count("}") == 1:
            key = template[1:-1]
            if key in context:
                return context[key]
        return template.format(**context)
    return template


def _render_backtest_payload(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if "backtest_spec_template" in config and "vectorbt_spec_template" in config:
        raise ValueError("Use either backtest_spec_template or vectorbt_spec_template, not both")
    return _render_value(config.get("backtest_spec_template", config.get("vectorbt_spec_template", {})), context)


def _grid_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = list(grid.keys())
    values = [grid[key] for key in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _passes_constraints(combo: dict[str, Any], constraints: list[str]) -> bool:
    if not constraints:
        return True
    safe_globals = {"__builtins__": {}}
    safe_locals = dict(combo)
    for constraint in constraints:
        if not bool(eval(constraint, safe_globals, safe_locals)):
            return False
    return True


def _canonical_payload_key(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dynamic_universe_usage_key(
    *,
    feature_payload: Any,
    universe_payload: dict[str, Any],
    market_score_payload: Any,
    market_score_spec_path: object,
) -> str:
    return _canonical_payload_key(
        {
            "feature_payload": feature_payload,
            "universe_payload": _universe_payload_for_cache(universe_payload),
            "market_score_payload": market_score_payload,
            "market_score_spec_path": (
                str(Path(str(market_score_spec_path)).resolve())
                if market_score_spec_path
                else ""
            ),
        }
    )


def _dynamic_rank_usage_key(
    *,
    feature_payload: Any,
    market_score_payload: Any,
    market_score_spec_path: object,
) -> str:
    return _canonical_payload_key(
        {
            "feature_payload": feature_payload,
            "market_score_payload": market_score_payload,
            "market_score_spec_path": (
                str(Path(str(market_score_spec_path)).resolve())
                if market_score_spec_path
                else ""
            ),
        }
    )


def _drop_rank_cache_by_namespace(
    cache: dict[tuple[object, ...], pd.DataFrame],
    namespace: tuple[object, ...],
) -> int:
    keys = [
        key
        for key in cache
        if isinstance(key, tuple) and key[: len(namespace)] == namespace
    ]
    for key in keys:
        cache.pop(key, None)
    return len(keys)


def _universe_payload_for_cache(payload: dict[str, Any]) -> dict[str, Any]:
    cache_payload = copy.deepcopy(payload)
    cache_payload.pop("name", None)
    return cache_payload


def _universe_referenced_feature_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def _add(value: Any) -> None:
        if value is not None and str(value):
            names.add(str(value))

    _add(payload.get("feature_column"))
    _add(payload.get("sort_column"))
    if payload.get("holding") is not None:
        _add(payload["holding"].get("exit_feature"))
    if payload.get("min_age_days") is not None:
        _add("trade_price")
    for item in payload.get("value_filters", []) or []:
        _add(item.get("feature_column"))
    for item in payload.get("rank_filters", []) or []:
        _add(item.get("feature_column"))
    for stage in payload.get("filter_stages", []) or []:
        for item in stage.get("filters", []) or []:
            _add(item.get("feature_column"))
    return names


def _strategy_sleeve_payload_parts(weight_payload: Any) -> list[tuple[dict | None, dict]]:
    """(universe payload or None, weight payload) per sleeve, or the payload itself."""
    sleeves = load_strategy_sleeves_from_payload(weight_payload)
    if sleeves is None:
        return [(None, weight_payload)]
    return [(sleeve["universe_spec"], sleeve["weight_spec"]) for sleeve in sleeves]


def _config_uses_strategy_sleeves(config, combinations) -> bool:
    try:
        return any(
            load_strategy_sleeves_from_payload(_render_value(
                config["weight_spec_template"],
                {**combo, "run_name": config["run_name_template"].format(**combo)})) is not None
            for combo in combinations)
    except (ValueError, KeyError, TypeError):
        return False


def _needed_feature_names(frames, config, combinations, static_market_score_spec) -> set[str] | None:
    """Feature outputs any candidate consumes (sleeve universes/weights included).

    None means the payloads could not be parsed, so callers keep everything.
    """
    needed = {next(iter(frames)), CACHE_ELIGIBILITY_FRAME}

    def collect(value):
        if isinstance(value, str):
            if value in frames:
                needed.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    if static_market_score_spec is not None:
        collect(asdict(static_market_score_spec))
    try:
        for combo in combinations:
            context = {**combo, "run_name": config["run_name_template"].format(**combo)}
            collect(_render_value(config["universe_spec_template"], context))
            weight_payload = _render_value(config["weight_spec_template"], context)
            for universe_payload, sleeve_weight in _strategy_sleeve_payload_parts(weight_payload):
                if universe_payload is not None:
                    collect(universe_payload)
                collect(asdict(load_weight_spec_from_payload(sleeve_weight)))
            collect(_render_backtest_payload(config, context))
            payload = _render_value(config.get("feature_spec_template") or [], context)
            needed.update(select_feature_input_frames(load_feature_specs_from_payload(payload), frames))
            for key in ("market_scores_spec_template", "weight_market_scores_spec_template"):
                if config.get(key) is not None:
                    collect(asdict(load_market_score_spec_from_payload(_render_value(config[key], context))))
    except (ValueError, KeyError, TypeError):
        # Projection is optional. Preserve the normal candidate error/reporting
        # path rather than failing early while planning memory retention.
        return None
    return needed


def _retain_shared_feature_frames(frames, config, combinations, static_market_score_spec):
    """Retain shared outputs consumed by any candidate, not dead intermediates.

    Include parsed defaults and every declared candidate. Artifact-producing
    single-strategy runs keep the full feature dictionary for compatibility;
    strategy_sleeves configs (no such history, several universes) always
    project, since artifacts never read features.
    """
    if config.get("save_run_artifacts", False) and not _config_uses_strategy_sleeves(config, combinations):
        return frames
    needed = _needed_feature_names(frames, config, combinations, static_market_score_spec)
    if needed is None:
        return frames
    return {name: frame for name, frame in frames.items() if name in needed}


def _feature_identity_map_from_payloads(payloads: list[Any]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for payload in payloads:
        if not payload:
            continue
        specs = load_feature_specs_from_payload(payload)
        for item, spec in zip(payload, specs):
            identities[spec.resolved_column_name()] = _canonical_payload_key(item)
    return identities


def _feature_outputs_only(
    frames: dict[str, pd.DataFrame],
    feature_specs,
) -> dict[str, pd.DataFrame]:
    return {
        spec.resolved_column_name(): frames[spec.resolved_column_name()]
        for spec in feature_specs
    }


class CandidateFeatureBuilder:
    """Feature frames for one candidate, prepared the way the grid prepares them.

    With shared_feature_spec_template, shared features are built once per
    data-usage policy and reduced to the outputs some candidate consumes; a
    candidate's feature_spec_template is built on top and only its outputs
    are added. Without shared features each call builds the candidate's full
    feature set (nothing is cached across candidates here). Used by the grid
    (shared path), walk-forward, PBO and SPA so they hold one shared copy
    instead of one full copy per candidate.

    project_shared=None keeps the grid rule (_retain_shared_feature_frames);
    True always keeps only consumed shared outputs (tools that save no
    feature artifacts).
    """

    def __init__(
        self,
        config: dict[str, Any],
        combinations: list[dict[str, Any]],
        source_cache_dir: Path,
        *,
        static_market_score_spec: Any,
        preselection_plans: dict | None,
        project_shared: bool | None = None,
        node_frame_cache: FeatureFrameCache | None = None,
        source_frame_cache: dict | None = None,
    ) -> None:
        self.config = config
        self.combinations = combinations
        self.source_cache_dir = source_cache_dir
        self.static_market_score_spec = static_market_score_spec
        self.project_shared = project_shared
        self.max_markets = config.get("max_markets")
        self.tail_hours = config.get("tail_hours")
        self.node_frame_cache = node_frame_cache if node_frame_cache is not None else FeatureFrameCache()
        self.source_frame_cache = source_frame_cache if source_frame_cache is not None else {}
        self.shared_payload = config.get("shared_feature_spec_template") or None
        self.shared_specs = None
        self.shared_payload_key = ""
        self.shared_required_feature_markets: list[str] | None = None
        self.shared_output_feature_markets: list[str] | None = None
        self.shared_required_columns: set[str] | None = None
        self.shared_source_frames: dict[str, pd.DataFrame] | None = None
        self._shared_variants: dict[tuple, dict[str, pd.DataFrame]] = {}
        if self.shared_payload:
            self._prepare_shared(preselection_plans)

    @property
    def has_shared(self) -> bool:
        return self.shared_specs is not None

    def candidate_feature_payload(self, context: dict[str, Any]) -> Any:
        """Feature payload candidate() expects for this context.

        With shared features: feature_spec_template only (None if absent).
        Without: shared and candidate templates rendered together, as before.
        """
        template = self.config.get("feature_spec_template")
        if self.has_shared:
            return _render_value(template, context) if template is not None else None
        payload: list[dict[str, Any]] = []
        if self.config.get("shared_feature_spec_template") is not None:
            payload.extend(_render_value(self.config["shared_feature_spec_template"], context) or [])
        if template is not None:
            payload.extend(_render_value(template, context) or [])
        return payload

    def candidate_required_markets(
        self, universe_payload: dict[str, Any], feature_payload: Any, market_score_spec: Any,
    ) -> list[str] | None:
        """Markets candidate() builds features for, without building them."""
        if not self.has_shared:
            return _resolve_required_feature_markets(
                load_feature_specs_from_payload(feature_payload), universe_payload, market_score_spec)
        required = self.shared_required_feature_markets
        if feature_payload is not None:
            per_run = _resolve_required_feature_markets(
                load_feature_specs_from_payload(feature_payload), universe_payload, market_score_spec)
            if per_run is not None:
                required = sorted(set(required or ()) | set(per_run))
        return required

    def _prepare_shared(self, preselection_plans: dict | None) -> None:
        config = self.config
        run_name_template = config["run_name_template"]
        self.shared_specs = load_feature_specs_from_payload(self.shared_payload)
        self.shared_payload_key = _canonical_payload_key(self.shared_payload)
        required_sets: list[set[str]] = []
        output_sets: list[set[str]] = []
        resolvable = True
        for combo in self.combinations:
            context = dict(combo)
            context["run_name"] = run_name_template.format(**context)
            universe_payload = _render_value(config["universe_spec_template"], context)
            if preselection_plans is not None:
                universe_payload = _preselected_universe_payload(
                    universe_payload, preselection_plans[_canonical_payload_key(combo)])
            rendered_market_score_payload = (
                _render_value(config["market_scores_spec_template"], context)
                if config.get("market_scores_spec_template") is not None
                else None
            )
            market_score_spec = (
                load_market_score_spec_from_payload(rendered_market_score_payload)
                if rendered_market_score_payload is not None
                else self.static_market_score_spec
            )
            required_markets = _resolve_required_feature_markets(self.shared_specs, universe_payload, market_score_spec)
            if required_markets is None:
                resolvable = False
                break
            required_sets.append(set(required_markets))
            output_markets = _resolve_output_feature_markets(universe_payload)
            if output_markets is not None:
                output_sets.append(set(output_markets))
        if resolvable and required_sets:
            self.shared_required_feature_markets = sorted(set().union(*required_sets))
        if output_sets:
            self.shared_output_feature_markets = sorted(set().union(*output_sets))
        required_columns, uses_market_source = required_source_columns_for_feature_specs(
            self.shared_specs, SUPPORTED_SOURCE_COLUMNS)
        self.shared_required_columns = set(required_columns or {"trade_price"})
        self.shared_source_frames = read_wide_frames_from_cache(
            self.source_cache_dir,
            sorted(self.shared_required_columns),
            market_columns=self.shared_required_feature_markets,
            max_markets=(
                None
                if (uses_market_source or self.shared_required_feature_markets is not None)
                else self.max_markets
            ),
        )

    def _retain(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        if self.project_shared is None:
            return _retain_shared_feature_frames(frames, self.config, self.combinations, self.static_market_score_spec)
        if not self.project_shared:
            return frames
        needed = _needed_feature_names(frames, self.config, self.combinations, self.static_market_score_spec)
        if needed is None:
            return frames
        return {name: frame for name, frame in frames.items() if name in needed}

    def shared_variant(self, data_usage_starts: dict) -> dict[str, pd.DataFrame]:
        """Shared outputs under one data-usage (funding start) policy."""
        key = tuple(sorted(data_usage_starts.items()))
        variant = self._shared_variants.get(key)
        if variant is None:
            variant = build_feature_frames_from_cache(
                self.source_cache_dir,
                self.shared_specs,
                market_columns=self.shared_required_feature_markets,
                output_market_columns=self.shared_output_feature_markets,
                max_markets=None if self.shared_required_feature_markets is not None else self.max_markets,
                tail_rows=self.tail_hours,
                source_frames=self.shared_source_frames,
                frame_cache=self.node_frame_cache,
                frame_cache_namespace=(
                    "grid_v2",
                    "shared",
                    self.shared_payload_key,
                    tuple(sorted(self.shared_required_columns or ())),
                    tuple(self.shared_required_feature_markets or ()),
                    tuple(self.shared_output_feature_markets or ()),
                    self.tail_hours,
                    key,
                ),
                data_usage_starts=data_usage_starts,
            )
            if self.static_market_score_spec is not None:
                variant[self.static_market_score_spec.output_column] = build_market_score_frame(
                    variant, self.static_market_score_spec)
            variant = self._retain(variant)
            self._shared_variants[key] = variant
        return variant

    def _source_frames(self, columns: set[str], required_feature_markets, uses_market_source) -> dict:
        source_cache_key = (
            tuple(sorted(columns)),
            tuple(required_feature_markets) if required_feature_markets is not None else (),
            None if (uses_market_source or required_feature_markets is not None) else self.max_markets,
        )
        source_frames = self.source_frame_cache.get(source_cache_key)
        if source_frames is None:
            source_frames = read_wide_frames_from_cache(
                self.source_cache_dir,
                list(source_cache_key[0]),
                market_columns=required_feature_markets,
                max_markets=source_cache_key[2],
            )
            self.source_frame_cache[source_cache_key] = source_frames
        return source_frames

    def candidate(
        self,
        *,
        universe_payload: dict[str, Any],
        feature_payload: Any,
        market_score_payload: Any,
        market_score_spec: Any,
        weight_market_score_spec: Any,
        data_usage_starts: dict,
    ) -> tuple[dict[str, pd.DataFrame], list[str] | None]:
        """(feature frames, required feature markets) for one candidate."""
        output_feature_markets = _resolve_output_feature_markets(universe_payload)
        if self.has_shared:
            required_feature_markets = self.shared_required_feature_markets
            feature_frames = dict(self.shared_variant(data_usage_starts))
            if feature_payload is not None:
                feature_specs = load_feature_specs_from_payload(feature_payload)
                per_run_required = _resolve_required_feature_markets(feature_specs, universe_payload, market_score_spec)
                if per_run_required is not None:
                    required_feature_markets = sorted(set(required_feature_markets or ()) | set(per_run_required))
                required_columns, uses_market_source = required_source_columns_for_feature_specs(
                    feature_specs, SUPPORTED_SOURCE_COLUMNS)
                # Shared raw frames already have the candidate's history
                # policy. Re-reading the same columns only adds unused copies.
                needed_columns = {
                    name for name in (required_columns or {"trade_price"})
                    if name not in feature_frames or (
                        required_feature_markets is not None
                        and not set(required_feature_markets).issubset(feature_frames[name].columns))
                }
                source_frames = (
                    self._source_frames(needed_columns, required_feature_markets, uses_market_source)
                    if needed_columns else {}
                )
                per_run_source_frames = dict(source_frames)
                per_run_source_frames.update(feature_frames)
                per_run_source_frames = select_feature_input_frames(feature_specs, per_run_source_frames)
                per_run_frames = build_feature_frames_from_cache(
                    self.source_cache_dir,
                    feature_specs,
                    market_columns=required_feature_markets,
                    output_market_columns=output_feature_markets,
                    max_markets=None if required_feature_markets is not None else self.max_markets,
                    tail_rows=self.tail_hours,
                    source_frames=per_run_source_frames,
                    data_usage_starts=data_usage_starts,
                    frame_cache=self.node_frame_cache,
                    frame_cache_namespace=(
                        "grid_v2",
                        "per_run",
                        tuple(required_feature_markets or ()),
                        tuple(output_feature_markets or ()),
                        self.tail_hours,
                    ),
                )
                feature_frames.update(_feature_outputs_only(per_run_frames, feature_specs))
                if market_score_payload is not None:
                    feature_frames[market_score_spec.output_column] = build_market_score_frame(
                        feature_frames, market_score_spec)
            if weight_market_score_spec is not None:
                feature_frames[weight_market_score_spec.output_column] = build_market_score_frame(
                    feature_frames, weight_market_score_spec)
            return feature_frames, required_feature_markets

        feature_specs = load_feature_specs_from_payload(feature_payload)
        required_feature_markets = _resolve_required_feature_markets(feature_specs, universe_payload, market_score_spec)
        required_columns, uses_market_source = required_source_columns_for_feature_specs(
            feature_specs, SUPPORTED_SOURCE_COLUMNS)
        source_frames = self._source_frames(
            set(required_columns or {"trade_price"}), required_feature_markets, uses_market_source)
        feature_frames = build_feature_frames_from_cache(
            self.source_cache_dir,
            feature_specs,
            market_columns=required_feature_markets,
            output_market_columns=output_feature_markets,
            max_markets=None if required_feature_markets is not None else self.max_markets,
            tail_rows=self.tail_hours,
            source_frames=source_frames,
            data_usage_starts=data_usage_starts,
            frame_cache=self.node_frame_cache,
            frame_cache_namespace=(
                "grid_v2",
                "per_run",
                tuple(required_feature_markets or ()),
                tuple(output_feature_markets or ()),
                self.tail_hours,
            ),
        )
        if market_score_spec is not None:
            feature_frames = dict(feature_frames)
            feature_frames[market_score_spec.output_column] = build_market_score_frame(feature_frames, market_score_spec)
        if weight_market_score_spec is not None:
            feature_frames = dict(feature_frames)
            feature_frames[weight_market_score_spec.output_column] = build_market_score_frame(
                feature_frames, weight_market_score_spec)
        return feature_frames, required_feature_markets


class LazyCandidateFeatures(Mapping):
    """Read-only feature mapping built on first access and released after use.

    PBO and SPA evaluate candidates one at a time; holding every candidate's
    frames at once is what exhausted memory. Candidates with identical
    feature inputs share one instance (add_user); the frames are freed when
    the last of them calls release(), and a later pass rebuilds them.
    """

    def __init__(self, build) -> None:
        self._build = build
        self._frames: dict[str, pd.DataFrame] | None = None
        self._users = 0
        self._remaining = 0

    def add_user(self) -> "LazyCandidateFeatures":
        self._users += 1
        self._remaining += 1
        return self

    def load(self) -> dict[str, pd.DataFrame]:
        if self._frames is None:
            self._frames = self._build()
        return self._frames

    def release(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._frames = None
            self._remaining = self._users

    def __getitem__(self, key):
        return self.load()[key]

    def __iter__(self):
        return iter(self.load())

    def __len__(self) -> int:
        return len(self.load())


def materialize_candidate_features(value) -> dict[str, pd.DataFrame]:
    return value.load() if isinstance(value, LazyCandidateFeatures) else value


def release_candidate_features(value) -> None:
    if isinstance(value, LazyCandidateFeatures):
        value.release()


def _resolve_prune_inactive_price_columns(
    vectorbt_payload: dict[str, Any],
    *,
    has_explicit_universe_markets: bool,
) -> bool:
    value = vectorbt_payload.get("prune_inactive_price_columns", "auto")
    if value is None:
        value = "auto"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "auto":
            return not has_explicit_universe_markets
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
        raise ValueError("prune_inactive_price_columns must be true, false, or 'auto'")
    return bool(value)


def _prune_inactive_target_columns(
    target: pd.DataFrame, *, keep_markets: set[str] | None = None,
) -> pd.DataFrame:
    """Drop only markets with no nonzero request anywhere in this evaluation.

    Keep earlier positions and their zero exits. NaN hold instructions remain
    unchanged for retained markets. Cash-only runs keep one original market,
    since the native futures engines require a nonempty execution universe.
    """
    keep = keep_markets or set()
    active = target.fillna(0.0).ne(0.0).any(axis=0)
    columns = [column for column in target.columns if active[column] or column in keep]
    if not columns and len(target.columns):
        columns = [target.columns[0]]
    return target.loc[:, columns]


def _reference_index_key(index: pd.Index) -> tuple[int, str, str]:
    if len(index) == 0:
        return (0, "", "")
    return (len(index), str(index[0]), str(index[-1]))


def _universe_cache_key(
    universe_payload: dict[str, Any],
    feature_identities: dict[str, str],
    warning_cache_key: tuple[Any, ...],
    reference_index: pd.Index,
    backtest_payload: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    referenced_names = sorted(_universe_referenced_feature_names(universe_payload))
    referenced_feature_key = tuple(
        (name, feature_identities.get(name, "<missing>"))
        for name in referenced_names
    )
    # Feature specs can depend on other generated features. A universe may only
    # reference the final score column, while that score depends on a per-run
    # mask such as mcap_topn_mask. Include all feature identities so dependency
    # changes cannot accidentally reuse a stale universe result.
    all_feature_key = tuple(sorted(feature_identities.items()))
    key = (
        _canonical_payload_key(_universe_payload_for_cache(universe_payload)),
        referenced_feature_key,
        all_feature_key,
        warning_cache_key,
        _reference_index_key(reference_index),
    )
    if universe_payload.get("holding") is not None:
        key += (("holding_start", str(holding_selection_start(backtest_payload or {}))),)
    return key


def _selection_stats(weight_frame: pd.DataFrame) -> dict[str, float | int]:
    rebalance_mask = weight_frame.notna().any(axis=1)
    if not bool(rebalance_mask.any()):
        return {
            "rebalance_dates": 0,
            "active_rebalance_dates": 0,
            "avg_selected_count": 0.0,
            "min_selected_count": 0,
            "max_selected_count": 0,
            "avg_selected_count_active": 0.0,
            "min_selected_count_active": 0,
            "max_selected_count_active": 0,
            "weight_rows": 0,
        }
    # Asset-scope requests leave unchanged holdings as NaN. Count the resolved
    # target book, rather than just markets requesting an order on this row.
    counts = weight_frame.loc[rebalance_mask].ffill().fillna(0.0).ne(0.0).sum(axis=1)
    values = [int(value) for value in counts.tolist()]
    active_values = [value for value in values if value > 0]
    return {
        "rebalance_dates": len(values),
        "active_rebalance_dates": len(active_values),
        "avg_selected_count": (sum(values) / len(values)) if values else 0.0,
        "min_selected_count": min(values) if values else 0,
        "max_selected_count": max(values) if values else 0,
        "avg_selected_count_active": (sum(active_values) / len(active_values)) if active_values else 0.0,
        "min_selected_count_active": min(active_values) if active_values else 0,
        "max_selected_count_active": max(active_values) if active_values else 0,
        "weight_rows": int(sum(values)),
    }


def _attach_plateau_air_mean(
    result_frame: pd.DataFrame,
    grid_config: dict[str, list[Any]],
) -> pd.DataFrame:
    if result_frame.empty:
        return result_frame
    if "Annualized Information Ratio" not in result_frame.columns:
        return result_frame

    grid_axes = [key for key in grid_config.keys() if key in result_frame.columns]
    if not grid_axes:
        return result_frame

    numeric_axes: list[str] = []
    categorical_axes: list[str] = []
    axis_index: dict[str, dict[Any, int]] = {}
    for axis in grid_axes:
        values = list(grid_config.get(axis, []))
        if not values:
            continue
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            numeric_axes.append(axis)
            axis_index[axis] = {value: idx for idx, value in enumerate(values)}
        else:
            categorical_axes.append(axis)

    if not numeric_axes:
        return result_frame

    frame = result_frame.copy()
    frame["Plateau AIR Mean"] = float("nan")

    def _group_key(value: Any) -> Any:
        if isinstance(value, (list, dict, set)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return value

    group_cols = ["status"] if "status" in frame.columns else []
    temporary_group_cols: list[str] = []
    for axis in categorical_axes:
        group_col = f"__plateau_group_{axis}"
        frame[group_col] = frame[axis].map(_group_key)
        temporary_group_cols.append(group_col)
    group_cols.extend(temporary_group_cols)
    grouped = frame.groupby(group_cols, dropna=False) if group_cols else [(None, frame)]

    for _, group in grouped:
        ok_group = group[group["status"] == "ok"].copy() if "status" in group.columns else group.copy()
        if ok_group.empty:
            continue
        indexed_positions = {
            axis: ok_group[axis].map(axis_index[axis])
            for axis in numeric_axes
        }
        for idx, row in ok_group.iterrows():
            keep = ok_group.index != idx
            for axis in numeric_axes:
                row_pos = axis_index[axis].get(row[axis])
                if row_pos is None:
                    keep &= False
                    continue
                keep &= indexed_positions[axis].sub(row_pos).abs().le(1)
            neighbors = ok_group[keep]
            if neighbors.empty:
                continue
            frame.loc[idx, "Plateau AIR Mean"] = pd.to_numeric(
                neighbors["Annualized Information Ratio"],
                errors="coerce",
            ).mean()
    return frame.drop(columns=temporary_group_cols, errors="ignore")


def _resolve_required_feature_markets(
    feature_specs,
    universe_payload: dict[str, Any],
    market_score_spec=None,
) -> list[str] | None:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    explicit_markets: set[str] = set()
    if universe_spec.allowed_markets:
        explicit_markets.update(str(market).upper() for market in universe_spec.allowed_markets)
    referenced_markets: set[str] = set()
    if market_score_spec is not None:
        referenced_markets.update(required_markets_for_market_score_spec(market_score_spec))
    referenced_markets.update(referenced_markets_for_feature_specs(feature_specs))
    if explicit_markets:
        return sorted(explicit_markets | referenced_markets)
    return None


def _resolve_output_feature_markets(universe_payload: dict[str, Any]) -> list[str] | None:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    if not universe_spec.allowed_markets:
        return None
    return sorted(str(market).upper() for market in universe_spec.allowed_markets)


def _preselection_specs(feature_specs, universe_payload):
    """Resolve only the declared ancestors of the preselection conditions."""
    by_name = {}
    for spec in feature_specs:
        name = spec.resolved_column_name()
        if name in by_name and by_name[name] != spec:
            raise ValueError(f"Conflicting preselection feature definitions: {name}")
        by_name[name] = spec
    needed = set()
    visiting = set()

    def visit(name):
        if name not in by_name or name in needed:
            return  # Raw/custom inputs are resolved by the existing graph.
        if name in visiting:
            raise ValueError(f"Cyclic preselection feature dependency: {name}")
        visiting.add(name)
        for dependency in _spec_dependencies(by_name[name], SUPPORTED_SOURCE_COLUMNS):
            visit(dependency)
        visiting.remove(name)
        needed.add(name)

    roots = _universe_referenced_feature_names(universe_payload)
    for name in roots:
        visit(name)
    raw_roots = [{"source": name} for name in sorted(roots)
                 if name in SUPPORTED_SOURCE_COLUMNS and name not in by_name]
    return load_feature_specs_from_payload(raw_roots) + [spec for name, spec in by_name.items() if name in needed]


def _preselection_changes_cross_section(feature_specs):
    """Future-dependent column unions cannot change a cross-sectional feature."""
    kinds = {"cross_rank", "cross_percentile", "group_demean", "group_rank",
             "group_percentile", "corr_greedy_filter"}
    return any(spec.breadth is not None or any(step.kind in kinds for step in spec.steps)
               for spec in feature_specs)


def _preselection_source_identity(source_cache_dir):
    # Metadata identifies ordinary cache generations without scanning gigabytes
    # of prices on each full-grid run. Adaptive search also hashes actual inputs.
    paths = sorted(p for p in source_cache_dir.iterdir() if p.suffix in {".parquet", ".json"})
    return {p.name: [p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns] for p in paths}


def _prepare_grid_preselection(config, source_cache_dir, out_dir, combinations):
    """Optional grid orchestration; no changes to feature/weight/engine semantics."""
    if config.get("preselection") is None:
        return None
    block = config["preselection"]
    if not isinstance(block, dict) or set(block) - {"rebalance_frequency", "universe_spec"}:
        raise ValueError("preselection must contain universe_spec and optional rebalance_frequency")
    if not isinstance(block.get("universe_spec"), dict):
        raise ValueError("preselection.universe_spec must be an object")
    source_identity = _preselection_source_identity(source_cache_dir)
    runtime = {name: version(name) for name in ("pandas", "numpy", "numba", "scikit-learn", "hmmlearn")}
    runtime["python"] = sys.version
    implementation = {
        name: sha256((ROOT_DIR / name).read_bytes()).hexdigest()
        for name in ("scripts/run_cross_section_grid_v2.py", "lib/feature_graph_v2.py",
                     "lib/features_v2.py", "lib/universe_v2.py", "lib/weights_v2.py",
                     "lib/dataframes.py", "lib/spec_io.py", "lib/specs.py", "lib/backtest_engine_v2.py",
                     "lib/funding_schedule_v2.py")
    }
    plans, reused = {}, {}
    directory = out_dir / "preselection"
    directory.mkdir(parents=True, exist_ok=True)
    for combo in combinations:
        context = {**combo, "run_name": config["run_name_template"].format(**combo)}
        settings = _render_value(block, context)
        frequency = str(settings.get("rebalance_frequency", "daily")).strip().lower()
        _rebalance_mask(pd.date_range("2024-01-01", periods=1), frequency)  # Validate early.
        universe_payload = _render_value(config["universe_spec_template"], context)
        original = load_universe_spec_from_payload(universe_payload)
        selection_payload = settings["universe_spec"]
        spec = load_universe_spec_from_payload(selection_payload)
        if spec.holding is not None:
            raise ValueError("preselection is an entry screen and cannot contain universe.holding")
        lags = [spec.lag, spec.signal_lag, *(f.lag for f in spec.value_filters),
                *(f.lag for f in spec.rank_filters), *(f.lag for s in spec.filter_stages for f in s.filters)]
        if any(type(lag) is not int or lag < 0 for lag in lags):
            raise ValueError("preselection lags must be non-negative integers")
        if spec.mode not in {"all", "top_n", "quantile"} or (spec.mode == "top_n" and spec.top_n <= 0):
            raise ValueError("Invalid preselection universe mode/top_n")
        allowed = set(original.allowed_markets)
        if spec.allowed_markets:
            allowed = allowed.intersection(spec.allowed_markets) if allowed else set(spec.allowed_markets)
            if not allowed:
                raise ValueError("preselection and strategy allowed_markets have no intersection")
        spec = replace(spec, allowed_markets=tuple(sorted(allowed)),
                       excluded_markets=tuple(sorted(set(original.excluded_markets) | set(spec.excluded_markets))),
                       exclude_warnings=original.exclude_warnings or spec.exclude_warnings,
                       min_age_days=max(original.min_age_days or 0, spec.min_age_days or 0) or None)
        all_specs = load_feature_specs_from_payload(config.get("shared_feature_spec_template") or [])
        all_specs += load_feature_specs_from_payload(_render_value(config.get("feature_spec_template") or [], context))
        selection_specs = _preselection_specs(all_specs, selection_payload)
        # A wider context is intentional for these features. Keep it intact and
        # still apply the date-specific screen; never silently change ranks.
        backend = _render_backtest_payload(config, context)
        projection_reason = None
        if _preselection_changes_cross_section(all_specs):
            projection_reason = "cross_sectional_features"
        elif (config.get("tail_hours") or 0) > 0:
            projection_reason = "per_market_tail_timeline"
        elif backend.get("benchmark_mode") in {"portfolio_group", "portfolio_group_rebalance"}:
            projection_reason = "portfolio_benchmark_universe"
        project = projection_reason is None
        identity = dict(version=1, source=str(source_cache_dir.resolve()), files=source_identity,
                        implementation=implementation, runtime=runtime, universe=asdict(spec), frequency=frequency,
                        features=[asdict(s) for s in selection_specs], backend=backend,
                        max_markets=config.get("max_markets"), tail_hours=config.get("tail_hours"))
        identity = json.loads(_canonical_payload_key(identity))
        key = sha256(_canonical_payload_key(identity).encode("utf-8")).hexdigest()
        if key not in reused:
            manifest_path, mask_path = directory / (key + ".json"), directory / (key + ".parquet")
            sampled, audit = None, None
            if manifest_path.exists() and mask_path.exists():
                try:
                    audit = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if audit["identity"] == identity and audit["mask_sha256"] == sha256(mask_path.read_bytes()).hexdigest():
                        sampled = pd.read_parquet(mask_path)
                        if (not isinstance(sampled.index, pd.DatetimeIndex)
                                or not sampled.index.is_unique or not sampled.index.is_monotonic_increasing
                                or not sampled.columns.is_unique or sampled.isna().any().any()
                                or not all(dtype == bool for dtype in sampled.dtypes)
                                or audit["selected_markets"] != sampled.columns[sampled.any(axis=0)].tolist()
                                or audit["candidate_markets"] != len(sampled.columns)
                                or audit["sampled_rows"] != len(sampled)):
                            sampled = None
                except (OSError, ValueError, KeyError):
                    sampled = None
            cached = sampled is not None
            if sampled is None:
                required_markets = _resolve_required_feature_markets(selection_specs, asdict(spec))
                # Age needs the actual trade history, even if screening only volume.
                build_specs = selection_specs
                if spec.min_age_days is not None and not any(s.resolved_column_name() == "trade_price" for s in build_specs):
                    build_specs = load_feature_specs_from_payload([{"source": "trade_price"}]) + build_specs
                frames = build_feature_frames_from_cache(
                    source_cache_dir, build_specs, market_columns=required_markets,
                    output_market_columns=sorted(allowed) if allowed else None,
                    max_markets=None if required_markets is not None else config.get("max_markets"),
                    tail_rows=config.get("tail_hours"),
                    data_usage_starts=funding_usage_starts_v2(source_cache_dir, backend))
                index = next(iter(frames.values())).index
                spec_with_policy, exclusions = apply_observed_universe_policy_v2(
                    source_cache_dir, index, next(iter(frames.values())).columns, spec, backend)
                warnings = _read_warning_frame(source_cache_dir, market_columns=required_markets,
                    max_markets=None if required_markets is not None else config.get("max_markets"),
                    require_warning=spec.exclude_warnings)
                result = build_universe_mask_v2(frames, warnings.reindex(index=index), spec_with_policy)
                sampled = result.selection_mask.loc[_rebalance_mask(index, frequency)].copy()
                # Raw inputs / all hourly Boolean workspaces are no longer needed.
                del frames, warnings, result
                selected = sampled.columns[sampled.any(axis=0)].tolist()
                audit = dict(identity=identity, selected_markets=selected,
                             candidate_markets=len(sampled.columns), sampled_rows=len(sampled),
                             data_exclusions=exclusions)
                temporary = mask_path.with_suffix(".parquet.tmp")
                sampled.to_parquet(temporary)
                replace_file(temporary, mask_path)
                audit["mask_sha256"] = sha256(mask_path.read_bytes()).hexdigest()
                temporary = manifest_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
                replace_file(temporary, manifest_path)
            reused[key] = dict(key=key, mask=sampled, audit=audit,
                               manifest_path=str(manifest_path.resolve()), mask_path=str(mask_path.resolve()))
            print(f"[preselection] {'reused' if cached else 'built'} {len(audit['selected_markets'])}"
                  f"/{audit['candidate_markets']} markets; {audit['sampled_rows']} selection dates")
        plan = dict(reused[key], project=project, projection_reason=projection_reason)
        projected = set(plan["audit"]["selected_markets"])
        weight = load_weight_spec_from_payload(_render_value(config["weight_spec_template"], context))
        if weight.hedge is not None:
            projected.add(weight.hedge.market)
        if weight.defensive_sleeve is not None:
            projected.update(weight.defensive_sleeve.markets)
        # A cash-only run still needs one price column for the existing engines.
        if not projected and len(plan["mask"].columns):
            projected.add(plan["mask"].columns[0])
        if not projected:
            raise ValueError("No source markets available for preselection")
        plan["markets"] = sorted(projected)
        plans[_canonical_payload_key(combo)] = plan
    if any(not p["project"] for p in plans.values()):
        reasons = sorted({p["projection_reason"] for p in plans.values() if not p["project"]})
        print(f"[preselection] retain full input universe ({', '.join(reasons)}); date filters still apply")
    return plans


def _preselected_universe_payload(payload, plan):
    if plan is None or not plan["project"]:
        return payload
    payload = copy.deepcopy(payload)
    # spec_io merges file + inline lists, so the original file must not widen it.
    payload.pop("allowed_markets_file", None)
    payload["allowed_markets"] = plan["markets"]
    return payload


def _preselection_universe_frames(feature_frames, universe_spec, preselection):
    """Apply the dated screen only to selection; preserve allocation history."""
    if preselection is None:
        return feature_frames
    reference_index = next(iter(feature_frames.values())).index
    columns = feature_frames[universe_spec.sort_column or universe_spec.feature_column].columns
    sampled = preselection["mask"].reindex(columns=columns, fill_value=False)
    selected = sampled.reindex(index=reference_index, method="ffill").fillna(False).astype(bool)
    usage = feature_frames.get(CACHE_ELIGIBILITY_FRAME)
    if usage is not None:
        selected &= usage.reindex(index=reference_index, columns=columns).eq(True)
    # Do not propagate the temporary selection boundary to the weight wrapper's
    # forced-zero logic or to rolling/state feature history.
    universe_frames = dict(feature_frames)
    if universe_spec.holding is not None:
        universe_frames[ENTRY_ELIGIBILITY_FRAME] = selected
    else:
        universe_frames[CACHE_ELIGIBILITY_FRAME] = selected
    return universe_frames


def _read_warning_frame(
    source_cache_dir: Path,
    *,
    market_columns: list[str] | None = None,
    max_markets: int | None = None,
    require_warning: bool = False,
) -> pd.DataFrame:
    warning_path = source_cache_dir / "market_warning.parquet"
    if not warning_path.exists():
        if require_warning:
            raise FileNotFoundError(f"market_warning.parquet is required when exclude_warnings=true: {warning_path}")
        return pd.DataFrame()

    requested_columns = None if market_columns is None else sorted({str(column).upper() for column in market_columns})
    try:
        warning_frame = pd.read_parquet(warning_path, columns=requested_columns)
    except Exception:
        warning_frame = pd.read_parquet(warning_path)
        if requested_columns is not None:
            warning_frame = warning_frame.reindex(columns=requested_columns)
    warning_frame.index = pd.to_datetime(warning_frame.index, utc=False)
    if requested_columns is not None:
        warning_frame = warning_frame.reindex(columns=requested_columns)
    elif max_markets is not None:
        warning_frame = warning_frame.reindex(columns=sorted(warning_frame.columns)[:max_markets])
    return warning_frame.sort_index().sort_index(axis=1)


def _write_numeric_cache(
    candle_dir: Path,
    cache_dir: Path,
    max_markets: int | None,
    tail_rows: int | None,
) -> None:
    frames, meta_by_market = build_wide_frames_from_candle_dir(
        candle_dir,
        DEFAULT_CACHE_VALUE_COLUMNS,
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    for column, frame in frames.items():
        frame.to_parquet(cache_dir / f"{column}.parquet")
    meta_frame = (
        pd.DataFrame.from_records(sorted(meta_by_market.values(), key=lambda item: item["market"]))
        if meta_by_market
        else pd.DataFrame(columns=["market", "korean_name", "english_name", "market_warning"])
    )
    meta_frame.to_parquet(cache_dir / "market_meta.parquet", index=False)


def _write_warning_cache(
    candle_dir: Path,
    cache_dir: Path,
    max_markets: int | None,
    tail_rows: int | None,
) -> None:
    long_frame = build_long_frame_from_candle_dir(
        candle_dir,
        usecols=["date_utc", "market", "market_warning"],
        max_markets=max_markets,
        tail_rows=tail_rows,
    )
    if long_frame.empty:
        warning_frame = pd.DataFrame()
    else:
        warning_frame = (
            long_frame.assign(
                date_utc=pd.to_datetime(long_frame["date_utc"], utc=False),
                market=long_frame["market"].astype(str).str.upper(),
                market_warning=long_frame["market_warning"].astype(str).str.upper(),
            )
            .pivot(index="date_utc", columns="market", values="market_warning")
            .sort_index()
            .sort_index(axis=1)
        )
    warning_frame.to_parquet(cache_dir / "market_warning.parquet")


def _resolve_source_cache_dir(
    config: dict[str, Any],
    candle_dir: Path,
    out_dir: Path,
) -> Path:
    configured = config.get("source_cache_dir")
    if configured:
        return Path(str(configured))

    parts = candle_dir.as_posix().split("/")
    if len(parts) >= 4 and parts[0] == "data":
        timeframe = parts[-1]
        if parts[1] == "upbit_research" and parts[2] == "minutes":
            candidate = ROOT_DIR / "data" / "upbit_research_cache" / timeframe
            if candidate.exists():
                return candidate
        if parts[1] == "upbit" and parts[2] == "minutes":
            candidate = ROOT_DIR / "data" / "upbit_cache" / timeframe
            if candidate.exists():
                return candidate

    cache_dir = out_dir / "_source_cache"
    if not cache_dir.exists():
        _write_numeric_cache(
            candle_dir,
            cache_dir,
            max_markets=config.get("max_markets"),
            tail_rows=config.get("tail_hours"),
        )
        _write_warning_cache(
            candle_dir,
            cache_dir,
            max_markets=config.get("max_markets"),
            tail_rows=config.get("tail_hours"),
        )
    return cache_dir


def _build_strategy_sleeve_parts(
    source_cache_dir: Path,
    feature_frames: dict[str, pd.DataFrame],
    warning_frame: pd.DataFrame,
    account_universe_payload: dict[str, Any],
    sleeves: tuple[dict, ...],
    vectorbt_payload: dict[str, Any],
    *,
    policy_index: pd.Index | None = None,
    holding_eval_start: pd.Timestamp | None = None,
    holding_eval_end: pd.Timestamp | None = None,
    trim_start_mode: str | None = None,
    part_cache: dict | None = None,
    cache_owner: tuple | None = None,
) -> tuple[list[dict[str, Any]], dict]:
    """Build each sleeve's full-history targets with the ordinary pipeline.

    A sleeve keeps only the markets it ever targets with a nonzero weight, so
    its zero rows for other markets cannot re-request another sleeve's holdings.
    Zero-capital sleeves are skipped. Walk-forward windows pass their
    observed-policy index and holding window, exactly as the single-strategy
    window path does.

    Memory: each part stores only its request rows. change_only sleeves store
    their full-history change_only rows; a window start state is rebuilt from
    them by _strategy_sleeve_request_frame. part_cache reuses a part when the
    same feature/warning objects, sleeve weight payload, policy-adjusted
    universe and holding window recur (walk-forward folds). cache_owner names
    the source objects that define validity when callers pass re-aligned copies.
    """
    reference_index = next(iter(feature_frames.values())).index
    markets = next(iter(feature_frames.values())).columns
    account_universe = load_universe_spec_from_payload(account_universe_payload)
    parts, exclusions = [], {}
    for sleeve in sleeves:
        if sleeve["capital"] == 0.0:
            continue
        payload = sleeve["universe_spec"] if sleeve["universe_spec"] is not None else account_universe_payload
        universe_spec = load_universe_spec_from_payload(payload)
        # Features, prices and warnings are loaded for the account universe.
        missing = sorted(set(universe_spec.allowed_markets or ()) - set(markets))
        if missing:
            raise ValueError(f"Strategy sleeve {sleeve['name']} markets are outside the account feature universe: {missing[:5]}")
        if universe_spec.exclude_warnings and not account_universe.exclude_warnings:
            raise ValueError(f"Strategy sleeve {sleeve['name']} exclude_warnings requires the account universe to load warnings")
        weight_spec = load_weight_spec_from_payload(sleeve["weight_spec"])
        validate_holding_selection_config(universe_spec, weight_spec, vectorbt_payload)
        universe_spec, sleeve_exclusions = apply_observed_universe_policy_v2(
            source_cache_dir, reference_index if policy_index is None else policy_index, markets, universe_spec,
            vectorbt_payload)
        exclusions.update(sleeve_exclusions)
        universe_frames, selection_start = feature_frames, None
        if universe_spec.holding is not None:
            selection_start = holding_selection_start(vectorbt_payload, eval_start=holding_eval_start,
                                                      trim_start_mode=trim_start_mode)
        owner = cache_owner if cache_owner is not None else (feature_frames, warning_frame)
        cache_key = (tuple(id(item) for item in owner), _canonical_payload_key(sleeve["weight_spec"]),
                     repr(universe_spec), str(selection_start),
                     str(holding_eval_end) if universe_spec.holding is not None else None)
        cached = part_cache.get(cache_key) if part_cache is not None else None
        if cached is not None and len(cached[0]) == len(owner) and all(a is b for a, b in zip(cached[0], owner)):
            parts.append({**cached[2], "name": sleeve["name"], "capital": sleeve["capital"]})
            continue
        if universe_spec.holding is not None and holding_eval_end is not None:
            universe_frames = {name: frame.loc[frame.index < holding_eval_end]
                               for name, frame in feature_frames.items()}
        universe_result = build_universe_mask_v2(
            universe_frames, warning_frame, universe_spec, selection_start=selection_start)
        frame = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames,
                                      base_eligible_mask=universe_result.base_eligible_mask)
        del universe_result
        change_only = is_change_only_weight_output(weight_spec)
        if change_only:
            frame = frame.ffill().fillna(0.0)
        nonzero = frame.fillna(0.0).ne(0.0)
        owned = nonzero.any(axis=0)
        frame = frame.loc[:, owned[owned].index]
        # Ownership starts at the sleeve's first nonzero target for each market,
        # so later data cannot add requests on earlier rows.
        first_target = nonzero.loc[:, frame.columns].idxmax(axis=0)
        del nonzero
        if change_only:
            frame = to_change_only_target_frame(frame, scope=weight_spec.change_only_scope)
        part = {"frame": frame.loc[frame.notna().any(axis=1).to_numpy()].copy(), "index": reference_index,
                "first_target": first_target, "change_only": change_only,
                "change_only_scope": weight_spec.change_only_scope}
        del frame
        if part_cache is not None:
            part_cache[cache_key] = (owner, None, part)
        parts.append({**part, "name": sleeve["name"], "capital": sleeve["capital"]})
    if not parts or not any(len(part["frame"].columns) for part in parts):
        raise ValueError("strategy_sleeves produced no nonzero target in any sleeve")
    return parts, exclusions


def _strategy_sleeve_request_frame(parts: list[dict[str, Any]], start, end=None) -> pd.DataFrame:
    """Account requests on the reference rows in [start, end).

    Equivalent to slicing each sleeve's dense targets to the window, applying
    its own change_only conversion (first window row = full target state),
    dropping requests before the sleeve owns a market and merging. Parts hold
    only request rows, so the merge runs on their union and expands once.
    Parts are not modified.
    """
    index = parts[0]["index"]
    keep = (index >= start) if start is not None else np.zeros(len(index), dtype=bool)
    if end is not None:
        keep &= index < end
    window = index[keep]
    frames = []
    for part in parts:
        rows = part["frame"]
        if not len(window):
            frames.append(rows.iloc[0:0])
            continue
        first = window[0]
        inside = rows.loc[(rows.index >= first) & (rows.index <= window[-1])]
        if part["change_only"]:
            # Full-history change_only rows begin with the first reference row,
            # so the latest value per market up to the window start is the dense state.
            state = rows.loc[rows.index <= first].ffill().iloc[-1].fillna(0.0)
            inside = pd.concat([state.to_frame(first).T.astype(float), inside.loc[inside.index > first]])
        owned_rows = inside.index.to_numpy()[:, None] >= part["first_target"].reindex(inside.columns).to_numpy()[None, :]
        frames.append(inside.where(owned_rows))
    union = pd.DatetimeIndex(sorted(set().union(*(frame.index for frame in frames)))) if len(window) else window
    merged = combine_strategy_sleeve_frames(
        [frame.reindex(union) for frame in frames], [part["capital"] for part in parts])
    return merged.reindex(window)


def _validate_strategy_sleeve_run(vectorbt_payload: dict[str, Any], preselection: dict | None) -> None:
    if preselection is not None:
        raise ValueError("strategy_sleeves cannot be combined with preselection")
    if str(vectorbt_payload.get("engine", "vectorbt")).lower() != "numba_futures":
        # Per-cell NaN holds in the merged frame are verified only for this engine.
        raise ValueError("strategy_sleeves currently supports only engine='numba_futures'")


def _strategy_sleeve_metadata(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    owners: Counter[str] = Counter(str(column) for part in parts for column in part["frame"].columns)
    return [{"name": part["name"], "capital": part["capital"], "markets": len(part["frame"].columns),
             "change_only": part["change_only"],
             "shared_markets": sorted(str(c) for c in part["frame"].columns if owners[str(c)] > 1)}
            for part in parts]


def _run_v2_backtest(
    candle_dir: Path,
    source_cache_dir: Path,
    feature_frames: dict[str, pd.DataFrame],
    warning_frame: pd.DataFrame,
    required_feature_markets: list[str] | None,
    universe_payload: dict[str, Any],
    weight_payload: dict[str, Any],
    vectorbt_payload: dict[str, Any],
    backtest_dir: Path,
    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame],
    max_markets: int | None,
    tail_hours: int | None,
    compute_rolling_ir_enabled: bool,
    print_run_summary: bool,
    save_run_artifacts: bool,
    universe_cache: dict[tuple[Any, ...], Any] | None = None,
    universe_cache_key: tuple[Any, ...] | None = None,
    universe_rank_cache: dict[tuple[object, ...], pd.DataFrame] | None = None,
    universe_rank_cache_namespace: tuple[object, ...] | None = None,
    artifact_format: str = DEFAULT_ARTIFACT_FORMAT,
    prepared_cache: dict[str, Any] | None = None,
    preselection: dict | None = None,
) -> tuple[pd.Series, pd.DataFrame]:
    artifact_format = normalize_artifact_format(artifact_format)
    sleeves = load_strategy_sleeves_from_payload(weight_payload)
    reference_index = next(iter(feature_frames.values())).index
    warning_frame = warning_frame.reindex(index=reference_index)
    sleeve_parts = None
    if sleeves is not None:
        _validate_strategy_sleeve_run(vectorbt_payload, preselection)
        universe_spec = load_universe_spec_from_payload(universe_payload)
        weight_spec = None
        sleeve_parts, data_exclusions = _build_strategy_sleeve_parts(
            source_cache_dir, feature_frames, warning_frame, universe_payload, sleeves, vectorbt_payload)
        change_only_weights = False
        sleeve_markets = list(dict.fromkeys(column for part in sleeve_parts for column in part["frame"].columns))
        # One-column activity probe for trimming (first nonzero request of any
        # sleeve); the request frame itself is built after trimming.
        active = np.zeros(len(reference_index), dtype=float)
        for part in sleeve_parts:
            rows = part["frame"]
            nonzero_rows = rows.index[rows.fillna(0.0).ne(0.0).any(axis=1).to_numpy()]
            active[reference_index.get_indexer(nonzero_rows)] = 1.0
        weight_frame = pd.DataFrame({"__sleeve_activity__": active}, index=reference_index)
    else:
        universe_spec = load_universe_spec_from_payload(universe_payload)
        weight_spec = load_weight_spec_from_payload(weight_payload)
        validate_holding_selection_config(universe_spec, weight_spec, vectorbt_payload)
        selection_start = holding_selection_start(vectorbt_payload) if universe_spec.holding is not None else None

        universe_spec, data_exclusions = apply_observed_universe_policy_v2(
            source_cache_dir, reference_index, next(iter(feature_frames.values())).columns, universe_spec, vectorbt_payload,
            required_markets=[weight_spec.hedge.market] if weight_spec.hedge is not None else ())
        if data_exclusions and universe_cache_key is not None:
            universe_cache_key = (*universe_cache_key, ("observed_price_exclusions", tuple(sorted(data_exclusions))))

        universe_result = (
            universe_cache.get(universe_cache_key)
            if universe_cache is not None and universe_cache_key is not None
            else None
        )
        if universe_result is None:
            universe_frames = _preselection_universe_frames(feature_frames, universe_spec, preselection)
            universe_result = build_universe_mask_v2(
                universe_frames,
                warning_frame,
                universe_spec,
                rank_cache=universe_rank_cache,
                rank_cache_namespace=universe_rank_cache_namespace,
                selection_start=selection_start,
            )
            if universe_cache is not None and universe_cache_key is not None:
                universe_cache[universe_cache_key] = universe_result
        weight_frame = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames,
                                           base_eligible_mask=universe_result.base_eligible_mask)
        change_only_weights = is_change_only_weight_output(weight_spec)
        if change_only_weights:
            weight_frame = weight_frame.ffill().fillna(0.0)

    price_column = str(vectorbt_payload.get("price_column", "trade_price"))
    benchmark_price_column = str(vectorbt_payload.get("benchmark_price_column") or price_column)
    benchmark_source_cache_dir = Path(
        str(vectorbt_payload.get("benchmark_source_cache_dir") or source_cache_dir)
    )
    init_cash = float(vectorbt_payload.get("init_cash", 1_000_000.0))
    fees = float(vectorbt_payload.get("fees", 0.0))
    slippage = float(vectorbt_payload.get("slippage", 0.0))
    benchmark_market = str(vectorbt_payload.get("benchmark_market", "KRW-BTC")).upper()
    benchmark_mode = str(vectorbt_payload.get("benchmark_mode", "single_market"))
    benchmark_uses_strategy_price_frame = (
        benchmark_mode == "single_market"
        and benchmark_price_column == price_column
        and benchmark_source_cache_dir.resolve() == source_cache_dir.resolve()
    )
    prune_inactive_price_columns = _resolve_prune_inactive_price_columns(
        vectorbt_payload,
        has_explicit_universe_markets=bool(universe_spec.allowed_markets),
    )
    if sleeve_parts is not None:
        # Sleeves already hold only their owned markets; prices follow that union.
        required_price_market_set = {str(column).upper() for column in sleeve_markets}
    elif prune_inactive_price_columns:
        keep_markets = {weight_spec.hedge.market} if weight_spec.hedge is not None else set()
        weight_frame = _prune_inactive_target_columns(weight_frame, keep_markets=keep_markets)
        required_price_market_set = {str(column).upper() for column in weight_frame.columns}
    elif universe_spec.allowed_markets:
        required_price_market_set = {str(market).upper() for market in universe_spec.allowed_markets}
    else:
        required_price_market_set = set()
    if weight_spec is not None and weight_spec.hedge is not None:
        if prune_inactive_price_columns:
            required_price_market_set.add(weight_spec.hedge.market)
        else:
            required_price_market_set.update(weight_frame.columns)
    if benchmark_uses_strategy_price_frame:
        required_price_market_set.add(benchmark_market)
    for market in (vectorbt_payload.get("benchmark_fixed_weights") or {}).keys():
        required_price_market_set.add(str(market).upper())
    required_price_markets = sorted(required_price_market_set) if required_price_market_set else None

    price_cache_key = (
        str(candle_dir),
        str(source_cache_dir),
        price_column,
        tuple(required_price_markets) if required_price_markets is not None else (),
        max_markets,
    )
    price_frame = price_frame_cache.get(price_cache_key)
    if price_frame is None:
        price_frame = load_price_frame(
            candle_dir,
            price_column,
            load_mode="wide",
            source_cache_dir=source_cache_dir,
            market_columns=required_price_markets,
        )
        if required_price_markets is None and max_markets is not None:
            price_frame = price_frame.reindex(columns=sorted(price_frame.columns)[:max_markets])
        price_frame_cache[price_cache_key] = price_frame
    benchmark_price_frame = None
    if benchmark_mode == "single_market" and not benchmark_uses_strategy_price_frame:
        benchmark_price_markets = [benchmark_market]
        benchmark_price_cache_key = (
            str(candle_dir),
            str(benchmark_source_cache_dir),
            benchmark_price_column,
            tuple(benchmark_price_markets),
            max_markets,
        )
        benchmark_price_frame = price_frame_cache.get(benchmark_price_cache_key)
        if benchmark_price_frame is None:
            benchmark_price_frame = load_price_frame(
                candle_dir,
                benchmark_price_column,
                load_mode="wide",
                source_cache_dir=benchmark_source_cache_dir,
                market_columns=benchmark_price_markets,
            )
            price_frame_cache[benchmark_price_cache_key] = benchmark_price_frame
    trim_start_mode = str(vectorbt_payload.get("trim_start_mode", "none"))
    trim_start_timestamp_value = vectorbt_payload.get("trim_start_timestamp")
    if trim_start_mode == "timestamp":
        if trim_start_timestamp_value is None:
            raise ValueError("trim_start_mode='timestamp' requires vectorbt_spec.trim_start_timestamp")
        trimmed_start_timestamp = pd.Timestamp(str(trim_start_timestamp_value))
        price_frame = price_frame.loc[price_frame.index >= trimmed_start_timestamp]
        weight_frame = weight_frame.loc[weight_frame.index >= trimmed_start_timestamp]
    else:
        # For sleeves the union probe holds every sleeve's raw targets, so the
        # first active row is the earliest first request of any sleeve.
        price_frame, weight_frame, trimmed_start_timestamp = trim_frames_to_first_weight(
            price_frame,
            weight_frame,
            mode=trim_start_mode,
        )
    if change_only_weights:
        weight_frame = to_change_only_target_frame(weight_frame, scope=weight_spec.change_only_scope)
    if sleeve_parts is not None:
        # Every sleeve trims to the same first row before its own change_only conversion.
        weight_frame = _strategy_sleeve_request_frame(
            sleeve_parts, weight_frame.index.min() if len(weight_frame.index) else None)
    if benchmark_price_frame is not None and not price_frame.empty:
        benchmark_price_frame = benchmark_price_frame.loc[
            (benchmark_price_frame.index >= price_frame.index.min())
            & (benchmark_price_frame.index <= price_frame.index.max())
        ]

    timeframe = infer_timeframe(candle_dir, vectorbt_payload.get("timeframe"))
    periods_per_year = resolve_periods_per_year(
        timeframe,
        vectorbt_payload.get("periods_per_year"),
    )
    periods_per_day = periods_per_day_for_timeframe(timeframe)
    pandas_freq = timeframe_to_pandas_freq(timeframe)

    portfolio = run_backtest_v2(
        price_frame=price_frame,
        target_weight_frame=weight_frame,
        spec=VectorBTSpec(
            price_column=price_column,
            init_cash=init_cash,
            fees=fees,
            slippage=slippage,
            freq=pandas_freq,
        ),
        payload=vectorbt_payload,
        source_cache_dir=source_cache_dir,
        timeframe=timeframe,
        feature_frames=feature_frames,
        **({"prepared_cache": prepared_cache}
           if prepared_cache is not None and str(vectorbt_payload.get("engine", "vectorbt")).lower() == "numba_futures"
           else {}),
    )
    record_data_policy_audit_v2(portfolio, data_exclusions)
    if sleeve_parts is not None and isinstance(portfolio, BacktestResultV2) and portfolio.metadata is not None:
        portfolio.metadata["strategy_sleeves"] = _strategy_sleeve_metadata(sleeve_parts)

    summary = portfolio.stats(settings={"freq": pandas_freq})
    summary = record_execution_availability_summary_v2(portfolio, summary)
    if "Benchmark Return [%]" in summary.index:
        summary = summary.rename(index={"Benchmark Return [%]": "VectorBT Benchmark Return [%]"})
    equity_curve = portfolio.value()
    benchmark_curve, benchmark_label = resolve_benchmark_curve(
        source_cache_dir=source_cache_dir,
        benchmark_source_cache_dir=benchmark_source_cache_dir,
        benchmark_price_column=benchmark_price_column,
        price_frame=price_frame,
        portfolio=portfolio,
        init_cash=init_cash,
        benchmark_mode=benchmark_mode,
        benchmark_market=benchmark_market,
        benchmark_price_frame=benchmark_price_frame,
        vectorbt_spec=VectorBTSpec(
            price_column=price_column,
            init_cash=init_cash,
            fees=fees,
            slippage=slippage,
            freq=pandas_freq,
        ),
        benchmark_fixed_weights=vectorbt_payload.get("benchmark_fixed_weights"),
        benchmark_rebalance_frequency=str(vectorbt_payload.get("benchmark_rebalance_frequency", "every_bar")),
        benchmark_listed_normalize=bool(vectorbt_payload.get("benchmark_listed_normalize", False)),
    )
    comparison_equity_curve, aligned_benchmark_curve = build_benchmark_comparison_curves(
        equity_curve,
        benchmark_curve,
        init_cash,
    )
    benchmark_stats = benchmark_summary(
        aligned_benchmark_curve,
        init_cash,
        benchmark_label,
        annualization_factor=periods_per_year,
        benchmark_mode=benchmark_mode,
    )
    comparison_period_stats = benchmark_comparison_period_summary(
        comparison_equity_curve,
        annualization_factor=periods_per_year,
    )
    strategy_returns = compute_return_series(equity_curve)
    comparison_strategy_returns = compute_return_series(comparison_equity_curve)
    benchmark_returns = compute_return_series(aligned_benchmark_curve)
    ir_stats = compute_information_ratio(
        comparison_strategy_returns,
        benchmark_returns,
        annualization_factor=periods_per_year,
    )
    regression_stats = compute_benchmark_regression_stats(
        comparison_strategy_returns,
        benchmark_returns,
        annualization_factor=periods_per_year,
    )
    excess_returns, excess_equity_curve = compute_excess_curves(
        comparison_equity_curve,
        aligned_benchmark_curve,
        init_cash,
    )
    rolling_ir = pd.DataFrame(index=excess_returns.index)
    if compute_rolling_ir_enabled:
        rolling_ir = compute_rolling_information_ratio(
            excess_returns,
            periods_per_day=periods_per_day,
            annualization_factor=periods_per_year,
        )
    recent_1y_stats = compute_recent_1y_stats(
        comparison_equity_curve,
        aligned_benchmark_curve,
        annualization_factor=periods_per_year,
    )
    recent_2y_stats = compute_recent_2y_stats(
        comparison_equity_curve,
        aligned_benchmark_curve,
        annualization_factor=periods_per_year,
    )
    rolling_ir_summary = summarize_rolling_information_ratio(rolling_ir)
    strategy_stats = strategy_performance_summary(
        equity_curve,
        annualization_factor=periods_per_year,
    )
    for metric_name, metric_value in strategy_stats.items():
        summary.loc[metric_name] = metric_value
    summary.loc["Load Mode"] = "wide"
    summary.loc["Weights CSV Format"] = "wide"
    summary.loc["Trim Start Mode"] = trim_start_mode
    if trimmed_start_timestamp is not None:
        summary.loc["Trimmed Start Timestamp"] = trimmed_start_timestamp.isoformat()
    summary.loc["Timeframe"] = timeframe
    summary.loc["Periods Per Year"] = periods_per_year
    summary.loc["Benchmark Price Column"] = benchmark_price_column
    summary.loc["Benchmark Source Cache Dir"] = str(benchmark_source_cache_dir)
    summary = pd.concat(
        [
            summary,
            comparison_period_stats,
            benchmark_stats,
            ir_stats,
            regression_stats,
            recent_1y_stats,
            recent_2y_stats,
            compute_drawdown_recovery_stats(equity_curve),
            rolling_ir_summary,
        ]
    )

    summary.loc["Artifact Format"] = artifact_format
    summary = pd.concat([summary, write_backtest_analysis_v2(
        portfolio, backtest_dir, equity_curve=equity_curve, init_cash=init_cash)])
    if preselection is not None:
        summary.loc["Preselection Key"] = preselection["key"]
        summary.loc["Preselection Manifest"] = preselection["manifest_path"]
        if isinstance(portfolio, BacktestResultV2) and portfolio.metadata is not None:
            portfolio.metadata["preselection"] = {name: preselection[name]
                for name in ("key", "manifest_path", "mask_path", "project")}
    if print_run_summary:
        print_summary(summary)
    if save_run_artifacts:
        backtest_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(portfolio, BacktestResultV2):
            portfolio.save_artifacts(backtest_dir, artifact_format=artifact_format)
        if getattr(portfolio, "execution_availability_audit", None) is not None:
            (backtest_dir / "execution_availability_audit.json").write_text(
                json.dumps(portfolio.execution_availability_audit, ensure_ascii=False, indent=2), encoding="utf-8")
        write_backtest_frame(backtest_dir / "equity_curve.csv", equity_curve, artifact_format=artifact_format)
        write_backtest_frame(backtest_dir / "benchmark_curve.csv", aligned_benchmark_curve, artifact_format=artifact_format)
        write_backtest_frame(backtest_dir / "excess_equity_curve.csv", excess_equity_curve, artifact_format=artifact_format)
        if compute_rolling_ir_enabled:
            write_backtest_frame(backtest_dir / "rolling_information_ratio.csv", rolling_ir, artifact_format=artifact_format)
        # Publish the selected format after its files have been written. This
        # lets readers distinguish current output from older files in the folder.
        write_summary_csv(backtest_dir / "summary.csv", summary)
    return summary, weight_frame


def _run_grid_candidates(config, candle_dir, source_cache_dir, out_dir, combinations, controller=None) -> None:
    """One shared evaluation path for exhaustive and adaptive candidate selection."""
    artifact_format = normalize_artifact_format(config.get("artifact_format", DEFAULT_ARTIFACT_FORMAT))
    if controller is not None and (controller.evaluations >= controller.settings["n_trials"] or not controller.pool):
        controller.stop_reason = "target_reached" if controller.evaluations >= controller.settings["n_trials"] else "exhausted"
        return
    market_score_spec_path = config.get("market_scores_spec_json")
    static_market_score_spec = (
        load_market_score_spec(Path(str(market_score_spec_path)))
        if market_score_spec_path
        else None
    )

    run_name_template = config["run_name_template"]
    compute_rolling_ir_enabled = bool(config.get("compute_rolling_ir", False))
    print_run_summaries = bool(config.get("print_run_summaries", False))
    save_run_artifacts = bool(config.get("save_run_artifacts", False))
    max_markets = config.get("max_markets")
    tail_hours = config.get("tail_hours")
    preselection_plans = _prepare_grid_preselection(config, source_cache_dir, out_dir, combinations)

    feature_use_counts: Counter[str] = Counter()
    universe_use_counts: Counter[str] = Counter()
    rank_use_counts: Counter[str] = Counter()
    for combo in combinations if controller is None else ():
        usage_context = dict(combo)
        usage_context["run_name"] = run_name_template.format(**usage_context)
        usage_feature_payload = (
            _render_value(config["feature_spec_template"], usage_context)
            if config.get("feature_spec_template") is not None
            else None
        )
        if usage_feature_payload is not None:
            feature_use_counts[_canonical_payload_key(usage_feature_payload)] += 1
        usage_universe_payload = _render_value(
            config["universe_spec_template"], usage_context
        )
        usage_market_score_payload = (
            _render_value(config["market_scores_spec_template"], usage_context)
            if config.get("market_scores_spec_template") is not None
            else None
        )
        universe_use_counts[
            _dynamic_universe_usage_key(
                feature_payload=usage_feature_payload,
                universe_payload=usage_universe_payload,
                market_score_payload=usage_market_score_payload,
                market_score_spec_path=market_score_spec_path,
            )
        ] += 1
        rank_use_counts[
            _dynamic_rank_usage_key(
                feature_payload=usage_feature_payload,
                market_score_payload=usage_market_score_payload,
                market_score_spec_path=market_score_spec_path,
            )
        ] += 1

    feature_cache: dict[tuple[str, tuple[str, ...], str], dict[str, pd.DataFrame]] = {}
    node_frame_cache = FeatureFrameCache()
    source_frame_cache: dict[tuple[tuple[str, ...], tuple[str, ...], int | None], dict[str, pd.DataFrame]] = {}
    price_frame_cache: dict[tuple[str, str, str, tuple[str, ...], int | None], pd.DataFrame] = {}
    prepared_cache: dict[str, Any] = {}
    warning_frame_cache: dict[tuple[tuple[str, ...], int | None], pd.DataFrame] = {}
    universe_cache: dict[tuple[Any, ...], Any] = {}
    universe_rank_cache: dict[tuple[object, ...], pd.DataFrame] = {}
    universe_cache_keys_by_usage: dict[str, set[tuple[Any, ...]]] = {}
    universe_rank_namespaces_by_usage: dict[str, set[tuple[object, ...]]] = {}
    results: list[dict[str, Any]] = list(controller.rows) if controller is not None else []

    shared_feature_payload = config.get("shared_feature_spec_template")
    feature_builder = CandidateFeatureBuilder(
        config,
        combinations,
        source_cache_dir,
        static_market_score_spec=static_market_score_spec,
        preselection_plans=preselection_plans,
        node_frame_cache=node_frame_cache,
        source_frame_cache=source_frame_cache,
    )
    if feature_builder.has_shared:
        # Prepare the first candidate's actual policy immediately. An unused
        # default-history copy of every shared feature can consume gigabytes.
        first_context = dict(combinations[0]) if combinations else {}
        first_context["run_name"] = run_name_template.format(**first_context)
        feature_builder.shared_variant(
            funding_usage_starts_v2(source_cache_dir, _render_backtest_payload(config, first_context)))

    total_runs = len(combinations) if controller is None else controller.settings["n_trials"]
    candidate_source = combinations if controller is None else controller

    for run_index, combo in enumerate(candidate_source, start=1):
        context = dict(combo)
        run_name = run_name_template.format(**context)
        context["run_name"] = run_name
        print(f"[{run_index}/{total_runs}] {run_name}")

        run_dir = out_dir / run_name
        specs_dir = run_dir / "specs"
        backtest_dir = run_dir / "backtest"
        if save_run_artifacts:
            specs_dir.mkdir(parents=True, exist_ok=True)
            backtest_dir.mkdir(parents=True, exist_ok=True)

        universe_payload = _render_value(config["universe_spec_template"], context)
        preselection = None if preselection_plans is None else preselection_plans[_canonical_payload_key(combo)]
        universe_payload = _preselected_universe_payload(universe_payload, preselection)
        weight_payload = _render_value(config["weight_spec_template"], context)
        vectorbt_payload = _render_backtest_payload(config, context)
        data_usage_starts = funding_usage_starts_v2(source_cache_dir, vectorbt_payload)
        data_usage_key = tuple(sorted(data_usage_starts.items()))
        market_score_payload = (
            _render_value(config["market_scores_spec_template"], context)
            if config.get("market_scores_spec_template") is not None
            else None
        )
        weight_market_score_payload = (
            _render_value(config["weight_market_scores_spec_template"], context)
            if config.get("weight_market_scores_spec_template") is not None
            else None
        )
        market_score_spec = (
            load_market_score_spec_from_payload(market_score_payload)
            if market_score_payload is not None
            else static_market_score_spec
        )
        weight_market_score_spec = (
            load_market_score_spec_from_payload(weight_market_score_payload)
            if weight_market_score_payload is not None
            else None
        )
        feature_payload = (
            _render_value(config["feature_spec_template"], context)
            if config.get("feature_spec_template") is not None
            else None
        )
        feature_payload_key = (
            _canonical_payload_key(feature_payload)
            if feature_payload is not None
            else ""
        )
        universe_usage_key = _dynamic_universe_usage_key(
            feature_payload=feature_payload,
            universe_payload=universe_payload,
            market_score_payload=market_score_payload,
            market_score_spec_path=market_score_spec_path,
        )
        rank_usage_key = _dynamic_rank_usage_key(
            feature_payload=feature_payload,
            market_score_payload=market_score_payload,
            market_score_spec_path=market_score_spec_path,
        )

        if save_run_artifacts and feature_payload is not None:
            (specs_dir / "features.json").write_text(
                json.dumps(feature_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if save_run_artifacts:
            (specs_dir / "backtest.json").write_text(
                json.dumps(vectorbt_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (specs_dir / "universe.json").write_text(
                json.dumps(universe_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (specs_dir / "weights.json").write_text(
                json.dumps(weight_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if preselection is not None:
                (specs_dir / "preselection.json").write_text(json.dumps(
                    {name: preselection[name] for name in ("key", "manifest_path", "mask_path", "project")},
                    ensure_ascii=False, indent=2), encoding="utf-8")

        result_row: dict[str, Any] = {"run_name": run_name, **combo}
        if preselection is not None:
            result_row.update(preselection_key=preselection["key"],
                              preselection_union_markets=len(preselection["audit"]["selected_markets"]),
                              preselection_candidate_markets=preselection["audit"]["candidate_markets"],
                              preselection_projection_enabled=preselection["project"])
        feature_key = None
        feature_frames = None
        per_run_frames = None
        per_run_source_frames = None
        universe_cache_lookup_key = None
        universe_rank_cache_namespace = None
        weight_frame = None
        fatal_error = None
        try:
            required_feature_markets = None
            output_feature_markets = _resolve_output_feature_markets(universe_payload)
            if feature_builder.has_shared:
                feature_frames, required_feature_markets = feature_builder.candidate(
                    universe_payload=universe_payload,
                    feature_payload=feature_payload,
                    market_score_payload=market_score_payload,
                    market_score_spec=market_score_spec,
                    weight_market_score_spec=weight_market_score_spec,
                    data_usage_starts=data_usage_starts,
                )
                primary_frame = next(iter(feature_frames.values()))
                result_row["feature_rows"] = int(primary_frame.notna().sum().sum())
                result_row["features_source"] = "shared"
            else:
                feature_specs = load_feature_specs_from_payload(feature_payload)
                required_feature_markets = _resolve_required_feature_markets(
                    feature_specs,
                    universe_payload,
                    market_score_spec,
                )
                feature_key = (
                    data_usage_key,
                    _canonical_payload_key(feature_payload),
                    tuple(required_feature_markets) if required_feature_markets is not None else (),
                    tuple(output_feature_markets) if output_feature_markets is not None else (),
                    _canonical_payload_key(market_score_payload) if market_score_payload is not None else (
                        str(Path(str(market_score_spec_path)).resolve()) if market_score_spec_path else ""
                    ),
                    _canonical_payload_key(weight_market_score_payload) if weight_market_score_payload is not None else "",
                )
                feature_frames = feature_cache.get(feature_key)
                if feature_frames is None:
                    required_columns, uses_market_source = required_source_columns_for_feature_specs(
                        feature_specs,
                        SUPPORTED_SOURCE_COLUMNS,
                    )
                    source_cache_key = (
                        tuple(sorted(required_columns or {"trade_price"})),
                        tuple(required_feature_markets) if required_feature_markets is not None else (),
                        None if (uses_market_source or required_feature_markets is not None) else max_markets,
                    )
                    source_frames = source_frame_cache.get(source_cache_key)
                    if source_frames is None:
                        source_frames = read_wide_frames_from_cache(
                            source_cache_dir,
                            list(source_cache_key[0]),
                            market_columns=required_feature_markets,
                            max_markets=source_cache_key[2],
                        )
                        source_frame_cache[source_cache_key] = source_frames
                    feature_frames = build_feature_frames_from_cache(
                        source_cache_dir,
                        feature_specs,
                        market_columns=required_feature_markets,
                        output_market_columns=output_feature_markets,
                        max_markets=None if required_feature_markets is not None else max_markets,
                        tail_rows=tail_hours,
                        source_frames=source_frames,
                        data_usage_starts=data_usage_starts,
                        frame_cache=node_frame_cache,
                        frame_cache_namespace=(
                            "grid_v2",
                            "per_run",
                            tuple(required_feature_markets or ()),
                            tuple(output_feature_markets or ()),
                            tail_hours,
                        ),
                    )
                    if market_score_spec is not None:
                        feature_frames = dict(feature_frames)
                        feature_frames[market_score_spec.output_column] = build_market_score_frame(feature_frames, market_score_spec)
                    if weight_market_score_spec is not None:
                        feature_frames = dict(feature_frames)
                        feature_frames[weight_market_score_spec.output_column] = build_market_score_frame(
                            feature_frames,
                            weight_market_score_spec,
                        )
                    feature_cache[feature_key] = feature_frames
                primary_frame = next(iter(feature_frames.values()))
                result_row["feature_rows"] = int(primary_frame.notna().sum().sum())
                result_row["features_source"] = "per_run"

            warning_cache_key = (
                tuple(required_feature_markets) if required_feature_markets is not None else (),
                None if required_feature_markets is not None else max_markets,
                bool(load_universe_spec_from_payload(universe_payload).exclude_warnings),
            )
            warning_frame = warning_frame_cache.get(warning_cache_key)
            if warning_frame is None:
                warning_frame = _read_warning_frame(
                    source_cache_dir,
                    market_columns=required_feature_markets,
                    max_markets=None if required_feature_markets is not None else max_markets,
                    require_warning=warning_cache_key[2],
                )
                warning_frame_cache[warning_cache_key] = warning_frame

            feature_identities = _feature_identity_map_from_payloads(
                [
                    shared_feature_payload,
                    feature_payload,
                ]
            )
            if data_usage_starts:
                feature_identities["__funding_usage_starts__"] = _canonical_payload_key(data_usage_starts)
            if preselection is not None:
                feature_identities["__preselection__"] = preselection["key"]
            if market_score_spec is not None:
                market_score_key = (
                    _canonical_payload_key(market_score_payload)
                    if market_score_payload is not None
                    else (
                        f"path:{Path(str(market_score_spec_path)).resolve()}"
                        if market_score_spec_path
                        else "static_market_score"
                    )
                )
                feature_identities[market_score_spec.output_column] = market_score_key
            if weight_market_score_spec is not None:
                feature_identities[weight_market_score_spec.output_column] = _canonical_payload_key(
                    weight_market_score_payload
                )
            reference_index = next(iter(feature_frames.values())).index
            universe_cache_lookup_key = _universe_cache_key(
                universe_payload,
                feature_identities,
                warning_cache_key,
                reference_index,
                backtest_payload=vectorbt_payload,
            )
            universe_rank_cache_namespace = (
                "grid_v2_universe_rank",
                str(source_cache_dir.resolve()),
                tuple(sorted(feature_identities.items())),
                warning_cache_key,
                _reference_index_key(reference_index),
            )
            universe_cache_keys_by_usage.setdefault(universe_usage_key, set()).add(
                universe_cache_lookup_key
            )
            universe_rank_namespaces_by_usage.setdefault(
                rank_usage_key, set()
            ).add(universe_rank_cache_namespace)

            summary, weight_frame = _run_v2_backtest(
                candle_dir,
                source_cache_dir,
                feature_frames,
                warning_frame,
                required_feature_markets,
                universe_payload,
                weight_payload,
                vectorbt_payload,
                backtest_dir,
                price_frame_cache,
                max_markets=max_markets,
                tail_hours=tail_hours,
                compute_rolling_ir_enabled=compute_rolling_ir_enabled,
                print_run_summary=print_run_summaries,
                save_run_artifacts=save_run_artifacts,
                artifact_format=artifact_format,
                universe_cache=universe_cache,
                universe_cache_key=universe_cache_lookup_key,
                universe_rank_cache=universe_rank_cache,
                universe_rank_cache_namespace=universe_rank_cache_namespace,
                prepared_cache=prepared_cache,
                **({"preselection": preselection} if preselection is not None else {}),
            )
            result_row.update(_selection_stats(weight_frame))
            result_row["status"] = "ok"
            result_row.update(summary.to_dict())
            print(f"[{run_index}/{total_runs}] ok: {run_name}")
        except Exception as exc:
            result_row["status"] = "bankrupt" if controller is not None and isinstance(exc, FuturesBankruptcyError) else "error"
            result_row["error"] = str(exc)
            print(f"[{run_index}/{total_runs}] error: {run_name} :: {exc}")
            if controller is not None and not isinstance(exc, FuturesBankruptcyError):
                fatal_error = exc
        finally:
            if feature_payload_key:
                feature_use_counts[feature_payload_key] -= 1
                if controller is not None or feature_use_counts[feature_payload_key] <= 0:
                    feature_use_counts.pop(feature_payload_key, None)
                    # Transform reuse is independent of the full candidate
                    # payload and bounded by the node cache's LRU budget.
                    stale_feature_keys = [
                        key
                        for key in feature_cache
                        if isinstance(key, tuple)
                        and key
                        and key[0] == feature_payload_key
                    ]
                    for key in stale_feature_keys:
                        feature_cache.pop(key, None)

            universe_use_counts[universe_usage_key] -= 1
            if controller is not None or universe_use_counts[universe_usage_key] <= 0:
                universe_use_counts.pop(universe_usage_key, None)
                for cache_key in universe_cache_keys_by_usage.pop(
                    universe_usage_key, set()
                ):
                    universe_cache.pop(cache_key, None)

            rank_use_counts[rank_usage_key] -= 1
            if controller is not None or rank_use_counts[rank_usage_key] <= 0:
                rank_use_counts.pop(rank_usage_key, None)
                for namespace in universe_rank_namespaces_by_usage.pop(
                    rank_usage_key, set()
                ):
                    _drop_rank_cache_by_namespace(
                        universe_rank_cache,
                        namespace,
                    )

            feature_frames = None
            per_run_frames = None
            per_run_source_frames = None
            weight_frame = None
        results.append(result_row)
        if controller is not None:
            controller.record(result_row)
        _write_grid_results(results, out_dir, config.get("grid", {}) if controller is None else None)
        if fatal_error is not None:
            raise fatal_error

    print(f"Wrote {len(results)} grid result rows to {out_dir / 'summary_results.csv'}")


def _write_grid_results(results, out_dir, grid=None):
    if not results:
        return
    result_frame = pd.DataFrame(results)
    if grid is not None:
        result_frame = _attach_plateau_air_mean(result_frame, grid)
    temporary = out_dir / "summary_results.csv.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result_frame.columns))
        writer.writeheader()
        writer.writerows(result_frame.to_dict(orient="records"))
    replace_file(temporary, out_dir / "summary_results.csv")


def _grid_input_paths(config, combinations):
    paths = set()
    templates = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"candle_dir", "daily_dir", "source_cache_dir", "benchmark_source_cache_dir",
                           "market_scores_spec_json", "contract_specs_json"} and isinstance(item, str):
                    templates.add(item)
                elif config.get("preselection") is not None and key in {"allowed_markets_file", "excluded_markets_file"} and isinstance(item, str):
                    path = Path(item)
                    templates.add(str(path if path.is_absolute() else ROOT_DIR / path))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(config)
    for template in templates:
        if "{" not in template:
            paths.add(Path(template))
            continue
        for combo in combinations:
            context = {**combo, "run_name": config["run_name_template"].format(**combo)}
            paths.add(Path(_render_value(template, context)))
    return paths


def _run_grid_config(config, adaptive=False):
    candle_dir_value = config.get("candle_dir") or config.get("daily_dir")
    if not candle_dir_value:
        raise SystemExit("config must define candle_dir or daily_dir")
    candle_dir = Path(str(candle_dir_value))
    out_dir = Path(config.get("out_dir", "data/grid/cross_section_v2"))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not adaptive and (out_dir / "search_manifest.json").exists():
        raise ValueError("out_dir contains an adaptive search; use a new out_dir for the full grid")
    combinations = [combo for combo in _grid_combinations(config.get("grid", {}))
                    if _passes_constraints(combo, config.get("constraints", []))]
    if not combinations:
        raise SystemExit("No valid grid combinations after applying constraints")
    if adaptive:
        combinations = list({_canonical_payload_key(combo): combo for combo in combinations}.values())
        names = [config["run_name_template"].format(**combo) for combo in combinations]
        if len(names) != len(set(names)):
            raise ValueError("run_name_template must uniquely identify each valid grid combination")
    source_cache_dir = _resolve_source_cache_dir(config, candle_dir, out_dir)
    if not adaptive:
        return _run_grid_candidates(config, candle_dir, source_cache_dir, out_dir, combinations)
    from lib.strategy_search_v2.samplers import open_grid_search
    with open_grid_search(config, combinations, source_cache_dir, out_dir,
                          input_paths=_grid_input_paths(config, combinations)) as controller:
        try:
            _run_grid_candidates(config, candle_dir, source_cache_dir, out_dir, combinations, controller)
        finally:
            # SQLite is authoritative, including a result saved just before CSV export failed.
            rows = [t.user_attrs["result_row"] for t in controller.study.get_trials(deepcopy=False)
                    if "result_row" in t.user_attrs]
            _write_grid_results(rows, out_dir)
    print(f"Search stopped: evaluations={controller.evaluations}/{controller.settings['n_trials']}; "
          f"reason={controller.stop_reason}")


def main() -> None:
    args = build_parser().parse_args()
    config = json.loads(Path(args.config_json).read_text(encoding="utf-8-sig"))
    adaptive = False
    if "search" in config:
        from lib.strategy_search_v2.samplers import exclusive_run, grid_search_settings
        adaptive = grid_search_settings(config)["sampler"] != "grid"
    if adaptive:
        out_dir = Path(config.get("out_dir", "data/grid/cross_section_v2"))
        with exclusive_run(out_dir):
            _run_grid_config(config, adaptive=True)
    else:
        _run_grid_config(config)


if __name__ == "__main__":
    main()
