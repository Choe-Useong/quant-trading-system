#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IN_PATH = ROOT / "data" / "us_etfs" / "raw" / "etf_master.parquet"
DEFAULT_OUT_DIR = ROOT / "data" / "us_etfs" / "universe"


STRUCTURED_PATTERNS = [
    r"\bautocallable\b",
    r"\bbarrier\b",
    r"\bbuffer\b",
    r"\bbuffer\d+\b",
    r"\bbuffered\b",
    r"\bcapped\s+accelerated\b",
    r"\bcollared\b",
    r"\bcovered\s+strategy\b",
    r"\bcovered\s+call\b",
    r"\bdefined\b",
    r"\bdefined\s+outcome\b",
    r"\bequity\s+premium\b",
    r"\byieldmax\b",
    r"\bincome\s+strategy\b",
    r"\bmanaged\s+distribution\b",
    r"\bmonthly\s+pay\b",
    r"\bmonthly\s+option\b",
    r"\boption\b",
    r"\boption\s+income\b",
    r"\boptions\b",
    r"\boptions\s+income\b",
    r"\boutcome\b",
    r"\bpremium\b",
    r"\bpremium\s+income\b",
    r"\bprotection\b",
    r"\btarget\b.*\bdistribution\b",
    r"\btarget\s+distribution\b",
    r"\btarget\s+outcome\b",
    r"\bweekly\s+distribution\b",
    r"\bweekly\s+pay\b",
    r"\bweeklypay\b",
]

INVERSE_PATTERNS = [
    r"\bbear\b",
    r"\binverse\b",
    r"\bultrashort\b",
    r"\bproshares\s+short\b",
    r"\b2\s*x\s+short\b",
    r"\b2x\s+short\b",
    r"\bshort\s+[a-z0-9+.&-]{1,12}\s+daily\b",
    r"\b-\s*[1234]\s*x\b",
    r"\b-[1234]x\b",
]

THREE_PLUS_PATTERNS = [
    r"\b3\s*x\b",
    r"\b3x\b",
    r"\b4\s*x\b",
    r"\b4x\b",
    r"\bultrapro\b",
]

TWO_X_PATTERNS = [
    r"\b2\s*x\b",
    r"\b2x\b",
    r"\bbull\s+2\s*x\b",
    r"\bbull\s+2x\b",
    r"\bproshares\s+ultra\b",
]

OTHER_LEVERAGE_PATTERNS = [
    r"\b1\.5\s*x\b",
    r"\b1\.5x\b",
    r"\bdaily\s+leveraged\b",
    r"\bleveraged\s+long\b",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build plain and 2x leveraged US ETF universe files from the US ETF master cache."
    )
    parser.add_argument("--input", default=str(DEFAULT_IN_PATH))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args()


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _append_if_match(reasons: list[str], reason: str, text: str, patterns: list[str]) -> None:
    if _contains_any(text, patterns):
        reasons.append(reason)


def _is_two_x_long_candidate(text: str) -> bool:
    if _contains_any(text, THREE_PLUS_PATTERNS):
        return False
    if _contains_any(text, INVERSE_PATTERNS):
        return False
    if _contains_any(text, TWO_X_PATTERNS):
        return True
    return False


def _classify_row(row: pd.Series) -> dict[str, str]:
    name = str(row.get("Name", ""))
    text = name.lower()
    reasons: list[str] = []

    _append_if_match(reasons, "structured_options_or_buffer", text, STRUCTURED_PATTERNS)
    _append_if_match(reasons, "inverse_or_short", text, INVERSE_PATTERNS)
    _append_if_match(reasons, "three_plus_leverage", text, THREE_PLUS_PATTERNS)
    _append_if_match(reasons, "etn", text, [r"\betn\b", r"\betns\b"])
    _append_if_match(reasons, "vix", text, [r"\bvix\b"])
    _append_if_match(reasons, "other_leverage", text, OTHER_LEVERAGE_PATTERNS)

    if reasons:
        return {"Universe": "excluded", "Reason": "|".join(dict.fromkeys(reasons))}

    if _is_two_x_long_candidate(text):
        return {"Universe": "leveraged_2x", "Reason": "two_x_long_candidate"}

    return {"Universe": "plain", "Reason": "plain_candidate"}


def _classify(master: pd.DataFrame) -> pd.DataFrame:
    required = {"Symbol", "Name"}
    missing = required.difference(master.columns)
    if missing:
        raise ValueError(f"US ETF master cache missing required columns: {sorted(missing)}")

    out = master.copy()
    details = out.apply(_classify_row, axis=1, result_type="expand")
    out["Universe"] = details["Universe"]
    out["Reason"] = details["Reason"]

    ordered_columns = [
        "Symbol",
        "Name",
        "Universe",
        "Reason",
        "ExchangeName",
        "SourceFile",
        "ETF",
        "TestIssue",
        "FinancialStatus",
    ]
    columns = [column for column in ordered_columns if column in out.columns]
    return out[columns].sort_values(["Universe", "Symbol"]).reset_index(drop=True)


def _write_universe(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _write_report(out_dir: Path, classified: pd.DataFrame, outputs: dict[str, Path]) -> None:
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(classified.shape[0]),
        "universe_counts": classified["Universe"].value_counts(dropna=False).sort_index().astype(int).to_dict(),
        "excluded_reason_counts": (
            classified.loc[classified["Universe"].eq("excluded"), "Reason"]
            .str.get_dummies(sep="|")
            .sum()
            .sort_index()
            .astype(int)
            .to_dict()
        ),
        "outputs": {name: str(path) for name, path in outputs.items()},
    }
    (out_dir / "filter_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    input_path = _resolve(args.input)
    out_dir = _resolve(args.out_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"US ETF master cache not found: {input_path}")

    master = pd.read_parquet(input_path)
    classified = _classify(master)

    plain = classified[classified["Universe"].eq("plain")].copy()
    leveraged_2x = classified[classified["Universe"].eq("leveraged_2x")].copy()
    excluded = classified[classified["Universe"].eq("excluded")].copy()

    outputs = {
        "plain": out_dir / "plain.csv",
        "leveraged_2x": out_dir / "leveraged_2x.csv",
        "excluded": out_dir / "excluded.csv",
        "report": out_dir / "filter_report.json",
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_universe(outputs["plain"], plain)
    _write_universe(outputs["leveraged_2x"], leveraged_2x)
    _write_universe(outputs["excluded"], excluded)
    _write_report(out_dir, classified, outputs)

    print(f"Wrote US ETF universes to {out_dir}")
    print(f"Rows: {classified.shape[0]}")
    print(f"plain: {plain.shape[0]}")
    print(f"leveraged_2x: {leveraged_2x.shape[0]}")
    print(f"excluded: {excluded.shape[0]}")
    print("Wrote: plain.csv, leveraged_2x.csv, excluded.csv, filter_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
