#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.active_profile import resolve_profile_reference


DEFAULT_PROFILE_JSON = ROOT_DIR / "us_stock_live" / "configs" / "us_2x_ugl_tlt_mom126_top3_p975.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update the stock-live price cache using the existing stock/FX cache builders."
    )
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="Stock-live profile JSON path.")
    parser.add_argument("--start", default="", help="Override download start date.")
    parser.add_argument("--end", default="", help="Override exclusive download end date.")
    parser.add_argument("--period", default="", help="Override yfinance period, for example max.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument(
        "--recent-completeness-rows",
        type=int,
        default=10,
        help=(
            "After building signal_price.parquet, drop rows in the latest N rows "
            "when any market is missing. This protects cross-sectional live ranks "
            "from partially populated latest Yahoo rows."
        ),
    )
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def _run(command: list[str], *, dry_run: bool) -> None:
    print(" ".join(command))
    if dry_run:
        return
    subprocess.run(command, cwd=ROOT_DIR, check=True)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Cannot parse boolean value: {value!r}")


def _drop_recent_incomplete_signal_rows(source_cache_dir: Path, *, recent_rows: int, dry_run: bool) -> None:
    if recent_rows <= 0:
        return

    signal_path = source_cache_dir / "signal_price.parquet"
    if not signal_path.exists():
        print(f"signal_completeness_check=skipped missing={signal_path}")
        return

    prices = pd.read_parquet(signal_path).sort_index()
    if prices.empty:
        print(f"signal_completeness_check=skipped empty={signal_path}")
        return

    recent = prices.tail(recent_rows)
    incomplete_mask = recent.isna().any(axis=1)
    incomplete_dates = list(recent.index[incomplete_mask])
    if not incomplete_dates:
        latest = prices.index.max()
        print(
            "signal_completeness_check=ok "
            f"latest={latest.date()} columns={prices.shape[1]} recent_rows={len(recent)}"
        )
        return

    for timestamp in incomplete_dates:
        missing = prices.columns[prices.loc[timestamp].isna()].tolist()
        print(
            "signal_completeness_check=drop "
            f"date={timestamp.date()} missing_count={len(missing)} missing={','.join(map(str, missing))}"
        )

    cleaned = prices.drop(index=incomplete_dates)
    if cleaned.empty:
        raise ValueError("Refusing to write empty signal_price.parquet after completeness filtering")

    if dry_run:
        print(
            "signal_completeness_check=dry_run "
            f"would_drop={len(incomplete_dates)} remaining_rows={len(cleaned)}"
        )
        return

    cleaned.to_parquet(signal_path)
    print(
        "signal_completeness_check=updated "
        f"dropped={len(incomplete_dates)} rows={len(cleaned)} latest={cleaned.index.max().date()}"
    )


def main() -> int:
    args = build_parser().parse_args()
    profile_path, _active_payload = resolve_profile_reference(_resolve_path(args.profile_json))
    profile = _read_json(profile_path)
    data = profile.get("data") or {}
    strategy = profile.get("strategy") or {}

    tickers = data.get("tickers") or []
    if not tickers:
        raise ValueError("Profile data.tickers must not be empty")
    trade_csv = _resolve_path(data["trade_csv"])
    signal_csv = _resolve_path(data["signal_csv"])
    source_cache_dir = _resolve_path(strategy["source_cache_dir"])

    period = args.period or data.get("period") or ""
    start = args.start or data.get("start") or ""
    end = args.end or data.get("end") or ""
    build_trade_price = _coerce_bool(data.get("build_trade_price"), default=True)

    trade_csv.parent.mkdir(parents=True, exist_ok=True)
    signal_csv.parent.mkdir(parents=True, exist_ok=True)
    source_cache_dir.mkdir(parents=True, exist_ok=True)

    download_command = [
        sys.executable,
        "scripts/build_fx_adjusted_stock_universe.py",
        "--tickers",
        ",".join(tickers),
        "--out-csv",
        str(trade_csv),
        "--signal-out-csv",
        str(signal_csv),
        "--interval",
        str(data.get("interval", "1d")),
        "--price-field",
        str(data.get("price_field", "Close")),
        "--fx-series",
        str(data.get("fx_series", "DEXKOUS")),
    ]
    if not build_trade_price:
        download_command.append("--signal-only")
    if period:
        download_command.extend(["--period", str(period)])
    else:
        if start:
            download_command.extend(["--start", str(start)])
        if end:
            download_command.extend(["--end", str(end)])

    trade_cache_command = [
        sys.executable,
        "scripts/build_price_only_source_cache.py",
        "--input-csv",
        str(trade_csv),
        "--out-dir",
        str(source_cache_dir),
        "--output-column-name",
        "trade_price",
        "--write-market-warning",
        "--write-market-meta",
    ]
    signal_cache_command = [
        sys.executable,
        "scripts/build_price_only_source_cache.py",
        "--input-csv",
        str(signal_csv),
        "--out-dir",
        str(source_cache_dir),
        "--output-column-name",
        "signal_price",
    ]

    _run(download_command, dry_run=args.dry_run)
    if build_trade_price:
        _run(trade_cache_command, dry_run=args.dry_run)
    _run(signal_cache_command, dry_run=args.dry_run)
    _drop_recent_incomplete_signal_rows(
        source_cache_dir,
        recent_rows=int(args.recent_completeness_rows),
        dry_run=bool(args.dry_run),
    )
    print(f"updated_source_cache_dir={source_cache_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
