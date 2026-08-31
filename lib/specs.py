from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TransformSpec:
    kind: str
    params: dict[str, int | float | str] = field(default_factory=dict)

    def resolved_name(self) -> str:
        if not self.params:
            return self.kind
        suffix = "_".join(f"{key}{value}" for key, value in sorted(self.params.items()))
        return f"{self.kind}_{suffix}"


@dataclass(frozen=True)
class ScoreComponentSpec:
    feature_column: str
    weight: float = 1.0


@dataclass(frozen=True)
class MarketScoreComponentSpec:
    feature_column: str
    weight: float = 1.0


@dataclass(frozen=True)
class MarketScoreRuleSpec:
    market: str
    mode: str = "weighted_sum"
    components: tuple[MarketScoreComponentSpec, ...] = ()


@dataclass(frozen=True)
class MarketScoreSpec:
    output_column: str = "custom_score"
    rules: tuple[MarketScoreRuleSpec, ...] = ()


@dataclass(frozen=True)
class CompareSpec:
    left_feature: str
    operator: str
    right_feature: str | None = None
    right_value: float | None = None


@dataclass(frozen=True)
class LogicalSpec:
    operator: str
    features: tuple[str, ...]


@dataclass(frozen=True)
class StateSpec:
    entry_feature: str
    exit_feature: str
    cooldown_bars: int = 0


@dataclass(frozen=True)
class BreadthSpec:
    driver_feature: str
    signal_feature: str
    mode: str = "top_n"
    top_n: int = 4
    quantiles: int = 4
    bucket_values: tuple[int, ...] = (1,)
    ascending: bool = False


@dataclass(frozen=True)
class FeatureSpec:
    source: str | None = None
    steps: tuple[TransformSpec, ...] = ()
    components: tuple[ScoreComponentSpec, ...] = ()
    combine: str | None = None
    compare: CompareSpec | None = None
    logical: LogicalSpec | None = None
    state: StateSpec | None = None
    breadth: BreadthSpec | None = None
    column_name: str | None = None

    def resolved_column_name(self) -> str:
        if self.column_name:
            return self.column_name
        if self.compare is not None:
            right = (
                self.compare.right_feature
                if self.compare.right_feature is not None
                else f"value{self.compare.right_value:g}"
            )
            return f"{self.compare.left_feature}_{self.compare.operator}_{right}"
        if self.logical is not None:
            joined = "__".join(self.logical.features)
            return f"{self.logical.operator}__{joined}"
        if self.state is not None:
            cooldown_suffix = "" if self.state.cooldown_bars <= 0 else f"__cooldown{self.state.cooldown_bars}"
            return f"hold__{self.state.entry_feature}__until__{self.state.exit_feature}{cooldown_suffix}"
        if self.breadth is not None:
            if self.breadth.mode == "top_n":
                order = "asc" if self.breadth.ascending else "desc"
                return (
                    f"breadth__{self.breadth.driver_feature}__top{self.breadth.top_n}_{order}"
                    f"__{self.breadth.signal_feature}"
                )
            buckets = "-".join(str(value) for value in self.breadth.bucket_values)
            order = "asc" if self.breadth.ascending else "desc"
            return (
                f"breadth__{self.breadth.driver_feature}__q{self.breadth.quantiles}_b{buckets}_{order}"
                f"__{self.breadth.signal_feature}"
            )
        if self.components:
            suffix = "__".join(
                f"{component.feature_column}_w{component.weight:g}"
                for component in self.components
            )
            prefix = self.combine or "weighted_sum"
            return f"{prefix}__{suffix}"
        if not self.steps:
            if self.source is None:
                raise ValueError("FeatureSpec must define source, components, compare, logical, state, or breadth")
            return self.source
        suffix = "__".join(step.resolved_name() for step in self.steps)
        if self.source is None:
            raise ValueError("FeatureSpec with steps must define source")
        return f"{self.source}__{suffix}"


@dataclass(frozen=True)
class ValueFilterSpec:
    feature_column: str
    operator: str
    value: float
    lag: int = 0


@dataclass(frozen=True)
class RankFilterSpec:
    feature_column: str
    mode: str = "top_n"
    lag: int = 0
    top_n: int = 30
    quantiles: int = 5
    bucket_values: tuple[int, ...] = (1,)
    ascending: bool = False
    scope: str = "filtered"


@dataclass(frozen=True)
class FilterStageSpec:
    mode: str = "sequential"
    filters: tuple[RankFilterSpec, ...] = ()


@dataclass(frozen=True)
class DefensiveSleeveSpec:
    risk_on_feature: str
    markets: tuple[str, ...]
    risk_on_lag: int = 0
    weighting: str = "equal"
    gross_exposure: float = 1.0
    listed_feature: str = "trade_price"


@dataclass(frozen=True)
class PortfolioVolTargetSpec:
    price_feature: str = "signal_price"
    window: int = 40
    target_annual_vol: float = 0.30
    periods_per_year: int = 252
    min_multiplier: float = 0.0
    max_multiplier: float = 1.0
    step_size: float = 0.0
    rounding: str = "floor"
    min_valid_ratio: float = 0.9
    min_pair_obs_ratio: float = 0.8


@dataclass(frozen=True)
class UniverseSpec:
    feature_column: str
    sort_column: str | None = None
    lag: int = 1
    signal_lag: int = 0
    start_min_cross_section_size: int = 0
    mode: str = "top_n"
    top_n: int = 30
    quantiles: int = 5
    bucket_values: tuple[int, ...] = (1,)
    ascending: bool = False
    scope: str = "filtered"
    exclude_warnings: bool = False
    min_age_days: int | None = None
    allowed_markets: tuple[str, ...] = ()
    excluded_markets: tuple[str, ...] = ()
    value_filters: tuple[ValueFilterSpec, ...] = ()
    rank_filters: tuple[RankFilterSpec, ...] = ()
    filter_stages: tuple[FilterStageSpec, ...] = ()
    name: str | None = None

    def resolved_name(self) -> str:
        sort_column = self.sort_column or self.feature_column
        lag_part = f"lag{self.lag}"
        if self.signal_lag > 0:
            lag_part += f"_siglag{self.signal_lag}"
        if self.start_min_cross_section_size > 0:
            lag_part += f"_startcs{self.start_min_cross_section_size}"
        if self.name:
            return self.name
        if self.mode == "top_n":
            order = "asc" if self.ascending else "desc"
            return f"{sort_column}_{lag_part}_{order}_top{self.top_n}"
        if self.mode == "all":
            return f"{sort_column}_{lag_part}_all"
        order = "asc" if self.ascending else "desc"
        buckets = "-".join(str(value) for value in self.bucket_values)
        return f"{sort_column}_{lag_part}_{order}_q{self.quantiles}_b{buckets}"


@dataclass(frozen=True)
class WeightSpec:
    weighting: str = "equal"
    gross_exposure: float = 1.0
    gross_exposure_feature: str | None = None
    gross_exposure_lag: int = 0
    gross_exposure_clip_min: float = 0.0
    gross_exposure_clip_max: float = 1.0
    fixed_weight: float | None = None
    rank_weight_feature: str | None = None
    rank_weight_lag: int = 0
    rank_weight_ascending: bool = True
    rank_weights: tuple[float, ...] = ()
    position_score_feature: str | None = None
    position_score_lag: int = 0
    rank_power: float = 1.0
    max_positions: int | None = None
    universe_name: str | None = None
    rebalance_frequency: str = "daily"
    feature_value_scale: float = 1.0
    feature_value_clip_min: float = 0.0
    feature_value_clip_max: float = 1.0
    incremental_step_size: float = 0.25
    incremental_step_up: float | None = None
    incremental_step_down: float | None = None
    incremental_min_weight: float = 0.0
    incremental_max_weight: float = 1.0
    listed_feature: str = "trade_price"
    gross_exposure_update_frequency: str = ""
    output_mode: str = ""
    hrp_window: int = 126
    hrp_min_valid_ratio: float = 0.9
    hrp_min_pair_obs_ratio: float = 0.8
    hrp_price_feature: str = "trade_price"
    hrp_linkage_method: str = "single"
    cluster_corr_window: int = 126
    cluster_corr_threshold: float = 0.85
    cluster_min_valid_ratio: float = 0.9
    cluster_min_pair_obs_ratio: float = 0.8
    cluster_price_feature: str = "trade_price"
    defensive_sleeve: DefensiveSleeveSpec | None = None
    portfolio_vol_target: PortfolioVolTargetSpec | None = None

    def resolved_name(self) -> str:
        prefix = self.universe_name or "universe"
        schedule_suffix = str(self.rebalance_frequency)
        if self.gross_exposure_update_frequency:
            schedule_suffix += f"_gupd{self.gross_exposure_update_frequency}"
        if self.output_mode:
            schedule_suffix += f"_out{self.output_mode}"
        if self.portfolio_vol_target is not None:
            target = self.portfolio_vol_target
            price_token = (
                str(target.price_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            schedule_suffix += (
                f"_pvol{target.target_annual_vol:g}"
                f"w{target.window}"
                f"ppy{target.periods_per_year}"
                f"m{target.min_multiplier:g}-{target.max_multiplier:g}"
                f"v{target.min_valid_ratio:g}"
                f"pair{target.min_pair_obs_ratio:g}"
                f"price{price_token}"
            )
            if target.step_size > 0.0:
                schedule_suffix += f"s{target.step_size:g}{target.rounding}"
        exposure_suffix = ""
        if self.gross_exposure_feature:
            feature_token = (
                str(self.gross_exposure_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            exposure_suffix = (
                f"_gfeat{feature_token}"
                f"_lag{self.gross_exposure_lag}"
                f"_clip{self.gross_exposure_clip_min:g}_{self.gross_exposure_clip_max:g}"
            )
        defensive_suffix = ""
        if self.defensive_sleeve is not None:
            sleeve = self.defensive_sleeve
            feature_token = (
                str(sleeve.risk_on_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            market_token = "-".join(
                str(market)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
                for market in sleeve.markets
            )
            defensive_suffix = (
                f"_def_{str(sleeve.weighting)}"
                f"_risk{feature_token}"
                f"_lag{sleeve.risk_on_lag}"
                f"_gross{sleeve.gross_exposure:g}"
                f"_{market_token}"
            )
        if self.weighting == "equal":
            return (
                f"{prefix}__equal_{schedule_suffix}"
                f"_gross{self.gross_exposure:g}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "feature_value":
            score_suffix = ""
            if self.position_score_feature:
                score_token = (
                    str(self.position_score_feature)
                    .replace(":", "_")
                    .replace("/", "_")
                    .replace("\\", "_")
                    .replace("{", "")
                    .replace("}", "")
                )
                score_suffix = (
                    f"_pscore{score_token}"
                    f"_lag{self.position_score_lag}"
                    f"_scale{self.feature_value_scale:g}"
                    f"_clip{self.feature_value_clip_min:g}_{self.feature_value_clip_max:g}"
                )
            return (
                f"{prefix}__feature_value_{schedule_suffix}"
                f"_gross{self.gross_exposure:g}"
                f"{score_suffix}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "fixed":
            if self.fixed_weight is None:
                raise ValueError("fixed_weight must be provided for fixed weighting")
            score_suffix = ""
            if self.position_score_feature:
                score_token = (
                    str(self.position_score_feature)
                    .replace(":", "_")
                    .replace("/", "_")
                    .replace("\\", "_")
                    .replace("{", "")
                    .replace("}", "")
                )
                score_suffix = f"_pscore{score_token}_lag{self.position_score_lag}"
            return (
                f"{prefix}__fixed_{schedule_suffix}"
                f"_w{self.fixed_weight:g}"
                f"{score_suffix}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "rank_fixed":
            if not self.rank_weight_feature:
                raise ValueError("rank_weight_feature must be provided for rank_fixed weighting")
            if not self.rank_weights:
                raise ValueError("rank_weights must be provided for rank_fixed weighting")
            feature_token = (
                str(self.rank_weight_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            weights_token = "_".join(f"{float(value):g}" for value in self.rank_weights)
            order = "asc" if self.rank_weight_ascending else "desc"
            score_suffix = ""
            if self.position_score_feature:
                score_token = (
                    str(self.position_score_feature)
                    .replace(":", "_")
                    .replace("/", "_")
                    .replace("\\", "_")
                    .replace("{", "")
                    .replace("}", "")
                )
                score_suffix = f"_pscore{score_token}_lag{self.position_score_lag}"
            return (
                f"{prefix}__rank_fixed_{schedule_suffix}"
                f"_feat{feature_token}"
                f"_lag{self.rank_weight_lag}"
                f"_{order}_w{weights_token}"
                f"{score_suffix}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "listed_fixed":
            feature_token = (
                str(self.listed_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            return (
                f"{prefix}__listed_fixed_{schedule_suffix}"
                f"_gross{self.gross_exposure:g}"
                f"_listed{feature_token}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "hrp":
            feature_token = (
                str(self.hrp_price_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            return (
                f"{prefix}__hrp_{schedule_suffix}"
                f"_gross{self.gross_exposure:g}"
                f"_win{self.hrp_window}"
                f"_valid{self.hrp_min_valid_ratio:g}"
                f"_pair{self.hrp_min_pair_obs_ratio:g}"
                f"_price{feature_token}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "cluster_equal":
            feature_token = (
                str(self.cluster_price_feature)
                .replace(":", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("{", "")
                .replace("}", "")
            )
            return (
                f"{prefix}__cluster_equal_{schedule_suffix}"
                f"_gross{self.gross_exposure:g}"
                f"_win{self.cluster_corr_window}"
                f"_thr{self.cluster_corr_threshold:g}"
                f"_valid{self.cluster_min_valid_ratio:g}"
                f"_pair{self.cluster_min_pair_obs_ratio:g}"
                f"_price{feature_token}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        if self.weighting == "incremental_signal":
            step_up = self.incremental_step_up if self.incremental_step_up is not None else self.incremental_step_size
            step_down = self.incremental_step_down if self.incremental_step_down is not None else self.incremental_step_size
            return (
                f"{prefix}__incremental_signal_{schedule_suffix}"
                f"_up{step_up:g}_down{step_down:g}"
                f"_gross{self.gross_exposure:g}"
                f"{exposure_suffix}"
                f"{defensive_suffix}"
            )
        return (
            f"{prefix}__rank_p{self.rank_power:g}_{schedule_suffix}"
            f"_gross{self.gross_exposure:g}"
            f"{exposure_suffix}"
            f"{defensive_suffix}"
        )
