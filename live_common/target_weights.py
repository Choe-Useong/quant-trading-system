#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import read_wide_frames_from_cache
from lib.feature_graph_v2 import (
    referenced_markets_for_feature_specs,
    required_source_columns_for_feature_specs,
)
from lib.features_v2 import SUPPORTED_SOURCE_COLUMNS, build_feature_frames_from_cache
from lib.spec_io import (
    load_feature_specs_from_payload,
    load_universe_spec_from_payload,
    load_weight_spec_from_payload,
)
from lib.universe_v2 import build_universe_mask_v2
from lib.weights_v2 import build_weight_frame_v2


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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def _strategy_feature_tail_rows(strategy: dict[str, Any]) -> int | None:
    value = strategy.get("feature_tail_rows")
    if value is None or value == "":
        return None
    tail_rows = int(value)
    return tail_rows if tail_rows > 0 else None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Cannot parse boolean value: {value!r}")


def live_target_extension_options(strategy: dict[str, Any]) -> dict[str, Any]:
    return {
        "extend_to_as_of": _coerce_bool(strategy.get("live_extend_to_as_of"), default=False),
        "extension_max_stale_days": int(strategy.get("live_extend_max_stale_days", 7)),
        "extension_weekdays_only": _coerce_bool(strategy.get("live_extend_weekdays_only"), default=True),
    }


def _resolve_required_feature_markets(feature_specs, universe_payload: dict[str, Any]) -> list[str] | None:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    explicit_markets = {str(market).upper() for market in universe_spec.allowed_markets}
    referenced_markets = referenced_markets_for_feature_specs(feature_specs)
    if explicit_markets:
        return sorted(explicit_markets | referenced_markets)
    return None


def _resolve_output_feature_markets(universe_payload: dict[str, Any]) -> list[str] | None:
    universe_spec = load_universe_spec_from_payload(universe_payload)
    if not universe_spec.allowed_markets:
        return None
    return sorted(str(market).upper() for market in universe_spec.allowed_markets)


def _extension_target_timestamp(as_of: str) -> pd.Timestamp:
    if as_of:
        return pd.Timestamp(as_of).normalize()
    return pd.Timestamp.now().normalize()


def _extend_source_frames_to_as_of(
    source_frames: dict[str, pd.DataFrame],
    *,
    as_of: str,
    max_stale_days: int,
    weekdays_only: bool,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    target_timestamp = _extension_target_timestamp(as_of)
    metadata: dict[str, Any] = {
        "enabled": True,
        "target_timestamp": target_timestamp.isoformat(),
        "extended": False,
        "reason": "",
        "source_cache_last_timestamp": None,
        "max_stale_days": int(max_stale_days),
        "weekdays_only": bool(weekdays_only),
    }
    if weekdays_only and target_timestamp.weekday() >= 5:
        metadata["reason"] = "target_date_is_weekend"
        return {name: frame.copy() for name, frame in source_frames.items()}, metadata

    if max_stale_days < 0:
        raise ValueError("live_extend_max_stale_days must be non-negative")

    extended_frames: dict[str, pd.DataFrame] = {}
    source_last_timestamp: pd.Timestamp | None = None
    extended = False
    for name, frame in source_frames.items():
        if frame.empty:
            extended_frames[name] = frame.copy()
            continue
        sorted_frame = frame.copy()
        sorted_frame.index = pd.to_datetime(sorted_frame.index, utc=False)
        sorted_frame = sorted_frame.sort_index()
        last_timestamp = pd.Timestamp(sorted_frame.index[-1]).normalize()
        source_last_timestamp = (
            last_timestamp
            if source_last_timestamp is None
            else max(source_last_timestamp, last_timestamp)
        )
        if target_timestamp <= last_timestamp:
            extended_frames[name] = sorted_frame
            continue

        stale_days = int((target_timestamp - last_timestamp).days)
        if stale_days > max_stale_days:
            raise ValueError(
                f"Cannot extend live target source frame '{name}' from {last_timestamp.date()} "
                f"to {target_timestamp.date()}: stale_days={stale_days} > max_stale_days={max_stale_days}"
            )

        extension = pd.DataFrame([sorted_frame.iloc[-1]], index=[target_timestamp], columns=sorted_frame.columns)
        extended_frame = pd.concat([sorted_frame, extension], axis=0)
        extended_frame = extended_frame[~extended_frame.index.duplicated(keep="last")].sort_index()
        extended_frames[name] = extended_frame
        extended = True

    metadata["extended"] = extended
    metadata["reason"] = "extended_to_as_of" if extended else "target_timestamp_already_available"
    if source_last_timestamp is not None:
        metadata["source_cache_last_timestamp"] = source_last_timestamp.isoformat()
    return extended_frames, metadata


def _feature_payload_from_config(config: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    if config.get("shared_feature_spec_template") is not None:
        payload.extend(_render_value(config["shared_feature_spec_template"], context))
    if config.get("feature_spec_template") is not None:
        payload.extend(_render_value(config["feature_spec_template"], context))
    if not payload:
        raise ValueError("Config must define feature_spec_template or shared_feature_spec_template")
    return payload


def _market_to_order_symbol(market: str) -> str:
    market = str(market).upper()
    if market.endswith("-KRW"):
        return market[:-4]
    return market


def _latest_row_at_or_before(frame: pd.DataFrame, as_of: str) -> tuple[pd.Timestamp, pd.Series]:
    if frame.empty:
        raise ValueError("Weight frame is empty")
    dense = frame.ffill().fillna(0.0)
    if as_of:
        timestamp = pd.Timestamp(as_of)
        eligible_index = dense.index[pd.to_datetime(dense.index) <= timestamp]
        if len(eligible_index) == 0:
            raise ValueError(f"No target weights available at or before as-of timestamp: {as_of}")
        target_timestamp = pd.Timestamp(eligible_index[-1])
    else:
        target_timestamp = pd.Timestamp(dense.index[-1])
    return target_timestamp, dense.loc[target_timestamp]


def build_latest_target_weights(
    *,
    config_json: Path,
    context: dict[str, Any],
    source_cache_dir_override: Path | None = None,
    as_of: str = "",
    feature_tail_rows: int | None = None,
    extend_to_as_of: bool = False,
    extension_max_stale_days: int = 7,
    extension_weekdays_only: bool = True,
) -> dict[str, Any]:
    config = _read_json(config_json)
    run_name = config["run_name_template"].format(**context)
    context = {**context, "run_name": run_name}

    feature_payload = _feature_payload_from_config(config, context)
    universe_payload = _render_value(config["universe_spec_template"], context)
    weight_payload = _render_value(config["weight_spec_template"], context)

    feature_specs = load_feature_specs_from_payload(feature_payload)
    load_universe_spec_from_payload(universe_payload)
    weight_spec = load_weight_spec_from_payload(weight_payload)

    source_cache_dir = source_cache_dir_override or _resolve_path(str(config["source_cache_dir"]))
    required_markets = _resolve_required_feature_markets(feature_specs, universe_payload)
    output_markets = _resolve_output_feature_markets(universe_payload)
    source_frames = None
    extension_metadata: dict[str, Any] = {"enabled": False, "extended": False}
    if extend_to_as_of:
        required_source_columns, _uses_market_source = required_source_columns_for_feature_specs(
            feature_specs,
            SUPPORTED_SOURCE_COLUMNS,
        )
        if not required_source_columns:
            required_source_columns = {"trade_price"}
        source_frames = read_wide_frames_from_cache(
            source_cache_dir,
            sorted(required_source_columns),
            market_columns=required_markets,
        )
        source_frames, extension_metadata = _extend_source_frames_to_as_of(
            source_frames,
            as_of=as_of,
            max_stale_days=extension_max_stale_days,
            weekdays_only=extension_weekdays_only,
        )
    feature_frames = build_feature_frames_from_cache(
        source_cache_dir,
        feature_specs,
        market_columns=required_markets,
        output_market_columns=output_markets,
        tail_rows=feature_tail_rows,
        source_frames=source_frames,
    )
    universe_result = build_universe_mask_v2(feature_frames, pd.DataFrame(), load_universe_spec_from_payload(universe_payload))
    sparse_weights = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames)
    target_timestamp, target_row = _latest_row_at_or_before(sparse_weights, as_of)

    weights = []
    for market, weight in target_row[target_row > 0.0].sort_values(ascending=False).items():
        weights.append(
            {
                "market": str(market),
                "symbol": _market_to_order_symbol(str(market)),
                "target_weight": float(weight),
            }
        )

    latest_feature_timestamp = max(pd.Timestamp(frame.index[-1]) for frame in feature_frames.values() if not frame.empty)
    last_change_timestamp = None
    target_is_signal_change = False
    changed = sparse_weights.loc[pd.to_datetime(sparse_weights.index) <= target_timestamp].dropna(how="all")
    if not changed.empty:
        last_change = pd.Timestamp(changed.index[-1])
        last_change_timestamp = last_change.isoformat()
        target_is_signal_change = last_change == target_timestamp

    return {
        "strategy": "live_target_weights_v1",
        "run_name": run_name,
        "config_json": str(config_json.relative_to(ROOT_DIR) if config_json.is_relative_to(ROOT_DIR) else config_json),
        "source_cache_dir": str(source_cache_dir.relative_to(ROOT_DIR) if source_cache_dir.is_relative_to(ROOT_DIR) else source_cache_dir),
        "feature_tail_rows": feature_tail_rows,
        "context": context,
        "as_of": as_of or "latest",
        "target_timestamp": target_timestamp.isoformat(),
        "latest_feature_timestamp": latest_feature_timestamp.isoformat(),
        "source_cache_last_timestamp": extension_metadata.get("source_cache_last_timestamp"),
        "live_target_extension": extension_metadata,
        "extended_target_row": bool(extension_metadata.get("extended", False)),
        "last_change_timestamp": last_change_timestamp,
        "target_is_signal_change": target_is_signal_change,
        "gross_target_weight": float(sum(item["target_weight"] for item in weights)),
        "weights": weights,
    }


def print_target_weights_table(payload: dict[str, Any]) -> None:
    print(f"run_name: {payload['run_name']}")
    print(f"target_timestamp: {payload['target_timestamp']}")
    print(f"latest_feature_timestamp: {payload['latest_feature_timestamp']}")
    print(f"last_change_timestamp: {payload['last_change_timestamp']}")
    print(f"gross_target_weight: {payload['gross_target_weight']:.6f}")
    print()
    if not payload["weights"]:
        print("No active target weights.")
        return
    print("market      symbol  target_weight")
    for item in payload["weights"]:
        print(f"{item['market']:<11} {item['symbol']:<7} {item['target_weight']:.8f}")
