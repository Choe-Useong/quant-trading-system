#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "data" / "us_etfs" / "raw"

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

EXCHANGE_NAME_MAP = {
    "A": "NYSE_MKT",
    "N": "NYSE",
    "P": "NYSE_ARCA",
    "V": "IEX",
    "Z": "CBOE_BZX",
    "Q": "NASDAQ_GLOBAL_SELECT",
    "G": "NASDAQ_GLOBAL",
    "S": "NASDAQ_CAPITAL",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a US ETF raw master cache from Nasdaq Trader symbol directory files."
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--keep-test-issues", action="store_true")
    parser.add_argument(
        "--include-non-normal-financial-status",
        action="store_true",
        help="Keep Nasdaq-listed ETF rows whose Financial Status is not normal.",
    )
    parser.add_argument(
        "--save-snapshot",
        action="store_true",
        help="Also save a timestamped master snapshot under us_etf_master_snapshots.",
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _download_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _read_symbol_directory(url: str) -> pd.DataFrame:
    text = _download_text(url)
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and lines[-1].startswith("File Creation Time:"):
        lines = lines[:-1]
    if not lines:
        raise ValueError(f"No rows returned from {url}")
    return pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str)


def _clean_text(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in out.columns:
        out[column] = out[column].fillna("").astype(str).str.strip()
    return out


def _normalize_nasdaq_listed(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _clean_text(frame)
    required = {"Symbol", "Security Name", "Market Category", "Test Issue", "Financial Status", "Round Lot Size", "ETF"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"nasdaqlisted.txt missing required columns: {sorted(missing)}")

    nextshares = frame["NextShares"].str.upper() if "NextShares" in frame else ""
    out = pd.DataFrame(
        {
            "Symbol": frame["Symbol"].str.upper(),
            "Name": frame["Security Name"],
            "SourceFile": "nasdaqlisted",
            "Exchange": "Q",
            "ExchangeName": frame["Market Category"].map(EXCHANGE_NAME_MAP).fillna("NASDAQ"),
            "MarketCategory": frame["Market Category"],
            "ETF": frame["ETF"].str.upper(),
            "TestIssue": frame["Test Issue"].str.upper(),
            "FinancialStatus": frame["Financial Status"].str.upper(),
            "RoundLotSize": pd.to_numeric(frame["Round Lot Size"], errors="coerce"),
            "NasdaqSymbol": frame["Symbol"].str.upper(),
            "CQSSymbol": "",
            "NextShares": nextshares,
        }
    )
    return out


def _normalize_other_listed(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _clean_text(frame)
    required = {"ACT Symbol", "Security Name", "Exchange", "CQS Symbol", "ETF", "Round Lot Size", "Test Issue", "NASDAQ Symbol"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"otherlisted.txt missing required columns: {sorted(missing)}")

    exchange = frame["Exchange"].str.upper()
    out = pd.DataFrame(
        {
            "Symbol": frame["ACT Symbol"].str.upper(),
            "Name": frame["Security Name"],
            "SourceFile": "otherlisted",
            "Exchange": exchange,
            "ExchangeName": exchange.map(EXCHANGE_NAME_MAP).fillna("OTHER"),
            "MarketCategory": "",
            "ETF": frame["ETF"].str.upper(),
            "TestIssue": frame["Test Issue"].str.upper(),
            "FinancialStatus": "",
            "RoundLotSize": pd.to_numeric(frame["Round Lot Size"], errors="coerce"),
            "NasdaqSymbol": frame["NASDAQ Symbol"].str.upper(),
            "CQSSymbol": frame["CQS Symbol"].str.upper(),
            "NextShares": "",
        }
    )
    return out


def _filter_master(
    master: pd.DataFrame,
    *,
    keep_test_issues: bool,
    include_non_normal_financial_status: bool,
) -> pd.DataFrame:
    filtered = master[master["ETF"].eq("Y")].copy()
    if not keep_test_issues:
        filtered = filtered[~filtered["TestIssue"].eq("Y")]
    if not include_non_normal_financial_status:
        normal_status = filtered["FinancialStatus"].isin(["", "N"])
        filtered = filtered[normal_status]
    filtered = filtered.drop_duplicates(subset=["Symbol"], keep="first")
    return filtered.sort_values("Symbol").reset_index(drop=True)


def _write_report(
    out_dir: Path,
    *,
    master: pd.DataFrame,
    raw_rows: dict[str, int],
    args: argparse.Namespace,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "nasdaqlisted": NASDAQ_LISTED_URL,
            "otherlisted": OTHER_LISTED_URL,
        },
        "filters": {
            "ETF": "Y",
            "keep_test_issues": bool(args.keep_test_issues),
            "include_non_normal_financial_status": bool(args.include_non_normal_financial_status),
        },
        "raw_rows": raw_rows,
        "rows": int(master.shape[0]),
        "columns": list(master.columns),
        "exchange_counts": master["ExchangeName"].value_counts(dropna=False).sort_index().astype(int).to_dict(),
        "source_file_counts": master["SourceFile"].value_counts(dropna=False).sort_index().astype(int).to_dict(),
        "outputs": {
            "etf_master": str(out_dir / "etf_master.parquet"),
            "master_report": str(out_dir / "master_report.json"),
        },
    }
    (out_dir / "master_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_snapshot(out_dir: Path, master: pd.DataFrame) -> Path:
    snapshot_dir = out_dir / "us_etf_master_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"etf_master_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
    master.to_parquet(snapshot_path, index=False)
    return snapshot_path


def main() -> int:
    args = parse_args()
    out_dir = _resolve(args.out_dir)

    nasdaq = _normalize_nasdaq_listed(_read_symbol_directory(NASDAQ_LISTED_URL))
    other = _normalize_other_listed(_read_symbol_directory(OTHER_LISTED_URL))
    raw_rows = {"nasdaqlisted": int(nasdaq.shape[0]), "otherlisted": int(other.shape[0])}
    master = _filter_master(
        pd.concat([nasdaq, other], ignore_index=True),
        keep_test_issues=bool(args.keep_test_issues),
        include_non_normal_financial_status=bool(args.include_non_normal_financial_status),
    )
    if master.empty:
        raise SystemExit("Nasdaq Trader symbol directory returned no ETF rows after filtering")

    out_dir.mkdir(parents=True, exist_ok=True)
    master.to_parquet(out_dir / "etf_master.parquet", index=False)
    snapshot_path = _write_snapshot(out_dir, master) if args.save_snapshot else None
    _write_report(out_dir, master=master, raw_rows=raw_rows, args=args)

    print(f"Wrote US ETF master cache to {out_dir}")
    print(f"Rows: {master.shape[0]}")
    print("Wrote: etf_master.parquet, master_report.json")
    if snapshot_path is not None:
        print(f"Snapshot: {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
