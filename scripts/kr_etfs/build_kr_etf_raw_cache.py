#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "data" / "kr_etfs" / "raw"

CATEGORY_NAME_MAP = {
    0: "ALL",
    1: "DOMESTIC_MARKET_INDEX",
    2: "DOMESTIC_SECTOR_THEME",
    3: "DOMESTIC_DERIVATIVE",
    4: "OVERSEAS_EQUITY",
    5: "COMMODITY",
    6: "BOND",
    7: "OTHER",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Korean ETF raw master cache from the current FDR ETF/KR listing."
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _load_fdr():
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise ImportError(
            "FinanceDataReader is required. Install it with: py -m pip install finance-datareader"
        ) from exc
    return fdr


def _normalize_master(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"Symbol", "Category", "Name"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"ETF/KR listing missing required columns: {sorted(missing)}")

    out = frame.copy()
    out["Symbol"] = out["Symbol"].astype(str).str.strip().str.zfill(6)
    out["Name"] = out["Name"].fillna("").astype(str)
    out["Category"] = pd.to_numeric(out["Category"], errors="coerce").astype("Int64")
    out["CategoryName"] = out["Category"].map(CATEGORY_NAME_MAP).fillna("UNKNOWN")
    out = out.drop_duplicates(subset=["Symbol"], keep="last")
    out = out.sort_values("Symbol").reset_index(drop=True)
    return out


def _write_report(out_dir: Path, master: pd.DataFrame) -> None:
    category_counts = (
        master["CategoryName"]
        .value_counts(dropna=False)
        .sort_index()
        .astype(int)
        .to_dict()
    )
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": 'FinanceDataReader.StockListing("ETF/KR")',
        "rows": int(master.shape[0]),
        "columns": list(master.columns),
        "category_counts": category_counts,
        "outputs": {
            "etf_master": str(out_dir / "etf_master.parquet"),
            "master_report": str(out_dir / "master_report.json"),
        },
    }
    (out_dir / "master_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    out_dir = _resolve(args.out_dir)
    fdr = _load_fdr()

    master = _normalize_master(fdr.StockListing("ETF/KR"))
    if master.empty:
        raise SystemExit("ETF/KR listing returned no rows")

    out_dir.mkdir(parents=True, exist_ok=True)
    master.to_parquet(out_dir / "etf_master.parquet", index=False)
    _write_report(out_dir, master)

    print(f"Wrote Korean ETF master cache to {out_dir}")
    print(f"Rows: {master.shape[0]}")
    print("Wrote: etf_master.parquet, master_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
