import argparse
import contextlib
import importlib
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.storage import replace_file  # noqa: E402

DEFAULT_OUT_DIR = ROOT / "data" / "kr_stocks" / "raw"
DEFAULT_INDEX_SYMBOLS = "KOSPI=^KS11,KOSDAQ=^KQ11"
INDUSTRY_FREQUENCIES = {"monthly", "weekly", "daily"}

MARCAP_COLUMNS = [
    "Date",
    "Code",
    "Name",
    "Market",
    "Close",
    "Marcap",
    "Amount",
    "Volume",
    "Stocks",
]

OUTPUT_MARCAP_COLUMNS = [
    "Date",
    "Code",
    "Name",
    "Market",
    "Close_raw",
    "Marcap",
    "Amount",
    "Volume",
    "Stocks",
]

INDUSTRY_COLUMNS = [
    "Date",
    "Code",
    "Name",
    "Market",
    "Industry",
]

KRX_PRICE_FILE = "krx_price_daily.parquet"
KRX_ADJUSTED_CLOSE_FILE = "close_krx_adj.parquet"
KRX_ADJUST_EVENTS_FILE = "krx_adjust_events.json"
KRX_PRICE_COLUMNS = [
    "Date",
    "Code",
    "Market",
    "Open",
    "High",
    "Low",
    "Close",
    "Change",
    "FlucType",
    "Volume",
    "Amount",
    "Marcap",
    "Stocks",
]
# KRX all-market daily quote fields (MDCSTAT01501).
KRX_FIELD_MAP = {
    "TDD_OPNPRC": "Open",
    "TDD_HGPRC": "High",
    "TDD_LWPRC": "Low",
    "TDD_CLSPRC": "Close",
    "CMPPREVDD_PRC": "Change",
    "ACC_TRDVOL": "Volume",
    "ACC_TRDVAL": "Amount",
    "MKTCAP": "Marcap",
    "LIST_SHRS": "Stocks",
}
# FLUC_TP_CD: 1 up, 2 down, 3 unchanged. Other codes are not sign-checked.
KRX_FLUC_SIGN = {"1": 1, "2": -1, "3": 0}
KRX_DAY_COUNT_MIN_RATIO = 0.9
# A consolidation finished during a halt and reopening in delisting liquidation trading (no price
# limit) can come with the base price left at the pre-consolidation close (DATA-018: 052670
# 2026-02-09, 070480 2014-07-08). Only then the listed-share ratio becomes the factor.
KRX_DAILY_PRICE_LIMIT = 0.30
KRX_OVERRIDE_MIN_SHARE_RATIO = 1.5
KRX_LABEL_COLUMNS = ("Code", "Market", "FlucType")
KRX_ADJUSTMENT_RULE = (
    "factor_t = (Close_t - Change_t) / Close_prev, i.e. KRX base price over the previous close; "
    "adjusted_t = Close_t * product of factors after t, so the last date equals the raw close. "
    "factor is 1 when the code has no row on the previous trading date (listing or reappearance). "
    "Reopening after a halt uses the KRX auction base price, so that day's gap is not a return. "
    "Cash dividends are not adjusted. Exception: when the base equals the previous close, the previous "
    "day was halted, listed shares fell at least 1.5x and the close moved beyond the 30% daily limit, "
    "the factor is the previous / current listed-share ratio (share_ratio_override)."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Korean stock raw caches from existing local marcap/FDR data."
    )
    parser.add_argument("--start", default="2014-01-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--markets", default="KOSPI,KOSDAQ,KOSDAQ GLOBAL")
    parser.add_argument(
        "--marcap-dir",
        default="",
        help="Seed marcap parquet directory. Required for rebuild unless --skip-marcap or --update is used.",
    )
    parser.add_argument(
        "--close-file",
        default="",
        help="Seed adjusted close parquet file. Required for rebuild unless --skip-close or --update is used.",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--skip-marcap", action="store_true")
    parser.add_argument("--skip-close", action="store_true")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Incrementally update existing raw caches instead of rebuilding from local seed files.",
    )
    parser.add_argument(
        "--marcap-overlap-days",
        type=int,
        default=10,
        help="Calendar-day overlap to re-fetch for pykrx marcap updates.",
    )
    parser.add_argument(
        "--close-overlap-days",
        type=int,
        default=10,
        help="Calendar-day overlap to re-fetch for FDR adjusted-close updates.",
    )
    parser.add_argument(
        "--close-sleep-seconds",
        type=float,
        default=0.05,
        help="Sleep between FDR ticker requests.",
    )
    parser.add_argument(
        "--include-index",
        action="store_true",
        help="Also update raw index_price.parquet for benchmark/index features.",
    )
    parser.add_argument(
        "--index-symbols",
        default=DEFAULT_INDEX_SYMBOLS,
        help="Comma-separated index mapping such as KOSPI=^KS11,KOSDAQ=^KQ11.",
    )
    parser.add_argument(
        "--index-overlap-days",
        type=int,
        default=10,
        help="Calendar-day overlap to re-fetch for index price updates.",
    )
    parser.add_argument(
        "--include-industry",
        action="store_true",
        help=(
            "Update point-in-time KRX industry classifications. "
            "Existing snapshots for the selected frequency are reused."
        ),
    )
    parser.add_argument(
        "--industry-frequency",
        choices=sorted(INDUSTRY_FREQUENCIES),
        default="monthly",
        help="KRX industry snapshot frequency.",
    )
    parser.add_argument(
        "--industry-sleep-seconds",
        type=float,
        default=0.5,
        help="Sleep between KRX industry snapshot requests.",
    )
    parser.add_argument(
        "--include-krx-price",
        action="store_true",
        help=(
            f"Update {KRX_PRICE_FILE} from KRX all-market daily quotes and rebuild "
            f"{KRX_ADJUSTED_CLOSE_FILE} (regular-session close adjusted by KRX base prices)."
        ),
    )
    parser.add_argument(
        "--krx-price-overlap-days",
        type=int,
        default=10,
        help="Calendar-day overlap to re-fetch for KRX daily quote updates.",
    )
    parser.add_argument(
        "--krx-price-sleep-seconds",
        type=float,
        default=0.3,
        help="Sleep between KRX daily quote requests.",
    )
    parser.add_argument(
        "--krx-final-time",
        default="20:30",
        help="KST time after which today's KRX quotes are stored. Earlier runs stop at the previous day.",
    )
    return parser.parse_args()


def years_between(start: pd.Timestamp, end: pd.Timestamp) -> range:
    return range(int(start.year), int(end.year) + 1)


def latest_date_in_marcap_dir(marcap_dir: Path) -> pd.Timestamp:
    latest = None
    for path in sorted(marcap_dir.glob("marcap-*.parquet")):
        try:
            dates = pd.read_parquet(path, columns=["Date"])
        except Exception:
            continue
        if dates.empty:
            continue
        max_date = pd.to_datetime(dates["Date"]).max().normalize()
        if latest is None or max_date > latest:
            latest = max_date
    if latest is None:
        raise FileNotFoundError(f"No usable marcap parquet files found in {marcap_dir}")
    return latest


def build_marcap_cache(
    marcap_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: set[str],
) -> pd.DataFrame:
    parts = []
    for year in years_between(start, end):
        path = marcap_dir / f"marcap-{year}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=MARCAP_COLUMNS)
        df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
        df = df[(df["Date"] >= start) & (df["Date"] <= end)].copy()
        if df.empty:
            continue
        df["Market"] = df["Market"].astype(str)
        df = df[df["Market"].isin(markets)].copy()
        if df.empty:
            continue
        df["Code"] = df["Code"].astype(str).str.zfill(6)
        parts.append(df)

    if not parts:
        return pd.DataFrame(columns=OUTPUT_MARCAP_COLUMNS)

    out = pd.concat(parts, ignore_index=True)
    out = out.rename(columns={"Close": "Close_raw"})
    out = out[OUTPUT_MARCAP_COLUMNS].copy()
    out = out.drop_duplicates(subset=["Date", "Code", "Market"], keep="last")
    out = out.sort_values(["Date", "Market", "Code"]).reset_index(drop=True)
    return out


def copy_close_cache(close_file: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    close = pd.read_parquet(close_file)
    close.index = pd.to_datetime(close.index).normalize()
    close.columns = [str(c).zfill(6) for c in close.columns]
    close = close[(close.index >= start) & (close.index <= end)].copy()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    close = close.loc[:, ~pd.Index(close.columns).duplicated()]
    return close


def frame_summary(df: pd.DataFrame, date_col: str | None = None) -> dict:
    if df.empty:
        return {
            "rows": 0,
            "columns": int(df.shape[1]),
            "start": None,
            "end": None,
        }
    if date_col:
        dates = pd.to_datetime(df[date_col])
    else:
        dates = pd.to_datetime(df.index)
    return {
        "rows": int(len(df)),
        "columns": int(df.shape[1]),
        "start": dates.min().date().isoformat(),
        "end": dates.max().date().isoformat(),
    }


def _industry_file(frequency: str) -> str:
    normalized = str(frequency).strip().lower()
    if normalized not in INDUSTRY_FREQUENCIES:
        raise ValueError(f"Unsupported industry frequency: {frequency}")
    return f"industry_{normalized}.parquet"


def write_report(
    out_dir: Path,
    args: argparse.Namespace,
    marcap: pd.DataFrame | None,
    close: pd.DataFrame | None,
    index_price: pd.DataFrame | None,
    industry: pd.DataFrame | None,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
    krx_price: dict | None = None,
) -> dict:
    marcap_codes = set()
    close_codes = set()
    if marcap is not None and not marcap.empty:
        marcap_codes = set(marcap["Code"].astype(str))
    if close is not None and not close.empty:
        close_codes = set(str(c) for c in close.columns)

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "update" if args.update else "rebuild",
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "markets": markets,
        "sources": {
            "marcap_dir": str(Path(args.marcap_dir)),
            "close_file": str(Path(args.close_file)),
            "index_symbols": args.index_symbols if args.include_index else None,
        },
        "outputs": {
            "marcap_daily": str(out_dir / "marcap_daily.parquet"),
            "close_fdr": str(out_dir / "close_fdr.parquet"),
            "index_price": str(out_dir / "index_price.parquet") if args.include_index else None,
            "industry_snapshots": (
                str(out_dir / _industry_file(args.industry_frequency))
                if args.include_industry
                else None
            ),
            "download_report": str(out_dir / "download_report.json"),
            "krx_price": str(out_dir / KRX_PRICE_FILE) if krx_price is not None else None,
            "krx_adjusted_close": str(out_dir / KRX_ADJUSTED_CLOSE_FILE) if krx_price is not None else None,
            "krx_adjust_events": str(out_dir / KRX_ADJUST_EVENTS_FILE) if krx_price is not None else None,
        },
        "krx_price": krx_price,
        "marcap": frame_summary(marcap, "Date") if marcap is not None else None,
        "close_fdr": frame_summary(close) if close is not None else None,
        "index_price": frame_summary(index_price) if index_price is not None else None,
        "industry_snapshots": (
            {
                **frame_summary(industry, "Date"),
                "frequency": args.industry_frequency,
                "snapshots": int(
                    industry[["Date", "Market"]].drop_duplicates().shape[0]
                ),
                "codes": int(industry["Code"].nunique()),
                "blank_industries": int(
                    industry["Industry"].fillna("").astype(str).str.strip().eq("").sum()
                ),
            }
            if industry is not None and not industry.empty
            else None
        ),
        "code_coverage": {
            "marcap_codes": len(marcap_codes),
            "close_codes": len(close_codes),
            "common_codes": len(marcap_codes & close_codes),
            "marcap_only_codes": len(marcap_codes - close_codes),
            "close_only_codes": len(close_codes - marcap_codes),
            "marcap_only_examples": sorted(marcap_codes - close_codes)[:20],
            "close_only_examples": sorted(close_codes - marcap_codes)[:20],
        },
    }
    (out_dir / "download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in __import__("os").environ:
            __import__("os").environ[key] = value


def _load_pykrx_stock():
    _load_env()
    with contextlib.redirect_stdout(io.StringIO()):
        module = importlib.import_module("pykrx.stock")
    return module


def _load_fdr():
    try:
        return importlib.import_module("FinanceDataReader")
    except ImportError as exc:
        raise ImportError(
            "FinanceDataReader is required for close_fdr updates. "
            "Install it with: py -m pip install finance-datareader"
        ) from exc


def _required_seed_path(raw: str, option_name: str) -> Path:
    if not str(raw or "").strip():
        raise ValueError(f"{option_name} is required for rebuild mode. Use --update to refresh existing raw caches.")
    path = Path(raw)
    if not path.exists():
        raise FileNotFoundError(f"{option_name} does not exist: {path}")
    return path


def _load_yfinance():
    try:
        return importlib.import_module("yfinance")
    except ImportError as exc:
        raise ImportError(
            "yfinance is required for index price updates. "
            "Install it with: py -m pip install yfinance"
        ) from exc


def _previous_fetch_start(last_date: pd.Timestamp, overlap_days: int) -> pd.Timestamp:
    overlap_days = max(int(overlap_days), 0)
    return (last_date - pd.Timedelta(days=overlap_days)).normalize()


def _date_range_for_update(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    if start > end:
        return pd.DatetimeIndex([])
    return pd.date_range(start, end, freq="B")


def _parse_index_symbols(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Invalid index symbol mapping: {chunk}")
        name, ticker = chunk.split("=", 1)
        name = name.strip().upper()
        ticker = ticker.strip()
        if not name or not ticker:
            raise ValueError(f"Invalid index symbol mapping: {chunk}")
        mapping[name] = ticker
    if not mapping:
        raise ValueError("--index-symbols must include at least one NAME=TICKER mapping")
    return mapping


def _select_yahoo_close_column(data: pd.DataFrame, ticker: str) -> pd.Series | None:
    if isinstance(data.columns, pd.MultiIndex):
        for close_column in ("Adj Close", "Close"):
            if close_column not in data.columns.get_level_values(0):
                continue
            selected = data.xs(close_column, axis=1, level=0, drop_level=True)
            if isinstance(selected, pd.Series):
                return selected
            if ticker in selected.columns:
                return selected[ticker]
            if selected.shape[1] == 1:
                return selected.iloc[:, 0]
        return None
    for close_column in ("Adj Close", "Close"):
        if close_column in data.columns:
            return data[close_column]
    return None


def _ticker_name(stock_module, code: str) -> str:
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return str(stock_module.get_market_ticker_name(code) or "")
    except Exception:
        return ""


def fetch_pykrx_marcap(
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
) -> pd.DataFrame:
    stock_module = _load_pykrx_stock()
    rows = []
    name_cache: dict[str, str] = {}
    column_map = {
        "종가": "Close_raw",
        "시가총액": "Marcap",
        "거래대금": "Amount",
        "거래량": "Volume",
        "상장주식수": "Stocks",
    }

    for date in _date_range_for_update(start, end):
        date_key = date.strftime("%Y%m%d")
        for market in markets:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    frame = stock_module.get_market_cap(date_key, market=market)
            except Exception as exc:
                print(f"warn: pykrx marcap fetch failed date={date_key} market={market}: {exc}", file=sys.stderr)
                continue
            if frame is None or frame.empty:
                continue
            frame = frame.rename(columns=column_map)
            required = ["Close_raw", "Marcap", "Amount", "Volume", "Stocks"]
            missing = [col for col in required if col not in frame.columns]
            if missing:
                raise ValueError(f"pykrx market cap result missing columns {missing} for {date_key} {market}")
            frame = frame[required].copy()
            frame.insert(0, "Date", date.normalize())
            frame.insert(1, "Code", [str(code).zfill(6) for code in frame.index])
            frame.insert(2, "Name", "")
            frame.insert(3, "Market", market)
            for code in frame["Code"].unique():
                if code not in name_cache:
                    name_cache[code] = _ticker_name(stock_module, code)
            frame["Name"] = frame["Code"].map(name_cache).fillna("")
            rows.append(frame[OUTPUT_MARCAP_COLUMNS])

    if not rows:
        return pd.DataFrame(columns=OUTPUT_MARCAP_COLUMNS)
    out = pd.concat(rows, ignore_index=True)
    out["Code"] = out["Code"].astype(str).str.zfill(6)
    out = drop_zero_activity_dates(out)
    return out.sort_values(["Date", "Market", "Code"]).reset_index(drop=True)


def _normalize_industry(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=INDUSTRY_COLUMNS)
    normalized = frame.copy()
    for column in INDUSTRY_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized["Date"] = pd.to_datetime(normalized["Date"]).dt.normalize()
    normalized["Code"] = normalized["Code"].astype(str).str.strip().str.upper().str.zfill(6)
    for column in ("Name", "Market", "Industry"):
        normalized[column] = normalized[column].fillna("").astype(str).str.strip()
    normalized["Market"] = normalized["Market"].str.upper()
    normalized = normalized[
        normalized["Code"].astype(bool)
        & normalized["Market"].astype(bool)
        & normalized["Industry"].astype(bool)
    ]
    return (
        normalized[INDUSTRY_COLUMNS]
        .drop_duplicates(["Date", "Market", "Code"], keep="last")
        .sort_values(["Date", "Market", "Code"])
        .reset_index(drop=True)
    )


def _industry_market_sources(markets: list[str]) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    normalized = {str(market).strip().upper() for market in markets}
    if "KOSPI" in normalized:
        sources["KOSPI"] = {"KOSPI"}
    if normalized & {"KOSDAQ", "KOSDAQ GLOBAL"}:
        sources["KOSDAQ"] = {"KOSDAQ", "KOSDAQ GLOBAL"}
    return sources


def _industry_period_key(dates: pd.Series, frequency: str) -> pd.Series:
    normalized = str(frequency).strip().lower()
    if normalized == "monthly":
        return dates.dt.to_period("M").astype(str)
    if normalized == "weekly":
        return dates.dt.to_period("W-SUN").astype(str)
    if normalized == "daily":
        return dates.dt.strftime("%Y-%m-%d")
    raise ValueError(f"Unsupported industry frequency: {frequency}")


def _industry_snapshot_requests(
    marcap: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
    frequency: str,
) -> list[tuple[pd.Timestamp, str]]:
    if marcap.empty:
        return []
    calendar = marcap[["Date", "Market"]].copy()
    calendar["Date"] = pd.to_datetime(calendar["Date"]).dt.normalize()
    calendar["Market"] = calendar["Market"].fillna("").astype(str).str.upper()
    calendar = calendar[
        calendar["Date"].between(start.normalize(), end.normalize(), inclusive="both")
    ]
    requests: list[tuple[pd.Timestamp, str]] = []
    for source_market, raw_markets in _industry_market_sources(markets).items():
        dates = calendar.loc[calendar["Market"].isin(raw_markets), "Date"].drop_duplicates()
        if dates.empty:
            continue
        period_ends = (
            pd.DataFrame({"Date": dates})
            .assign(
                Period=lambda item: _industry_period_key(
                    item["Date"],
                    frequency,
                )
            )
            .groupby("Period", sort=True)["Date"]
            .max()
        )
        requests.extend((pd.Timestamp(date).normalize(), source_market) for date in period_ends)
    return sorted(set(requests))


def _fetch_pykrx_industry_snapshot(
    stock_module,
    *,
    date: pd.Timestamp,
    market: str,
) -> pd.DataFrame:
    date_key = date.strftime("%Y%m%d")
    with contextlib.redirect_stdout(io.StringIO()):
        frame = stock_module.get_market_sector_classifications(date_key, market)
    if frame is None or frame.empty:
        raise ValueError(f"Empty KRX industry result for {date_key} {market}")
    result = frame.reset_index()
    if result.shape[1] < 3:
        raise ValueError(
            f"KRX industry result has fewer than three columns for {date_key} {market}"
        )
    result = result.iloc[:, :3].copy()
    result.columns = ["Code", "Name", "Industry"]
    result.insert(0, "Date", date.normalize())
    result.insert(3, "Market", market)
    result = _normalize_industry(result)
    if result.empty:
        raise ValueError(f"KRX industry result has no usable rows for {date_key} {market}")
    return result


def _merge_industry(
    existing: pd.DataFrame,
    fetched: pd.DataFrame,
    *,
    frequency: str,
) -> pd.DataFrame:
    existing = _normalize_industry(existing)
    fetched = _normalize_industry(fetched)
    if fetched.empty:
        return existing
    if existing.empty:
        return fetched
    fetched_period_keys = set(
        zip(_industry_period_key(fetched["Date"], frequency), fetched["Market"])
    )
    existing_keys = list(
        zip(_industry_period_key(existing["Date"], frequency), existing["Market"])
    )
    keep = [key not in fetched_period_keys for key in existing_keys]
    existing = existing.loc[keep].copy()
    return _normalize_industry(pd.concat([existing, fetched], ignore_index=True))


def update_industry_cache(
    *,
    out_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
    frequency: str,
    sleep_seconds: float,
) -> pd.DataFrame:
    marcap_path = out_dir / "marcap_daily.parquet"
    if not marcap_path.exists():
        raise FileNotFoundError(
            f"KRX industry updates require the existing marcap cache: {marcap_path}"
        )
    marcap = pd.read_parquet(marcap_path, columns=["Date", "Market"])
    requests = _industry_snapshot_requests(
        marcap,
        start=start,
        end=end,
        markets=markets,
        frequency=frequency,
    )
    if not requests:
        raise ValueError("No trading dates available for KRX industry updates")

    path = out_dir / _industry_file(frequency)
    existing = (
        _normalize_industry(pd.read_parquet(path))
        if path.exists()
        else pd.DataFrame(columns=INDUSTRY_COLUMNS)
    )
    existing_keys = set(zip(existing["Date"], existing["Market"]))
    pending = [
        (date, market)
        for date, market in requests
        if (date, market) not in existing_keys
    ]
    if not pending:
        print(f"industry monthly cache is current: {path} snapshots={len(requests)}")
        return existing

    stock_module = _load_pykrx_stock()
    checkpoint_parts: list[pd.DataFrame] = []
    failures: list[str] = []
    for completed, (date, market) in enumerate(pending, start=1):
        try:
            checkpoint_parts.append(
                _fetch_pykrx_industry_snapshot(
                    stock_module,
                    date=date,
                    market=market,
                )
            )
        except Exception as exc:
            failures.append(f"{date.date()} {market}: {type(exc).__name__}: {exc}")
        should_checkpoint = completed % 12 == 0 or completed == len(pending)
        if should_checkpoint and checkpoint_parts:
            existing = _merge_industry(
                existing,
                pd.concat(checkpoint_parts, ignore_index=True),
                frequency=frequency,
            )
            existing.to_parquet(path, index=False)
            checkpoint_parts.clear()
        if completed % 12 == 0 or completed == len(pending):
            print(
                f"krx industry {frequency} progress: "
                f"{completed}/{len(pending)}"
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if failures:
        examples = failures[:10]
        raise RuntimeError(
            f"KRX industry lookup failed for {len(failures)} snapshots; "
            f"successful checkpoints were saved. Rerun the same command. examples={examples}"
        )
    print(
        "updated:",
        path,
        "rows:",
        len(existing),
        "snapshots:",
        existing[["Date", "Market"]].drop_duplicates().shape[0],
        "codes:",
        existing["Code"].nunique(),
    )
    if not existing.empty:
        print(
            f"industry {frequency} range:",
            existing["Date"].min().date(),
            "->",
            existing["Date"].max().date(),
        )
    return existing


def drop_zero_activity_dates(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    daily = frame.groupby("Date")[["Amount", "Volume"]].sum(min_count=1)
    valid_dates = daily[(daily["Amount"] > 0) | (daily["Volume"] > 0)].index
    return frame[frame["Date"].isin(valid_dates)].copy()


def merge_marcap(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    if fetched.empty:
        return existing.copy()
    out = pd.concat([existing, fetched], ignore_index=True)
    out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
    out["Code"] = out["Code"].astype(str).str.zfill(6)
    out = out[OUTPUT_MARCAP_COLUMNS].copy()
    out = drop_zero_activity_dates(out)
    out = out.drop_duplicates(subset=["Date", "Code", "Market"], keep="last")
    return out.sort_values(["Date", "Market", "Code"]).reset_index(drop=True)


def _download_fdr_close_one(fdr, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    parts = []
    chunk_start = start.normalize()
    while chunk_start <= end:
        chunk_end = min(
            end,
            chunk_start + pd.DateOffset(years=4) - pd.Timedelta(days=1),
        )
        # FDR treats the end argument as exclusive for Korean stocks in practice.
        request_end = (chunk_end + pd.Timedelta(days=1)).date().isoformat()
        data = fdr.DataReader(
            code,
            chunk_start.date().isoformat(),
            request_end,
        )
        if data is not None and not data.empty and "Close" in data.columns:
            close = data["Close"].copy()
            close.index = pd.to_datetime(close.index).normalize()
            parts.append(close)
        chunk_start = chunk_end + pd.Timedelta(days=1)
    if not parts:
        return pd.Series(dtype="float64", name=code)
    close = pd.concat(parts)
    close = close[~close.index.duplicated(keep="last")].sort_index()
    close.name = code
    return close


def fetch_fdr_close(
    *,
    codes: list[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    sleep_seconds: float,
) -> pd.DataFrame:
    fdr = _load_fdr()
    series = []
    total = len(codes)
    for index, code in enumerate(codes, start=1):
        try:
            close = _download_fdr_close_one(fdr, code, start, end)
        except Exception as exc:
            print(f"warn: FDR close fetch failed code={code}: {exc}", file=sys.stderr)
            close = pd.Series(dtype="float64", name=code)
        if not close.empty:
            series.append(close)
        if index % 100 == 0 or index == total:
            print(f"fdr close progress: {index}/{total}")
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    if not series:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=codes)
    out = pd.concat(series, axis=1)
    out.columns = [str(c).zfill(6) for c in out.columns]
    out = out.loc[:, ~pd.Index(out.columns).duplicated()]
    return out.sort_index()


def find_retroactive_close_change_codes(
    existing: pd.DataFrame,
    fetched: pd.DataFrame,
) -> set[str]:
    if existing.empty or fetched.empty:
        return set()
    common_index = existing.index.intersection(fetched.index)
    common_columns = existing.columns.intersection(fetched.columns)
    if common_index.empty or common_columns.empty:
        return set()
    old = existing.reindex(index=common_index, columns=common_columns)
    new = fetched.reindex(index=common_index, columns=common_columns)
    comparable = old.notna() & new.notna()
    changed = comparable & old.ne(new)
    return set(str(code).zfill(6) for code in changed.columns[changed.any(axis=0)])


def find_new_stock_count_change_codes(
    existing: pd.DataFrame,
    fetched: pd.DataFrame,
    *,
    existing_end: pd.Timestamp,
) -> set[str]:
    required = {"Date", "Code", "Stocks"}
    if (
        existing.empty
        or fetched.empty
        or not required.issubset(existing.columns)
        or not required.issubset(fetched.columns)
    ):
        return set()

    def normalized(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame[["Date", "Code", "Stocks"]].copy()
        out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
        out["Code"] = out["Code"].astype(str).str.zfill(6)
        out["Stocks"] = pd.to_numeric(out["Stocks"], errors="coerce")
        return (
            out.dropna(subset=["Date", "Code", "Stocks"])
            .sort_values(["Code", "Date"])
            .drop_duplicates(["Date", "Code"], keep="last")
        )

    old = normalized(existing)
    new = normalized(fetched)
    future = new[new["Date"] > existing_end]
    if future.empty:
        return set()
    anchors = old[old["Date"] <= existing_end].groupby(
        "Code", sort=False, as_index=False
    ).tail(1)
    sequence = pd.concat([anchors, future], ignore_index=True).sort_values(
        ["Code", "Date"]
    )
    sequence["previous_stocks"] = sequence.groupby("Code")["Stocks"].shift()
    changed = sequence[
        sequence["previous_stocks"].notna()
        & sequence["Stocks"].ne(sequence["previous_stocks"])
        & sequence["Date"].gt(existing_end)
    ]
    return set(changed["Code"].astype(str))


def fetch_yahoo_index_price(
    *,
    symbols: dict[str, str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    yf = _load_yfinance()
    series = []
    request_end = (end + pd.Timedelta(days=1)).date().isoformat()
    for name, ticker in symbols.items():
        data = yf.download(
            ticker,
            start=start.date().isoformat(),
            end=request_end,
            progress=False,
            auto_adjust=False,
        )
        if data is None or data.empty:
            print(f"warn: Yahoo index fetch returned empty data name={name} ticker={ticker}", file=sys.stderr)
            continue
        close = _select_yahoo_close_column(data, ticker)
        if close is None:
            print(f"warn: Yahoo index fetch missing close column name={name} ticker={ticker}", file=sys.stderr)
            continue
        close = close.copy()
        close.index = pd.to_datetime(close.index).normalize()
        close.name = name
        series.append(close)
    if not series:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=sorted(symbols))
    out = pd.concat(series, axis=1)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.reindex(columns=sorted(out.columns)).apply(pd.to_numeric, errors="coerce")


def merge_close(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    if fetched.empty:
        return existing.copy()
    existing = existing.copy()
    existing.index = pd.to_datetime(existing.index).normalize()
    existing.columns = [str(c).zfill(6) for c in existing.columns]
    fetched = fetched.copy()
    fetched.index = pd.to_datetime(fetched.index).normalize()
    fetched.columns = [str(c).zfill(6) for c in fetched.columns]
    out = existing.combine_first(fetched)
    out.update(fetched)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.loc[:, sorted(out.columns)]
    return out


def replace_close_histories(
    merged: pd.DataFrame,
    refreshed: pd.DataFrame,
    *,
    codes: set[str],
) -> pd.DataFrame:
    if not codes:
        return merged
    missing = sorted(
        code
        for code in codes
        if code not in refreshed.columns or refreshed[code].dropna().empty
    )
    if missing:
        raise RuntimeError(
            "Full FDR history refresh failed for codes: "
            + ", ".join(missing[:20])
        )

    out = merged.reindex(index=merged.index.union(refreshed.index).sort_values())
    for code in sorted(codes):
        out[code] = refreshed[code].reindex(out.index)
    return out.loc[:, sorted(out.columns)]


def merge_index_price(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    if fetched.empty:
        return existing.copy()
    existing = existing.copy()
    existing.index = pd.to_datetime(existing.index).normalize()
    existing.columns = [str(c).upper() for c in existing.columns]
    fetched = fetched.copy()
    fetched.index = pd.to_datetime(fetched.index).normalize()
    fetched.columns = [str(c).upper() for c in fetched.columns]
    out = existing.combine_first(fetched)
    out.update(fetched)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.loc[:, sorted(out.columns)].apply(pd.to_numeric, errors="coerce")


def _krx_number(values: pd.Series) -> pd.Series:
    text = values.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(text.where(~text.isin(["", "-"])), errors="coerce").astype("float64")


def parse_krx_price_frame(raw: pd.DataFrame | None, date: pd.Timestamp, markets: list[str]) -> pd.DataFrame:
    """Convert one KRX all-market daily quote response into KRX_PRICE_COLUMNS rows."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=KRX_PRICE_COLUMNS)
    missing = [column for column in ("ISU_SRT_CD", "MKT_NM", "FLUC_TP_CD", *KRX_FIELD_MAP) if column not in raw.columns]
    if missing:
        raise ValueError(f"KRX daily quotes for {date.date()} miss columns {missing}")
    market = raw["MKT_NM"].astype(str).str.strip().str.upper()
    frame = raw[market.isin(markets)]
    out = pd.DataFrame(
        {
            "Date": pd.Timestamp(date).normalize(),
            "Code": frame["ISU_SRT_CD"].astype(str).str.strip().str.upper().str.zfill(6).to_numpy(),
            "Market": market[frame.index].to_numpy(),
        },
        index=range(len(frame)),
    )
    for field, name in KRX_FIELD_MAP.items():
        out[name] = _krx_number(frame[field]).to_numpy()
    out["FlucType"] = frame["FLUC_TP_CD"].astype(str).str.strip().to_numpy()
    return out[KRX_PRICE_COLUMNS]


def _label_values(series: pd.Series) -> set:
    if isinstance(series.dtype, pd.CategoricalDtype):
        return set(series.cat.categories)
    return set(series.dropna().unique())


def concat_krx_price(frames: list[pd.DataFrame | None]) -> pd.DataFrame:
    """Concatenate KRX quote frames keeping the label columns categorical (about 4x less memory)."""
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=KRX_PRICE_COLUMNS)
    for column in KRX_LABEL_COLUMNS:
        dtype = pd.CategoricalDtype(sorted(set().union(*(_label_values(frame[column]) for frame in frames))))
        frames = [frame.assign(**{column: frame[column].astype(dtype)}) for frame in frames]
    return pd.concat(frames, ignore_index=True)[KRX_PRICE_COLUMNS]


def check_krx_price_rows(frame: pd.DataFrame) -> list[str]:
    """Row-level integrity: unique (Date, Code) and Change sign agreeing with FLUC_TP_CD."""
    errors = []
    duplicated = frame.duplicated(["Date", "Code"], keep=False)
    if duplicated.any():
        errors.append(
            f"KRX quotes repeat {int(duplicated.sum())} (Date, Code) rows, e.g. "
            f"{frame.loc[duplicated, ['Date', 'Code']].head(5).astype(str).to_dict('records')}"
        )
    expected = frame["FlucType"].astype(object).map(KRX_FLUC_SIGN).astype("float64")
    known = expected.notna() & frame["Change"].notna()
    bad = known & (np.sign(frame["Change"]) != expected)
    if bad.any():
        errors.append(
            f"KRX Change sign disagrees with FLUC_TP_CD on {int(bad.sum())} rows, e.g. "
            f"{frame.loc[bad, ['Date', 'Code', 'Change', 'FlucType']].head(5).astype(str).to_dict('records')}"
        )
    return errors


def _load_krx_daily_quote_fetcher():
    _load_env()
    # pykrx prints the KRX login banner (including the ID) on import and on requests.
    with contextlib.redirect_stdout(io.StringIO()):
        core = importlib.import_module("pykrx.website.krx.market.core")
    quotes = core.전종목시세

    def fetch(date_key: str) -> pd.DataFrame:
        with contextlib.redirect_stdout(io.StringIO()):
            return quotes().fetch(date_key, "ALL")

    return fetch


def fetch_krx_price(
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    dates=None,
    markets: list[str],
    sleep_seconds: float,
    fetch_day=None,
    retries: int = 3,
) -> pd.DataFrame:
    """Fetch KRX all-market daily quotes for each business day (or the given dates).

    Holidays return no active rows and are skipped; request errors are retried, then raised.
    """
    fetch_day = fetch_day or _load_krx_daily_quote_fetcher()
    dates = _date_range_for_update(start, end) if dates is None else pd.DatetimeIndex(dates)
    parts = []
    for number, date in enumerate(dates, start=1):
        for attempt in range(retries):
            try:
                raw = fetch_day(date.strftime("%Y%m%d"))
                break
            except Exception as exc:
                if attempt == retries - 1:
                    raise RuntimeError(f"KRX daily quote fetch failed for {date.date()}: {exc}") from exc
                time.sleep(2 * (attempt + 1))
        frame = parse_krx_price_frame(raw, date, markets)
        if not frame.empty and frame["Volume"].fillna(0).sum() > 0:
            parts.append(frame)
        if number % 100 == 0 or number == len(dates):
            print(f"krx price progress: {number}/{len(dates)} dates (through {date.date()})", flush=True)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return concat_krx_price(parts)


def krx_quote_end(end: pd.Timestamp, *, now_kst: pd.Timestamp, final_time: str) -> pd.Timestamp:
    """Last date whose KRX quotes may be stored: today only after final_time (KST)."""
    hour, minute = (int(part) for part in str(final_time).split(":"))
    today = pd.Timestamp(now_kst.year, now_kst.month, now_kst.day)
    end = min(pd.Timestamp(end).normalize(), today)
    if end == today and (now_kst.hour, now_kst.minute) < (hour, minute):
        return today - pd.Timedelta(days=1)
    return end


def merge_krx_price(existing: pd.DataFrame | None, fetched: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Replace whole fetched dates; report rows whose stored Close/Change changed on refetch."""
    if existing is None or existing.empty:
        merged = concat_krx_price([fetched])
        changed: list[dict] = []
    else:
        fetched_dates = set(fetched["Date"])
        old = existing[existing["Date"].isin(fetched_dates)][["Date", "Code", "Close", "Change"]]
        new = fetched[["Date", "Code", "Close", "Change"]]
        both = old.astype({"Code": object}).merge(new.astype({"Code": object}), on=["Date", "Code"],
                                                  suffixes=("_old", ""))
        moved = both[
            ~np.isclose(both["Close_old"], both["Close"], equal_nan=True)
            | ~np.isclose(both["Change_old"], both["Change"], equal_nan=True)
        ]
        changed = [
            {
                "date": row.Date.date().isoformat(),
                "code": row.Code,
                "close_old": row.Close_old,
                "close_new": row.Close,
                "change_old": row.Change_old,
                "change_new": row.Change,
            }
            for row in moved.itertuples(index=False)
        ]
        merged = concat_krx_price([existing[~existing["Date"].isin(fetched_dates)], fetched])
    merged = merged.sort_values(["Date", "Code"]).reset_index(drop=True)
    return merged, changed


def check_krx_day_counts(prices: pd.DataFrame, dates) -> list[str]:
    counts = prices.groupby("Date").size().sort_index()
    errors = []
    for date in sorted(set(dates)):
        position = counts.index.get_loc(date)
        if position == 0:
            continue
        previous = counts.iloc[position - 1]
        if counts.iloc[position] < KRX_DAY_COUNT_MIN_RATIO * previous:
            errors.append(
                f"KRX quotes on {pd.Timestamp(date).date()} have {int(counts.iloc[position])} codes, "
                f"previous trading date had {int(previous)}"
            )
    return errors


def _marcap_slice(marcap: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    dates = pd.to_datetime(marcap["Date"]).dt.normalize()
    raw = marcap.loc[(dates >= start) & (dates <= end), ["Date", "Code", "Close_raw"]].copy()
    raw["Date"] = pd.to_datetime(raw["Date"]).dt.normalize()
    raw["Code"] = raw["Code"].astype(str).str.zfill(6)
    return raw.drop_duplicates(["Date", "Code"], keep="last")


def compare_krx_close_with_marcap(
    prices: pd.DataFrame,
    marcap: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict:
    """KRX Close must equal marcap Close_raw; marcap trading dates must exist in KRX quotes."""
    krx = prices[(prices["Date"] >= start) & (prices["Date"] <= end)][["Date", "Code", "Close"]]
    krx = krx.astype({"Code": object})
    raw = _marcap_slice(marcap, start, end)
    both = krx.merge(raw, on=["Date", "Code"], how="inner")
    mismatch = both[(both["Close"] - both["Close_raw"]).abs() > 0.5]
    missing_dates = sorted(set(raw["Date"]) - set(krx["Date"]))
    return {
        "compared_rows": int(len(both)),
        "mismatched_rows": int(len(mismatch)),
        "mismatched_dates": int(mismatch["Date"].nunique()),
        "mismatch_examples": mismatch.head(20).assign(Date=lambda f: f["Date"].dt.date.astype(str)).to_dict("records"),
        "marcap_dates_missing_in_krx": [date.date().isoformat() for date in missing_dates],
    }


def _merge_marcap_checks(checks: list[dict]) -> dict | None:
    if not checks:
        return None
    return {
        "compared_rows": sum(check["compared_rows"] for check in checks),
        "mismatched_rows": sum(check["mismatched_rows"] for check in checks),
        "mismatched_dates": sum(check["mismatched_dates"] for check in checks),
        "mismatch_examples": [example for check in checks for example in check["mismatch_examples"]][:20],
        "marcap_dates_missing_in_krx": [date for check in checks for date in check["marcap_dates_missing_in_krx"]],
    }


def compute_krx_adjusted_close(prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Adjusted close and adjustment events from KRX close and change (see KRX_ADJUSTMENT_RULE)."""
    frame = prices.drop_duplicates(["Date", "Code"], keep="last")
    if isinstance(frame["Code"].dtype, pd.CategoricalDtype):
        frame = frame.assign(Code=frame["Code"].cat.remove_unused_categories())

    def wide(column: str) -> pd.DataFrame:
        table = frame.pivot(index="Date", columns="Code", values=column)
        table.columns = pd.Index([str(code) for code in table.columns], name="Code")
        return table.sort_index()

    close = wide("Close")
    close = close.reindex(columns=sorted(close.columns))
    change = wide("Change").reindex(index=close.index, columns=close.columns)
    volume = wide("Volume").reindex(index=close.index, columns=close.columns)
    stocks = wide("Stocks").reindex(index=close.index, columns=close.columns)
    present = close.notna()
    previous = close.shift(1)
    base = close - change
    linked = present & previous.gt(0)
    invalid = linked & ~base.gt(0)
    factor = (base / previous).where(linked & base.gt(0), 1.0)
    share_ratio = stocks.shift(1) / stocks
    override = (
        linked
        & factor.eq(1.0)
        & volume.shift(1).eq(0)
        & share_ratio.ge(KRX_OVERRIDE_MIN_SHARE_RATIO)
        & (close / previous - 1.0).abs().gt(KRX_DAILY_PRICE_LIMIT)
    )
    factor = factor.mask(override, share_ratio)
    after = factor.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    adjusted = (close * after).where(present)
    adjusted.index.name = "Date"

    seen_before = present.cumsum().shift(1).fillna(0).gt(0)
    reappear = present & previous.isna() & seen_before
    mask = (factor.ne(1.0) | reappear | invalid).to_numpy()
    rows, cols = np.nonzero(mask)

    def pick(values: pd.DataFrame) -> np.ndarray:
        return values.to_numpy()[rows, cols]

    kind = np.select(
        [pick(invalid), pick(reappear), pick(override)],
        ["invalid_base", "reappear", "share_ratio_override"],
        default="adjustment",
    )
    events = pd.DataFrame(
        {
            "Date": close.index[rows],
            "Code": close.columns[cols],
            "kind": kind,
            "factor": pick(factor),
            "previous_close": pick(previous),
            "base_price": pick(base),
            "close": pick(close),
            "previous_volume": pick(volume.shift(1)),
            "share_ratio": pick(share_ratio),
        }
    )
    events["halted_before"] = events["previous_volume"].eq(0)
    share_changed = events["share_ratio"].notna() & (events["share_ratio"] - 1).abs().gt(1e-9)
    events["share_ratio_mismatch"] = (
        events["kind"].eq("adjustment")
        & share_changed
        & (events["factor"] / events["share_ratio"] - 1).abs().gt(0.05)
    )
    return adjusted, events.sort_values(["Date", "Code"]).reset_index(drop=True)


def _write_parquet_atomic(frame: pd.DataFrame, path: Path, *, index: bool) -> None:
    temp_path = path.with_name(f"{path.name}.tmp")
    frame.to_parquet(temp_path, index=index)
    replace_file(temp_path, path)  # retries transient Windows access denials (DATA-008)


def build_krx_price_history(
    *,
    out_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
    sleep_seconds: float,
    marcap: pd.DataFrame | None,
    fetch_day=None,
) -> tuple[pd.DataFrame, dict | None]:
    """Full-history fetch in calendar-year chunks with a resumable checkpoint.

    Each checked year is appended to ``<KRX_PRICE_FILE>.partial``; a rerun with the same start and
    markets resumes after the last stored date. A marcap trading date missing from KRX is fetched
    once more and is an error if still missing; Close_raw mismatches are only reported.
    """
    partial_path = out_dir / f"{KRX_PRICE_FILE}.partial"
    meta_path = out_dir / f"{KRX_PRICE_FILE}.partial.json"
    meta = {"start": start.date().isoformat(), "markets": markets}
    done = None
    resume_start = start
    if partial_path.exists():
        stored = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
        if stored != meta:
            raise ValueError(
                f"{partial_path} was built with {stored}, not {meta}. Delete it to start the history fetch over."
            )
        done = pd.read_parquet(partial_path)
        resume_start = done["Date"].max() + pd.Timedelta(days=1)
        print(f"krx price resume: {len(done)} rows through {done['Date'].max().date()}", flush=True)
    checks = []
    for year in range(resume_start.year, end.year + 1):
        chunk_start = max(resume_start, pd.Timestamp(year, 1, 1))
        chunk_end = min(end, pd.Timestamp(year, 12, 31))
        if chunk_start > chunk_end:
            continue
        fetched = fetch_krx_price(
            start=chunk_start, end=chunk_end, markets=markets, sleep_seconds=sleep_seconds, fetch_day=fetch_day
        )
        if marcap is not None:
            expected = set(_marcap_slice(marcap, chunk_start, chunk_end)["Date"])
            missing = sorted(expected - set(fetched["Date"]))
            if missing:
                print(f"krx price refetch {len(missing)} missing dates: {[d.date().isoformat() for d in missing[:10]]}",
                      flush=True)
                fetched = concat_krx_price([fetched, fetch_krx_price(
                    dates=missing, markets=markets, sleep_seconds=sleep_seconds, fetch_day=fetch_day)])
                fetched = fetched.sort_values(["Date", "Code"]).reset_index(drop=True)
        errors = check_krx_price_rows(fetched)
        previous_day = None if done is None else done[done["Date"] == done["Date"].max()]
        errors += check_krx_day_counts(concat_krx_price([previous_day, fetched]), fetched["Date"].unique())
        if marcap is not None and not fetched.empty:
            check = compare_krx_close_with_marcap(fetched, marcap, start=chunk_start, end=chunk_end)
            if check["marcap_dates_missing_in_krx"]:
                errors.append(f"marcap trading dates missing in KRX quotes: {check['marcap_dates_missing_in_krx'][:10]}")
            checks.append(check)
        if errors:
            raise ValueError(f"KRX price history rejected in {year} (earlier years kept in {partial_path}): "
                             + "; ".join(errors))
        done = concat_krx_price([done, fetched])
        _write_parquet_atomic(done, partial_path, index=False)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        print(f"krx price checkpoint: {year} done, {len(done)} rows", flush=True)
    if done is None or done.empty:
        raise ValueError("KRX price history produced no rows")
    return done.sort_values(["Date", "Code"]).reset_index(drop=True), _merge_marcap_checks(checks)


def update_krx_price_cache(
    *,
    out_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
    incremental: bool,
    overlap_days: int,
    sleep_seconds: float,
    final_time: str,
    marcap: pd.DataFrame | None,
    now_kst: pd.Timestamp | None = None,
    fetch_day=None,
) -> dict:
    """Fetch KRX daily quotes, check them, and rebuild the adjusted close from the full stored history.

    With an existing price file and ``incremental`` the overlap window is refetched and any failed
    check (including a marcap Close_raw mismatch or a missing marcap trading date) writes nothing.
    Otherwise the full history is fetched by build_krx_price_history.
    """
    price_path = out_dir / KRX_PRICE_FILE
    now_kst = now_kst if now_kst is not None else pd.Timestamp.now(tz="Asia/Seoul")
    fetch_end = krx_quote_end(end, now_kst=now_kst, final_time=final_time)
    warnings = []
    changed: list[dict] = []
    incremental = incremental and price_path.exists()
    if incremental:
        existing = pd.read_parquet(price_path)
        existing["Date"] = pd.to_datetime(existing["Date"]).dt.normalize()
        fetch_start = _previous_fetch_start(existing["Date"].max(), overlap_days)
        fetched = fetch_krx_price(
            start=fetch_start, end=fetch_end, markets=markets, sleep_seconds=sleep_seconds, fetch_day=fetch_day
        )
        errors = check_krx_price_rows(fetched)
        prices, changed = merge_krx_price(existing, fetched)
        del existing
        errors += check_krx_day_counts(prices, fetched["Date"].unique())
        marcap_check = None
        if marcap is not None and not fetched.empty:
            marcap_check = compare_krx_close_with_marcap(fetched, marcap, start=fetch_start, end=fetch_end)
            if marcap_check["mismatched_rows"]:
                errors.append(f"KRX Close differs from marcap Close_raw on {marcap_check['mismatched_rows']} rows")
            if marcap_check["marcap_dates_missing_in_krx"]:
                errors.append(
                    f"marcap trading dates missing in KRX quotes: {marcap_check['marcap_dates_missing_in_krx'][:10]}"
                )
        if errors:
            raise ValueError("KRX price update rejected: " + "; ".join(errors))
        fetched_dates = int(fetched["Date"].nunique()) if not fetched.empty else 0
    else:
        fetch_start = start
        prices, marcap_check = build_krx_price_history(
            out_dir=out_dir, start=start, end=fetch_end, markets=markets, sleep_seconds=sleep_seconds,
            marcap=marcap, fetch_day=fetch_day)
        if marcap_check and marcap_check["mismatched_rows"]:
            warnings.append(f"KRX Close differs from marcap Close_raw on {marcap_check['mismatched_rows']} rows "
                            f"({marcap_check['mismatched_dates']} dates)")
        fetched_dates = int(prices["Date"].nunique())
    if prices.empty:
        raise ValueError("KRX price update produced no rows")

    adjusted, events = compute_krx_adjusted_close(prices)
    _write_parquet_atomic(prices, price_path, index=False)
    _write_parquet_atomic(adjusted, out_dir / KRX_ADJUSTED_CLOSE_FILE, index=True)
    event_counts = events["kind"].value_counts().to_dict()
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rule": KRX_ADJUSTMENT_RULE,
        "counts": {
            **{str(key): int(value) for key, value in event_counts.items()},
            "halted_before_adjustment": int((events["kind"].eq("adjustment") & events["halted_before"]).sum()),
            "share_ratio_mismatch": int(events["share_ratio_mismatch"].sum()),
        },
        "events": events.assign(Date=events["Date"].dt.date.astype(str)).to_dict("records"),
    }
    (out_dir / KRX_ADJUST_EVENTS_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8",
    )
    if not incremental:
        for path in (out_dir / f"{KRX_PRICE_FILE}.partial", out_dir / f"{KRX_PRICE_FILE}.partial.json"):
            path.unlink(missing_ok=True)
    for warning in warnings:
        print(f"warn: {warning}", file=sys.stderr)
    summary = {
        **frame_summary(prices, "Date"),
        "codes": int(prices["Code"].nunique()),
        "mode": "incremental" if incremental else "full_history",
        "fetch_start": fetch_start.date().isoformat(),
        "fetch_end": fetch_end.date().isoformat(),
        "fetched_dates": fetched_dates,
        "refetch_changed_rows": len(changed),
        "refetch_changed_examples": changed[:20],
        "marcap_check": marcap_check,
        "warnings": warnings,
        "adjusted_close": frame_summary(adjusted),
        "events": payload["counts"],
        "rule": KRX_ADJUSTMENT_RULE,
    }
    print("updated:", price_path, "rows:", len(prices), "range:", summary["start"], "->", summary["end"], flush=True)
    print("events:", payload["counts"], flush=True)
    return summary


def _read_marcap_for_checks(path: Path) -> pd.DataFrame:
    """Columns the report, alignment check and KRX comparison use; Code as a dictionary (low memory)."""
    import pyarrow.parquet as pq

    return pq.read_table(path, columns=["Date", "Code", "Close_raw"], read_dictionary=["Code"]).to_pandas()


def validate_cache_alignment(
    marcap: pd.DataFrame | None,
    close: pd.DataFrame | None,
    close_label: str = "close_fdr",
) -> list[str]:
    errors = []
    if marcap is not None and close is not None and not marcap.empty and not close.empty:
        marcap_end = pd.to_datetime(marcap["Date"]).max().normalize()
        close_end = pd.to_datetime(close.index).max().normalize()
        if marcap_end != close_end:
            errors.append(f"marcap end date {marcap_end.date()} != {close_label} end date {close_end.date()}")
    return errors


def update_index_price_cache(
    *,
    out_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    symbols: dict[str, str],
    overlap_days: int,
) -> pd.DataFrame:
    path = out_dir / "index_price.parquet"
    if path.exists():
        existing = pd.read_parquet(path)
        existing.index = pd.to_datetime(existing.index).normalize()
        fetch_start = _previous_fetch_start(existing.index.max().normalize(), overlap_days)
    else:
        existing = pd.DataFrame(index=pd.DatetimeIndex([]), columns=sorted(symbols))
        fetch_start = start.normalize()
    fetched = fetch_yahoo_index_price(symbols=symbols, start=fetch_start, end=end)
    index_price = merge_index_price(existing, fetched)
    index_price.to_parquet(path)
    print(
        "updated:",
        path,
        "rows:",
        len(index_price),
        "columns:",
        len(index_price.columns),
        "fetched_columns:",
        len(fetched.columns),
    )
    if not index_price.empty:
        print("index_price range:", index_price.index.min().date(), "->", index_price.index.max().date())
    return index_price


def update_raw_caches(
    *,
    args: argparse.Namespace,
    out_dir: Path,
    end: pd.Timestamp,
    markets: list[str],
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    marcap_path = out_dir / "marcap_daily.parquet"
    close_path = out_dir / "close_fdr.parquet"

    marcap = None
    close = None
    stock_count_change_codes: set[str] = set()

    if not args.skip_marcap:
        if not marcap_path.exists():
            raise FileNotFoundError(f"Missing existing marcap cache: {marcap_path}")
        existing_marcap = pd.read_parquet(marcap_path)
        existing_marcap["Date"] = pd.to_datetime(existing_marcap["Date"]).dt.normalize()
        last_marcap_date = existing_marcap["Date"].max().normalize()
        fetch_start = _previous_fetch_start(last_marcap_date, args.marcap_overlap_days)
        fetched_marcap = fetch_pykrx_marcap(start=fetch_start, end=end, markets=markets)
        stock_count_change_codes = find_new_stock_count_change_codes(
            existing_marcap,
            fetched_marcap,
            existing_end=last_marcap_date,
        )
        marcap = merge_marcap(existing_marcap, fetched_marcap)
        marcap.to_parquet(marcap_path, index=False)
        print(
            "updated:",
            marcap_path,
            "rows:",
            len(marcap),
            "codes:",
            marcap["Code"].nunique() if not marcap.empty else 0,
            "fetched_rows:",
            len(fetched_marcap),
        )
        if not marcap.empty:
            print("marcap range:", marcap["Date"].min().date(), "->", marcap["Date"].max().date())

    if not args.skip_close:
        if not close_path.exists():
            raise FileNotFoundError(f"Missing existing close cache: {close_path}")
        existing_close = pd.read_parquet(close_path)
        existing_close.index = pd.to_datetime(existing_close.index).normalize()
        existing_close.columns = [str(code).zfill(6) for code in existing_close.columns]
        existing_close = existing_close.loc[
            :, ~pd.Index(existing_close.columns).duplicated(keep="last")
        ]
        last_close_date = existing_close.index.max().normalize()
        fetch_start = _previous_fetch_start(last_close_date, args.close_overlap_days)
        if marcap is None and marcap_path.exists():
            marcap_for_codes = pd.read_parquet(marcap_path, columns=["Date", "Code"])
            marcap_for_codes["Date"] = pd.to_datetime(marcap_for_codes["Date"]).dt.normalize()
        elif marcap is not None:
            marcap_for_codes = marcap[["Date", "Code"]].copy()
        else:
            marcap_for_codes = pd.DataFrame(columns=["Date", "Code"])
        new_codes = set(
            marcap_for_codes.loc[marcap_for_codes["Date"] >= fetch_start, "Code"].astype(str).str.zfill(6)
        )
        codes = sorted(set(str(c).zfill(6) for c in existing_close.columns) | new_codes)
        fetched_close = fetch_fdr_close(
            codes=codes,
            start=fetch_start,
            end=end,
            sleep_seconds=args.close_sleep_seconds,
        )
        retroactive_change_codes = find_retroactive_close_change_codes(
            existing_close,
            fetched_close,
        )
        history_refresh_codes = (
            retroactive_change_codes | stock_count_change_codes
        ) & set(codes)
        close = merge_close(existing_close, fetched_close)
        if history_refresh_codes:
            print(
                "full FDR history refresh:",
                len(history_refresh_codes),
                "codes:",
                sorted(history_refresh_codes)[:20],
            )
            refreshed_close = fetch_fdr_close(
                codes=sorted(history_refresh_codes),
                start=existing_close.index.min().normalize(),
                end=end,
                sleep_seconds=args.close_sleep_seconds,
            )
            close = replace_close_histories(
                close,
                refreshed_close,
                codes=history_refresh_codes,
            )
        temp_close_path = close_path.with_name(f"{close_path.name}.tmp")
        close.to_parquet(temp_close_path)
        temp_close_path.replace(close_path)
        print(
            "updated:",
            close_path,
            "rows:",
            len(close),
            "codes:",
            len(close.columns),
            "fetched_columns:",
            len(fetched_close.columns),
        )
        if not close.empty:
            print("close range:", close.index.min().date(), "->", close.index.max().date())

    return marcap, close


def _run_krx_price_update(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    markets: list[str],
    marcap: pd.DataFrame | None,
) -> dict | None:
    if not args.include_krx_price:
        return None
    return update_krx_price_cache(
        out_dir=out_dir,
        start=start,
        end=end,
        markets=markets,
        incremental=bool(args.update),
        overlap_days=args.krx_price_overlap_days,
        sleep_seconds=args.krx_price_sleep_seconds,
        final_time=args.krx_final_time,
        marcap=marcap,
    )


def main() -> int:
    args = parse_args()
    start = pd.Timestamp(args.start).normalize()
    marcap_dir = Path(args.marcap_dir) if str(args.marcap_dir or "").strip() else Path()
    close_file = Path(args.close_file) if str(args.close_file or "").strip() else Path()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.end:
        end = pd.Timestamp(args.end).normalize()
    elif args.update and args.include_krx_price:
        # Same stored-day rule for every source: today only after --krx-final-time (KST).
        end = krx_quote_end(
            pd.Timestamp.today(), now_kst=pd.Timestamp.now(tz="Asia/Seoul"), final_time=args.krx_final_time
        )
    elif args.update:
        end = pd.Timestamp.today().normalize()
    else:
        if not args.skip_marcap:
            marcap_dir = _required_seed_path(args.marcap_dir, "--marcap-dir")
        end = latest_date_in_marcap_dir(marcap_dir)

    markets = [x.strip().upper() for x in args.markets.split(",") if x.strip()]
    if not markets:
        raise ValueError("--markets must include at least one market")

    if args.update:
        marcap, close = update_raw_caches(args=args, out_dir=out_dir, end=end, markets=markets)
        index_price = None
        industry = None
        if args.include_index:
            index_price = update_index_price_cache(
                out_dir=out_dir,
                start=start,
                end=end,
                symbols=_parse_index_symbols(args.index_symbols),
                overlap_days=args.index_overlap_days,
            )
        if args.include_industry:
            industry = update_industry_cache(
                out_dir=out_dir,
                start=start,
                end=end,
                markets=markets,
                frequency=args.industry_frequency,
                sleep_seconds=args.industry_sleep_seconds,
            )
        if marcap is None and (out_dir / "marcap_daily.parquet").exists():
            marcap_path = out_dir / "marcap_daily.parquet"
            marcap = _read_marcap_for_checks(marcap_path) if args.include_krx_price else pd.read_parquet(marcap_path)
        if close is None and (out_dir / "close_fdr.parquet").exists():
            close = pd.read_parquet(out_dir / "close_fdr.parquet")
        if index_price is None and (out_dir / "index_price.parquet").exists():
            index_price = pd.read_parquet(out_dir / "index_price.parquet")
        industry_path = out_dir / _industry_file(args.industry_frequency)
        if industry is None and industry_path.exists():
            industry = pd.read_parquet(industry_path)
        krx_price = _run_krx_price_update(args, out_dir=out_dir, start=start, end=end, markets=markets, marcap=marcap)
        report = write_report(
            out_dir,
            args,
            marcap,
            close,
            index_price,
            industry,
            start,
            end,
            markets,
            krx_price=krx_price,
        )
        if args.include_krx_price and args.skip_close:
            # close_fdr is no longer refreshed; the KRX adjusted close is the price that must line up.
            alignment_errors = validate_cache_alignment(
                marcap, pd.read_parquet(out_dir / KRX_ADJUSTED_CLOSE_FILE).iloc[:, :1], KRX_ADJUSTED_CLOSE_FILE
            )
        else:
            alignment_errors = validate_cache_alignment(marcap, close)
        if alignment_errors:
            report["validation_errors"] = alignment_errors
            (out_dir / "download_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for error in alignment_errors:
                print(f"validation_error: {error}", file=sys.stderr)
            return 2
        print("saved:", out_dir / "download_report.json")
        print("coverage:", report["code_coverage"])
        return 0

    marcap = None
    close = None
    index_price = None
    industry = None

    if not args.skip_marcap:
        marcap_dir = _required_seed_path(args.marcap_dir, "--marcap-dir")
        marcap = build_marcap_cache(marcap_dir, start, end, set(markets))
        marcap_path = out_dir / "marcap_daily.parquet"
        marcap.to_parquet(marcap_path, index=False)
        print(
            "saved:",
            marcap_path,
            "rows:",
            len(marcap),
            "codes:",
            marcap["Code"].nunique() if not marcap.empty else 0,
        )
        if not marcap.empty:
            print("marcap range:", marcap["Date"].min().date(), "->", marcap["Date"].max().date())

    if not args.skip_close:
        close_file = _required_seed_path(args.close_file, "--close-file")
        close = copy_close_cache(close_file, start, end)
        close_path = out_dir / "close_fdr.parquet"
        close.to_parquet(close_path)
        print("saved:", close_path, "rows:", len(close), "codes:", len(close.columns))
        if not close.empty:
            print("close range:", close.index.min().date(), "->", close.index.max().date())

    if args.include_index:
        index_price = update_index_price_cache(
            out_dir=out_dir,
            start=start,
            end=end,
            symbols=_parse_index_symbols(args.index_symbols),
            overlap_days=args.index_overlap_days,
        )

    if args.include_industry:
        industry = update_industry_cache(
            out_dir=out_dir,
            start=start,
            end=end,
            markets=markets,
            frequency=args.industry_frequency,
            sleep_seconds=args.industry_sleep_seconds,
        )

    if marcap is None and (out_dir / "marcap_daily.parquet").exists() and args.include_krx_price:
        marcap = _read_marcap_for_checks(out_dir / "marcap_daily.parquet")
    krx_price = _run_krx_price_update(args, out_dir=out_dir, start=start, end=end, markets=markets, marcap=marcap)
    report = write_report(
        out_dir,
        args,
        marcap,
        close,
        index_price,
        industry,
        start,
        end,
        markets,
        krx_price=krx_price,
    )
    print("saved:", out_dir / "download_report.json")
    print("coverage:", report["code_coverage"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
