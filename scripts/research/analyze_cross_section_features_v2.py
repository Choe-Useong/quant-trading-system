#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dataframes import compute_market_forward_return_frame, read_wide_frame_from_cache
from lib.feature_graph_v2 import referenced_markets_for_feature_specs
from lib.features_v2 import build_feature_frames_from_cache
from lib.spec_io import load_feature_specs, load_universe_spec
from lib.universe_v2 import build_universe_mask_v2


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


def _quantile_rows(
    *,
    subset: str,
    label: str,
    feature: str,
    x: pd.DataFrame,
    y: pd.DataFrame,
    quantiles: int,
) -> list[dict[str, Any]]:
    rank = x.rank(axis=1, method="first", ascending=True)
    counts = x.notna().sum(axis=1).astype(float)
    bucket = np.floor((rank.sub(1.0)).div(counts, axis=0).mul(float(quantiles))).add(1.0)
    bucket = bucket.where((bucket >= 1.0) & (bucket <= float(quantiles)))
    y_values = y.to_numpy(dtype=float)
    bucket_values = bucket.to_numpy(dtype=float)
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


def main() -> int:
    args = build_parser().parse_args()
    source_cache_dir = Path(args.source_cache_dir)
    feature_spec_json = Path(args.feature_spec_json)
    universe_spec_json = Path(args.universe_spec_json) if args.universe_spec_json else None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_specs = load_feature_specs(feature_spec_json)
    universe_spec = load_universe_spec(universe_spec_json) if universe_spec_json is not None else None
    output_markets = _resolve_output_markets(_parse_markets(args.market_columns), universe_spec)
    required_markets = _resolve_required_markets(output_markets, feature_specs)
    horizons = _parse_horizons(args.forward_horizons)
    requested_subsets = _parse_csv(args.subsets)
    if not requested_subsets:
        requested_subsets = ["base", "selected"]

    feature_frames = build_feature_frames_from_cache(
        source_cache_dir,
        feature_specs,
        market_columns=required_markets,
        output_market_columns=output_markets,
        max_markets=None if required_markets is not None else args.max_markets,
        tail_rows=args.tail_rows,
    )
    if not feature_frames:
        raise SystemExit("feature spec produced no feature frames")

    price_frame = read_wide_frame_from_cache(
        source_cache_dir,
        args.price_column,
        market_columns=output_markets,
        max_markets=None if output_markets is not None else args.max_markets,
        tail_rows=args.tail_rows,
    )
    if price_frame.empty:
        raise SystemExit(f"price frame is empty: {args.price_column}")
    price_frame = price_frame.sort_index().sort_index(axis=1)

    feature_columns = [spec.resolved_column_name() for spec in feature_specs]
    for column in feature_columns:
        feature_frames[column] = feature_frames[column].reindex(index=price_frame.index, columns=price_frame.columns)
    feature_frames[args.price_column] = price_frame

    if universe_spec is not None:
        warning_frame = _read_warning_frame(
            source_cache_dir,
            market_columns=required_markets,
            max_markets=None if required_markets is not None else args.max_markets,
            require_warning=bool(universe_spec.exclude_warnings),
        )
        warning_frame = warning_frame.reindex(index=price_frame.index)
        universe_result = build_universe_mask_v2(feature_frames, warning_frame, universe_spec)
        base_eligible = universe_result.base_eligible_mask.reindex(index=price_frame.index, columns=price_frame.columns)
        selected = universe_result.selection_mask.reindex(index=price_frame.index, columns=price_frame.columns)
    else:
        base_eligible = price_frame.notna()
        selected = base_eligible.copy()

    include_features = set(_parse_csv(args.features))
    exclude_features = set(_parse_csv(args.exclude_features))
    analysis_features = [
        column
        for column in feature_columns
        if (not include_features or column in include_features) and column not in exclude_features
    ]
    if not analysis_features:
        raise SystemExit("no features selected for analysis")

    masks = _subset_masks(requested_subsets, price_frame, base_eligible, selected)
    summary_rows: list[dict[str, Any]] = []
    quantile_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []

    for horizon in horizons:
        label = f"fwd_return_{horizon}"
        forward_return = compute_market_forward_return_frame(price_frame, horizon)
        for subset_name, mask in masks.items():
            y = forward_return.where(mask)
            for feature in analysis_features:
                x = feature_frames[feature].where(mask)
                spearman = _rowwise_corr(
                    x.rank(axis=1, method="average"),
                    y.rank(axis=1, method="average"),
                    min_count=args.min_count,
                )
                pearson = _rowwise_corr(x, y, min_count=args.min_count)
                s = spearman.dropna()
                p = pearson.dropna()
                summary_rows.append(
                    {
                        "subset": subset_name,
                        "label": label,
                        "horizon": int(horizon),
                        "feature": feature,
                        "n_ic": int(len(s)),
                        "spearman_ic_mean": float(s.mean()) if len(s) else float("nan"),
                        "spearman_ic_median": float(s.median()) if len(s) else float("nan"),
                        "spearman_ic_t": _t_stat(spearman),
                        "spearman_ic_pos_ratio": float((s > 0).mean()) if len(s) else float("nan"),
                        "pearson_ic_mean": float(p.mean()) if len(p) else float("nan"),
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
                    )
                )

    summary = pd.DataFrame(summary_rows).sort_values(
        ["subset", "label", "spearman_ic_mean"],
        ascending=[True, True, False],
    )
    quantiles = pd.DataFrame(quantile_rows)
    yearly = pd.DataFrame(yearly_rows)
    summary.to_csv(out_dir / "feature_ic_summary.csv", index=False)
    quantiles.to_csv(out_dir / "feature_quantile_returns.csv", index=False)
    yearly.to_csv(out_dir / "feature_ic_by_year.csv", index=False)

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
    }
    (out_dir / "summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote analysis to {out_dir}")
    print(f"Features: {len(analysis_features)}")
    print(f"Rows x markets: {price_frame.shape[0]} x {price_frame.shape[1]}")
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
