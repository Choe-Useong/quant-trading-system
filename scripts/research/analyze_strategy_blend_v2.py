#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.run_vectorbt import (  # noqa: E402
    compute_drawdown_recovery_stats,
    compute_return_series,
    compute_sharpe_ratio,
    compute_sortino_ratio,
    compute_ulcer_index_pct,
)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    equity_csv: Path
    value_column: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Combine v2 strategy equity curves and compare sleeve allocations."
    )
    parser.add_argument("--config-json", required=True, help="Strategy blend configuration JSON")
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("type") != "strategy_blend_analysis_v2":
        raise ValueError("Config type must be 'strategy_blend_analysis_v2'")
    if not payload.get("sources"):
        raise ValueError("Config sources must not be empty")
    if not payload.get("allocations"):
        raise ValueError("Config allocations must not be empty")
    return payload


def _source_specs(payload: dict[str, Any]) -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    names: set[str] = set()
    for item in payload["sources"]:
        name = str(item["name"]).strip()
        if not name or name in names:
            raise ValueError(f"Source names must be non-empty and unique: {name!r}")
        names.add(name)
        specs.append(
            SourceSpec(
                name=name,
                equity_csv=_resolve_path(item["equity_csv"]),
                value_column=str(item["value_column"]) if item.get("value_column") else None,
            )
        )
    return specs


def _read_equity_curve(spec: SourceSpec) -> pd.Series:
    if not spec.equity_csv.exists():
        raise FileNotFoundError(f"Missing equity curve for {spec.name}: {spec.equity_csv}")

    frame = pd.read_csv(spec.equity_csv)
    if frame.empty or frame.shape[1] < 2:
        raise ValueError(f"Equity curve needs a date and value column: {spec.equity_csv}")

    date_column = "date" if "date" in frame.columns else str(frame.columns[0])
    value_column = spec.value_column
    if value_column is None:
        candidates = [column for column in frame.columns if column != date_column]
        if len(candidates) != 1:
            raise ValueError(
                f"Specify value_column for a multi-value equity curve: {spec.equity_csv}"
            )
        value_column = str(candidates[0])
    if value_column not in frame.columns:
        raise ValueError(f"Missing value column {value_column!r}: {spec.equity_csv}")

    dates = pd.to_datetime(frame[date_column], errors="coerce")
    values = pd.to_numeric(frame[value_column], errors="coerce")
    curve = pd.Series(values.to_numpy(), index=dates, name=spec.name).dropna().sort_index()
    if curve.index.has_duplicates:
        raise ValueError(f"Equity curve contains duplicate dates: {spec.equity_csv}")
    if len(curve) < 3:
        raise ValueError(f"Equity curve needs at least 3 observations: {spec.equity_csv}")
    if (curve <= 0.0).any():
        raise ValueError(f"Equity curve must remain positive: {spec.equity_csv}")
    return curve


def _align_curves(
    curves: dict[str, pd.Series],
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp | None,
) -> pd.DataFrame:
    effective_start = max(curve.index.min() for curve in curves.values())
    effective_end = min(curve.index.max() for curve in curves.values())
    if start_date is not None:
        effective_start = max(effective_start, start_date)
    if end_date is not None:
        effective_end = min(effective_end, end_date)
    if effective_start >= effective_end:
        raise ValueError(
            f"Sources have no usable common period: {effective_start} to {effective_end}"
        )

    union_index = pd.DatetimeIndex(
        sorted(
            {
                timestamp
                for curve in curves.values()
                for timestamp in curve.index
                if effective_start <= timestamp <= effective_end
            }
        )
    )
    aligned = pd.DataFrame(index=union_index)
    for name, curve in curves.items():
        aligned[name] = curve.reindex(union_index).ffill()
    aligned = aligned.dropna()
    if len(aligned) < 3:
        raise ValueError("Aligned equity curves need at least 3 common observations")
    return aligned.div(aligned.iloc[0])


def _validate_weights(
    allocation: dict[str, Any],
    source_names: list[str],
) -> pd.Series:
    name = str(allocation["name"])
    raw_weights = allocation.get("weights", {})
    unknown = sorted(set(raw_weights) - set(source_names))
    if unknown:
        raise ValueError(f"Allocation {name!r} contains unknown sources: {unknown}")

    weights = pd.Series(
        {source: float(raw_weights.get(source, 0.0)) for source in source_names},
        dtype=float,
    )
    if not np.isfinite(weights.to_numpy()).all() or (weights < 0.0).any():
        raise ValueError(f"Allocation {name!r} weights must be finite and non-negative")
    if not math.isclose(float(weights.sum()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"Allocation {name!r} weights must sum to 1.0")
    return weights


def _drift_curve(source_curves: pd.DataFrame, weights: pd.Series) -> pd.Series:
    curve = source_curves.mul(weights, axis=1).sum(axis=1)
    curve.name = "portfolio"
    return curve


def _periodic_rebalance_curve(
    source_curves: pd.DataFrame,
    weights: pd.Series,
    frequency: str,
    fee_bps: float,
) -> tuple[pd.Series, float]:
    if frequency not in {"monthly", "quarterly", "yearly"}:
        raise ValueError(f"Unsupported rebalance mode: {frequency}")

    period_alias = {"monthly": "M", "quarterly": "Q", "yearly": "Y"}[frequency]
    periods = source_curves.index.to_period(period_alias)
    returns = source_curves.pct_change().fillna(0.0)
    sleeve_values = weights.copy()
    portfolio_values = [float(sleeve_values.sum())]
    total_turnover = 0.0
    fee_rate = fee_bps / 10_000.0

    for index_position in range(1, len(source_curves)):
        if periods[index_position] != periods[index_position - 1]:
            total_value = float(sleeve_values.sum())
            target_values = weights * total_value
            turnover = float((target_values - sleeve_values).abs().sum() / (2.0 * total_value))
            total_turnover += turnover
            total_value *= max(0.0, 1.0 - turnover * fee_rate)
            sleeve_values = weights * total_value

        sleeve_values = sleeve_values.mul(1.0 + returns.iloc[index_position])
        total_value = float(sleeve_values.sum())
        portfolio_values.append(total_value)

    curve = pd.Series(portfolio_values, index=source_curves.index, name="portfolio")
    return curve, total_turnover


def _monthly_source_trend_curve(
    source_curves: pd.DataFrame,
    weights: pd.Series,
    lookback_months: int,
    nonpositive_exposure: float,
    fee_bps: float,
) -> tuple[pd.Series, float, int]:
    if lookback_months <= 0:
        raise ValueError("source trend lookback_months must be positive")
    if not 0.0 <= nonpositive_exposure <= 1.0:
        raise ValueError("source trend nonpositive_exposure must be between 0 and 1")

    monthly_curve = source_curves.resample("ME").last()
    monthly_trend = monthly_curve.pct_change(periods=lookback_months)
    monthly_trend.index = monthly_trend.index.to_period("M") + 1

    periods = source_curves.index.to_period("M")
    returns = source_curves.pct_change().fillna(0.0)
    sleeve_values = weights.copy()
    cash_value = max(0.0, 1.0 - float(weights.sum()))
    portfolio_values = [float(sleeve_values.sum() + cash_value)]
    total_turnover = 0.0
    fee_rate = fee_bps / 10_000.0
    risk_off_source_months = 0

    for index_position in range(1, len(source_curves)):
        if periods[index_position] != periods[index_position - 1]:
            period = periods[index_position]
            trend = (
                monthly_trend.loc[period]
                if period in monthly_trend.index
                else pd.Series(float("nan"), index=source_curves.columns)
            )
            exposure = pd.Series(1.0, index=source_curves.columns)
            valid = trend.notna()
            exposure.loc[valid & trend.le(0.0)] = nonpositive_exposure
            risk_off_source_months += int((valid & trend.le(0.0)).sum())

            total_value = float(sleeve_values.sum() + cash_value)
            target_weights = weights.mul(exposure)
            target_cash_weight = max(0.0, 1.0 - float(target_weights.sum()))
            target_sleeves = target_weights * total_value
            target_cash = target_cash_weight * total_value
            turnover = float(
                (
                    (target_sleeves - sleeve_values).abs().sum()
                    + abs(target_cash - cash_value)
                )
                / (2.0 * total_value)
            )
            total_turnover += turnover
            total_value *= max(0.0, 1.0 - turnover * fee_rate)
            sleeve_values = target_weights * total_value
            cash_value = target_cash_weight * total_value

        sleeve_values = sleeve_values.mul(1.0 + returns.iloc[index_position])
        portfolio_values.append(float(sleeve_values.sum() + cash_value))

    curve = pd.Series(portfolio_values, index=source_curves.index, name="portfolio")
    return curve, total_turnover, risk_off_source_months


def _calendar_cagr(curve: pd.Series) -> float:
    elapsed_days = (pd.Timestamp(curve.index[-1]) - pd.Timestamp(curve.index[0])).days
    if elapsed_days <= 0:
        return float("nan")
    return float((curve.iloc[-1] / curve.iloc[0]) ** (365.25 / elapsed_days) - 1.0)


def _summary_row(
    name: str,
    curve: pd.Series,
    periods_per_year: int,
    mode: str,
    weights: pd.Series,
    turnover: float,
) -> dict[str, Any]:
    returns = compute_return_series(curve)
    cagr = _calendar_cagr(curve)
    drawdown = (curve / curve.cummax()) - 1.0
    max_drawdown = float(drawdown.min())
    recovery = compute_drawdown_recovery_stats(curve)
    annual_volatility = float(returns.std(ddof=0) * math.sqrt(periods_per_year))
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0.0 else float("nan")
    return {
        "name": name,
        "mode": mode,
        "start": curve.index[0].date().isoformat(),
        "end": curve.index[-1].date().isoformat(),
        "observations": len(curve),
        "total_return_pct": (float(curve.iloc[-1] / curve.iloc[0]) - 1.0) * 100.0,
        "cagr_pct": cagr * 100.0,
        "annual_volatility_pct": annual_volatility * 100.0,
        "max_drawdown_pct": max_drawdown * 100.0,
        "sharpe_ratio": compute_sharpe_ratio(returns, periods_per_year),
        "sortino_ratio": compute_sortino_ratio(returns, periods_per_year),
        "calmar_ratio": calmar,
        "ulcer_index_pct": compute_ulcer_index_pct(curve),
        "longest_peak_to_recovery_bars": recovery["Longest Peak-to-Recovery Bars"],
        "longest_drawdown_duration_bars": recovery["Longest Drawdown Duration Bars"],
        "drawdown_duration_p90_bars": recovery["Drawdown Duration P90 Bars"],
        "current_drawdown_pct": recovery["Current Drawdown [%]"],
        "underwater_time_pct": recovery["Underwater Time [%]"],
        "total_rebalance_turnover": turnover,
        "weights": json.dumps(weights.to_dict(), sort_keys=True),
    }


def _drawdown_episodes(curve: pd.Series, variant_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    peak_value = float(curve.iloc[0])
    peak_date = pd.Timestamp(curve.index[0])
    trough_value = peak_value
    trough_date = peak_date
    in_drawdown = False

    for timestamp, raw_value in curve.iloc[1:].items():
        date = pd.Timestamp(timestamp)
        value = float(raw_value)
        if value >= peak_value:
            if in_drawdown:
                rows.append(
                    {
                        "name": variant_name,
                        "peak_date": peak_date.date().isoformat(),
                        "trough_date": trough_date.date().isoformat(),
                        "end_date": date.date().isoformat(),
                        "recovered": True,
                        "drawdown_pct": (trough_value / peak_value - 1.0) * 100.0,
                        "duration_bars": int(curve.loc[peak_date:date].shape[0] - 1),
                    }
                )
            peak_value = value
            peak_date = date
            trough_value = value
            trough_date = date
            in_drawdown = False
            continue

        in_drawdown = True
        if value < trough_value:
            trough_value = value
            trough_date = date

    if in_drawdown:
        end_date = pd.Timestamp(curve.index[-1])
        rows.append(
            {
                "name": variant_name,
                "peak_date": peak_date.date().isoformat(),
                "trough_date": trough_date.date().isoformat(),
                "end_date": end_date.date().isoformat(),
                "recovered": False,
                "drawdown_pct": (trough_value / peak_value - 1.0) * 100.0,
                "duration_bars": int(curve.loc[peak_date:end_date].shape[0] - 1),
            }
        )
    return rows


def _annual_returns(curves: pd.DataFrame) -> pd.DataFrame:
    year_end = curves.resample("YE").last()
    result = year_end.pct_change()
    result.iloc[0] = year_end.iloc[0].div(curves.iloc[0]) - 1.0
    result = result.mul(100.0)
    result.index = result.index.year
    result.index.name = "year"
    return result


def main() -> int:
    args = build_parser().parse_args()
    config_path = _resolve_path(args.config_json)
    payload = _load_config(config_path)
    specs = _source_specs(payload)
    source_names = [spec.name for spec in specs]

    start_date = pd.Timestamp(payload["start_date"]) if payload.get("start_date") else None
    end_date = pd.Timestamp(payload["end_date"]) if payload.get("end_date") else None
    periods_per_year = int(payload.get("periods_per_year", 252))
    fee_bps = float(payload.get("rebalance_fee_bps", 0.0))
    rebalance_modes = [str(mode) for mode in payload.get("rebalance_modes", ["drift"])]
    invalid_modes = sorted(set(rebalance_modes) - {"drift", "monthly", "quarterly", "yearly"})
    if invalid_modes:
        raise ValueError(f"Unsupported rebalance modes: {invalid_modes}")

    raw_curves = {spec.name: _read_equity_curve(spec) for spec in specs}
    source_curves = _align_curves(raw_curves, start_date, end_date)
    all_curves: dict[str, pd.Series] = {}
    summaries: list[dict[str, Any]] = []
    allocation_weights: dict[str, pd.Series] = {}

    for source_name in source_names:
        curve = source_curves[source_name].rename(source_name)
        weights = pd.Series(
            {name: 1.0 if name == source_name else 0.0 for name in source_names}
        )
        all_curves[source_name] = curve
        summaries.append(
            _summary_row(source_name, curve, periods_per_year, "standalone", weights, 0.0)
        )

    for allocation in payload["allocations"]:
        allocation_name = str(allocation["name"])
        weights = _validate_weights(allocation, source_names)
        if allocation_name in allocation_weights:
            raise ValueError(f"Allocation names must be unique: {allocation_name!r}")
        allocation_weights[allocation_name] = weights
        for mode in rebalance_modes:
            variant_name = f"{allocation_name}_{mode}"
            if mode == "drift":
                curve = _drift_curve(source_curves, weights)
                turnover = 0.0
            else:
                curve, turnover = _periodic_rebalance_curve(
                    source_curves,
                    weights,
                    mode,
                    fee_bps,
                )
            curve = curve.rename(variant_name)
            all_curves[variant_name] = curve
            summaries.append(
                _summary_row(
                    variant_name,
                    curve,
                    periods_per_year,
                    mode,
                    weights,
                    turnover,
                )
            )

    for overlay in payload.get("source_trend_overlays", []):
        overlay_name = str(overlay["name"])
        base_allocation = str(overlay["base_allocation"])
        if overlay_name in all_curves:
            raise ValueError(f"Strategy blend variant names must be unique: {overlay_name!r}")
        if base_allocation not in allocation_weights:
            raise ValueError(
                f"Source trend overlay {overlay_name!r} references unknown allocation: "
                f"{base_allocation!r}"
            )
        lookback_months = int(overlay["lookback_months"])
        nonpositive_exposure = float(overlay["nonpositive_exposure"])
        weights = allocation_weights[base_allocation]
        curve, turnover, risk_off_source_months = _monthly_source_trend_curve(
            source_curves,
            weights,
            lookback_months,
            nonpositive_exposure,
            fee_bps,
        )
        curve = curve.rename(overlay_name)
        all_curves[overlay_name] = curve
        summary = _summary_row(
            overlay_name,
            curve,
            periods_per_year,
            "source_trend_monthly",
            weights,
            turnover,
        )
        summary.update(
            {
                "base_allocation": base_allocation,
                "lookback_months": lookback_months,
                "nonpositive_exposure": nonpositive_exposure,
                "risk_off_source_months": risk_off_source_months,
            }
        )
        summaries.append(summary)

    curve_frame = pd.DataFrame(all_curves)
    source_returns = source_curves.pct_change().dropna()
    daily_correlation = source_returns.corr()
    monthly_returns = source_curves.resample("ME").last().pct_change().dropna()
    monthly_correlation = monthly_returns.corr()

    drawdown_rows = [
        row
        for name, curve in all_curves.items()
        for row in _drawdown_episodes(curve, name)
    ]
    drawdown_frame = pd.DataFrame(drawdown_rows)
    if not drawdown_frame.empty:
        drawdown_frame = (
            drawdown_frame.sort_values(["name", "drawdown_pct"])
            .groupby("name", as_index=False)
            .head(int(payload.get("drawdown_episode_count", 5)))
        )
        for source_name in source_names:
            source_curve = source_curves[source_name]
            drawdown_frame[f"{source_name}_peak_to_trough_pct"] = drawdown_frame.apply(
                lambda row: (
                    source_curve.loc[pd.Timestamp(row["trough_date"])]
                    / source_curve.loc[pd.Timestamp(row["peak_date"])]
                    - 1.0
                )
                * 100.0,
                axis=1,
            )
            drawdown_frame[f"{source_name}_peak_to_end_pct"] = drawdown_frame.apply(
                lambda row: (
                    source_curve.loc[pd.Timestamp(row["end_date"])]
                    / source_curve.loc[pd.Timestamp(row["peak_date"])]
                    - 1.0
                )
                * 100.0,
                axis=1,
            )

    out_dir = _resolve_path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    source_curves.to_csv(out_dir / "aligned_source_equity_curves.csv", encoding="utf-8-sig")
    curve_frame.to_csv(out_dir / "blended_equity_curves.csv", encoding="utf-8-sig")
    pd.DataFrame(summaries).to_csv(out_dir / "summary_results.csv", index=False, encoding="utf-8-sig")
    daily_correlation.to_csv(out_dir / "daily_return_correlation.csv", encoding="utf-8-sig")
    monthly_correlation.to_csv(out_dir / "monthly_return_correlation.csv", encoding="utf-8-sig")
    _annual_returns(curve_frame).to_csv(out_dir / "annual_returns.csv", encoding="utf-8-sig")
    drawdown_frame.to_csv(out_dir / "drawdown_episodes.csv", index=False, encoding="utf-8-sig")

    metadata = {
        "type": "strategy_blend_analysis_result_v2",
        "name": payload["name"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "config_json": str(config_path),
        "effective_start": source_curves.index[0].date().isoformat(),
        "effective_end": source_curves.index[-1].date().isoformat(),
        "periods_per_year": periods_per_year,
        "rebalance_fee_bps": fee_bps,
        "source_files": {
            spec.name: str(spec.equity_csv)
            for spec in specs
        },
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote strategy blend analysis to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
