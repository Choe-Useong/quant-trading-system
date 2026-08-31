#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from kr_stock_live.active_profile import resolve_profile_reference

DEFAULT_PROFILE_JSON = (
    ROOT_DIR
    / "kr_stock_live"
    / "configs"
    / "kr_kospi_rank12_skip3_top2_breadth200_gt0p5.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update Korean stock live raw/source caches using the existing v2 builders."
    )
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="KR stock live profile JSON path.")
    parser.add_argument("--start", default="", help="Optional rebuild/update start date override.")
    parser.add_argument("--end", default="", help="Optional rebuild/update end date override.")
    parser.add_argument(
        "--force-fundamental-update",
        action="store_true",
        help="Refresh fundamental raw data even when its configured interval has not elapsed.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT_DIR / path


def _run(command: list[str], *, dry_run: bool) -> None:
    print(" ".join(command))
    if dry_run:
        return
    subprocess.run(command, cwd=ROOT_DIR, check=True)


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return [str(item) for item in value]


def _fundamental_raw_update_due(report_path: Path, interval_days: float) -> bool:
    if interval_days < 0:
        raise ValueError("data.fundamental_update.raw_update_interval_days must be non-negative")
    if interval_days == 0 or not report_path.exists():
        return True
    age_seconds = max(0.0, time.time() - report_path.stat().st_mtime)
    return age_seconds >= interval_days * 86400.0


def _load_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _read_json(path)


def _print_cache_summary(raw_dir: Path, source_cache_dir: Path) -> None:
    raw_report = _load_report(raw_dir / "download_report.json")
    source_report = _load_report(source_cache_dir / "source_cache_report.json")
    marcap_end = ((raw_report.get("marcap") or {}).get("end")) if raw_report else None
    close_end = ((raw_report.get("close_fdr") or {}).get("end")) if raw_report else None
    index_end = ((raw_report.get("index_price") or {}).get("end")) if raw_report else None
    source_end = source_report.get("end") if source_report else None
    print(f"raw_marcap_end={marcap_end}")
    print(f"raw_close_end={close_end}")
    print(f"raw_index_end={index_end}")
    print(f"source_cache_end={source_end}")


def main() -> int:
    args = build_parser().parse_args()
    profile_reference_path = _resolve_path(args.profile_json)
    profile_path, _active_profile = resolve_profile_reference(profile_reference_path)
    profile = _read_json(profile_path)
    data = profile.get("data") or {}

    raw_dir = _resolve_path(data["raw_dir"])
    source_cache_dir = _resolve_path(data["source_cache_dir"])
    raw_update_script = str(data.get("raw_update_script") or "scripts/kr_stocks/build_kr_stock_raw_cache.py")
    raw_update_out_arg = str(data.get("raw_update_out_arg") or "--out-dir")
    source_cache_script = str(data.get("source_cache_script") or "scripts/kr_stocks/build_kr_stock_source_cache.py")
    source_raw_arg = str(data.get("source_raw_arg") or "--raw-dir")
    source_out_arg = str(data.get("source_out_arg") or "--out-dir")
    raw_update_args = _string_list(data.get("raw_update_args"), field_name="data.raw_update_args")
    source_cache_args = _string_list(data.get("source_cache_args"), field_name="data.source_cache_args")

    fundamental = data.get("fundamental_update") or {}
    if not isinstance(fundamental, dict):
        raise ValueError("data.fundamental_update must be an object")
    fundamental_enabled = bool(fundamental.get("enabled", False))

    raw_command = [
        sys.executable,
        raw_update_script,
        raw_update_out_arg,
        str(raw_dir),
        *raw_update_args,
    ]
    source_command = [
        sys.executable,
        source_cache_script,
        source_raw_arg,
        str(raw_dir),
        source_out_arg,
        str(source_cache_dir),
        *source_cache_args,
    ]
    if args.start:
        raw_command.extend(["--start", args.start])
        source_command.extend(["--start", args.start])
    if args.end:
        raw_command.extend(["--end", args.end])
        source_command.extend(["--end", args.end])

    _run(raw_command, dry_run=args.dry_run)

    fundamental_cache_command: list[str] | None = None
    if fundamental_enabled:
        raw_script = str(
            fundamental.get("raw_update_script")
            or "scripts/kr_stocks/update_kr_stock_fundamentals.py"
        )
        raw_args = _string_list(
            fundamental.get("raw_update_args"),
            field_name="data.fundamental_update.raw_update_args",
        )
        report_path = raw_dir / str(
            fundamental.get("raw_report_file") or "fundamental_download_report.json"
        )
        interval_days = float(fundamental.get("raw_update_interval_days", 7.0))
        raw_due = args.force_fundamental_update or _fundamental_raw_update_due(
            report_path,
            interval_days,
        )
        if raw_due:
            fundamental_raw_command = [
                sys.executable,
                raw_script,
                "--raw-dir",
                str(raw_dir),
                *raw_args,
            ]
            if args.end:
                fundamental_raw_command.extend(["--end-date", args.end])
            _run(fundamental_raw_command, dry_run=args.dry_run)
        else:
            age_days = max(0.0, time.time() - report_path.stat().st_mtime) / 86400.0
            print(
                "fundamental raw update skipped:"
                f" age_days={age_days:.2f} interval_days={interval_days:g}"
            )

        cache_script = str(
            fundamental.get("cache_build_script")
            or "scripts/kr_stocks/build_kr_stock_fundamental_cache.py"
        )
        cache_args = _string_list(
            fundamental.get("cache_build_args"),
            field_name="data.fundamental_update.cache_build_args",
        )
        fundamental_cache_command = [
            sys.executable,
            cache_script,
            "--raw-dir",
            str(raw_dir),
            "--source-cache-dir",
            str(source_cache_dir),
            "--out-dir",
            str(source_cache_dir),
            *cache_args,
        ]
        if args.start:
            fundamental_cache_command.extend(["--start", args.start])
        if args.end:
            fundamental_cache_command.extend(["--end", args.end])

    _run(source_command, dry_run=args.dry_run)
    if fundamental_cache_command is not None:
        _run(fundamental_cache_command, dry_run=args.dry_run)
    if not args.dry_run:
        _print_cache_summary(raw_dir, source_cache_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
