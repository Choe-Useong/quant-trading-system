from __future__ import annotations

import csv
import re
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "research" / "strategy_exhaustive_table.parquet"


def normalize_key(raw: str) -> str:
    key = raw.strip().lower()
    key = key.replace("%", "pct")
    key = key.replace("[", "").replace("]", "")
    key = re.sub(r"[^a-z0-9]+", "_", key)
    key = re.sub(r"_+", "_", key).strip("_")
    return key or "value"


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        out[normalize_key(key)] = value
    return out


def infer_timeframe(dataset_name: str, source_path: str, row: dict[str, str]) -> str:
    for candidate_key in ("timeframe",):
        value = row.get(candidate_key, "")
        if value:
            return value
    text = f"{dataset_name} {source_path}"
    match = re.search(r"(\d+)m", text)
    return f"{match.group(1)}m" if match else ""


def infer_timeframe_minutes(timeframe: str) -> str:
    match = re.fullmatch(r"(\d+)m", timeframe)
    return match.group(1) if match else ""


def infer_asset_hint(dataset_name: str, source_path: str, row: dict[str, str]) -> str:
    text = " ".join(
        part
        for part in (
            row.get("benchmark_market", ""),
            row.get("run_name", ""),
            dataset_name,
            source_path,
        )
        if part
    ).lower()
    assets = [
        "btc_eth",
        "btc",
        "eth",
        "sol",
        "xrp",
        "ada",
        "doge",
        "avax",
        "upbit",
    ]
    for asset in assets:
        if asset in text:
            return asset.upper()
    return ""


def collect_grid_records() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for path in DATA_DIR.glob("grid/**/summary_results.csv"):
        source_path = path.relative_to(ROOT).as_posix()
        dataset_name = path.parent.name
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                norm = normalize_row(row)
                timeframe = infer_timeframe(dataset_name, source_path, norm)
                record = {
                    "record_type": "grid_run",
                    "source_kind": "grid",
                    "sample_type": "full_sample_grid",
                    "dataset_name": dataset_name,
                    "source_path": source_path,
                    "timeframe_norm": timeframe,
                    "timeframe_minutes_norm": infer_timeframe_minutes(timeframe),
                    "asset_hint": infer_asset_hint(dataset_name, source_path, norm),
                }
                record.update(norm)
                records.append(record)
    return records


def read_vertical_summary(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    data: dict[str, str] = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key = normalize_key(row[0])
        data[key] = row[1]
    return data


def collect_vertical_records(glob_pattern: str, record_type: str, sample_type: str, source_kind: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for path in DATA_DIR.glob(glob_pattern):
        source_path = path.relative_to(ROOT).as_posix()
        dataset_name = path.parent.name
        norm = read_vertical_summary(path)
        timeframe = infer_timeframe(dataset_name, source_path, norm)
        record = {
            "record_type": record_type,
            "source_kind": source_kind,
            "sample_type": sample_type,
            "dataset_name": dataset_name,
            "source_path": source_path,
            "run_name": dataset_name,
            "timeframe_norm": timeframe,
            "timeframe_minutes_norm": infer_timeframe_minutes(timeframe),
            "asset_hint": infer_asset_hint(dataset_name, source_path, norm),
        }
        record.update(norm)
        records.append(record)
    return records


def ordered_fieldnames(records: list[dict[str, str]]) -> list[str]:
    fieldnames: list[str] = []
    seen: set[str] = set()
    preferred = [
        "record_type",
        "source_kind",
        "sample_type",
        "dataset_name",
        "source_path",
        "asset_hint",
        "run_name",
        "timeframe_norm",
        "timeframe_minutes_norm",
        "status",
    ]
    for key in preferred:
        if any(key in record for record in records):
            fieldnames.append(key)
            seen.add(key)
    for record in records:
        for key in record.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    return fieldnames


def write_parquet(records: list[dict[str, str]], out_path: Path) -> None:
    fieldnames = ordered_fieldnames(records)
    rows = [{key: record.get(key, "") for key in fieldnames} for record in records]
    frame = pd.DataFrame(rows, columns=fieldnames)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out_path, index=False)


def main() -> int:
    records: list[dict[str, str]] = []
    records.extend(collect_grid_records())
    records.extend(
        collect_vertical_records(
            "backtest/**/summary.csv",
            record_type="backtest_summary",
            sample_type="backtest_summary",
            source_kind="backtest",
        )
    )
    records.extend(
        collect_vertical_records(
            "validation/**/stitched_oos_summary.csv",
            record_type="walkforward_stitched",
            sample_type="walkforward_stitched",
            source_kind="validation",
        )
    )
    write_parquet(records, OUT_PATH)
    print(f"Wrote {len(records)} strategy rows to {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
