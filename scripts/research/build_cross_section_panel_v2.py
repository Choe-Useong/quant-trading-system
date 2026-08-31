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
            "Build a long cross-sectional research panel from a v2 wide source cache. "
            "The output is suitable for feature diagnostics, IC checks, and ML training."
        )
    )
    parser.add_argument("--source-cache-dir", required=True, help="Wide source cache directory")
    parser.add_argument("--feature-spec-json", required=True, help="v2 feature spec JSON path")
    parser.add_argument("--universe-spec-json", default="", help="Optional v2 universe spec JSON path")
    parser.add_argument("--price-column", default="trade_price", help="Price column used for forward labels")
    parser.add_argument(
        "--forward-horizons",
        default="6,12",
        help="Comma-separated forward label horizons in bars, e.g. 6,12,24",
    )
    parser.add_argument(
        "--market-columns",
        default="",
        help="Optional comma-separated markets to export. Defaults to universe allowed_markets or all cache columns.",
    )
    parser.add_argument("--max-markets", type=int, default=None, help="Optional market limit for smoke runs")
    parser.add_argument("--tail-rows", type=int, default=None, help="Optional tail rows for smoke runs")
    parser.add_argument(
        "--row-filter",
        choices=["none", "base", "selected"],
        default="base",
        help=(
            "Rows to keep in the long panel. 'base' uses base investable rows, "
            "'selected' uses final universe selection rows, and 'none' keeps all price rows."
        ),
    )
    parser.add_argument("--out-dir", required=True, help="Output directory")
    return parser


def _parse_markets(value: str) -> list[str] | None:
    if not value.strip():
        return None
    markets = sorted({item.strip().upper() for item in value.split(",") if item.strip()})
    return markets or None


def _parse_horizons(value: str) -> list[int]:
    horizons: list[int] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        horizon = int(text)
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


def _resolve_output_markets(
    explicit_markets: list[str] | None,
    universe_spec,
) -> list[str] | None:
    if explicit_markets is not None:
        return explicit_markets
    if universe_spec is not None and universe_spec.allowed_markets:
        return sorted(str(market).upper() for market in universe_spec.allowed_markets)
    return None


def _resolve_required_markets(
    output_markets: list[str] | None,
    feature_specs,
) -> list[str] | None:
    referenced_markets = referenced_markets_for_feature_specs(feature_specs)
    if output_markets is None:
        return None
    return sorted(set(output_markets) | set(referenced_markets))


def _stack_frame(frame: pd.DataFrame, name: str, *, index: pd.Index, columns: pd.Index) -> pd.Series:
    aligned = frame.reindex(index=index, columns=columns)
    series = aligned.stack(future_stack=True).rename(name)
    series.index = series.index.set_names(["timestamp", "market"])
    return series


def _future_rank_frame(forward_return: pd.DataFrame) -> pd.DataFrame:
    return forward_return.rank(axis=1, method="first", ascending=False)


def _future_rank_pct_frame(forward_rank: pd.DataFrame, forward_return: pd.DataFrame) -> pd.DataFrame:
    counts = forward_return.notna().sum(axis=1).astype(float)
    denom = counts.sub(1.0).replace(0.0, np.nan)
    pct = 1.0 - forward_rank.sub(1.0).div(denom, axis=0)
    single_mask = counts.eq(1.0)
    if bool(single_mask.any()):
        pct.loc[single_mask, :] = forward_rank.loc[single_mask, :].where(
            forward_rank.loc[single_mask, :].isna(),
            1.0,
        )
    return pct.where(forward_return.notna())


def _build_row_mask(
    row_filter: str,
    price_frame: pd.DataFrame,
    base_eligible: pd.DataFrame,
    selected: pd.DataFrame,
) -> pd.DataFrame:
    if row_filter == "none":
        return price_frame.notna()
    if row_filter == "selected":
        return selected.fillna(False).astype(bool)
    return base_eligible.fillna(False).astype(bool)


def _missing_summary(panel: pd.DataFrame, feature_columns: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    denominator = float(len(panel.index))
    if denominator <= 0.0:
        return rows
    for column in feature_columns:
        if column not in panel.columns:
            continue
        missing_count = int(panel[column].isna().sum())
        rows.append(
            {
                "column": column,
                "missing_count": missing_count,
                "missing_ratio": missing_count / denominator,
            }
        )
    return sorted(rows, key=lambda item: item["missing_ratio"], reverse=True)


def main() -> int:
    args = build_parser().parse_args()
    source_cache_dir = Path(args.source_cache_dir)
    feature_spec_json = Path(args.feature_spec_json)
    universe_spec_json = Path(args.universe_spec_json) if args.universe_spec_json else None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_specs = load_feature_specs(feature_spec_json)
    universe_spec = load_universe_spec(universe_spec_json) if universe_spec_json is not None else None
    explicit_markets = _parse_markets(args.market_columns)
    output_markets = _resolve_output_markets(explicit_markets, universe_spec)
    required_markets = _resolve_required_markets(output_markets, feature_specs)
    horizons = _parse_horizons(args.forward_horizons)

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

    row_mask = _build_row_mask(args.row_filter, price_frame, base_eligible, selected)
    row_index = row_mask.stack(future_stack=True)
    row_index.index = row_index.index.set_names(["timestamp", "market"])
    row_index = row_index[row_index.astype(bool)].index

    panel = pd.DataFrame(index=row_index)
    panel["base_eligible"] = _stack_frame(
        base_eligible.astype(float),
        "base_eligible",
        index=price_frame.index,
        columns=price_frame.columns,
    ).reindex(row_index).fillna(0.0).astype(bool)
    panel["selected"] = _stack_frame(
        selected.astype(float),
        "selected",
        index=price_frame.index,
        columns=price_frame.columns,
    ).reindex(row_index).fillna(0.0).astype(bool)

    for column in feature_columns:
        panel[column] = _stack_frame(
            feature_frames[column],
            column,
            index=price_frame.index,
            columns=price_frame.columns,
        ).reindex(row_index)

    label_columns: list[str] = []
    for horizon in horizons:
        fwd_return = compute_market_forward_return_frame(price_frame, horizon)
        fwd_rank = _future_rank_frame(fwd_return)
        fwd_rank_pct = _future_rank_pct_frame(fwd_rank, fwd_return)
        for name, frame in (
            (f"fwd_return_{horizon}", fwd_return),
            (f"fwd_rank_{horizon}", fwd_rank),
            (f"fwd_rank_pct_{horizon}", fwd_rank_pct),
        ):
            panel[name] = _stack_frame(frame, name, index=price_frame.index, columns=price_frame.columns).reindex(row_index)
            label_columns.append(name)

    panel = panel.reset_index()
    panel_path = out_dir / "panel.parquet"
    panel.to_parquet(panel_path, index=False)

    if len(price_frame.index) > 0:
        start_timestamp = str(price_frame.index.min())
        end_timestamp = str(price_frame.index.max())
    else:
        start_timestamp = ""
        end_timestamp = ""

    summary = {
        "source_cache_dir": str(source_cache_dir),
        "feature_spec_json": str(feature_spec_json),
        "universe_spec_json": str(universe_spec_json) if universe_spec_json is not None else "",
        "price_column": str(args.price_column),
        "forward_horizons": horizons,
        "row_filter": str(args.row_filter),
        "rows": int(len(panel.index)),
        "timestamps": int(price_frame.shape[0]),
        "markets": int(price_frame.shape[1]),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "feature_columns": feature_columns,
        "label_columns": label_columns,
        "base_eligible_rows": int(base_eligible.fillna(False).astype(bool).sum().sum()),
        "selected_rows": int(selected.fillna(False).astype(bool).sum().sum()),
        "missing_summary": _missing_summary(panel, feature_columns + label_columns),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote panel to {panel_path}")
    print(f"Rows: {summary['rows']}")
    print(f"Markets: {summary['markets']}")
    print(f"Range: {summary['start_timestamp']} -> {summary['end_timestamp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
