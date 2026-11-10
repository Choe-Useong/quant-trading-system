"""Execution dispatch shared by v2 backtests, grids and validation windows."""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lib.vectorbt_adapter import VectorBTSpec, run_portfolio_from_target_weights


DEFAULT_ARTIFACT_FORMAT = "parquet"


def normalize_artifact_format(value: str) -> str:
    artifact_format = str(value).strip().lower()
    if artifact_format not in {"csv", "parquet"}:
        raise ValueError("artifact_format must be 'csv' or 'parquet'")
    return artifact_format


def write_backtest_frame(
    path: Path, frame: pd.Series | pd.DataFrame, *, artifact_format: str = DEFAULT_ARTIFACT_FORMAT,
    index_label: str | None = None,
) -> Path:
    """Write indexed v2 results without rounding or downcasting numeric values."""
    artifact_format = normalize_artifact_format(artifact_format)
    path = Path(path).with_suffix("." + artifact_format)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(frame, pd.Series):
        frame = frame.to_frame(name=frame.name or "value")
    if artifact_format == "parquet":
        if index_label is not None:
            frame = frame.rename_axis(index_label)
        frame.to_parquet(path, compression="zstd", index=True)
    else:
        frame.to_csv(path, index_label=index_label, encoding="utf-8-sig")
    return path


def resolve_backtest_artifact(path: Path, *, prefer_summary: bool = False) -> Path:
    """Resolve a curve format; analysis consumers can follow its saved summary."""
    path = Path(path)
    if path.suffix.lower() not in {".csv", ".parquet"}:
        raise ValueError(f"Backtest curve must be CSV or Parquet: {path}")
    summary_path = path.parent / "summary.csv"
    if prefer_summary and summary_path.exists():
        summary = pd.read_csv(summary_path, index_col=0, keep_default_na=False)
        if "Artifact Format" in summary.index:
            artifact_format = normalize_artifact_format(summary.loc["Artifact Format"].iloc[0])
            resolved = path.with_suffix("." + artifact_format)
            if not resolved.is_file():
                raise FileNotFoundError(f"Missing {artifact_format} artifact declared by summary.csv: {resolved}")
            return resolved
    if path.is_file():
        return path
    alternate = path.with_suffix(".parquet" if path.suffix.lower() == ".csv" else ".csv")
    if alternate.is_file():
        return alternate
    raise FileNotFoundError(f"Missing backtest artifact: {path} (or {alternate})")


def read_backtest_curve(path: Path, *, prefer_summary: bool = False) -> pd.DataFrame:
    """Read CSV or Parquet as a numeric-column table indexed by timestamps."""
    path = resolve_backtest_artifact(path, prefer_summary=prefer_summary)
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
        if isinstance(frame.index, pd.DatetimeIndex):
            return frame
    else:
        frame = pd.read_csv(path, float_precision="round_trip")
    if frame.empty or len(frame.columns) < 2:
        raise ValueError(f"Backtest curve needs a date and value column: {path}")
    date_column = "date" if "date" in frame.columns else frame.columns[0]
    dates = pd.to_datetime(frame[date_column], errors="coerce")
    result = frame.drop(columns=[date_column]).set_axis(pd.DatetimeIndex(dates), axis=0)
    result.index.name = None if str(date_column).startswith("Unnamed:") else date_column
    return result


class FuturesBankruptcyError(ValueError):
    """Finite account exhaustion after all native positions have been closed."""

    def __init__(self, timestamp, equity: float):
        if not isfinite(equity) or equity > 0:
            raise ValueError("Bankruptcy requires finite, non-positive equity")
        self.timestamp = pd.Timestamp(timestamp)
        self.equity = float(equity)
        super().__init__(f"Futures account bankrupt at {self.timestamp}: equity={self.equity}")


@dataclass
class BacktestResultV2:
    equity: pd.Series
    summary: pd.Series
    trades: pd.DataFrame
    orders: pd.DataFrame
    account: pd.DataFrame
    reference_prices: pd.DataFrame
    init_cash: float
    metadata: dict[str, Any] | None = None
    events: pd.DataFrame | None = None

    def value(self) -> pd.Series:
        return self.equity.copy()

    def stats(self, settings=None) -> pd.Series:
        return self.summary.copy()

    def benchmark_value(self, group_by=True) -> pd.Series:
        # Equal initial cash sleeves. Each sleeve holds cash until its market's
        # first observed price, then buys and holds without cross-market transfers.
        prices = self.reference_prices.astype(float)
        if prices.empty:
            raise ValueError("Grouped benchmark needs execution rows and markets")
        values = prices.to_numpy()
        if (np.isinf(values) | (np.isfinite(values) & (values <= 0))).any():
            raise ValueError("Grouped benchmark prices must be positive finite values or NaN")
        normalized = prices.ffill().div(prices.bfill().iloc[0]).fillna(1.0)
        return normalized.mean(axis=1) * self.init_cash

    def save_artifacts(self, directory: Path, *, artifact_format: str = DEFAULT_ARTIFACT_FORMAT) -> None:
        artifact_format = normalize_artifact_format(artifact_format)
        directory.mkdir(parents=True, exist_ok=True)
        self.trades.to_json(directory / "futures_trades.json", orient="records", date_format="iso", indent=2)
        self.orders.to_csv(directory / "futures_orders.csv", index=False, encoding="utf-8-sig")
        write_backtest_frame(directory / "futures_account.csv", self.account,
                             artifact_format=artifact_format, index_label="date_utc")
        active = {"futures_account." + artifact_format}
        if self.metadata is not None:
            (directory / "futures_metadata.json").write_text(
                json.dumps(self.metadata, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
            )
            active.add("futures_metadata.json")
        if self.events is not None:
            write_backtest_frame(directory / "futures_events.parquet", self.events,
                                 artifact_format=artifact_format)
            active.add("futures_events." + artifact_format)
        # Clean only known artifacts owned by this writer, after successful writes.
        # Disabled optional output or a format change must not expose an older run.
        for name in ("futures_account.csv", "futures_account.parquet", "futures_metadata.json",
                     "futures_events.csv", "futures_events.parquet"):
            if name not in active:
                (directory / name).unlink(missing_ok=True)


def build_backtest_analysis_v2(portfolio, *, equity_curve: pd.Series, init_cash: float):
    """Small post-run tables; never replay execution or allocate price matrices."""
    curve = equity_curve.astype(float).copy()
    curve.index = pd.to_datetime(curve.index, utc=True)
    if (not np.isfinite(curve.to_numpy()).all() or curve.index.hasnans
            or not curve.index.is_monotonic_increasing or curve.index.has_duplicates):
        raise ValueError("Backtest analysis requires finite equity on ordered unique timestamps")
    if not isfinite(float(init_cash)) or init_cash <= 0:
        raise ValueError("Backtest analysis requires positive finite initial cash")
    previous = float(init_cash)
    annual = []
    for year, part in curve.groupby(curve.index.year):
        end = float(part.iloc[-1])
        annual.append(dict(year=int(year), first_observation=part.index[0].isoformat(),
                           last_observation=part.index[-1].isoformat(), observations=len(part),
                           start_value=previous, end_value=end,
                           return_pct=(end / previous - 1) * 100 if previous > 0 else None))
        previous = end
    annual_frame = pd.DataFrame(annual, columns=["year", "first_observation", "last_observation",
        "observations", "start_value", "end_value", "return_pct"])

    raw = portfolio.trades
    native = isinstance(raw, pd.DataFrame)
    if not native:
        raw = raw.records_readable
    if raw.empty:
        trades = pd.DataFrame(columns=["trade_id", "pair", "side", "net_pnl", "status"])
        trades["net_pnl"] = trades["net_pnl"].astype(float)
    elif native:
        flags = raw["is_short"]
        if not flags.isin([True, False]).all():
            raise ValueError("Backtest analysis requires boolean trade directions")
        ids = raw["trade_id"].astype(str) if "trade_id" in raw else raw.index.astype(str)
        trades = pd.DataFrame({"trade_id": ids, "pair": raw["pair"].astype(str),
            "side": np.where(flags, "short", "long"), "net_pnl": raw["profit_abs"].astype(float),
            "status": np.where(raw["close_date"].notna(), "closed", "open")})
    else:
        direction = raw["Direction"].astype(str).str.lower()
        if not direction.isin(["long", "short"]).all():
            raise ValueError("Backtest analysis found unknown VectorBT trade directions")
        trades = pd.DataFrame({"trade_id": raw.index.astype(str), "pair": raw["Column"].astype(str),
            "side": direction, "net_pnl": raw["PnL"].astype(float),
            "status": raw["Status"].astype(str).str.lower()})
    if not np.isfinite(trades.net_pnl.to_numpy()).all():
        raise ValueError("Backtest analysis requires finite trade PnL")
    net = float(trades.net_pnl.sum())
    account_pnl = float(curve.iloc[-1] - init_cash) if len(curve) else 0.0
    assets = trades.groupby("pair", sort=True).agg(net_pnl=("net_pnl", "sum"), trade_count=("net_pnl", "size"))
    assets = assets.sort_values("net_pnl", ascending=False, kind="stable").reset_index()
    sides = trades.groupby("side", sort=True).agg(net_pnl=("net_pnl", "sum"), trade_count=("net_pnl", "size"))
    sides = sides.reindex(["long", "short"], fill_value=0).rename_axis("side").reset_index()
    for table in (assets, sides):
        table["share_net_profit_pct"] = table.net_pnl / net * 100 if net > 0 else np.nan
    asset_positive = float(assets.net_pnl.clip(lower=0).sum())
    assets["share_positive_asset_profit_pct"] = (
        assets.net_pnl.clip(lower=0) / asset_positive * 100 if asset_positive > 0 else np.nan)
    positive_trades = trades[trades.net_pnl > 0].sort_values("net_pnl", ascending=False, kind="stable")
    top_trades = positive_trades.head(10).copy()
    top_trades["share_net_profit_pct"] = top_trades.net_pnl / net * 100 if net > 0 else np.nan
    metrics = {"Analysis Trade Records": len(trades), "Traded Assets": len(assets)}
    for label, pnl in (("Asset", assets.net_pnl), ("Trade", positive_trades.net_pnl)):
        positive = pnl[pnl > 0]
        positive_total = float(positive.sum())
        for count in (1, 5, 10):
            top = float(positive.head(count).sum())
            metrics[f"{label} Top {count} Net Profit Share [%]"] = top / net * 100 if net > 0 else np.nan
        top5 = float(positive.head(5).sum())
        metrics[f"{label} Top 5 Positive Profit Share [%]"] = top5 / positive_total * 100 if positive_total > 0 else np.nan
    for row in sides.itertuples(index=False):
        metrics[f"{row.side.capitalize()} Net PnL"] = float(row.net_pnl)
        metrics[f"{row.side.capitalize()} Net Profit Share [%]"] = float(row.share_net_profit_pct)
    difference = net - account_pnl
    metrics["Trade PnL Reconciliation Difference"] = difference
    details = dict(schema_version=1,
        annual_return_basis="Calendar year in UTC; first start value is initial cash, later starts are previous year-end equity. Includes open-position valuation in the equity curve.",
        contribution_basis="Full-period native trade PnL grouped by asset/direction; not calendar-year side attribution. Native futures PnL already includes fees, funding, liquidation fees and insurance credits.",
        trade_basis="native futures trades" if native else "VectorBT trade records, including marked open trades",
        concentration_basis="Positive asset/trade PnL ranked descending. Net-profit denominator includes losing assets/trades, so shares can exceed 100%. Net shares are undefined when total trade PnL <= 0. Positive-profit shares use positive asset/trade aggregates respectively.",
        currency="Same units as engine account equity", trade_net_pnl=net, account_net_pnl=account_pnl,
        reconciliation_difference=difference,
        reconciled=bool(np.isclose(net, account_pnl, rtol=1e-9, atol=1e-6)),
        open_trade_records=int(trades.status.ne("closed").sum()),
        metrics={k: (float(v) if isfinite(float(v)) else None) for k, v in metrics.items()})
    tables = dict(annual_returns=annual_frame, asset_contribution=assets,
                  side_contribution=sides, top_trades=top_trades)
    return tables, details, pd.Series(metrics, dtype=float)


def write_backtest_analysis_v2(portfolio, directory: Path, *, equity_curve: pd.Series,
                               init_cash: float) -> pd.Series:
    tables, details, metrics = build_backtest_analysis_v2(
        portfolio, equity_curve=equity_curve, init_cash=init_cash)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_csv(directory / (name + ".csv"), index=False, encoding="utf-8-sig")
    (directory / "analysis_summary.json").write_text(
        json.dumps(details, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    metrics = metrics.astype(object)
    metrics.loc["Analysis Directory"] = str(directory)
    return metrics


@lru_cache(maxsize=4)
def _observed_quality_manifest(path: str, mtime_ns: int, size: int):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("cache_kind") == "archive_observed_checkpoint":
        raise ValueError("Per-symbol checkpoint is not a backtest cache; use cache_observed/wide")
    if payload.get("cache_kind") != "archive_observed_futures":
        return None
    if not isinstance(payload.get("quality_intervals"), dict):
        raise ValueError("Observed cache has no quality report; rebuild it before backtesting")
    return {
        "start": pd.Timestamp(payload["cache_start_inclusive"]).value,
        "end": pd.Timestamp(payload["cache_end_exclusive"]).value,
        "delta": pd.Timedelta(payload["interval"]).value,
        "quality": {market: [(pd.Timestamp(issue["start_inclusive"]).value,
                               pd.Timestamp(issue["end_exclusive"]).value,
                               frozenset(issue["fields"]), issue["reason"]) for issue in issues]
                    for market, issues in payload["quality_intervals"].items()},
    }


def numba_data_policies(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("numba_futures settings must be an object")
    research = {
        "unverified_lifecycle_policy": "observed_prices",
        "funding_missing_policy": "previous_observation",
        "funding_transition_policy": "observed_only",
        "conflicting_price_policy": "exclude_market",
        "funding_start_policy": "first_observation",
        "missing_price_policy": "exclude_market",
        "delisting_policy": "last_available_open",
    }
    preset = settings.get("data_policy_preset", "strict")
    if not isinstance(preset, str) or preset not in {"strict", "research"}:
        raise ValueError("data_policy_preset must be strict or research")
    result = {}
    for name, research_value in research.items():
        allowed = {"error", research_value}
        value = settings.get(name, research_value if preset == "research" else "error")
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{name} must be one of {sorted(allowed)}")
        result[name] = value
    return result


def funding_usage_starts_v2(cache_dir, payload) -> dict:
    if (str(payload.get("engine", "vectorbt")).lower() != "numba_futures"
            or numba_data_policies(payload.get("numba_futures", {}))["funding_start_policy"] != "first_observation"):
        return {}
    from lib.dataframes import cache_funding_start_adjustments
    return {market: item["data_start_inclusive"] for market, item in cache_funding_start_adjustments(cache_dir).items()}


@lru_cache(maxsize=4)
def _observed_price_gaps(cache_path, signature, markets):
    cache = Path(cache_path)
    frames = []
    dates = None
    for name in ("opening_price", "trade_price", "mark_price"):
        frame = pd.read_parquet(cache / (name + ".parquet"), columns=list(markets))
        frame.index = pd.to_datetime(frame.index, utc=True)
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError("Prices require unique sorted timestamps")
        dates = frame.index if dates is None else dates.union(frame.index)
        frames.append(frame)
    if len(dates) < 2:
        return {}
    path = cache / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    delta = pd.Timedelta(payload["interval"]) if payload.get("interval") else pd.Timedelta(int(np.diff(dates.asi8).min()), unit="ns")
    if delta <= pd.Timedelta(0):
        raise ValueError("Price interval must be positive")
    dates = pd.date_range(dates[0], dates[-1], freq=delta)
    result = {}
    for market in markets:
        values = np.column_stack([pd.to_numeric(frame[market], errors="raise").reindex(dates).to_numpy(dtype=float) for frame in frames])
        valid = np.flatnonzero((np.isfinite(values) & (values > 0)).all(axis=1))
        if not len(valid):
            continue
        missing = np.flatnonzero(np.isnan(values).any(axis=1))
        missing = missing[(missing > valid[0]) & (missing < valid[-1])]
        if not len(missing):
            continue
        groups = np.split(missing, np.flatnonzero(np.diff(missing) > 1) + 1)
        result[market] = [{"reason": "missing_candles", "source": "observed_price_scan",
                           "fields": [name for column, name in enumerate(("opening_price", "trade_price", "mark_price"))
                                      if np.isnan(values[group, column]).any()],
                           "start_inclusive": dates[group[0]].isoformat(),
                           "end_exclusive": (dates[group[-1]] + delta).isoformat(), "bars": len(group)} for group in groups]
    return result


def observed_price_gaps_v2(cache_dir, index, markets, *, data_usage_starts=None):
    if not len(index) or not markets:
        return {}
    cache = Path(cache_dir).resolve()
    signature = []
    for name in ("opening_price.parquet", "trade_price.parquet", "mark_price.parquet", "manifest.json"):
        path = cache / name
        stat = path.stat() if path.is_file() else None
        signature.append((name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if stat is not None else (name, None))
    gaps = _observed_price_gaps(str(cache), tuple(signature), tuple(sorted(markets)))
    dates = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    # A bar at the gap's beginning also overlaps it; a bar at its end does not.
    begin, last = dates.min(), dates.max()
    result = {}
    for market, issues in gaps.items():
        lower = begin
        if data_usage_starts and market in data_usage_starts:
            start = data_usage_starts[market]
            if start is None:
                continue
            lower = max(lower, pd.Timestamp(start))
        overlapping = [i for i in issues if pd.Timestamp(i["start_inclusive"]) <= last and pd.Timestamp(i["end_exclusive"]) > lower]
        if overlapping:
            result[market] = overlapping
    return result


def observed_market_exclusions_v2(cache_dir, index, markets, settings):
    policies = numba_data_policies(settings)
    result = (observed_price_conflicts_v2(cache_dir, index, markets)
              if policies["conflicting_price_policy"] == "exclude_market" else {})
    if policies["missing_price_policy"] == "exclude_market":
        starts = funding_usage_starts_v2(cache_dir, {"engine": "numba_futures", "numba_futures": settings})
        for market, issues in observed_price_gaps_v2(cache_dir, index, markets, data_usage_starts=starts).items():
            result.setdefault(market, []).extend(issues)
    return result


def observed_price_conflicts_v2(cache_dir: Path, index, markets) -> dict:
    path = Path(cache_dir) / "manifest.json"
    if not path.is_file() or not len(index):
        return {}
    stat = path.stat()
    manifest = _observed_quality_manifest(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if manifest is None:
        return {}
    dates = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    begin, end = dates.min().value, dates.max().value + manifest["delta"]
    result = {}
    for market in markets:
        issues = []
        for lower, upper, fields, reason in manifest["quality"].get(market, ()):
            if (reason == "conflicting_candles" and lower < end and upper > begin
                    and ("*" in fields or fields.intersection({"opening_price", "trade_price", "mark_price", "high_price", "low_price"}))):
                issues.append({"reason": reason, "fields": sorted(fields),
                               "start_inclusive": pd.Timestamp(lower, tz="UTC").isoformat(),
                               "end_exclusive": pd.Timestamp(upper, tz="UTC").isoformat()})
        if issues:
            result[market] = issues
    return result


def observed_funding_uncertainties_v2(cache_dir: Path, market: str) -> list:
    path = Path(cache_dir) / "manifest.json"
    if not path.is_file():
        return []
    stat = path.stat()
    manifest = _observed_quality_manifest(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if manifest is None:
        return []
    return [{"start_inclusive": pd.Timestamp(lower, tz="UTC").isoformat(),
             "end_exclusive": pd.Timestamp(upper, tz="UTC").isoformat()}
            for lower, upper, fields, reason in manifest["quality"].get(market, ())
            if reason == "unverified_funding_transition" and fields == {"funding_rate"}]


def select_numba_execution_markets_v2(cache_dir, index, markets, settings):
    policies = numba_data_policies(settings)
    excluded = observed_market_exclusions_v2(cache_dir, index, markets, settings)
    selected = [market for market in markets if market not in excluded]
    if markets and not selected:
        reason = "missing prices" if any(i["reason"] == "missing_candles" for issues in excluded.values() for i in issues) else "conflicting prices"
        raise ValueError(f"All target markets excluded because of {reason}: {sorted(excluded)}")
    validate_observed_cache_v2(cache_dir, index, selected, engine="numba_futures",
        unverified_lifecycle_policy=policies["unverified_lifecycle_policy"],
        funding_missing_policy=policies["funding_missing_policy"],
        funding_transition_policy=policies["funding_transition_policy"], funding_start_policy=policies["funding_start_policy"])
    return selected, excluded


def apply_observed_universe_policy_v2(cache_dir, index, markets, universe_spec, payload, *, required_markets=()):
    """Exclude before ranking/weight allocation, retaining source columns for references."""
    if str(payload.get("engine", "vectorbt")).lower() != "numba_futures":
        return universe_spec, {}
    policies = numba_data_policies(payload.get("numba_futures", {}))
    if policies["conflicting_price_policy"] != "exclude_market" and policies["missing_price_policy"] != "exclude_market":
        return universe_spec, {}
    dates = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    if payload.get("trim_start_mode") == "timestamp" and payload.get("trim_start_timestamp") is not None:
        start = pd.Timestamp(payload["trim_start_timestamp"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        dates = dates[dates >= start]
    candidates = sorted(set(universe_spec.allowed_markets or markets) | set(required_markets))
    excluded = observed_market_exclusions_v2(cache_dir, dates, candidates, payload.get("numba_futures", {}))
    required_conflicts = set(excluded).intersection(required_markets)
    if required_conflicts:
        raise ValueError(f"Cannot exclude required hedge markets with conflicting prices or missing prices: {sorted(required_conflicts)}")
    if excluded:
        universe_spec = replace(universe_spec, excluded_markets=tuple(sorted(set(universe_spec.excluded_markets) | set(excluded))))
    return universe_spec, excluded


def record_data_policy_audit_v2(result, excluded_markets=None):
    if not isinstance(result, BacktestResultV2) or result.metadata is None:
        return
    audit = result.metadata.setdefault("data_policy_audit", {})
    if excluded_markets:
        audit.setdefault("excluded_markets", {}).update(excluded_markets)
    result.summary.loc["Excluded Conflicting Markets"] = sum(any(i["reason"] == "conflicting_candles" for i in issues) for issues in audit.get("excluded_markets", {}).values())
    result.summary.loc["Excluded Missing-Price Markets"] = sum(any(i["reason"] == "missing_candles" for i in issues) for issues in audit.get("excluded_markets", {}).values())
    result.summary.loc["Funding Start Adjusted Markets"] = len(audit.get("funding_start_adjustments", {}))
    result.summary.loc["Delisting Boundary Markets"] = len(audit.get("delisting_settlements", {}))
    result.summary.loc["Replaced Funding Observations"] = sum(len(v) for v in audit.get("funding_replacements", {}).values())
    result.summary.loc["Observed-Only Funding Intervals"] = sum(len(v) for v in audit.get("funding_observed_only", {}).values())
    if any(audit.values()):
        result.summary.loc["Data Policy Audit"] = json.dumps(audit, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def validate_observed_cache_v2(source_cache_dir: Path, index, markets, *, engine: str, required_fields=None,
                               unverified_lifecycle_policy="error", funding_missing_policy="error",
                               funding_transition_policy="error", funding_start_policy="error"):
    """Check only execution markets/window; unrelated archive gaps do not block a run."""
    engine = str(engine).lower()
    numba_data_policies({"unverified_lifecycle_policy": unverified_lifecycle_policy,
                         "funding_missing_policy": funding_missing_policy,
                         "funding_transition_policy": funding_transition_policy, "funding_start_policy": funding_start_policy})
    starts = funding_usage_starts_v2(source_cache_dir, {"engine": "numba_futures", "numba_futures": {"funding_start_policy": funding_start_policy}})
    path = Path(source_cache_dir) / "manifest.json"
    if not path.is_file():
        return False
    stat = path.stat()
    manifest = _observed_quality_manifest(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if manifest is None or len(index) == 0:
        return False
    dates = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    begin, end = dates.min().value, dates.max().value + manifest["delta"]
    if begin < manifest["start"] or end > manifest["end"]:
        raise ValueError("Backtest window extends outside the observed cache period")
    fields = {"opening_price", "trade_price"}
    if engine in {"numba_futures", "freqtrade"}:
        fields.update({"mark_price", "funding_rate"})
    if engine == "freqtrade":
        fields.update({"high_price", "low_price", "candle_acc_trade_volume"})
    if required_fields is not None:
        fields = set(required_fields)
    for market in markets:
        if market not in manifest["quality"]:
            raise ValueError(f"Market is absent from observed-cache quality metadata: {market}")
        effective_begin = max(begin, pd.Timestamp(starts[market]).value) if market in starts and starts[market] is not None else begin
        for lower, upper, affected, reason in manifest["quality"][market]:
            check_begin = begin if reason == "unresolved_lifecycle" else effective_begin
            if lower < end and upper > check_begin and ("*" in affected or fields.intersection(affected)):
                if reason == "missing_funding" and funding_missing_policy == "previous_observation" and affected == {"funding_rate"}:
                    continue  # The engine still must find a strictly earlier real observation.
                if reason == "unverified_funding_transition" and funding_transition_policy == "observed_only" and affected == {"funding_rate"}:
                    continue  # The engine applies only explicitly recorded uncertainty intervals.
                if reason == "unresolved_lifecycle" and unverified_lifecycle_policy == "observed_prices":
                    from lib.dataframes import cache_unverified_lifecycle_records
                    record = cache_unverified_lifecycle_records(source_cache_dir, [market]).get(market, {})
                    if (record.get("data_usage_basis") != "observed_prices"
                            or not record.get("data_start_inclusive") or not record.get("data_end_exclusive")):
                        raise ValueError(f"Missing observed price coverage for {market}; refresh the observed cache")
                    continue
                stamp = pd.Timestamp(max(check_begin, lower), tz="UTC")
                raise ValueError(f"Observed cache has unresolved data for {market} at {stamp}: {reason}; repair or select a complete period")
    return True


def resolve_benchmark_curve_v2(*, source_cache_dir: Path, benchmark_source_cache_dir: Path | None = None,
                               benchmark_price_column: str | None = None, **kwargs):
    """Validate the benchmark's actual cache/markets/price field before comparison."""
    from scripts.run_vectorbt import resolve_benchmark_curve

    mode = kwargs["benchmark_mode"]
    prices = kwargs["price_frame"]
    spec = kwargs.get("vectorbt_spec")
    field = benchmark_price_column or (spec.price_column if spec is not None else "trade_price")
    cache = source_cache_dir
    if mode == "single_market":
        cache = benchmark_source_cache_dir or source_cache_dir
        reference = kwargs.get("benchmark_price_frame")
        reference = prices if reference is None else reference
        markets = [kwargs["benchmark_market"]]
    elif mode in {"portfolio_group", "portfolio_group_rebalance"}:
        field = spec.price_column if spec is not None else "trade_price"
        reference = (getattr(kwargs["portfolio"], "reference_prices", prices)
                     if mode == "portfolio_group" else prices)
        fixed = kwargs.get("benchmark_fixed_weights") if mode == "portfolio_group_rebalance" else None
        excluded = ((getattr(kwargs.get("portfolio"), "metadata", None) or {})
                    .get("data_policy_audit", {}).get("excluded_markets", {}))
        if mode == "portfolio_group_rebalance" and excluded:
            if fixed and any(str(m).upper() in excluded for m in fixed):
                raise ValueError("A fixed benchmark market was excluded because of conflicting prices")
            reference = reference.drop(columns=list(excluded), errors="ignore")
            kwargs["price_frame"] = reference
        markets = list(reference.columns) if not fixed else [str(m).upper() for m in fixed if str(m).upper() in reference.columns]
    else:
        return resolve_benchmark_curve(**kwargs)
    settings = (getattr(kwargs.get("portfolio"), "metadata", None) or {}).get("settings", {})
    policy = numba_data_policies(settings)["unverified_lifecycle_policy"]
    observed = validate_observed_cache_v2(cache, prices.index, markets, engine="vectorbt", required_fields=[field],
                                         unverified_lifecycle_policy=policy)
    if observed:
        from lib.dataframes import cache_data_usage_mask
        usage = cache_data_usage_mask(cache, reference.index, reference.columns)
        if usage is not None:
            reference = reference.where(usage)
            if mode == "single_market":
                kwargs["benchmark_price_frame"] = reference
            elif mode == "portfolio_group_rebalance":
                kwargs["price_frame"] = reference
    if observed:
        aligned = reference.reindex(prices.index)
        for market in markets:
            values = pd.to_numeric(aligned[market], errors="coerce").to_numpy(dtype=float)
            finite = np.isfinite(values)
            if np.isinf(values).any() or (finite & (values <= 0)).any():
                raise ValueError(f"Invalid observed-cache benchmark prices: {market}")
            valid = np.flatnonzero(finite)
            # Preserve intentional pre-listing/post-delisting boundaries, not internal holes.
            if not valid.size or not finite[valid[0]:valid[-1] + 1].all():
                raise ValueError(f"Missing observed-cache benchmark prices: {market}; comparison bars cannot be dropped")
    return resolve_benchmark_curve(**kwargs)


EXECUTION_AVAILABILITY_KEYS = frozenset({"volume_column"})


def apply_execution_availability_v2(price_frame, target_weight_frame, settings, source_cache_dir):
    """Defer requests on non-tradable bars and settle holdings whose prices end.

    A bar is tradable when its execution price is finite and volume > 0. A
    request on a non-tradable bar executes unchanged on the market's next
    tradable bar unless a newer request arrives first. A zero request while
    flat with nothing pending is a no-op. If a held target remains when the
    price ends for the rest of the run, the first missing bar sells to zero at
    the last finite price (merger/liquidation proceeds are not modelled).
    Returns (execution prices, requests, audit).
    """
    if not isinstance(settings, dict) or set(settings) - EXECUTION_AVAILABILITY_KEYS:
        raise ValueError(f"execution_availability supports only {sorted(EXECUTION_AVAILABILITY_KEYS)}")
    column = settings.get("volume_column")
    if not isinstance(column, str) or not column:
        raise ValueError("execution_availability requires a volume_column name")
    path = Path(source_cache_dir) / f"{column}.parquet"
    if not path.is_file():
        raise ValueError(f"execution_availability volume source is missing: {path}")
    markets = list(target_weight_frame.columns)
    missing = sorted(set(markets) - set(price_frame.columns))
    if missing:
        raise ValueError(f"execution_availability requires execution prices for target markets: {missing[:5]}")
    volume = pd.read_parquet(path)
    absent = sorted(set(markets) - set(volume.columns))
    if absent:
        raise ValueError(f"execution_availability volume source lacks target markets: {absent[:5]}")
    index = price_frame.index
    prices = price_frame[markets].to_numpy(dtype=float)
    requests = target_weight_frame.reindex(index=index).to_numpy(dtype=float)
    volumes = volume.reindex(index=index, columns=markets).to_numpy(dtype=float)
    finite = np.isfinite(prices)
    tradable = finite & (np.nan_to_num(volumes, nan=0.0) > 0.0)
    out = np.full(requests.shape, np.nan)
    execution = prices.copy()
    audit = {"volume_column": column, "deferred_requests": 0, "superseded_requests": 0,
             "unfilled_requests_at_end": 0, "canceled_at_delisting": 0, "dropped_after_delisting": 0,
             "delisting_settlements": []}
    rows = len(index)
    for j, market in enumerate(markets):
        request_rows = np.flatnonzero(~np.isnan(requests[:, j]))
        if not request_rows.size:
            continue
        valid = np.flatnonzero(finite[:, j])
        if valid.size and not np.isfinite(volumes[valid, j]).any():
            raise ValueError(f"execution_availability has prices without any volume observation: {market}")
        delist = int(valid[-1]) + 1 if valid.size and valid[-1] + 1 < rows else None
        tradable_rows = np.flatnonzero(tradable[:, j])
        held, pending = 0.0, None
        for position, row in enumerate(request_rows):
            value = requests[row, j]
            if delist is not None and row >= delist:
                audit["dropped_after_delisting"] += int(value != 0.0)
                continue
            if value == 0.0 and held == 0.0 and pending is None:
                continue
            if pending is not None:
                audit["superseded_requests"] += 1
            pending = None
            k = np.searchsorted(tradable_rows, row)
            executed = int(tradable_rows[k]) if k < tradable_rows.size else None
            if executed is not None and delist is not None and executed >= delist:
                executed = None
            following = request_rows[position + 1] if position + 1 < request_rows.size else None
            if executed is None or (following is not None and executed >= following):
                pending = value
                continue
            out[executed, j] = value
            held = value
            audit["deferred_requests"] += int(executed > row)
        if pending is not None:
            if delist is None:
                audit["unfilled_requests_at_end"] += 1
            elif pending != 0.0:
                audit["canceled_at_delisting"] += 1
        if delist is not None and held != 0.0:
            out[delist, j] = 0.0
            execution[delist, j] = prices[valid[-1], j]
            audit["delisting_settlements"].append({
                "market": str(market), "date": str(index[delist]), "price": float(prices[valid[-1], j]),
                "last_price_date": str(index[valid[-1]]), "target_before": float(held)})
    execution_frame = price_frame.copy()
    execution_frame[markets] = execution
    return execution_frame, pd.DataFrame(out, index=index, columns=target_weight_frame.columns), audit


def record_execution_availability_summary_v2(portfolio, summary: pd.Series) -> pd.Series:
    """Append execution-availability counts to a summary when the run used it."""
    audit = getattr(portfolio, "execution_availability_audit", None)
    if audit is None:
        return summary
    summary.loc["Deferred Non-Tradable Requests"] = audit["deferred_requests"]
    summary.loc["Superseded Deferred Requests"] = audit["superseded_requests"]
    summary.loc["Unfilled Requests At End"] = audit["unfilled_requests_at_end"]
    summary.loc["Canceled Requests At Delisting"] = audit["canceled_at_delisting"]
    summary.loc["Delisting Settlements"] = len(audit["delisting_settlements"])
    return summary


def run_backtest_v2(
    price_frame: pd.DataFrame,
    target_weight_frame: pd.DataFrame,
    spec: VectorBTSpec,
    *,
    payload: dict[str, Any],
    source_cache_dir: Path,
    timeframe: str,
    prepared_cache: dict[str, Any] | None = None,
    feature_frames: dict[str, pd.DataFrame] | None = None,
):
    engine = str(payload.get("engine", "vectorbt")).lower()
    availability = payload.get("execution_availability")
    if availability is not None and engine != "vectorbt":
        raise ValueError("execution_availability is supported only by the vectorbt engine")
    if engine != "numba_futures" and (payload.get("numba_futures") or {}).get("partial_exit") is not None:
        raise ValueError("partial_exit is supported only by the numba_futures engine")
    if engine == "numba_futures":
        select_numba_execution_markets_v2(source_cache_dir, price_frame.index, list(target_weight_frame.columns),
                                          payload.get("numba_futures", {}))
        from lib.numba_futures_v2 import run_numba_futures_backtest_v2
        return run_numba_futures_backtest_v2(
            price_frame, target_weight_frame, spec, source_cache_dir=source_cache_dir,
            timeframe=timeframe, settings=payload.get("numba_futures", {}),
            feature_frames=feature_frames,
            **({"prepared_cache": prepared_cache} if prepared_cache is not None else {}),
        )
    from lib.dataframes import cache_data_usage_mask
    usage = cache_data_usage_mask(source_cache_dir, target_weight_frame.index, target_weight_frame.columns)
    if usage is not None:
        target_weight_frame = target_weight_frame.where(usage, 0.0)
    validate_observed_cache_v2(source_cache_dir, price_frame.index, target_weight_frame.columns, engine=engine)
    if engine == "vectorbt":
        if availability is None:
            return run_portfolio_from_target_weights(price_frame, target_weight_frame, spec)
        execution_prices, requests, audit = apply_execution_availability_v2(
            price_frame, target_weight_frame, availability, source_cache_dir)
        portfolio = run_portfolio_from_target_weights(execution_prices, requests, spec)
        portfolio.execution_availability_audit = audit
        return portfolio
    if engine != "freqtrade":
        raise ValueError(f"Unsupported v2 backtest engine: {engine}")
    try:
        installed_version = version("freqtrade")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "The freqtrade engine requires the optional dependencies: "
            "python -m pip install -r requirements-futures.txt"
        ) from exc
    if installed_version != "2026.9":
        raise RuntimeError("The v2 adapter requires freqtrade==2026.9; install requirements-futures.txt")
    from lib.freqtrade_adapter_v2 import run_futures_backtest_v2
    return run_futures_backtest_v2(
        price_frame, target_weight_frame, spec,
        source_cache_dir=source_cache_dir, timeframe=timeframe,
        settings=payload.get("freqtrade", {}),
    )
