#!/usr/bin/env python3
"""Cross-sectional feature IC study on v2 wide caches.

Per sampled timestamp: Spearman/Pearson IC between a v2 feature and the forward
return across markets, quantile buckets, the top-minus-bottom bucket spread and
the feature-rank autocorrelation. Optional overlap-aware inference uses a moving
block bootstrap of each IC time series, then a Benjamini-Yekutieli adjustment
across every feature x horizon x subset. Defaults reproduce the original study
(every bar, close-to-close labels, all features built at once, no bootstrap).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
import zlib
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import compute_market_forward_return_frame, read_wide_frame_from_cache
from lib.feature_graph_v2 import _spec_dependencies, referenced_markets_for_feature_specs
from lib.features_v2 import SUPPORTED_SOURCE_COLUMNS, build_feature_frames_from_cache
from lib.spec_io import load_feature_specs, load_universe_spec
from lib.universe_v2 import build_universe_mask_v2
from scripts.research.analyze_time_series_features_v2 import by_adjust

LABEL_STARTS = ("close", "next_open")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze cross-sectional feature predictiveness from v2 wide caches without "
            "materializing a large long panel."
        )
    )
    parser.add_argument("--source-cache-dir", required=True, help="Wide source cache directory")
    parser.add_argument("--feature-spec-json", required=True, help="v2 feature spec JSON path")
    parser.add_argument("--universe-spec-json", default="", help="Optional v2 universe spec JSON path")
    parser.add_argument("--price-column", default="trade_price", help="Price column used for forward labels")
    parser.add_argument("--forward-horizons", default="6,12,24,42", help="Comma-separated forward horizons in bars")
    parser.add_argument("--market-columns", default="", help="Optional comma-separated markets to analyze")
    parser.add_argument("--features", default="", help="Optional comma-separated resolved feature columns to analyze")
    parser.add_argument("--exclude-features", default="", help="Optional comma-separated feature columns to skip")
    parser.add_argument("--subsets", default="base,selected", help="Comma-separated subset names: none,base,selected")
    parser.add_argument("--quantiles", type=int, default=5, help="Feature quantile buckets")
    parser.add_argument("--min-count", type=int, default=10, help="Minimum cross-section count per timestamp")
    parser.add_argument("--max-markets", type=int, default=None, help="Optional market limit for smoke runs")
    parser.add_argument("--tail-rows", type=int, default=None, help="Optional tail rows for smoke runs")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--label-start", choices=LABEL_STARTS, default="close",
                        help="close: price[t] -> price[t+h]. next_open: open[t+1] -> open[t+1+h] (backtest fill timing)")
    parser.add_argument("--open-column", default="opening_price", help="Open price column for --label-start next_open")
    parser.add_argument("--sample-every", type=int, default=1, help="Use every Nth bar from the first row (1 = every bar)")
    parser.add_argument("--sample-hours", default="",
                        help="Comma-separated UTC hours; use only bars starting exactly on these hours (e.g. 0)")
    parser.add_argument("--bootstrap-reps", type=int, default=0,
                        help="Moving block bootstrap replications per IC series (0 = off, otherwise >= 199)")
    parser.add_argument("--block-bars", type=int, default=0,
                        help="Bootstrap block length in bars (0 = the label horizon); converted to sampled rows")
    parser.add_argument("--bootstrap-seed", type=int, default=0, help="Bootstrap seed")
    parser.add_argument("--feature-chunk-size", type=int, default=0,
                        help="Build this many analysis features (plus dependencies) at a time (0 = all at once)")
    parser.add_argument("--rank-autocorr-lag", type=int, default=1,
                        help="Lag in sampled rows for the feature-rank autocorrelation")
    parser.add_argument("--save-ic-series", action="store_true",
                        help="Write the per-timestamp IC and quantile spread series to feature_ic_series.parquet")
    return parser


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_markets(value: str) -> list[str] | None:
    markets = sorted({item.upper() for item in _parse_csv(value)})
    return markets or None


def _parse_horizons(value: str) -> list[int]:
    horizons = []
    for item in _parse_csv(value):
        horizon = int(item)
        if horizon <= 0:
            raise ValueError("forward horizons must be positive integers")
        horizons.append(horizon)
    if not horizons:
        raise ValueError("at least one forward horizon is required")
    return sorted(dict.fromkeys(horizons))


def _read_warning_frame(
    source_cache_dir: Path,
    *,
    market_columns: Sequence[str] | None,
    max_markets: int | None,
    require_warning: bool,
) -> pd.DataFrame:
    path = source_cache_dir / "market_warning.parquet"
    if not path.exists():
        if require_warning:
            raise FileNotFoundError(f"market_warning.parquet is required by universe spec: {path}")
        return pd.DataFrame()
    requested_columns = None
    if market_columns is not None:
        requested_columns = sorted({str(column).upper() for column in market_columns})
    try:
        frame = pd.read_parquet(path, columns=requested_columns)
    except Exception:
        frame = pd.read_parquet(path)
        if requested_columns is not None:
            frame = frame.reindex(columns=requested_columns)
    frame.index = pd.to_datetime(frame.index, utc=False)
    frame = frame.sort_index().sort_index(axis=1)
    if requested_columns is not None:
        frame = frame.reindex(columns=requested_columns)
    elif max_markets is not None:
        frame = frame.reindex(columns=sorted(frame.columns)[:max_markets])
    return frame


def _resolve_output_markets(explicit_markets: list[str] | None, universe_spec) -> list[str] | None:
    if explicit_markets is not None:
        return explicit_markets
    if universe_spec is not None and universe_spec.allowed_markets:
        return sorted(str(market).upper() for market in universe_spec.allowed_markets)
    return None


def _resolve_required_markets(output_markets: list[str] | None, feature_specs) -> list[str] | None:
    if output_markets is None:
        return None
    return sorted(set(output_markets) | set(referenced_markets_for_feature_specs(feature_specs)))


def _rowwise_corr(x_df: pd.DataFrame, y_df: pd.DataFrame, *, min_count: int) -> pd.Series:
    x = x_df.to_numpy(dtype=float)
    y = y_df.to_numpy(dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    count = valid.sum(axis=1).astype(float)
    x0 = np.where(valid, x, 0.0)
    y0 = np.where(valid, y, 0.0)
    x_mean = np.divide(x0.sum(axis=1), count, out=np.full(x.shape[0], np.nan), where=count > 0)
    y_mean = np.divide(y0.sum(axis=1), count, out=np.full(y.shape[0], np.nan), where=count > 0)
    xc = np.where(valid, x - x_mean[:, None], 0.0)
    yc = np.where(valid, y - y_mean[:, None], 0.0)
    numerator = (xc * yc).sum(axis=1)
    denominator = np.sqrt((xc * xc).sum(axis=1) * (yc * yc).sum(axis=1))
    corr = np.divide(
        numerator,
        denominator,
        out=np.full(x.shape[0], np.nan),
        where=(denominator > 0) & (count >= float(min_count)),
    )
    return pd.Series(corr, index=x_df.index)


def _t_stat(series: pd.Series) -> float:
    values = series.dropna()
    if len(values) <= 2:
        return float("nan")
    std = values.std(ddof=1)
    if not np.isfinite(std) or std <= 0:
        return float("nan")
    return float(values.mean() / (std / np.sqrt(len(values))))


def _subset_masks(
    requested_subsets: list[str],
    price_frame: pd.DataFrame,
    base_eligible: pd.DataFrame,
    selected: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for subset in requested_subsets:
        if subset == "none":
            result[subset] = price_frame.notna()
        elif subset == "base":
            result[subset] = base_eligible.fillna(False).infer_objects(copy=False).astype(bool)
        elif subset == "selected":
            result[subset] = selected.fillna(False).infer_objects(copy=False).astype(bool)
        else:
            raise ValueError(f"unknown subset: {subset}")
    return result


def _quantile_buckets(x: pd.DataFrame, quantiles: int) -> np.ndarray:
    rank = x.rank(axis=1, method="first", ascending=True)
    counts = x.notna().sum(axis=1).astype(float)
    bucket = np.floor((rank.sub(1.0)).div(counts, axis=0).mul(float(quantiles))).add(1.0)
    bucket = bucket.where((bucket >= 1.0) & (bucket <= float(quantiles)))
    return bucket.to_numpy(dtype=float)


def _bucket_spread(bucket_values: np.ndarray, y: pd.DataFrame, quantiles: int, *, min_count: int) -> pd.Series:
    """Per timestamp: mean forward return of the top bucket minus that of the bottom bucket."""
    y_values = y.to_numpy(dtype=float)
    valid_rows = (np.isfinite(bucket_values) & np.isfinite(y_values)).sum(axis=1) >= int(min_count)

    def bucket_mean(q: int) -> np.ndarray:
        member = (bucket_values == float(q)) & np.isfinite(y_values)
        count = member.sum(axis=1)
        total = np.where(member, y_values, 0.0).sum(axis=1)
        return np.divide(total, count, out=np.full(len(count), np.nan), where=count > 0)

    spread = bucket_mean(quantiles) - bucket_mean(1)
    return pd.Series(np.where(valid_rows, spread, np.nan), index=y.index)


def _quantile_rows(
    *,
    subset: str,
    label: str,
    feature: str,
    x: pd.DataFrame,
    y: pd.DataFrame,
    quantiles: int,
    bucket_values: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    if bucket_values is None:
        bucket_values = _quantile_buckets(x, quantiles)
    y_values = y.to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []
    for q in range(1, quantiles + 1):
        values = y_values[bucket_values == float(q)]
        values = values[np.isfinite(values)]
        rows.append(
            {
                "subset": subset,
                "label": label,
                "feature": feature,
                "q_low_to_high": q,
                "mean_fwd_return": float(np.mean(values)) if len(values) else float("nan"),
                "median_fwd_return": float(np.median(values)) if len(values) else float("nan"),
                "count": int(len(values)),
            }
        )
    return rows


def _parse_hours(value: str) -> list[int]:
    hours = sorted({int(item) for item in _parse_csv(value)})
    if any(hour < 0 or hour > 23 for hour in hours):
        raise ValueError("--sample-hours values must be UTC hours 0..23")
    return hours


def sample_positions(index: pd.Index, *, every: int, hours: Sequence[int]) -> np.ndarray:
    """Row positions used for the IC study (labels and features are still computed on every bar)."""
    if every < 1:
        raise ValueError("--sample-every must be >= 1")
    if hours and every != 1:
        raise ValueError("use either --sample-every or --sample-hours, not both")
    if hours:
        stamps = pd.DatetimeIndex(index)
        if stamps.tz is not None:
            stamps = stamps.tz_convert("UTC")
        on_hour = stamps == stamps.floor("h")
        return np.flatnonzero(on_hour & stamps.hour.isin(list(hours)))
    return np.arange(0, len(index), every)


def sample_step_bars(positions: np.ndarray) -> int:
    """Typical bar spacing between sampled rows, used to convert bootstrap blocks to sampled rows."""
    if len(positions) < 2:
        return 1
    return max(1, int(np.median(np.diff(positions))))


def forward_label_frame(price_frame: pd.DataFrame, open_frame: pd.DataFrame | None, horizon: int,
                        label_start: str) -> pd.DataFrame:
    """close: price[t+h] / price[t] - 1. next_open: open[t+1+h] / open[t+1] - 1 (enter after the signal bar)."""
    if label_start == "close":
        return compute_market_forward_return_frame(price_frame, horizon)
    if label_start == "next_open":
        if open_frame is None:
            raise ValueError("next_open labels need an open price frame")
        return open_frame.shift(-(horizon + 1)).div(open_frame.shift(-1)).sub(1.0)
    raise ValueError(f"unknown label start: {label_start}")


def block_bootstrap_mean(values: np.ndarray, *, block: int, reps: int, seed: Sequence[int]) -> tuple[float, float, float]:
    """Moving block bootstrap of a time-ordered series mean: (2.5% bound, 97.5% bound, two-sided p).

    Missing values are dropped and the remaining order is kept. Each replicate joins
    ceil(n / block) uniformly drawn overlapping blocks, so serial dependence up to the
    block length (overlapping labels) stays inside a block. The p-value centers the
    bootstrap distribution at the observed mean: (#|mean* - mean| >= |mean| + 1) / (reps + 1).
    Returns NaN when the bootstrap is off or there are fewer than two blocks of data.
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    block = max(1, int(block))
    count = len(values)
    if reps <= 0 or count < 3 or count < 2 * block:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(list(seed))
    blocks_per_draw = math.ceil(count / block)
    sums = np.concatenate(([0.0], np.cumsum(values)))
    block_means = (sums[block:] - sums[:-block]) / block
    means = np.empty(reps)
    batch = max(1, 4_000_000 // blocks_per_draw)
    for start in range(0, reps, batch):
        size = min(batch, reps - start)
        picks = rng.integers(0, len(block_means), size=(size, blocks_per_draw))
        means[start:start + size] = block_means[picks].mean(axis=1)
    effect = float(values.mean())
    lower, upper = np.quantile(means, [0.025, 0.975])
    tail = np.count_nonzero(np.abs(means - effect) >= abs(effect))
    return float(lower), float(upper), float((tail + 1.0) / (reps + 1.0))


def _hypothesis_seed(seed: int, *parts: Any) -> list[int]:
    """Seed per hypothesis, independent of feature order and chunking."""
    return [int(seed), zlib.crc32("|".join(str(part) for part in parts).encode())]


def feature_closure(feature_specs: Sequence[Any], names: Iterable[str]) -> list[Any]:
    """The specs needed to build `names`: the named specs and every declared ancestor, in spec order."""
    specs_by_name = {spec.resolved_column_name(): spec for spec in feature_specs}
    needed: set[str] = set()
    stack = [name for name in names if name in specs_by_name]
    while stack:
        name = stack.pop()
        if name in needed:
            continue
        needed.add(name)
        for dependency in _spec_dependencies(specs_by_name[name], SUPPORTED_SOURCE_COLUMNS):
            if dependency in specs_by_name and dependency not in needed:
                stack.append(dependency)
    return [spec for spec in feature_specs if spec.resolved_column_name() in needed]


def _strings_in(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _strings_in(getattr(value, field.name))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings_in(key)
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings_in(item)


def _nan_stat(values: pd.Series, func) -> float:
    values = values.dropna()
    return float(func(values)) if len(values) else float("nan")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_cache_dir = Path(args.source_cache_dir)
    feature_spec_json = Path(args.feature_spec_json)
    universe_spec_json = Path(args.universe_spec_json) if args.universe_spec_json else None
    out_dir = Path(args.out_dir)
    sample_hours = _parse_hours(args.sample_hours)
    if args.bootstrap_reps != 0 and args.bootstrap_reps < 199:
        raise SystemExit("--bootstrap-reps must be 0 (off) or >= 199")
    if args.block_bars < 0 or args.feature_chunk_size < 0 or args.rank_autocorr_lag < 1:
        raise SystemExit("--block-bars and --feature-chunk-size must be >= 0 and --rank-autocorr-lag >= 1")
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_specs = load_feature_specs(feature_spec_json)
    universe_spec = load_universe_spec(universe_spec_json) if universe_spec_json is not None else None
    output_markets = _resolve_output_markets(_parse_markets(args.market_columns), universe_spec)
    required_markets = _resolve_required_markets(output_markets, feature_specs)
    horizons = _parse_horizons(args.forward_horizons)
    requested_subsets = _parse_csv(args.subsets)
    if not requested_subsets:
        requested_subsets = ["base", "selected"]

    feature_columns = [spec.resolved_column_name() for spec in feature_specs]
    include_features = set(_parse_csv(args.features))
    exclude_features = set(_parse_csv(args.exclude_features))
    analysis_features = [
        column
        for column in feature_columns
        if (not include_features or column in include_features) and column not in exclude_features
    ]

    def build_frames(specs) -> dict[str, pd.DataFrame]:
        return build_feature_frames_from_cache(
            source_cache_dir,
            specs,
            market_columns=required_markets,
            output_market_columns=output_markets,
            max_markets=None if required_markets is not None else args.max_markets,
            tail_rows=args.tail_rows,
        )

    def read_price(column: str) -> pd.DataFrame:
        return read_wide_frame_from_cache(
            source_cache_dir,
            column,
            market_columns=output_markets,
            max_markets=None if output_markets is not None else args.max_markets,
            tail_rows=args.tail_rows,
        )

    # chunk size 0 keeps the original flow: every spec is built once and shared.
    shared_frames = build_frames(feature_specs) if args.feature_chunk_size == 0 else None
    if shared_frames is not None and not shared_frames:
        raise SystemExit("feature spec produced no feature frames")

    price_frame = read_price(args.price_column)
    if price_frame.empty:
        raise SystemExit(f"price frame is empty: {args.price_column}")
    price_frame = price_frame.sort_index().sort_index(axis=1)
    open_frame = None
    if args.label_start == "next_open":
        open_frame = read_price(args.open_column)
        if open_frame.empty:
            raise SystemExit(f"open price frame is empty: {args.open_column}")
        open_frame = open_frame.sort_index().sort_index(axis=1).reindex(
            index=price_frame.index, columns=price_frame.columns)

    def align(frames: dict[str, pd.DataFrame]) -> None:
        for column in feature_columns:
            if column in frames:
                frames[column] = frames[column].reindex(index=price_frame.index, columns=price_frame.columns)
        frames[args.price_column] = price_frame

    if shared_frames is not None:
        align(shared_frames)

    if universe_spec is not None:
        if shared_frames is not None:
            universe_frames = shared_frames
        else:
            referenced = set(_strings_in(universe_spec)) & set(feature_columns)
            universe_frames = build_frames(feature_closure(feature_specs, referenced))
            align(universe_frames)
        warning_frame = _read_warning_frame(
            source_cache_dir,
            market_columns=required_markets,
            max_markets=None if required_markets is not None else args.max_markets,
            require_warning=bool(universe_spec.exclude_warnings),
        )
        warning_frame = warning_frame.reindex(index=price_frame.index)
        universe_result = build_universe_mask_v2(universe_frames, warning_frame, universe_spec)
        base_eligible = universe_result.base_eligible_mask.reindex(index=price_frame.index, columns=price_frame.columns)
        selected = universe_result.selection_mask.reindex(index=price_frame.index, columns=price_frame.columns)
        del universe_frames, universe_result
    else:
        base_eligible = price_frame.notna()
        selected = base_eligible.copy()

    if not analysis_features:
        raise SystemExit("no features selected for analysis")

    positions = sample_positions(price_frame.index, every=int(args.sample_every), hours=sample_hours)
    if not len(positions):
        raise SystemExit("sampling selected no rows")
    sample_all = len(positions) == len(price_frame.index)
    step_bars = sample_step_bars(positions)

    def sample(frame: pd.DataFrame) -> pd.DataFrame:
        return frame if sample_all else frame.iloc[positions]

    masks = {name: sample(mask) for name, mask in
             _subset_masks(requested_subsets, price_frame, base_eligible, selected).items()}
    chunk_size = args.feature_chunk_size or len(analysis_features)
    chunks = [analysis_features[start:start + chunk_size] for start in range(0, len(analysis_features), chunk_size)]

    summary_rows: list[dict[str, Any]] = []
    quantile_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    series_parts: list[pd.DataFrame] = []
    reps = int(args.bootstrap_reps)

    for chunk_number, chunk in enumerate(chunks, start=1):
        if shared_frames is not None:
            frames = shared_frames
        else:
            # Peak memory follows the chunk with the most ancestors: the graph keeps
            # every ancestor frame of the chunk until the build returns.
            closure = feature_closure(feature_specs, chunk)
            print(f"chunk {chunk_number}/{len(chunks)}: {len(chunk)} features, {len(closure)} specs built", flush=True)
            frames = build_frames(closure)
            align(frames)
        chunk_frames = {feature: sample(frames[feature]) for feature in chunk}
        if shared_frames is None:
            del frames
        for horizon in horizons:
            label = f"fwd_return_{horizon}"
            forward_return = sample(forward_label_frame(price_frame, open_frame, horizon, args.label_start))
            block_rows = max(1, math.ceil((args.block_bars or horizon) / step_bars))
            for subset_name, mask in masks.items():
                y = forward_return.where(mask)
                y_rank = y.rank(axis=1, method="average")
                for feature in chunk:
                    x = chunk_frames[feature].where(mask)
                    x_rank = x.rank(axis=1, method="average")
                    spearman = _rowwise_corr(x_rank, y_rank, min_count=args.min_count)
                    pearson = _rowwise_corr(x, y, min_count=args.min_count)
                    buckets = _quantile_buckets(x, int(args.quantiles))
                    spread = _bucket_spread(buckets, y, int(args.quantiles), min_count=args.min_count)
                    autocorr = _rowwise_corr(x_rank, x_rank.shift(args.rank_autocorr_lag), min_count=args.min_count)
                    ic_low, ic_high, ic_p = block_bootstrap_mean(
                        spearman.to_numpy(), block=block_rows, reps=reps,
                        seed=_hypothesis_seed(args.bootstrap_seed, subset_name, horizon, feature, "ic"))
                    _, _, spread_p = block_bootstrap_mean(
                        spread.to_numpy(), block=block_rows, reps=reps,
                        seed=_hypothesis_seed(args.bootstrap_seed, subset_name, horizon, feature, "spread"))
                    s = spearman.dropna()
                    summary_rows.append(
                        {
                            "subset": subset_name,
                            "label": label,
                            "horizon": int(horizon),
                            "feature": feature,
                            "n_ic": int(len(s)),
                            "spearman_ic_mean": _nan_stat(s, np.mean),
                            "spearman_ic_median": _nan_stat(s, np.median),
                            "spearman_ic_t": _t_stat(spearman),
                            "spearman_ic_pos_ratio": float((s > 0).mean()) if len(s) else float("nan"),
                            "pearson_ic_mean": _nan_stat(pearson, np.mean),
                            "spearman_ic_ci_low": ic_low,
                            "spearman_ic_ci_high": ic_high,
                            "spearman_ic_p_boot": ic_p,
                            "q_spread_mean": _nan_stat(spread, np.mean),
                            "q_spread_p_boot": spread_p,
                            "n_q_spread": int(spread.notna().sum()),
                            "rank_autocorr_mean": _nan_stat(autocorr, np.mean),
                            "block_rows": int(block_rows),
                        }
                    )

                    ic_frame = spearman.rename("ic").reset_index()
                    ic_frame = ic_frame.rename(columns={ic_frame.columns[0]: "timestamp"})
                    ic_frame["year"] = pd.to_datetime(ic_frame["timestamp"]).dt.year
                    yearly = ic_frame.dropna().groupby("year")["ic"].agg(["mean", "median", "count"]).reset_index()
                    for row in yearly.itertuples(index=False):
                        yearly_rows.append(
                            {
                                "subset": subset_name,
                                "label": label,
                                "horizon": int(horizon),
                                "feature": feature,
                                "year": int(row.year),
                                "ic_mean": float(row.mean),
                                "ic_median": float(row.median),
                                "count": int(row.count),
                            }
                        )

                    quantile_rows.extend(
                        _quantile_rows(
                            subset=subset_name,
                            label=label,
                            feature=feature,
                            x=x,
                            y=y,
                            quantiles=int(args.quantiles),
                            bucket_values=buckets,
                        )
                    )
                    if args.save_ic_series:
                        keep = spearman.notna().to_numpy() | spread.notna().to_numpy()
                        series_parts.append(pd.DataFrame({
                            "timestamp": spearman.index[keep],
                            "subset": subset_name,
                            "horizon": int(horizon),
                            "feature": feature,
                            "ic": spearman.to_numpy()[keep],
                            "q_spread": spread.to_numpy()[keep],
                        }))
            del forward_return
        del chunk_frames

    summary = pd.DataFrame(summary_rows)
    summary["spearman_ic_q_by"] = by_adjust(summary["spearman_ic_p_boot"].to_numpy()) if reps else np.nan
    summary = summary.sort_values(
        ["subset", "label", "spearman_ic_mean"],
        ascending=[True, True, False],
    )
    quantiles = pd.DataFrame(quantile_rows)
    yearly = pd.DataFrame(yearly_rows)
    summary.to_csv(out_dir / "feature_ic_summary.csv", index=False)
    quantiles.to_csv(out_dir / "feature_quantile_returns.csv", index=False)
    yearly.to_csv(out_dir / "feature_ic_by_year.csv", index=False)
    if args.save_ic_series:
        series = pd.concat(series_parts, ignore_index=True) if series_parts else pd.DataFrame(
            columns=["timestamp", "subset", "horizon", "feature", "ic", "q_spread"])
        for column in ("subset", "feature"):
            series[column] = series[column].astype("category")
        series.to_parquet(out_dir / "feature_ic_series.parquet", index=False)

    run_summary = {
        "source_cache_dir": str(source_cache_dir),
        "feature_spec_json": str(feature_spec_json),
        "universe_spec_json": str(universe_spec_json) if universe_spec_json is not None else "",
        "price_column": str(args.price_column),
        "forward_horizons": horizons,
        "subsets": requested_subsets,
        "features": analysis_features,
        "rows": int(price_frame.shape[0]),
        "markets": int(price_frame.shape[1]),
        "start_timestamp": str(price_frame.index.min()) if len(price_frame.index) else "",
        "end_timestamp": str(price_frame.index.max()) if len(price_frame.index) else "",
        "base_eligible_rows": int(base_eligible.fillna(False).astype(bool).sum().sum()),
        "selected_rows": int(selected.fillna(False).astype(bool).sum().sum()),
        "label_start": args.label_start,
        "open_column": args.open_column if args.label_start == "next_open" else "",
        "sample_every": int(args.sample_every),
        "sample_hours_utc": sample_hours,
        "sampled_rows": int(len(positions)),
        "sample_step_bars": int(step_bars),
        "bootstrap_reps": reps,
        "block_bars": int(args.block_bars) if args.block_bars else "horizon",
        "bootstrap_seed": int(args.bootstrap_seed),
        "fdr": "BY over every feature x horizon x subset row of spearman_ic_p_boot" if reps else "",
        "feature_chunk_size": int(args.feature_chunk_size),
        "rank_autocorr_lag_rows": int(args.rank_autocorr_lag),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote analysis to {out_dir}")
    print(f"Features: {len(analysis_features)}")
    print(f"Rows x markets: {price_frame.shape[0]} x {price_frame.shape[1]} (sampled rows {len(positions)})")
    print("Top IC rows:")
    print(
        summary.head(20).to_string(
            index=False,
            float_format=lambda value: f"{value:.6f}",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
