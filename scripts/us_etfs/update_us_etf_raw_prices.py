#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE = ROOT / "data" / "us_etfs" / "universe" / "leveraged_2x.csv"
DEFAULT_OUT_ROOT = ROOT / "data" / "us_etfs" / "raw" / "prices"
DEFAULT_START = "2000-01-01"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update US ETF raw adjusted close and volume caches from Yahoo Finance."
    )
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument(
        "--name",
        default="",
        help="Output universe name. Defaults to the universe CSV stem.",
    )
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default="")
    parser.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated symbols for a partial/sample update.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Incrementally refresh existing caches from latest date minus overlap.",
    )
    parser.add_argument("--overlap-days", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from download_progress.json for the same symbol set and fetch window.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the latest saved progress status and exit.",
    )
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _load_yfinance():
    try:
        return importlib.import_module("yfinance")
    except ImportError as exc:
        raise ImportError("yfinance is required. Install it with: py -m pip install yfinance") from exc


def _normalize_symbol(value: object) -> str:
    return str(value).strip().upper()


def _parse_symbols(raw: str) -> set[str] | None:
    values = {_normalize_symbol(item) for item in raw.split(",") if item.strip()}
    return values or None


def _load_symbols(universe_path: Path, requested: set[str] | None) -> list[str]:
    if not universe_path.exists():
        raise FileNotFoundError(universe_path)
    universe = pd.read_csv(universe_path, dtype=str)
    if "Symbol" not in universe.columns:
        raise ValueError(f"{universe_path} must contain a Symbol column")
    symbols = sorted({_normalize_symbol(value) for value in universe["Symbol"].dropna()})
    if requested is not None:
        missing = sorted(requested.difference(symbols))
        if missing:
            raise ValueError(f"Requested symbols not present in universe: {missing}")
        symbols = [symbol for symbol in symbols if symbol in requested]
    if not symbols:
        raise SystemExit("No ETF symbols selected")
    return symbols


def _read_existing_wide(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    if frame.empty:
        return pd.DataFrame()
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [_normalize_symbol(column) for column in frame.columns]
    frame = frame[~frame.index.duplicated(keep="last")]
    frame = frame.loc[:, ~pd.Index(frame.columns).duplicated()]
    return frame.sort_index().sort_index(axis=1)


def _validate_parquet_file(path: Path) -> None:
    importlib.import_module("pyarrow.parquet").ParquetFile(path).metadata


def _atomic_write_parquet(frame: pd.DataFrame, path: Path, **kwargs: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_parquet(tmp_path, **kwargs)
        _validate_parquet_file(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _progress_path(out_dir: Path) -> Path:
    return out_dir / "download_progress.json"


def _read_progress(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_progress(path: Path, progress: dict[str, object]) -> None:
    progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "NA"
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _print_status(progress: dict[str, object]) -> None:
    total = int(progress.get("symbols_requested", 0))
    completed = len(progress.get("completed_symbols", []))
    remaining = max(total - completed, 0)
    failed = len(progress.get("failed_symbols", []))
    no_data = len(progress.get("no_data_symbols", []))
    eta = progress.get("eta_seconds")
    print(f"phase: {progress.get('phase', 'unknown')}")
    print(
        f"yahoo: completed {completed}/{total}, remaining {remaining}, "
        f"failed {failed}, no_data {no_data}, eta {_format_duration(eta)}"
    )


def _new_progress(
    *,
    mode: str,
    universe_path: Path,
    output_name: str,
    symbols: list[str],
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, object]:
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "mode": mode,
        "phase": "yahoo",
        "universe": str(universe_path),
        "name": output_name,
        "symbols_requested": len(symbols),
        "fetch_window": {
            "start": fetch_start.date().isoformat(),
            "end": end.date().isoformat(),
        },
        "completed_symbols": [],
        "failed_symbols": [],
        "no_data_symbols": [],
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
    }


def _validate_resume_progress(
    progress: dict[str, object],
    *,
    mode: str,
    universe_path: Path,
    output_name: str,
    symbols: list[str],
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    expected_window = {
        "start": fetch_start.date().isoformat(),
        "end": end.date().isoformat(),
    }
    if progress.get("mode") != mode:
        raise ValueError("Cannot resume: progress mode differs from current run")
    if progress.get("name") != output_name:
        raise ValueError("Cannot resume: output name differs from current run")
    if Path(str(progress.get("universe", ""))) != universe_path:
        raise ValueError("Cannot resume: universe path differs from current run")
    if int(progress.get("symbols_requested", -1)) != len(symbols):
        raise ValueError("Cannot resume: selected symbol count differs from current run")
    if progress.get("fetch_window") != expected_window:
        raise ValueError("Cannot resume: fetch window differs from current run")


def _resolve_fetch_start(
    *,
    configured_start: str,
    update: bool,
    overlap_days: int,
    existing_dates: pd.Index,
) -> pd.Timestamp:
    start = pd.Timestamp(configured_start).normalize()
    if not update or len(existing_dates) == 0:
        return start
    last_date = pd.to_datetime(existing_dates).max().normalize()
    return max(start, last_date - pd.Timedelta(days=max(int(overlap_days), 0)))


def _resolve_fetch_end(raw: str) -> pd.Timestamp:
    if raw:
        return pd.Timestamp(raw).normalize()
    return pd.Timestamp.today().normalize()


def _split_batches(values: list[str], size: int) -> list[list[str]]:
    size = max(int(size), 1)
    return [values[i : i + size] for i in range(0, len(values), size)]


def _extract_yahoo_field(data: pd.DataFrame, field: str, tickers: list[str]) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        if field not in data.columns.get_level_values(0):
            return pd.DataFrame()
        frame = data[field].copy()
    else:
        if field not in data.columns:
            return pd.DataFrame()
        if len(tickers) != 1:
            raise ValueError(f"Unexpected single-index Yahoo response for multiple tickers: {tickers}")
        frame = data[[field]].rename(columns={field: tickers[0]})
    if isinstance(frame, pd.Series):
        frame = frame.to_frame()
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [_normalize_symbol(column) for column in frame.columns]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    return frame.dropna(axis=1, how="all").sort_index().sort_index(axis=1)


def _merge_wide(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        merged = incoming.copy()
    elif incoming.empty:
        merged = existing.copy()
    else:
        existing = existing.copy()
        incoming = incoming.copy()
        existing.index = pd.to_datetime(existing.index).normalize()
        incoming.index = pd.to_datetime(incoming.index).normalize()
        merged = existing.reindex(existing.index.union(incoming.index))
        for column in incoming.columns:
            if column not in merged.columns:
                merged[column] = pd.NA
            values = incoming[column].dropna()
            if not values.empty:
                merged.loc[values.index, column] = values
    if merged.empty:
        return pd.DataFrame()
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.loc[:, ~pd.Index(merged.columns).duplicated()]
    return merged.sort_index().sort_index(axis=1)


def _download_yahoo(
    symbols: list[str],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    batch_size: int,
    sleep_seconds: float,
    existing_adj_close: pd.DataFrame,
    existing_volume: pd.DataFrame,
    adj_close_path: Path,
    volume_path: Path,
    progress_path: Path,
    progress: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    yf = _load_yfinance()
    adj_close = existing_adj_close.copy()
    volume = existing_volume.copy()
    completed = set(progress.get("completed_symbols", []))
    failed = set(progress.get("failed_symbols", []))
    no_data = set(progress.get("no_data_symbols", []))
    pending = [symbol for symbol in symbols if symbol not in completed]
    started_at = time.monotonic()

    for batch in _split_batches(pending, batch_size):
        try:
            data = yf.download(
                tickers=batch,
                start=start.strftime("%Y-%m-%d"),
                end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=True,
                group_by="column",
            )
            batch_adj_close = _extract_yahoo_field(data, "Adj Close", batch)
            batch_volume = _extract_yahoo_field(data, "Volume", batch)
            downloaded = set(batch_adj_close.columns).union(batch_volume.columns)
            for symbol in batch:
                if symbol in downloaded:
                    failed.discard(symbol)
                    no_data.discard(symbol)
                else:
                    no_data.add(symbol)
                    failed.discard(symbol)
                completed.add(symbol)
        except Exception:
            batch_adj_close = pd.DataFrame()
            batch_volume = pd.DataFrame()
            for symbol in batch:
                failed.add(symbol)
                no_data.discard(symbol)
                completed.add(symbol)

        adj_close = _merge_wide(adj_close, batch_adj_close)
        volume = _merge_wide(volume, batch_volume)
        _atomic_write_parquet(adj_close, adj_close_path)
        _atomic_write_parquet(volume, volume_path)

        elapsed = float(progress.get("elapsed_seconds", 0.0)) + (time.monotonic() - started_at)
        completed_count = len(completed)
        eta = None
        if completed_count > 0 and completed_count < len(symbols):
            eta = elapsed / completed_count * (len(symbols) - completed_count)
        progress.update(
            {
                "completed_symbols": sorted(completed),
                "failed_symbols": sorted(failed),
                "no_data_symbols": sorted(no_data),
                "elapsed_seconds": elapsed,
                "eta_seconds": eta,
            }
        )
        _write_progress(progress_path, progress)
        print(
            f"yahoo: completed {completed_count}/{len(symbols)}, "
            f"remaining {len(symbols) - completed_count}, failed {len(failed)}, "
            f"no_data {len(no_data)}, eta {_format_duration(eta)}"
        )
        started_at = time.monotonic()
        if sleep_seconds > 0:
            time.sleep(float(sleep_seconds))

    return adj_close, volume, sorted(failed), sorted(no_data)


def _write_report(
    out_dir: Path,
    *,
    args: argparse.Namespace,
    universe_path: Path,
    output_name: str,
    symbols: list[str],
    adj_close: pd.DataFrame,
    volume: pd.DataFrame,
    failed: list[str],
    no_data: list[str],
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "update" if args.update else "rebuild",
        "name": output_name,
        "universe": str(universe_path),
        "symbols_requested": len(symbols),
        "fetch_start": fetch_start.date().isoformat(),
        "fetch_end": end.date().isoformat(),
        "adj_close": {
            "rows": int(adj_close.shape[0]),
            "symbols": int(adj_close.shape[1]),
            "start": adj_close.index.min().date().isoformat() if not adj_close.empty else None,
            "end": adj_close.index.max().date().isoformat() if not adj_close.empty else None,
        },
        "volume": {
            "rows": int(volume.shape[0]),
            "symbols": int(volume.shape[1]),
            "start": volume.index.min().date().isoformat() if not volume.empty else None,
            "end": volume.index.max().date().isoformat() if not volume.empty else None,
        },
        "failed_symbols": failed,
        "no_data_symbols": no_data,
        "outputs": {
            "adj_close": str(out_dir / "adj_close.parquet"),
            "volume": str(out_dir / "volume.parquet"),
            "download_report": str(out_dir / "download_report.json"),
        },
    }
    (out_dir / "download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    universe_path = _resolve(args.universe)
    output_name = args.name.strip() or universe_path.stem
    out_root = _resolve(args.out_root)
    out_dir = out_root / output_name
    progress_path = _progress_path(out_dir)
    if args.status:
        progress = _read_progress(progress_path)
        if progress is None:
            raise SystemExit(f"No saved progress found: {progress_path}")
        _print_status(progress)
        return 0

    symbols = _load_symbols(universe_path, _parse_symbols(args.symbols))
    out_dir.mkdir(parents=True, exist_ok=True)
    adj_close_path = out_dir / "adj_close.parquet"
    volume_path = out_dir / "volume.parquet"
    existing_adj_close = _read_existing_wide(adj_close_path)
    existing_volume = _read_existing_wide(volume_path)
    existing_dates = existing_adj_close.index.union(existing_volume.index)
    fetch_start = _resolve_fetch_start(
        configured_start=args.start,
        update=bool(args.update),
        overlap_days=int(args.overlap_days),
        existing_dates=existing_dates,
    )
    end = _resolve_fetch_end(args.end)
    if fetch_start > end:
        raise SystemExit("Fetch start is after fetch end")

    mode = "update" if args.update else "rebuild"
    if args.resume:
        progress = _read_progress(progress_path)
        if progress is None:
            raise SystemExit(f"Cannot resume without saved progress: {progress_path}")
        _validate_resume_progress(
            progress,
            mode=mode,
            universe_path=universe_path,
            output_name=output_name,
            symbols=symbols,
            fetch_start=fetch_start,
            end=end,
        )
    else:
        progress = _new_progress(
            mode=mode,
            universe_path=universe_path,
            output_name=output_name,
            symbols=symbols,
            fetch_start=fetch_start,
            end=end,
        )
        _write_progress(progress_path, progress)

    adj_close, volume, failed, no_data = _download_yahoo(
        symbols,
        start=fetch_start,
        end=end,
        batch_size=int(args.batch_size),
        sleep_seconds=float(args.sleep_seconds),
        existing_adj_close=existing_adj_close,
        existing_volume=existing_volume,
        adj_close_path=adj_close_path,
        volume_path=volume_path,
        progress_path=progress_path,
        progress=progress,
    )

    _atomic_write_parquet(adj_close, adj_close_path)
    _atomic_write_parquet(volume, volume_path)
    progress["phase"] = "completed"
    _write_progress(progress_path, progress)
    _write_report(
        out_dir,
        args=args,
        universe_path=universe_path,
        output_name=output_name,
        symbols=symbols,
        adj_close=adj_close,
        volume=volume,
        failed=failed,
        no_data=no_data,
        fetch_start=fetch_start,
        end=end,
    )

    print(f"Updated US ETF raw price caches in {out_dir}")
    print(f"Symbols requested: {len(symbols)}")
    print(f"adj_close rows: {adj_close.shape[0]} symbols: {adj_close.shape[1]}")
    print(f"volume rows: {volume.shape[0]} symbols: {volume.shape[1]}")
    print(f"failed: {len(failed)}")
    print(f"no_data: {len(no_data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
