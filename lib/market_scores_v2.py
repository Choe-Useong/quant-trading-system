from __future__ import annotations

import pandas as pd

from lib.specs import MarketScoreSpec


def required_markets_for_market_score_spec(spec: MarketScoreSpec) -> list[str]:
    return sorted({str(rule.market).upper() for rule in spec.rules})


def _binary_series(frame: pd.DataFrame, feature_column: str, market: str, index: pd.Index) -> pd.Series:
    feature_frame = frame.get(feature_column)
    if feature_frame is None:
        return pd.Series(0.0, index=index, dtype=float)
    series = feature_frame.reindex(index=index)
    if market in series.columns:
        values = pd.to_numeric(series[market], errors="coerce").fillna(0.0)
        return values.gt(0.0).astype(float)
    return pd.Series(0.0, index=index, dtype=float)


def build_market_score_frame(
    feature_frames: dict[str, pd.DataFrame],
    spec: MarketScoreSpec,
) -> pd.DataFrame:
    if not feature_frames:
        return pd.DataFrame()

    reference = next(iter(feature_frames.values()))
    index = reference.index
    columns = reference.columns
    score_frame = pd.DataFrame(0.0, index=index, columns=columns)

    for rule in spec.rules:
        market = str(rule.market).upper()
        if market not in score_frame.columns:
            continue
        if rule.mode == "all_true":
            if not rule.components:
                score_frame.loc[:, market] = 0.0
                continue
            active = pd.Series(True, index=index)
            for component in rule.components:
                active &= _binary_series(feature_frames, component.feature_column, market, index).gt(0.0)
            score_frame.loc[:, market] = active.astype(float)
            continue

        total = pd.Series(0.0, index=index, dtype=float)
        for component in rule.components:
            total += _binary_series(feature_frames, component.feature_column, market, index) * float(component.weight)
        score_frame.loc[:, market] = total

    return score_frame
