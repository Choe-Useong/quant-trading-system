from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from lib.specs import (
    BreadthSpec,
    CompareSpec,
    DefensiveSleeveSpec,
    FeatureSpec,
    FilterStageSpec,
    LogicalSpec,
    MarketScoreComponentSpec,
    MarketScoreRuleSpec,
    MarketScoreSpec,
    PortfolioVolTargetSpec,
    RankFilterSpec,
    ScoreComponentSpec,
    StateSpec,
    TransformSpec,
    UniverseSpec,
    ValueFilterSpec,
    WeightSpec,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FEATURE_PRESET_PATH = ROOT_DIR / "configs" / "presets" / "features_v2_presets.json"


def _render_template_value(template, context: dict[str, object]):
    if isinstance(template, dict):
        return {key: _render_template_value(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_template_value(value, context) for value in template]
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template.count("{") == 1 and template.count("}") == 1:
            key = template[1:-1]
            if key in context:
                return context[key]
        return template.format(**context)
    return template


@lru_cache(maxsize=1)
def _load_feature_preset_catalog() -> dict[str, dict]:
    if not DEFAULT_FEATURE_PRESET_PATH.exists():
        return {}
    payload = json.loads(DEFAULT_FEATURE_PRESET_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Feature preset catalog must be a JSON object")
    return payload


def _expand_feature_preset_item(item: dict) -> dict:
    preset_name = item.get("preset")
    if not preset_name:
        return item
    catalog = _load_feature_preset_catalog()
    if preset_name not in catalog:
        raise ValueError(f"Unknown feature preset: {preset_name}")
    preset_template = copy.deepcopy(catalog[preset_name])
    params = item.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"Feature preset params must be an object: {preset_name}")
    expanded = _render_template_value(preset_template, params)
    if not isinstance(expanded, dict):
        raise ValueError(f"Expanded feature preset must be an object: {preset_name}")
    result = dict(expanded)
    if "default_column_name" in result and "column_name" not in item:
        result["column_name"] = result.pop("default_column_name")
    else:
        result.pop("default_column_name", None)
    for key, value in item.items():
        if key in {"preset", "params"}:
            continue
        result[key] = value
    return result


def load_feature_specs_from_payload(payload: list[dict]) -> list[FeatureSpec]:
    specs: list[FeatureSpec] = []
    for raw_item in payload:
        item = _expand_feature_preset_item(raw_item)
        steps = tuple(
            TransformSpec(kind=step["kind"], params=step.get("params", {}))
            for step in item.get("steps", [])
        )
        components = tuple(
            ScoreComponentSpec(
                feature_column=component["feature_column"],
                weight=float(component.get("weight", 1.0)),
            )
            for component in item.get("components", [])
        )
        compare = None
        if "compare" in item:
            compare_payload = item["compare"]
            compare = CompareSpec(
                left_feature=compare_payload["left_feature"],
                operator=compare_payload["operator"],
                right_feature=compare_payload.get("right_feature"),
                right_value=(
                    None
                    if compare_payload.get("right_value") is None
                    else float(compare_payload["right_value"])
                ),
            )
        logical = None
        if "logical" in item:
            logical_payload = item["logical"]
            logical = LogicalSpec(
                operator=logical_payload["operator"],
                features=tuple(logical_payload["features"]),
            )
        state = None
        if "state" in item:
            state_payload = item["state"]
            state = StateSpec(
                entry_feature=state_payload["entry_feature"],
                exit_feature=state_payload["exit_feature"],
                cooldown_bars=int(state_payload.get("cooldown_bars", 0)),
            )
        breadth = None
        if "breadth" in item:
            breadth_payload = item["breadth"]
            breadth = BreadthSpec(
                driver_feature=breadth_payload["driver_feature"],
                signal_feature=breadth_payload["signal_feature"],
                mode=breadth_payload.get("mode", "top_n"),
                top_n=int(breadth_payload.get("top_n", 4)),
                quantiles=int(breadth_payload.get("quantiles", 4)),
                bucket_values=tuple(int(value) for value in breadth_payload.get("bucket_values", [1])),
                ascending=bool(breadth_payload.get("ascending", False)),
            )
        specs.append(
            FeatureSpec(
                source=item.get("source"),
                steps=steps,
                components=components,
                combine=item.get("combine"),
                compare=compare,
                logical=logical,
                state=state,
                breadth=breadth,
                column_name=item.get("column_name"),
            )
        )
    return specs


def load_feature_specs(path: Path) -> list[FeatureSpec]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return load_feature_specs_from_payload(payload)


def _resolve_repo_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def _load_markets_file(path_value: str | Path) -> tuple[str, ...]:
    path = _resolve_repo_path(path_value)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        markets = payload
    elif isinstance(payload, dict):
        markets = payload.get("markets")
        if markets is None:
            raise ValueError(f"Universe markets file must contain a 'markets' array: {path}")
    else:
        raise ValueError(f"Universe markets file must be a JSON object or array: {path}")
    if not isinstance(markets, list):
        raise ValueError(f"Universe markets file 'markets' must be an array: {path}")
    return tuple(str(market).upper() for market in markets if str(market).strip())


def load_universe_spec_from_payload(payload: dict) -> UniverseSpec:
    def _load_rank_filter_spec(item: dict) -> RankFilterSpec:
        return RankFilterSpec(
            feature_column=item["feature_column"],
            mode=item.get("mode", "top_n"),
            lag=item.get("lag", 0),
            top_n=item.get("top_n", 30),
            quantiles=item.get("quantiles", 5),
            bucket_values=tuple(item.get("bucket_values", [1])),
            ascending=item.get("ascending", False),
            scope=item.get("scope", "filtered"),
        )

    allowed_markets = tuple(str(market).upper() for market in payload.get("allowed_markets", []))
    if payload.get("allowed_markets_file"):
        file_markets = _load_markets_file(payload["allowed_markets_file"])
        allowed_markets = tuple(dict.fromkeys((*allowed_markets, *file_markets)))
    excluded_markets = tuple(str(market).upper() for market in payload.get("excluded_markets", []))
    if payload.get("excluded_markets_file"):
        file_markets = _load_markets_file(payload["excluded_markets_file"])
        excluded_markets = tuple(dict.fromkeys((*excluded_markets, *file_markets)))

    return UniverseSpec(
        feature_column=payload["feature_column"],
        sort_column=payload.get("sort_column"),
        lag=payload.get("lag", 1),
        signal_lag=payload.get("signal_lag", 0),
        start_min_cross_section_size=payload.get("start_min_cross_section_size", 0),
        mode=payload.get("mode", "top_n"),
        top_n=payload.get("top_n", 30),
        quantiles=payload.get("quantiles", 5),
        bucket_values=tuple(payload.get("bucket_values", [1])),
        ascending=payload.get("ascending", False),
        scope=payload.get("scope", "filtered"),
        exclude_warnings=payload.get("exclude_warnings", False),
        min_age_days=payload.get("min_age_days"),
        allowed_markets=allowed_markets,
        excluded_markets=excluded_markets,
        value_filters=tuple(
            ValueFilterSpec(
                feature_column=item["feature_column"],
                operator=item["operator"],
                value=float(item["value"]),
                lag=item.get("lag", 0),
            )
            for item in payload.get("value_filters", [])
        ),
        rank_filters=tuple(_load_rank_filter_spec(item) for item in payload.get("rank_filters", [])),
        filter_stages=tuple(
            FilterStageSpec(
                mode=item.get("mode", "sequential"),
                filters=tuple(_load_rank_filter_spec(filter_item) for filter_item in item.get("filters", [])),
            )
            for item in payload.get("filter_stages", [])
        ),
        name=payload.get("name"),
    )


def load_universe_spec(path: Path) -> UniverseSpec:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return load_universe_spec_from_payload(payload)


def load_weight_spec_from_payload(payload: dict) -> WeightSpec:
    defensive_sleeve = None
    if payload.get("defensive_sleeve") is not None:
        sleeve_payload = payload["defensive_sleeve"]
        defensive_sleeve = DefensiveSleeveSpec(
            risk_on_feature=str(sleeve_payload["risk_on_feature"]),
            markets=tuple(str(market).upper() for market in sleeve_payload.get("markets", [])),
            risk_on_lag=int(sleeve_payload.get("risk_on_lag", 0)),
            weighting=str(sleeve_payload.get("weighting", "equal")),
            gross_exposure=float(sleeve_payload.get("gross_exposure", 1.0)),
            listed_feature=str(sleeve_payload.get("listed_feature", "trade_price")),
        )

    portfolio_vol_target = None
    if payload.get("portfolio_vol_target") is not None:
        target_payload = payload["portfolio_vol_target"]
        portfolio_vol_target = PortfolioVolTargetSpec(
            price_feature=str(target_payload.get("price_feature", "signal_price")),
            window=int(target_payload.get("window", 40)),
            target_annual_vol=float(target_payload.get("target_annual_vol", 0.30)),
            periods_per_year=int(target_payload.get("periods_per_year", 252)),
            min_multiplier=float(target_payload.get("min_multiplier", 0.0)),
            max_multiplier=float(target_payload.get("max_multiplier", 1.0)),
            step_size=float(target_payload.get("step_size", 0.0)),
            rounding=str(target_payload.get("rounding", "floor")),
            min_valid_ratio=float(target_payload.get("min_valid_ratio", 0.9)),
            min_pair_obs_ratio=float(target_payload.get("min_pair_obs_ratio", 0.8)),
        )

    return WeightSpec(
        weighting=payload.get("weighting", "equal"),
        gross_exposure=payload.get("gross_exposure", 1.0),
        gross_exposure_feature=payload.get("gross_exposure_feature"),
        gross_exposure_lag=payload.get("gross_exposure_lag", 0),
        gross_exposure_clip_min=payload.get("gross_exposure_clip_min", 0.0),
        gross_exposure_clip_max=payload.get("gross_exposure_clip_max", 1.0),
        fixed_weight=payload.get("fixed_weight"),
        rank_weight_feature=payload.get("rank_weight_feature"),
        rank_weight_lag=payload.get("rank_weight_lag", 0),
        rank_weight_ascending=bool(payload.get("rank_weight_ascending", True)),
        rank_weights=tuple(float(value) for value in payload.get("rank_weights", [])),
        position_score_feature=payload.get("position_score_feature"),
        position_score_lag=payload.get("position_score_lag", 0),
        rank_power=payload.get("rank_power", 1.0),
        max_positions=payload.get("max_positions"),
        universe_name=payload.get("universe_name"),
        rebalance_frequency=payload.get("rebalance_frequency", "daily"),
        feature_value_scale=payload.get("feature_value_scale", 1.0),
        feature_value_clip_min=payload.get("feature_value_clip_min", 0.0),
        feature_value_clip_max=payload.get("feature_value_clip_max", 1.0),
        incremental_step_size=payload.get("incremental_step_size", 0.25),
        incremental_step_up=payload.get("incremental_step_up"),
        incremental_step_down=payload.get("incremental_step_down"),
        incremental_min_weight=payload.get("incremental_min_weight", 0.0),
        incremental_max_weight=payload.get("incremental_max_weight", 1.0),
        listed_feature=str(payload.get("listed_feature", "trade_price")),
        gross_exposure_update_frequency=str(payload.get("gross_exposure_update_frequency", "")),
        output_mode=str(payload.get("output_mode", "")),
        hrp_window=payload.get("hrp_window", 126),
        hrp_min_valid_ratio=payload.get("hrp_min_valid_ratio", 0.9),
        hrp_min_pair_obs_ratio=payload.get("hrp_min_pair_obs_ratio", 0.8),
        hrp_price_feature=str(payload.get("hrp_price_feature", "trade_price")),
        hrp_linkage_method=str(payload.get("hrp_linkage_method", "single")),
        cluster_corr_window=payload.get("cluster_corr_window", 126),
        cluster_corr_threshold=payload.get("cluster_corr_threshold", 0.85),
        cluster_min_valid_ratio=payload.get("cluster_min_valid_ratio", 0.9),
        cluster_min_pair_obs_ratio=payload.get("cluster_min_pair_obs_ratio", 0.8),
        cluster_price_feature=str(payload.get("cluster_price_feature", "trade_price")),
        defensive_sleeve=defensive_sleeve,
        portfolio_vol_target=portfolio_vol_target,
    )


def load_weight_spec(path: Path) -> WeightSpec:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return load_weight_spec_from_payload(payload)


def load_market_score_spec_from_payload(payload: dict) -> MarketScoreSpec:
    return MarketScoreSpec(
        output_column=str(payload.get("output_column", "custom_score")),
        rules=tuple(
            MarketScoreRuleSpec(
                market=str(item["market"]).upper(),
                mode=str(item.get("mode", "weighted_sum")),
                components=tuple(
                    MarketScoreComponentSpec(
                        feature_column=str(component["feature_column"]),
                        weight=float(component.get("weight", 1.0)),
                    )
                    for component in item.get("components", [])
                ),
            )
            for item in payload.get("rules", [])
        ),
    )


def load_market_score_spec(path: Path) -> MarketScoreSpec:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return load_market_score_spec_from_payload(payload)
