#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_CACHE_DIR = ROOT / "data" / "stocks_cache" / "kr_stock_daily"
MARKET_CODE_MAP = {
    "KOSPI": 1.0,
    "KOSDAQ": 2.0,
    "KOSDAQ GLOBAL": 3.0,
}
STOCK_META_FILE = "market_meta.parquet"
REPORT_FILE = "source_cache_report.json"
INDEX_PRICE_FILE = "index_price.parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a reduced Korean stock v2 source cache from the full wide cache. "
            "The reduced cache keeps the union of monthly average market-cap top-N stocks."
        )
    )
    parser.add_argument("--source-cache-dir", default=str(DEFAULT_SOURCE_CACHE_DIR))
    parser.add_argument(
        "--out-dir",
        default="",
        help=(
            "Output directory. Default: "
            "data/stocks_cache/kr_stock_daily_{market}_avgmcap{mcap_top_n}_union"
        ),
    )
    parser.add_argument("--mcap-top-n", type=int, default=100)
    parser.add_argument("--market", default="KOSPI", help="Market name, for example KOSPI or KOSDAQ.")
    parser.add_argument("--start", default="", help="Optional output/selection start date, YYYY-MM-DD.")
    parser.add_argument("--end", default="", help="Optional output/selection end date, YYYY-MM-DD.")
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _normalize_market(value: str) -> str:
    return str(value).strip().upper()


def _default_out_dir(market: str, top_n: int) -> Path:
    market_token = _normalize_market(market).lower().replace(" ", "_")
    return ROOT / "data" / "stocks_cache" / f"kr_stock_daily_{market_token}_avgmcap{top_n}_union"


def _date_filtered_index(index: pd.Index, start: str, end: str) -> pd.DatetimeIndex:
    dt_index = pd.to_datetime(index).normalize()
    mask = pd.Series(True, index=dt_index)
    if start:
        mask &= dt_index >= pd.Timestamp(start).normalize()
    if end:
        mask &= dt_index <= pd.Timestamp(end).normalize()
    return pd.DatetimeIndex(dt_index[mask.to_numpy()])


def _load_wide_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    frame.index = pd.to_datetime(frame.index).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.columns = [str(column).strip().upper() for column in frame.columns]
    frame = frame.loc[:, ~pd.Index(frame.columns).duplicated()]
    return frame.sort_index(axis=1)


def _select_union_markets(
    source_cache_dir: Path,
    *,
    market: str,
    top_n: int,
    start: str,
    end: str,
) -> tuple[list[str], dict[str, object]]:
    if top_n <= 0:
        raise ValueError("--mcap-top-n must be positive")

    market_name = _normalize_market(market)
    if market_name not in MARKET_CODE_MAP:
        known = ", ".join(sorted(MARKET_CODE_MAP))
        raise ValueError(f"Unsupported market '{market}'. Known markets: {known}")

    market_cap_path = source_cache_dir / "market_cap.parquet"
    market_code_path = source_cache_dir / "market_code.parquet"
    if not market_cap_path.exists():
        raise FileNotFoundError(market_cap_path)
    if not market_code_path.exists():
        raise FileNotFoundError(market_code_path)

    market_cap = _load_wide_frame(market_cap_path)
    market_code = _load_wide_frame(market_code_path).reindex(index=market_cap.index, columns=market_cap.columns)
    selected_index = _date_filtered_index(market_cap.index, start, end)
    market_cap = market_cap.reindex(index=selected_index)
    market_code = market_code.reindex(index=selected_index)

    eligible = market_code.eq(float(MARKET_CODE_MAP[market_name]))
    price_available_path = source_cache_dir / "price_available.parquet"
    if price_available_path.exists():
        price_available = _load_wide_frame(price_available_path).reindex(index=selected_index, columns=market_cap.columns)
        eligible &= price_available.ge(1.0)
    marcap_available_path = source_cache_dir / "marcap_available.parquet"
    if marcap_available_path.exists():
        marcap_available = _load_wide_frame(marcap_available_path).reindex(index=selected_index, columns=market_cap.columns)
        eligible &= marcap_available.ge(1.0)

    filtered_mcap = market_cap.where(eligible)
    monthly_avg = filtered_mcap.groupby(filtered_mcap.index.to_period("M")).mean()
    monthly_avg = monthly_avg.dropna(axis=0, how="all")
    if monthly_avg.empty:
        raise SystemExit("No monthly average market-cap data after filtering")
    # Match the strategy feature spec:
    # market_cap -> calendar_mean(freq=M, signal_timing=next_period).
    # A month average is selectable only when the next month exists in the
    # output index. This prevents the current incomplete month from entering
    # the reduced cache before the strategy can actually use it.
    applied_periods = pd.PeriodIndex(selected_index.to_period("M").unique(), freq="M")
    usable_source_periods = pd.PeriodIndex([period - 1 for period in applied_periods], freq="M")
    monthly_avg = monthly_avg.loc[monthly_avg.index.isin(usable_source_periods)]
    if monthly_avg.empty:
        raise SystemExit("No usable previous-month market-cap data after next_period alignment")

    selected: set[str] = set()
    monthly_counts: dict[str, int] = {}
    monthly_available_counts: dict[str, int] = {}
    for period, row in monthly_avg.iterrows():
        valid = row.dropna()
        monthly_available_counts[str(period)] = int(valid.shape[0])
        if valid.empty:
            monthly_counts[str(period)] = 0
            continue
        rank_input = pd.DataFrame(
            {
                "market_cap": valid.astype(float),
                "market": valid.index.astype(str),
            },
            index=valid.index,
        )
        ranked = rank_input.sort_values(["market_cap", "market"], ascending=[False, True])
        top_markets = [str(column) for column in ranked.index[:top_n]]
        selected.update(top_markets)
        monthly_counts[str(period)] = int(len(top_markets))

    selected_markets = sorted(selected)
    if not selected_markets:
        raise SystemExit("No selected markets for reduced cache")

    report = {
        "selection_market": market_name,
        "selection_market_code": MARKET_CODE_MAP[market_name],
        "selection_rule": "monthly_average_market_cap_top_n_union",
        "selection_timing": "previous_month_average_used_for_next_month_strategy_filter",
        "mcap_top_n": int(top_n),
        "selection_start": selected_index.min().date().isoformat() if len(selected_index) else None,
        "selection_end": selected_index.max().date().isoformat() if len(selected_index) else None,
        "selection_source_month_start": str(monthly_avg.index.min()) if len(monthly_avg) else None,
        "selection_source_month_end": str(monthly_avg.index.max()) if len(monthly_avg) else None,
        "selection_months": int(len(monthly_avg)),
        "selected_markets": int(len(selected_markets)),
        "monthly_selected_count_min": int(min(monthly_counts.values())) if monthly_counts else 0,
        "monthly_selected_count_max": int(max(monthly_counts.values())) if monthly_counts else 0,
        "monthly_available_count_min": int(min(monthly_available_counts.values())) if monthly_available_counts else 0,
        "monthly_available_count_max": int(max(monthly_available_counts.values())) if monthly_available_counts else 0,
    }
    return selected_markets, report


def _write_market_meta(source_path: Path, out_path: Path, selected_markets: list[str]) -> dict[str, int]:
    meta = pd.read_parquet(source_path)
    if "market" not in meta.columns:
        raise ValueError(f"{source_path} must contain a 'market' column")
    meta["market"] = meta["market"].astype(str).str.strip().str.upper()
    filtered = meta[meta["market"].isin(selected_markets)].copy()
    order = {market: idx for idx, market in enumerate(selected_markets)}
    filtered["_order"] = filtered["market"].map(order)
    filtered = filtered.sort_values("_order").drop(columns=["_order"])
    filtered.to_parquet(out_path, index=False)
    return {"rows": int(filtered.shape[0]), "columns": int(filtered.shape[1])}


def _write_wide_frame(
    source_path: Path,
    out_path: Path,
    *,
    selected_index: pd.DatetimeIndex,
    selected_markets: list[str] | None,
) -> dict[str, int]:
    frame = _load_wide_frame(source_path)
    if selected_markets is None:
        output = frame.reindex(index=selected_index)
    else:
        output = frame.reindex(index=selected_index, columns=selected_markets)
    output.to_parquet(out_path)
    return {
        "rows": int(output.shape[0]),
        "columns": int(output.shape[1]),
        "non_null": int(output.notna().sum().sum()),
    }


def _write_report(
    out_dir: Path,
    *,
    source_cache_dir: Path,
    selected_index: pd.DatetimeIndex,
    selected_markets: list[str],
    selection_report: dict[str, object],
    frame_report: dict[str, dict[str, int]],
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_cache_dir": str(source_cache_dir),
        "out_dir": str(out_dir),
        "rows": int(len(selected_index)),
        "codes": int(len(selected_markets)),
        "start": selected_index.min().date().isoformat() if len(selected_index) else None,
        "end": selected_index.max().date().isoformat() if len(selected_index) else None,
        **selection_report,
        "frames": frame_report,
    }
    (out_dir / REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "selected_markets.txt").write_text("\n".join(selected_markets) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    source_cache_dir = _resolve(args.source_cache_dir)
    out_dir = _resolve(args.out_dir) if args.out_dir else _default_out_dir(args.market, args.mcap_top_n)
    if source_cache_dir.resolve() == out_dir.resolve():
        raise SystemExit("out-dir must be different from source-cache-dir")
    if not source_cache_dir.exists():
        raise FileNotFoundError(source_cache_dir)

    selected_markets, selection_report = _select_union_markets(
        source_cache_dir,
        market=args.market,
        top_n=int(args.mcap_top_n),
        start=str(args.start),
        end=str(args.end),
    )

    market_cap = _load_wide_frame(source_cache_dir / "market_cap.parquet")
    selected_index = _date_filtered_index(market_cap.index, str(args.start), str(args.end))

    out_dir.mkdir(parents=True, exist_ok=True)
    frame_report: dict[str, dict[str, int]] = {}
    for source_path in sorted(source_cache_dir.glob("*.parquet")):
        out_path = out_dir / source_path.name
        if source_path.name == STOCK_META_FILE:
            frame_report[source_path.stem] = _write_market_meta(source_path, out_path, selected_markets)
        elif source_path.name == INDEX_PRICE_FILE:
            frame_report[source_path.stem] = _write_wide_frame(
                source_path,
                out_path,
                selected_index=selected_index,
                selected_markets=None,
            )
        else:
            frame_report[source_path.stem] = _write_wide_frame(
                source_path,
                out_path,
                selected_index=selected_index,
                selected_markets=selected_markets,
            )

    _write_report(
        out_dir,
        source_cache_dir=source_cache_dir,
        selected_index=selected_index,
        selected_markets=selected_markets,
        selection_report=selection_report,
        frame_report=frame_report,
    )

    print(f"Wrote reduced Korean stock source cache to {out_dir}")
    print(f"Source: {source_cache_dir}")
    print(f"Rule: {selection_report['selection_market']} avg market-cap top {args.mcap_top_n} monthly union")
    print(f"Rows: {len(selected_index)}")
    print(f"Selected codes: {len(selected_markets)}")
    print(f"Start: {selected_index.min().date() if len(selected_index) else 'NA'}")
    print(f"End: {selected_index.max().date() if len(selected_index) else 'NA'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
