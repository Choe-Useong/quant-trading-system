#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "data" / "kr_stocks" / "raw"
DEFAULT_OUT_DIR = ROOT / "data" / "stocks_cache" / "kr_stock_daily"
INDEX_PRICE_FILE = "index_price.parquet"
INDUSTRY_FREQUENCIES = {"monthly", "weekly", "daily"}

MARCAP_VALUE_COLUMNS = {
    "market_cap": "Marcap",
    "candle_acc_trade_price": "Amount",
    "candle_acc_trade_volume": "Volume",
}

MARKET_CODE_MAP = {
    "KOSPI": 1.0,
    "KOSDAQ": 2.0,
    "KOSDAQ GLOBAL": 3.0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a v2 wide source cache for Korean stocks from raw marcap/FDR caches."
    )
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument(
        "--markets",
        default="",
        help="Optional comma-separated Market allow-list, for example KOSPI,KOSDAQ,KOSDAQ GLOBAL.",
    )
    parser.add_argument(
        "--include-industry",
        action="store_true",
        help="Add a sparse point-in-time industry_id frame from the selected raw industry cache.",
    )
    parser.add_argument(
        "--industry-frequency",
        choices=sorted(INDUSTRY_FREQUENCIES),
        default="monthly",
        help="Raw KRX industry snapshot frequency.",
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _normalize_code(value: object) -> str:
    return str(value).strip().upper().zfill(6)


def _industry_file(frequency: str) -> str:
    normalized = str(frequency).strip().lower()
    if normalized not in INDUSTRY_FREQUENCIES:
        raise ValueError(f"Unsupported industry frequency: {frequency}")
    return f"industry_{normalized}.parquet"


def _date_filter_index(index: pd.DatetimeIndex, start: str, end: str) -> pd.DatetimeIndex:
    mask = pd.Series(True, index=index)
    if start:
        mask &= index >= pd.Timestamp(start).normalize()
    if end:
        mask &= index <= pd.Timestamp(end).normalize()
    return index[mask.to_numpy()]


def _load_close(path: Path, start: str, end: str) -> pd.DataFrame:
    close = pd.read_parquet(path)
    close.index = pd.to_datetime(close.index).normalize()
    close.columns = [_normalize_code(column) for column in close.columns]
    close = close[~close.index.duplicated(keep="last")].sort_index()
    close = close.loc[:, ~pd.Index(close.columns).duplicated()]
    close = close.reindex(columns=sorted(close.columns))
    selected_index = _date_filter_index(close.index, start, end)
    close = close.loc[selected_index]
    close = close.apply(pd.to_numeric, errors="coerce")
    close = close.dropna(axis=1, how="all")
    return close


def _load_index_price(path: Path, start: str, end: str) -> pd.DataFrame:
    index_price = pd.read_parquet(path)
    index_price.index = pd.to_datetime(index_price.index).normalize()
    index_price.columns = [str(column).strip().upper() for column in index_price.columns]
    index_price = index_price[~index_price.index.duplicated(keep="last")].sort_index()
    index_price = index_price.loc[:, ~pd.Index(index_price.columns).duplicated()]
    index_price = index_price.reindex(columns=sorted(index_price.columns))
    selected_index = _date_filter_index(index_price.index, start, end)
    index_price = index_price.loc[selected_index]
    index_price = index_price.apply(pd.to_numeric, errors="coerce")
    return index_price.dropna(axis=1, how="all")


def _load_marcap(path: Path, start: str, end: str, columns: list[str]) -> pd.DataFrame:
    required = sorted(set(["Date", "Code", "Name", "Market", *columns]))
    marcap = pd.read_parquet(path, columns=required)
    marcap["Date"] = pd.to_datetime(marcap["Date"]).dt.normalize()
    marcap["Code"] = marcap["Code"].map(_normalize_code)
    marcap["Name"] = marcap["Name"].fillna("").astype(str)
    marcap["Market"] = marcap["Market"].fillna("").astype(str).str.upper()
    if start:
        marcap = marcap[marcap["Date"] >= pd.Timestamp(start).normalize()]
    if end:
        marcap = marcap[marcap["Date"] <= pd.Timestamp(end).normalize()]
    return marcap


def _load_industry(
    path: Path,
    *,
    start: str,
    end: str,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = ["Date", "Code", "Industry"]
    industry = pd.read_parquet(path, columns=required)
    industry["Date"] = pd.to_datetime(industry["Date"]).dt.normalize()
    industry["Code"] = industry["Code"].map(_normalize_code)
    industry["Industry"] = industry["Industry"].fillna("").astype(str).str.strip()
    if start:
        industry = industry[industry["Date"] >= pd.Timestamp(start).normalize()]
    if end:
        industry = industry[industry["Date"] <= pd.Timestamp(end).normalize()]
    industry = industry[
        industry["Industry"].astype(bool)
        & industry["Code"].isin(columns)
        & industry["Date"].isin(index)
    ].copy()
    labels = sorted(industry["Industry"].unique())
    label_to_id = {label: identifier for identifier, label in enumerate(labels, start=1)}
    label_frame = pd.DataFrame(
        {
            "industry_id": list(range(1, len(labels) + 1)),
            "industry_name": labels,
        }
    )
    if industry.empty:
        return (
            pd.DataFrame(index=index, columns=columns, dtype="float64"),
            label_frame,
        )
    industry["industry_id"] = industry["Industry"].map(label_to_id).astype("float64")
    frame = (
        industry.pivot_table(
            index="Date",
            columns="Code",
            values="industry_id",
            aggfunc="last",
        )
        .sort_index()
        .sort_index(axis=1)
        .reindex(index=index, columns=columns)
    )
    return frame.astype("float64"), label_frame


def _parse_markets(raw: str) -> set[str] | None:
    values = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return values or None


def _pivot_marcap(
    marcap: pd.DataFrame,
    *,
    value_column: str,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> pd.DataFrame:
    if marcap.empty:
        return pd.DataFrame(index=index, columns=columns, dtype="float64")
    frame = (
        marcap.pivot_table(
            index="Date",
            columns="Code",
            values=value_column,
            aggfunc="last",
        )
        .sort_index()
        .sort_index(axis=1)
    )
    frame = frame.reindex(index=index, columns=columns)
    return frame.apply(pd.to_numeric, errors="coerce")


def _pivot_market_code(
    marcap: pd.DataFrame,
    *,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> pd.DataFrame:
    if marcap.empty:
        return pd.DataFrame(index=index, columns=columns, dtype="float64")
    coded = marcap[["Date", "Code", "Market"]].copy()
    coded["market_code"] = coded["Market"].map(MARKET_CODE_MAP)
    coded = coded.dropna(subset=["market_code"])
    if coded.empty:
        return pd.DataFrame(index=index, columns=columns, dtype="float64")
    frame = (
        coded.pivot_table(
            index="Date",
            columns="Code",
            values="market_code",
            aggfunc="last",
        )
        .sort_index()
        .sort_index(axis=1)
    )
    return frame.reindex(index=index, columns=columns).astype("float64")


def _build_market_meta(marcap: pd.DataFrame, columns: pd.Index) -> pd.DataFrame:
    if marcap.empty:
        latest = pd.DataFrame(columns=["Code", "Name", "Market"])
    else:
        latest = (
            marcap.sort_values(["Date", "Code"])
            .drop_duplicates(subset=["Code"], keep="last")[["Code", "Name", "Market"]]
            .set_index("Code")
        )
    rows = []
    for code in columns:
        row = latest.loc[code] if code in latest.index else None
        name = str(row["Name"]) if row is not None else code
        market_name = str(row["Market"]) if row is not None else ""
        rows.append(
            {
                "market": code,
                "korean_name": name or code,
                "english_name": market_name or code,
                "market_warning": "NONE",
            }
        )
    return pd.DataFrame(rows)


def _write_report(
    out_dir: Path,
    *,
    raw_dir: Path,
    close: pd.DataFrame,
    frames: dict[str, pd.DataFrame],
    industry_frequency: str | None,
    industry_labels: pd.DataFrame | None,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_dir": str(raw_dir),
        "out_dir": str(out_dir),
        "rows": int(len(close)),
        "codes": int(len(close.columns)),
        "start": close.index.min().date().isoformat() if not close.empty else None,
        "end": close.index.max().date().isoformat() if not close.empty else None,
        "industry": (
            {
                "frequency": industry_frequency,
                "labels": int(len(industry_labels)),
                "label_file": str(out_dir / "industry_labels.json"),
            }
            if industry_labels is not None
            else None
        ),
        "frames": {
            name: {
                "rows": int(frame.shape[0]),
                "columns": int(frame.shape[1]),
                "non_null": int(frame.notna().sum().sum()),
            }
            for name, frame in frames.items()
        },
    }
    (out_dir / "source_cache_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    raw_dir = _resolve(args.raw_dir)
    out_dir = _resolve(args.out_dir)
    close_path = raw_dir / "close_fdr.parquet"
    marcap_path = raw_dir / "marcap_daily.parquet"
    if not close_path.exists():
        raise FileNotFoundError(close_path)
    if not marcap_path.exists():
        raise FileNotFoundError(marcap_path)

    close = _load_close(close_path, args.start, args.end)
    if close.empty or close.shape[1] == 0:
        raise SystemExit("No close_fdr data available after date filtering")

    marcap = _load_marcap(marcap_path, args.start, args.end, list(MARCAP_VALUE_COLUMNS.values()))
    allowed_markets = _parse_markets(args.markets)
    if allowed_markets is not None:
        marcap = marcap[marcap["Market"].isin(allowed_markets)].copy()
    marcap = marcap[marcap["Code"].isin(close.columns)].copy()

    frames: dict[str, pd.DataFrame] = {"trade_price": close}
    for output_name, raw_column in MARCAP_VALUE_COLUMNS.items():
        frames[output_name] = _pivot_marcap(
            marcap,
            value_column=raw_column,
            index=close.index,
            columns=close.columns,
        )
    frames["market_code"] = _pivot_market_code(
        marcap,
        index=close.index,
        columns=close.columns,
    )
    frames["price_available"] = close.notna().astype("float64")
    frames["marcap_available"] = frames["market_cap"].notna().astype("float64")
    industry_labels = None
    if args.include_industry:
        industry_path = raw_dir / _industry_file(args.industry_frequency)
        if not industry_path.exists():
            raise FileNotFoundError(industry_path)
        frames["industry_id"], industry_labels = _load_industry(
            industry_path,
            start=args.start,
            end=args.end,
            index=close.index,
            columns=close.columns,
        )

    index_price_path = raw_dir / INDEX_PRICE_FILE
    if index_price_path.exists():
        index_price = _load_index_price(index_price_path, args.start, args.end)
        if not index_price.empty:
            frames["index_price"] = index_price.reindex(index=close.index)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_parquet(out_dir / f"{name}.parquet")
    if industry_labels is not None:
        label_payload = {
            str(int(row.industry_id)): str(row.industry_name)
            for row in industry_labels.itertuples(index=False)
        }
        (out_dir / "industry_labels.json").write_text(
            json.dumps(label_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    market_warning = pd.DataFrame("NONE", index=close.index, columns=close.columns)
    market_warning.to_parquet(out_dir / "market_warning.parquet")
    market_meta = _build_market_meta(marcap, close.columns)
    market_meta.to_parquet(out_dir / "market_meta.parquet", index=False)
    _write_report(
        out_dir,
        raw_dir=raw_dir,
        close=close,
        frames=frames,
        industry_frequency=args.industry_frequency if args.include_industry else None,
        industry_labels=industry_labels,
    )

    print(f"Wrote Korean stock source cache to {out_dir}")
    print(f"Rows: {close.shape[0]}")
    print(f"Codes: {close.shape[1]}")
    print(f"Start: {close.index.min().date()}")
    print(f"End: {close.index.max().date()}")
    print("Wrote: trade_price, market_cap, candle_acc_trade_price, candle_acc_trade_volume, market_code")
    print("Wrote: price_available, marcap_available, market_warning, market_meta")
    if "industry_id" in frames:
        print(
            f"Wrote: industry_id, industry_labels.json "
            f"(frequency={args.industry_frequency}, labels={len(industry_labels)})"
        )
    if "index_price" in frames:
        print("Wrote: index_price")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
