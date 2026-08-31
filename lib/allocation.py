from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class SleeveSource:
    name: str
    weights_csv: Path
    capital_weight: float
    weight_scale_mode: str = "keep_source"


def load_allocation_config(path: Path, root_dir: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    sleeves: list[SleeveSource] = []
    for item in payload.get("sleeves", []):
        weights_csv = Path(str(item["weights_csv"]))
        if not weights_csv.is_absolute():
            weights_csv = root_dir / weights_csv
        sleeves.append(
            SleeveSource(
                name=str(item["name"]),
                weights_csv=weights_csv,
                capital_weight=float(item.get("capital_weight", 0.0)),
                weight_scale_mode=str(item.get("weight_scale_mode", "keep_source")),
            )
        )
    return {
        "portfolio_name": str(payload["portfolio_name"]),
        "sleeves": sleeves,
        "portfolio_inactive_mode": str(payload.get("portfolio_inactive_mode", "keep_cash")),
        "market_caps": {str(key).upper(): float(value) for key, value in payload.get("market_caps", {}).items()},
        "cap_overflow_mode": str(payload.get("cap_overflow_mode", "keep_cash")),
    }


def _load_sparse_weight_frame(weights_csv: Path) -> tuple[pd.DataFrame, dict[str, dict[str, str]]]:
    frame = pd.read_csv(weights_csv, encoding="utf-8-sig", dtype=str)
    if frame.empty:
        return pd.DataFrame(columns=["date_utc", "market", "target_weight"]), {}
    frame["date_utc"] = pd.to_datetime(frame["date_utc"], utc=False)
    frame["market"] = frame["market"].astype(str).str.upper()
    frame["target_weight"] = pd.to_numeric(frame["target_weight"], errors="coerce").fillna(0.0)
    metadata_by_market: dict[str, dict[str, str]] = {}
    for row in frame.to_dict(orient="records"):
        market = str(row["market"])
        metadata_by_market.setdefault(
            market,
            {
                "korean_name": str(row.get("korean_name", "")),
                "english_name": str(row.get("english_name", "")),
                "market_warning": str(row.get("market_warning", "")),
            },
        )
    return frame, metadata_by_market


def _expand_source_frame(frame: pd.DataFrame, all_dates: pd.DatetimeIndex, source_markets: list[str]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(0.0, index=all_dates, columns=source_markets)
    explicit_dates = pd.DatetimeIndex(sorted(frame["date_utc"].drop_duplicates().tolist()))
    pivot = frame.pivot_table(index="date_utc", columns="market", values="target_weight", aggfunc="sum")
    pivot = pivot.reindex(index=explicit_dates, columns=source_markets).fillna(0.0)
    return pivot.reindex(all_dates).ffill().fillna(0.0)


def _scale_source_frame(frame: pd.DataFrame, capital_weight: float, weight_scale_mode: str) -> pd.DataFrame:
    if capital_weight <= 0.0:
        return frame * 0.0
    if weight_scale_mode == "keep_source":
        return frame * capital_weight
    if weight_scale_mode == "normalize_to_cap":
        row_sum = frame.sum(axis=1)
        normalized = frame.div(row_sum.replace(0.0, pd.NA), axis=0).fillna(0.0)
        return normalized * capital_weight
    raise ValueError(f"Unsupported weight_scale_mode: {weight_scale_mode}")


def _rescale_frame_to_target_total(frame: pd.DataFrame, target_total: float) -> pd.DataFrame:
    if frame.empty:
        return frame
    row_sum = frame.sum(axis=1)
    normalized = frame.div(row_sum.replace(0.0, pd.NA), axis=0).fillna(0.0)
    scaled = normalized.mul(target_total)
    return scaled.where(row_sum.gt(0.0), 0.0)


def _apply_caps_to_row(row: pd.Series, market_caps: dict[str, float], overflow_mode: str) -> pd.Series:
    if not market_caps:
        return row
    if overflow_mode == "keep_cash":
        capped = row.copy()
        for market, limit in market_caps.items():
            if market in capped.index:
                capped.loc[market] = min(float(capped.loc[market]), float(limit))
        return capped
    if overflow_mode != "redistribute":
        raise ValueError(f"Unsupported cap_overflow_mode: {overflow_mode}")

    base_weights = {str(market): float(value) for market, value in row.items() if float(value) > 0.0}
    if not base_weights:
        return row * 0.0

    target_total = sum(base_weights.values())
    free_markets = set(base_weights)
    result: dict[str, float] = {}
    remaining_total = target_total

    while free_markets:
        base_sum = sum(base_weights[market] for market in free_markets)
        if base_sum <= 0.0:
            break
        scaled = {market: remaining_total * base_weights[market] / base_sum for market in free_markets}
        breached = [market for market, value in scaled.items() if market in market_caps and value > float(market_caps[market])]
        if not breached:
            for market, value in scaled.items():
                result[market] = value
            break
        for market in breached:
            capped_value = float(market_caps[market])
            result[market] = capped_value
            remaining_total -= capped_value
            free_markets.remove(market)

    capped = pd.Series(0.0, index=row.index, dtype=float)
    for market, value in result.items():
        if market in capped.index:
            capped.loc[market] = value
    return capped


def apply_market_caps_frame(frame: pd.DataFrame, market_caps: dict[str, float], overflow_mode: str) -> pd.DataFrame:
    if frame.empty or not market_caps:
        return frame
    return frame.apply(lambda row: _apply_caps_to_row(row, market_caps, overflow_mode), axis=1)


def build_allocated_weight_frame(
    sleeves: list[SleeveSource],
    portfolio_inactive_mode: str = "keep_cash",
    market_caps: dict[str, float] | None = None,
    cap_overflow_mode: str = "keep_cash",
) -> tuple[pd.DataFrame, dict[str, dict[str, str]], list[str]]:
    if not sleeves:
        return pd.DataFrame(), {}, []

    source_frames: list[tuple[SleeveSource, pd.DataFrame]] = []
    metadata_by_market: dict[str, dict[str, str]] = {}
    source_names: list[str] = []
    all_dates: set[pd.Timestamp] = set()
    all_markets: set[str] = set()

    for sleeve in sleeves:
        frame, sleeve_metadata = _load_sparse_weight_frame(sleeve.weights_csv)
        source_frames.append((sleeve, frame))
        source_names.append(sleeve.name)
        if not frame.empty:
            all_dates.update(pd.DatetimeIndex(frame["date_utc"].drop_duplicates()))
            all_markets.update(frame["market"].drop_duplicates().tolist())
        metadata_by_market.update({market: sleeve_metadata[market] for market in sleeve_metadata if market not in metadata_by_market})

    all_dates_index = pd.DatetimeIndex(sorted(all_dates))
    all_markets_list = sorted(all_markets)
    if all_dates_index.empty or not all_markets_list:
        return pd.DataFrame(), metadata_by_market, source_names

    combined = pd.DataFrame(0.0, index=all_dates_index, columns=all_markets_list)
    for sleeve, frame in source_frames:
        source_markets = sorted(frame["market"].drop_duplicates().tolist()) if not frame.empty else []
        expanded = _expand_source_frame(frame, all_dates_index, source_markets)
        scaled = _scale_source_frame(expanded, sleeve.capital_weight, sleeve.weight_scale_mode)
        combined = combined.add(scaled.reindex(columns=all_markets_list, fill_value=0.0), fill_value=0.0)

    if portfolio_inactive_mode == "redistribute":
        target_total = min(sum(sleeve.capital_weight for sleeve in sleeves), 1.0)
        combined = _rescale_frame_to_target_total(combined, target_total)
    elif portfolio_inactive_mode != "keep_cash":
        raise ValueError(f"Unsupported portfolio_inactive_mode: {portfolio_inactive_mode}")

    combined = apply_market_caps_frame(combined, market_caps or {}, cap_overflow_mode)
    return combined.sort_index().sort_index(axis=1), metadata_by_market, source_names


def _date_kst_from_timestamp(timestamp: pd.Timestamp) -> str:
    return (timestamp + pd.Timedelta(hours=9)).isoformat()


def compress_weight_frame_to_rows(
    frame: pd.DataFrame,
    metadata_by_market: dict[str, dict[str, str]],
    weights_name: str,
    universe_name: str,
    source_names: list[str] | None = None,
) -> list[dict[str, str]]:
    if frame.empty:
        return []

    source_feature = ",".join(source_names or [])
    rows: list[dict[str, str]] = []
    fallback_market = frame.columns[0] if len(frame.columns) > 0 else ""
    previous_row: pd.Series | None = None

    for timestamp, current in frame.iterrows():
        if previous_row is not None and current.equals(previous_row):
            continue
        previous_row = current.copy()
        active = current[current > 0.0]
        date_utc = timestamp.isoformat()
        date_kst = _date_kst_from_timestamp(timestamp)
        gross_exposure = float(current.sum())

        if active.empty:
            meta = metadata_by_market.get(
                fallback_market,
                {"korean_name": "", "english_name": "", "market_warning": ""},
            )
            rows.append(
                {
                    "date_utc": date_utc,
                    "date_kst": date_kst,
                    "market": fallback_market,
                    "korean_name": meta["korean_name"],
                    "english_name": meta["english_name"],
                    "market_warning": meta["market_warning"],
                    "feature_column": "allocation",
                    "feature_value": source_feature,
                    "rank": "",
                    "selected_rank": "",
                    "weight_rank": "",
                    "target_weight": "0",
                    "gross_exposure": "0",
                    "weighting": "allocation",
                    "rebalance_frequency": "sparse",
                    "weights_name": weights_name,
                    "universe_name": universe_name,
                }
            )
            continue

        for local_rank, (market, weight) in enumerate(active.sort_index().items(), start=1):
            meta = metadata_by_market.get(
                str(market),
                {"korean_name": "", "english_name": "", "market_warning": ""},
            )
            rows.append(
                {
                    "date_utc": date_utc,
                    "date_kst": date_kst,
                    "market": str(market),
                    "korean_name": meta["korean_name"],
                    "english_name": meta["english_name"],
                    "market_warning": meta["market_warning"],
                    "feature_column": "allocation",
                    "feature_value": source_feature,
                    "rank": str(local_rank),
                    "selected_rank": str(local_rank),
                    "weight_rank": str(local_rank),
                    "target_weight": f"{float(weight):.12g}",
                    "gross_exposure": f"{gross_exposure:.12g}",
                    "weighting": "allocation",
                    "rebalance_frequency": "sparse",
                    "weights_name": weights_name,
                    "universe_name": universe_name,
                }
            )
    return rows
