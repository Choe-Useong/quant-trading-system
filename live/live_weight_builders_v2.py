from __future__ import annotations

from pathlib import Path

import pandas as pd

from lib.dataframes import read_wide_frames_from_cache
from lib.feature_graph_v2 import referenced_markets_for_feature_specs
from lib.features_v2 import build_feature_frames_from_cache
from lib.market_scores_v2 import build_market_score_frame, required_markets_for_market_score_spec
from lib.spec_io import load_feature_specs, load_market_score_spec, load_universe_spec, load_weight_spec
from lib.universe_v2 import build_universe_mask_v2
from lib.weights_v2 import build_weight_frame_v2, is_change_only_weight_output
from live.live_data_utils import apply_market_filters, merge_live_rows
from lib.upbit_collector import CandleRow, Market, collect_minute_candles, fetch_minute_candle_batch


UPBIT_LIVE_SOURCE_COLUMNS = (
    "trade_price",
    "signal_price",
    "opening_price",
    "high_price",
    "low_price",
    "candle_acc_trade_volume",
    "candle_acc_trade_price",
    "timestamp",
)


def _last_closed_candle_start_utc(minute_unit: int, now: pd.Timestamp | None = None) -> pd.Timestamp:
    current = pd.Timestamp.utcnow() if now is None else pd.Timestamp(now)
    if current.tzinfo is not None:
        current = current.tz_convert("UTC").tz_localize(None)
    else:
        current = current.tz_localize(None)
    minute_unit = max(int(minute_unit), 1)
    current_bucket_minute = (current.minute // minute_unit) * minute_unit
    current_bucket = current.replace(minute=current_bucket_minute, second=0, microsecond=0, nanosecond=0)
    return current_bucket - pd.Timedelta(minutes=minute_unit)


def _drop_incomplete_live_rows(
    rows: list[CandleRow],
    minute_unit: int,
    cutoff: pd.Timestamp | None = None,
) -> tuple[list[CandleRow], int, pd.Timestamp]:
    cutoff = _last_closed_candle_start_utc(minute_unit) if cutoff is None else pd.Timestamp(cutoff)
    completed = [row for row in rows if pd.Timestamp(row.date_utc) <= cutoff]
    return completed, len(rows) - len(completed), cutoff


def _history_param_bars(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    bars = int(value)
    if bars <= 0:
        return 0
    normalized = str(key).strip().lower()
    if normalized in {"window", "period", "periods", "lag", "lookback", "lookback_bars", "bars", "span"}:
        return bars
    if normalized.endswith(("_window", "_period", "_periods", "_lag", "_lookback", "_bars", "_span")):
        return bars
    return 0


def _feature_history_bars(spec) -> tuple[int, bool]:
    uses_state = getattr(spec, "state", None) is not None
    steps = getattr(spec, "steps", ()) or ()
    if not steps:
        return 1, uses_state

    bars = 1
    for step in steps:
        step_bars = 1
        for key, value in (step.params or {}).items():
            step_bars = max(step_bars, _history_param_bars(key, value))
        bars += max(step_bars - 1, 0)
    return bars, uses_state


def _feature_dependency_names(spec, supported_source_columns: set[str]) -> list[str]:
    if spec.compare is not None:
        dependencies = [spec.compare.left_feature]
        if spec.compare.right_feature is not None:
            dependencies.append(spec.compare.right_feature)
        return dependencies
    if spec.logical is not None:
        return list(spec.logical.features)
    if spec.state is not None:
        return [spec.state.entry_feature, spec.state.exit_feature]
    if spec.breadth is not None:
        return [spec.breadth.driver_feature, spec.breadth.signal_feature]
    if spec.components:
        return [component.feature_column for component in spec.components]

    dependencies: list[str] = []
    source = getattr(spec, "source", None)
    if source is not None and source not in supported_source_columns:
        if str(source).startswith("market:"):
            parts = str(source).split(":", 2)
            if len(parts) == 3 and parts[2] not in supported_source_columns:
                dependencies.append(parts[2])
        else:
            dependencies.append(source)

    for step in getattr(spec, "steps", ()) or ():
        if step.kind in {"subtract_reference", "ratio_to_reference", "residualize_reference"}:
            reference = str(step.params["reference"])
            if reference not in supported_source_columns:
                dependencies.append(reference)
        elif step.kind == "mask_by_feature":
            feature = str(step.params["feature"])
            if feature not in supported_source_columns:
                dependencies.append(feature)
        elif step.kind == "corr_greedy_filter":
            for key, default in (
                ("price_feature", "signal_price"),
                ("liquidity_feature", "candle_acc_trade_price"),
            ):
                reference = str(step.params.get(key, default))
                if reference not in supported_source_columns:
                    dependencies.append(reference)
        elif step.kind == "true_range":
            for key in ("low", "close"):
                reference = str(step.params.get(key, f"{key}_price"))
                if reference not in supported_source_columns:
                    dependencies.append(reference)
    return dependencies


def _infer_required_history_bars(feature_specs: list, universe_spec, refresh_candles: int) -> int:
    supported_source_columns = set(UPBIT_LIVE_SOURCE_COLUMNS) | {"market_warning"}
    specs_by_name = {spec.resolved_column_name(): spec for spec in feature_specs}
    history_cache: dict[str, int] = {}
    visiting: set[str] = set()
    uses_stateful_feature = any(getattr(spec, "state", None) is not None for spec in feature_specs)

    def required_bars_for_feature(name: str) -> int:
        if name in supported_source_columns:
            return 1
        if name in history_cache:
            return history_cache[name]
        spec = specs_by_name.get(name)
        if spec is None:
            return 1
        if name in visiting:
            raise ValueError(f"Cyclic feature dependency while inferring live history: {name}")

        visiting.add(name)
        own_bars, _ = _feature_history_bars(spec)
        dependencies = _feature_dependency_names(spec, supported_source_columns)
        if dependencies:
            required_bars = max(required_bars_for_feature(dependency) for dependency in dependencies) + own_bars - 1
        else:
            required_bars = own_bars
        visiting.remove(name)
        history_cache[name] = max(required_bars, 1)
        return history_cache[name]

    max_feature_bars = max(
        (required_bars_for_feature(spec.resolved_column_name()) for spec in feature_specs),
        default=1,
    )

    lag_values = [int(getattr(universe_spec, "lag", 0) or 0), int(getattr(universe_spec, "signal_lag", 0) or 0)]
    lag_values.extend(int(getattr(item, "lag", 0) or 0) for item in getattr(universe_spec, "value_filters", ()) or ())
    lag_values.extend(int(getattr(item, "lag", 0) or 0) for item in getattr(universe_spec, "rank_filters", ()) or ())
    for stage in getattr(universe_spec, "filter_stages", ()) or ():
        lag_values.extend(int(getattr(item, "lag", 0) or 0) for item in getattr(stage, "filters", ()) or ())
    max_lag = max(lag_values) if lag_values else 0

    inferred_bars = max(refresh_candles + max_feature_bars + max_lag + 10, 500)
    if uses_stateful_feature:
        inferred_bars = max(inferred_bars, max_feature_bars + 250)
    return inferred_bars


def _resolve_required_markets(feature_specs, universe_spec, market_score_spec=None) -> list[str]:
    explicit_markets: set[str] = set(str(market).upper() for market in (universe_spec.allowed_markets or ()))
    referenced_markets: set[str] = set(referenced_markets_for_feature_specs(feature_specs))
    if market_score_spec is not None:
        referenced_markets.update(required_markets_for_market_score_spec(market_score_spec))
    resolved = sorted(explicit_markets | referenced_markets)
    if not resolved:
        raise ValueError("portfolio_pipeline_v2 live execution requires explicit or referenced markets")
    return resolved


def _rows_to_frames(merged_rows_by_market: dict[str, list]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, float]]:
    record_rows: list[dict[str, object]] = []
    latest_price_by_market: dict[str, float] = {}
    for market, rows in merged_rows_by_market.items():
        if not rows:
            continue
        latest_price_by_market[market] = float(rows[-1].trade_price)
        for row in rows:
            record_rows.append(
                {
                    "date_utc": pd.Timestamp(row.date_utc),
                    "market": str(row.market).upper(),
                    "market_warning": str(row.market_warning).upper(),
                    "trade_price": float(row.trade_price),
                    "signal_price": float(row.trade_price),
                    "opening_price": float(row.opening_price),
                    "high_price": float(row.high_price),
                    "low_price": float(row.low_price),
                    "candle_acc_trade_volume": float(row.candle_acc_trade_volume),
                    "candle_acc_trade_price": float(row.candle_acc_trade_price),
                    "timestamp": float(row.timestamp) if row.timestamp is not None else float("nan"),
                }
            )

    frame = pd.DataFrame.from_records(record_rows).sort_values(["date_utc", "market"])
    if frame.empty:
        return {}, pd.DataFrame(), latest_price_by_market

    source_frames: dict[str, pd.DataFrame] = {}
    indexed = frame.set_index("date_utc")
    for column in UPBIT_LIVE_SOURCE_COLUMNS:
        source_frames[column] = indexed.pivot(columns="market", values=column).sort_index().sort_index(axis=1)
    warning_frame = indexed.pivot(columns="market", values="market_warning").sort_index().sort_index(axis=1)
    return source_frames, warning_frame, latest_price_by_market


def _infer_minute_unit(execution_config: dict[str, object]) -> int:
    explicit = execution_config.get("candle_unit")
    if explicit is not None:
        return int(explicit)
    candle_dir = Path(execution_config["candle_dir"])
    try:
        return int(candle_dir.name)
    except Exception as exc:
        raise ValueError("portfolio_pipeline_v2 requires candle_unit or a candle_dir ending with the minute unit") from exc


def _merge_latest_rows_from_cache(
    source_cache_dir: Path,
    market: str,
    required_history_bars: int,
    refresh_candles: int,
    minute_unit: int,
    last_closed_timestamp: pd.Timestamp,
    diagnostics: list[dict[str, object]] | None = None,
) -> list[CandleRow]:
    market_columns = [market]
    frames = read_wide_frames_from_cache(
        source_cache_dir,
        [
            "trade_price",
            "opening_price",
            "high_price",
            "low_price",
            "candle_acc_trade_volume",
            "candle_acc_trade_price",
            "timestamp",
        ],
        market_columns=market_columns,
        tail_rows=required_history_bars,
    )
    warning_frame = read_wide_frames_from_cache(
        source_cache_dir,
        ["market_warning"],
        market_columns=market_columns,
        tail_rows=required_history_bars,
    )["market_warning"]
    trade_frame = frames["trade_price"]
    if trade_frame.empty or market not in trade_frame.columns:
        raise FileNotFoundError(f"Missing cached frame data for market: {market}")

    local_rows: list[CandleRow] = []
    for ts, trade_value in trade_frame[market].dropna().items():
        local_rows.append(
            CandleRow(
                market=market,
                korean_name=market,
                english_name=market,
                market_warning=str(warning_frame.at[ts, market]) if market in warning_frame.columns and pd.notna(warning_frame.at[ts, market]) else "NONE",
                date_utc=pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%S"),
                date_kst="",
                opening_price=float(frames["opening_price"].at[ts, market]),
                high_price=float(frames["high_price"].at[ts, market]),
                low_price=float(frames["low_price"].at[ts, market]),
                trade_price=float(trade_value),
                candle_acc_trade_volume=float(frames["candle_acc_trade_volume"].at[ts, market]),
                candle_acc_trade_price=float(frames["candle_acc_trade_price"].at[ts, market]),
                timestamp=None if pd.isna(frames["timestamp"].at[ts, market]) else int(float(frames["timestamp"].at[ts, market])),
            )
        )

    market_meta = Market(
        market=market,
        korean_name=market,
        english_name=market,
        market_warning=local_rows[-1].market_warning if local_rows else "NONE",
    )
    cache_last = pd.Timestamp(local_rows[-1].date_utc)
    latest_probe = fetch_minute_candle_batch(market_meta, unit=minute_unit, count=1)
    latest_api_timestamp = pd.Timestamp(latest_probe[-1].date_utc) if latest_probe else cache_last
    latest_timestamp = min(latest_api_timestamp, last_closed_timestamp)
    interval_seconds = max(int(minute_unit) * 60, 1)
    missing_bars = max(0, int((latest_timestamp - cache_last).total_seconds() // interval_seconds))
    fetch_count = max(int(refresh_candles), missing_bars + 5)

    if fetch_count <= 200:
        latest_rows = fetch_minute_candle_batch(market_meta, unit=minute_unit, count=fetch_count)
    else:
        latest_rows = collect_minute_candles(market_meta, unit=minute_unit, candles=fetch_count)

    latest_rows, dropped_incomplete_rows, cutoff_timestamp = _drop_incomplete_live_rows(
        latest_rows,
        minute_unit,
        cutoff=last_closed_timestamp,
    )
    new_rows = [row for row in latest_rows if pd.Timestamp(row.date_utc) > cache_last]
    merged_by_date = {row.date_utc: row for row in local_rows}
    for row in new_rows:
        merged_by_date[row.date_utc] = row
    merged_rows = [merged_by_date[key] for key in sorted(merged_by_date)]

    if diagnostics is not None:
        diagnostics.append(
            {
                "market": market,
                "source": "cache+api_gap_fill",
                "cache_last": cache_last.isoformat(),
                "api_latest": latest_api_timestamp.isoformat(),
                "last_closed_candle_start": cutoff_timestamp.isoformat(),
                "missing_bars_estimate": missing_bars,
                "requested_api_candles": fetch_count,
                "fetched_api_rows": len(latest_rows),
                "dropped_incomplete_rows": dropped_incomplete_rows,
                "new_rows_after_cache": len(new_rows),
                "merged_rows": len(merged_rows),
                "merged_last": pd.Timestamp(merged_rows[-1].date_utc).isoformat() if merged_rows else None,
            }
        )
    return merged_rows


def build_latest_pipeline_weights_v2(execution_config: dict[str, object], refresh_candles: int) -> tuple[list[dict[str, str]], dict[str, float]]:
    feature_specs = load_feature_specs(Path(execution_config["features_spec_json"]))
    market_scores_path = execution_config.get("market_scores_spec_json")
    market_score_spec = load_market_score_spec(Path(market_scores_path)) if market_scores_path else None
    universe_spec = load_universe_spec(Path(execution_config["universe_spec_json"]))
    weight_spec = load_weight_spec(Path(execution_config["weights_spec_json"]))

    required_markets = apply_market_filters(
        _resolve_required_markets(feature_specs, universe_spec, market_score_spec),
        execution_config,
    )
    required_history_bars = _infer_required_history_bars(feature_specs, universe_spec, refresh_candles)
    minute_unit = _infer_minute_unit(execution_config)
    last_closed_timestamp = _last_closed_candle_start_utc(minute_unit)
    source_cache_dir = execution_config.get("source_cache_dir")
    diagnostics: list[dict[str, object]] = []

    merged_rows_by_market: dict[str, list] = {}
    for market in required_markets:
        if source_cache_dir:
            merged_rows_by_market[market] = _merge_latest_rows_from_cache(
                Path(source_cache_dir),
                market,
                required_history_bars,
                refresh_candles,
                minute_unit,
                last_closed_timestamp,
                diagnostics,
            )
        else:
            merged_rows_by_market[market] = merge_live_rows(
                Path(execution_config["candle_dir"]),
                market,
                refresh_candles,
                required_history_bars,
                minute_unit=minute_unit,
            )
            merged_rows_by_market[market], dropped_incomplete_rows, cutoff_timestamp = _drop_incomplete_live_rows(
                merged_rows_by_market[market],
                minute_unit,
                cutoff=last_closed_timestamp,
            )
            if merged_rows_by_market[market]:
                diagnostics.append(
                    {
                        "market": market,
                        "source": "csv+api_refresh",
                        "merged_rows": len(merged_rows_by_market[market]),
                        "last_closed_candle_start": cutoff_timestamp.isoformat(),
                        "dropped_incomplete_rows": dropped_incomplete_rows,
                        "merged_last": pd.Timestamp(merged_rows_by_market[market][-1].date_utc).isoformat(),
                    }
                )

    execution_config["_live_data_diagnostics"] = diagnostics

    merged_last_by_market = {
        market: pd.Timestamp(rows[-1].date_utc)
        for market, rows in merged_rows_by_market.items()
        if rows
    }
    if len(merged_last_by_market) != len(required_markets):
        missing_markets = sorted(set(required_markets) - set(merged_last_by_market))
        raise ValueError(f"Missing live candle rows for markets: {missing_markets}")
    if len(set(merged_last_by_market.values())) > 1:
        formatted = {
            market: timestamp.isoformat()
            for market, timestamp in sorted(merged_last_by_market.items())
        }
        raise ValueError(f"Live candle timestamps are not aligned: {formatted}")

    source_frames, warning_frame, latest_price_by_market = _rows_to_frames(merged_rows_by_market)
    if not source_frames:
        return [], latest_price_by_market

    feature_frames = build_feature_frames_from_cache(
        Path(execution_config["candle_dir"]),
        feature_specs,
        market_columns=required_markets,
        source_frames=source_frames,
    )
    if market_score_spec is not None:
        feature_frames[market_score_spec.output_column] = build_market_score_frame(feature_frames, market_score_spec)

    reference_index = next(iter(feature_frames.values())).index
    warning_frame = warning_frame.reindex(index=reference_index, columns=sorted(required_markets)).fillna("NONE")

    universe_result = build_universe_mask_v2(feature_frames, warning_frame, universe_spec)
    weight_frame = build_weight_frame_v2(universe_result.selection_mask, weight_spec, feature_frames)
    change_only_weights = is_change_only_weight_output(weight_spec)

    valid_rows = weight_frame.notna().any(axis=1)
    if change_only_weights:
        latest_date = pd.Timestamp(weight_frame.index[-1])
        latest_sparse_series = weight_frame.loc[latest_date]
        execution_config["_latest_target_changed"] = bool(latest_sparse_series.notna().any())
        latest_series = weight_frame.ffill().fillna(0.0).loc[latest_date]
    elif not bool(valid_rows.any()):
        return [], latest_price_by_market
    else:
        execution_config["_latest_target_changed"] = True
        latest_date = pd.Timestamp(valid_rows[valid_rows].index[-1])
        latest_series = weight_frame.loc[latest_date].fillna(0.0)

    latest_rows = [
        {
            "date_utc": latest_date.isoformat(),
            "market": str(market).upper(),
            "target_weight": f"{float(weight):.12g}",
        }
        for market, weight in latest_series.items()
    ]
    return latest_rows, latest_price_by_market
