#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


HIGHER_BETTER = (
    "cagr",
    "total_return",
    "sharpe",
    "sortino",
    "calmar",
    "martin",
    "cvar_sharpe",
    "recent_1y",
    "recent_2y",
    "information_ratio",
    "annualized_information_ratio",
)
LOWER_BETTER = (
    "mdd",
    "ulcer",
    "recovery_bars",
    "trades",
    "fees_paid",
)
DISPLAY_METRICS = (
    "cagr",
    "mdd",
    "sharpe",
    "calmar",
    "martin",
    "recovery_bars",
    "recent_2y",
    "trades",
)
PREFERENCE_FEATURES = (
    "cagr_rel",
    "calmar_rel",
    "martin_rel",
    "sharpe_rel",
    "sortino_rel",
    "recent_2y_rel",
    "mdd_improvement",
    "recovery_improvement",
    "ulcer_improvement",
    "turnover_improvement",
    "complexity_improvement",
    "cagr_pct_score",
    "calmar_pct_score",
    "martin_pct_score",
    "sharpe_pct_score",
    "mdd_pct_score",
    "recovery_pct_score",
    "turnover_pct_score",
)


@dataclass(frozen=True)
class Pair:
    left: str
    right: str
    reason: str
    priority: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Interactive preference survey for strategy summaries. Reads one or more "
            "summary_results.csv files and learns provisional preference weights and "
            "hard-cut candidates from pairwise choices."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask = subparsers.add_parser("ask", help="Ask pairwise preference questions")
    _add_common_args(ask)
    ask.add_argument("--questions", type=int, default=10, help="Maximum number of questions to ask")
    ask.add_argument("--seed", type=int, default=7, help="Question sampling seed")
    ask.add_argument("--dry-run", action="store_true", help="Print proposed questions without asking")
    ask.add_argument("--no-reason", action="store_true", help="Do not prompt for optional free-form reason")

    summarize = subparsers.add_parser("summarize", help="Summarize saved responses and rank strategies")
    _add_common_args(summarize)

    export = subparsers.add_parser("export", help="Export strategy metric table without asking")
    _add_common_args(export)
    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--summary-csv",
        action="append",
        help="Path to summary_results.csv. Repeat for multiple grids.",
    )
    parser.add_argument(
        "--summary-glob",
        action="append",
        default=[],
        help="Glob for summary_results.csv files. Repeatable. Example: data/grid/*/summary_results.csv",
    )
    parser.add_argument("--survey-name", required=True, help="Output namespace under data/research/preference_model")
    parser.add_argument(
        "--baseline-contains",
        default="",
        help="Substring used to identify the baseline run_name. Required unless --baseline-run-name is set.",
    )
    parser.add_argument("--baseline-run-name", default="", help="Exact baseline run_name")
    parser.add_argument(
        "--baseline-source-contains",
        default="",
        help="Optional substring used to narrow the baseline source_csv when --baseline-contains matches multiple rows.",
    )
    parser.add_argument("--out-root", default="data/research/preference_model", help="Output root directory")
    parser.add_argument(
        "--include-scope",
        default="",
        help="Comma-separated scopes to include after auto-tagging. Example: us_etf,kr_etf,coin",
    )
    parser.add_argument(
        "--exclude-scope",
        default="",
        help="Comma-separated scopes to exclude after auto-tagging.",
    )
    parser.add_argument(
        "--include-pattern",
        default="",
        help="Optional regex applied to run_name or source_csv.",
    )
    parser.add_argument(
        "--exclude-pattern",
        default="",
        help="Optional regex applied to run_name or source_csv.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=None,
        help="Optional cap after loading, useful for smoke tests.",
    )
    parser.add_argument(
        "--prefilter-loose",
        action="store_true",
        help="Apply loose baseline-relative filters before max-candidates sampling.",
    )
    parser.add_argument("--min-cagr-rel", type=float, default=None, help="Keep candidates with CAGR/base CAGR >= this value")
    parser.add_argument("--max-mdd-rel", type=float, default=None, help="Keep candidates with MDD/base MDD <= this value")
    parser.add_argument("--min-calmar-rel", type=float, default=None, help="Keep candidates with Calmar/base Calmar >= this value")
    parser.add_argument("--min-sharpe-rel", type=float, default=None, help="Keep candidates with Sharpe/base Sharpe >= this value")
    parser.add_argument(
        "--max-recovery-rel",
        type=float,
        default=None,
        help="Keep candidates with recovery bars/base recovery bars <= this value",
    )
    parser.add_argument("--max-trades-rel", type=float, default=None, help="Keep candidates with trades/base trades <= this value")


def _safe_float(value: Any) -> float:
    if value is None:
        return float("nan")
    try:
        if pd.isna(value):
            return float("nan")
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _first_existing(row: pd.Series, names: Sequence[str]) -> float:
    for name in names:
        if name in row.index:
            value = _safe_float(row[name])
            if not math.isnan(value):
                return value
    return float("nan")


def _finite(value: float, default: float = 0.0) -> float:
    if value is None or not math.isfinite(float(value)):
        return default
    return float(value)


def _ratio(value: float, base: float, *, higher_is_better: bool, default: float = 1.0) -> float:
    value = _safe_float(value)
    base = _safe_float(base)
    if not math.isfinite(value) or not math.isfinite(base) or value == 0 or base == 0:
        return default
    if higher_is_better:
        return value / base
    return base / value


def _clean_run_label(run_name: str) -> str:
    label = str(run_name)
    for token in (
        "us_etf_1x_2x_sector17_m12skip1_",
        "vol10d_pct252d_lep975_changeonly_",
        "start20130225_end20260529_",
        "fee5bp_v2",
    ):
        label = label.replace(token, "")
    label = label.replace("__", "_").strip("_")
    return label or str(run_name)


def _complexity_score(run_name: str, top_n: float) -> float:
    score = 1.0
    if "rankfixed" in str(run_name) and "rankfixed100" not in str(run_name):
        score += 0.2
    if math.isfinite(top_n):
        score += max(0.0, float(top_n) - 2.0) * 0.15
    return score


def _parse_csv_list(value: str) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _resolve_summary_paths(csv_paths: Sequence[str] | None, glob_patterns: Sequence[str] | None) -> list[str]:
    resolved: list[str] = []
    for raw_path in csv_paths or []:
        if raw_path:
            resolved.append(str(Path(raw_path)))
    for pattern in glob_patterns or []:
        matches = sorted(glob.glob(pattern, recursive=True))
        resolved.extend(matches)
    unique = []
    seen = set()
    for item in resolved:
        path = str(Path(item))
        if path not in seen:
            seen.add(path)
            unique.append(path)
    if not unique:
        raise ValueError("at least one --summary-csv or --summary-glob is required")
    return unique


def _infer_scope(run_name: str, source_csv: str) -> str:
    text = f"{run_name} {source_csv}".lower()
    coin_prefixes = (
        "btc_",
        "eth_",
        "sol_",
        "xrp_",
        "ada_",
        "trx_",
        "doge_",
        "bnb_",
        "link_",
        "avax_",
    )
    if "upbit" in text or "krw-btc" in text or "krw-eth" in text or "core4" in text:
        return "coin"
    if any(str(run_name).lower().startswith(prefix) for prefix in coin_prefixes):
        return "coin"
    if "kr_etf" in text:
        return "kr_etf"
    if "kr_stock" in text or "kospi" in text:
        return "kr_stock"
    if "us_etf" in text or "stock_us_etf" in text or "leveraged2x" in text or "leveraged3x" in text:
        return "us_etf"
    if "stock_us" in text or "us_stock" in text:
        return "us_stock"
    return "unknown"


def _load_summary(paths: Sequence[str]) -> pd.DataFrame:
    frames = []
    for index, raw_path in enumerate(paths):
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_csv(path)
        if "run_name" not in frame.columns:
            raise ValueError(f"summary csv must contain run_name: {path}")
        frame = frame.drop(columns=["source_csv", "source_index", "asset_scope"], errors="ignore")
        run_names = frame["run_name"].astype(str)
        metadata = pd.DataFrame(
            {
                "source_csv": str(path),
                "source_index": index,
                "asset_scope": [_infer_scope(str(run_name), str(path)) for run_name in run_names],
            },
            index=frame.index,
        )
        frame = pd.concat([frame.reset_index(drop=True), metadata.reset_index(drop=True)], axis=1)
        frames.append(frame)
    if not frames:
        raise ValueError("at least one summary csv is required")
    data = pd.concat(frames, ignore_index=True).copy()
    run_names = data["run_name"].astype(str)
    duplicated = run_names.duplicated(keep=False)
    strategy_id = run_names.where(~duplicated, data["source_index"].astype(str) + "::" + run_names)
    data = data.assign(run_name=run_names, strategy_id=strategy_id).copy()
    return data.reset_index(drop=True)


def _filter_data(data: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    filtered = data.copy()
    include_scope = set(_parse_csv_list(args.include_scope))
    exclude_scope = set(_parse_csv_list(args.exclude_scope))
    if include_scope:
        filtered = filtered[filtered["asset_scope"].isin(include_scope)]
    if exclude_scope:
        filtered = filtered[~filtered["asset_scope"].isin(exclude_scope)]
    text = filtered["run_name"].astype(str) + " " + filtered["source_csv"].astype(str)
    if args.include_pattern:
        filtered = filtered[text.str.contains(args.include_pattern, regex=True, case=False, na=False)]
    text = filtered["run_name"].astype(str) + " " + filtered["source_csv"].astype(str)
    if args.exclude_pattern:
        filtered = filtered[~text.str.contains(args.exclude_pattern, regex=True, case=False, na=False)]
    if filtered.empty:
        raise ValueError("all candidates were filtered out")
    return filtered.reset_index(drop=True)


def _select_baseline(data: pd.DataFrame, *, exact: str, contains: str, source_contains: str = "") -> pd.Series:
    if exact:
        matches = data[data["run_name"] == exact]
    elif contains:
        matches = data[data["run_name"].str.contains(contains, na=False, regex=False)]
    else:
        raise ValueError("--baseline-contains or --baseline-run-name is required")
    if source_contains:
        matches = matches[matches["source_csv"].astype(str).str.contains(source_contains, na=False, regex=False)]
    if len(matches) != 1:
        labels = matches["run_name"].head(20).tolist()
        raise ValueError(f"baseline selector matched {len(matches)} rows: {labels}")
    return matches.iloc[0]


def _extract_metrics(data: pd.DataFrame, baseline: pd.Series, args: argparse.Namespace) -> pd.DataFrame:
    baseline_metrics = _metrics_from_row(baseline)
    rows = []
    for _, row in data.iterrows():
        metrics = _metrics_from_row(row)
        run_name = str(row["run_name"])
        top_n = _safe_float(row.get("top_n", float("nan")))
        strategy_id = str(row.get("strategy_id", run_name))
        item: dict[str, Any] = {
            "strategy_id": strategy_id,
            "run_name": run_name,
            "label": _clean_run_label(run_name),
            "source_csv": row.get("source_csv", ""),
            "asset_scope": row.get("asset_scope", "unknown"),
            "top_n": top_n,
            "complexity": _complexity_score(run_name, top_n),
            "is_baseline": run_name == str(baseline["run_name"]),
        }
        item.update(metrics)
        for metric in HIGHER_BETTER:
            item[f"{metric}_rel"] = _ratio(metrics.get(metric, float("nan")), baseline_metrics.get(metric, float("nan")), higher_is_better=True)
        for metric in LOWER_BETTER:
            item[f"{metric}_improvement"] = _ratio(metrics.get(metric, float("nan")), baseline_metrics.get(metric, float("nan")), higher_is_better=False)
            item[f"{metric}_rel_bad"] = _ratio(metrics.get(metric, float("nan")), baseline_metrics.get(metric, float("nan")), higher_is_better=True)
        item["turnover_improvement"] = item.get("trades_improvement", 1.0)
        item["complexity_improvement"] = _ratio(item["complexity"], _complexity_score(str(baseline["run_name"]), _safe_float(baseline.get("top_n", float("nan")))), higher_is_better=False)
        item["baseline_distance"] = _distance_from_baseline(item)
        item["default_score"] = _default_score(item)
        rows.append(item)
    metrics_frame = pd.DataFrame(rows)
    metrics_frame = _add_percentile_features(metrics_frame)
    metrics_frame = _drop_duplicate_metric_rows(metrics_frame)
    metrics_frame = _apply_prefilter(metrics_frame, args)
    if args.max_candidates is not None and len(metrics_frame) > args.max_candidates:
        baseline_row = metrics_frame[metrics_frame["is_baseline"]]
        other = metrics_frame[~metrics_frame["is_baseline"]].head(args.max_candidates - len(baseline_row))
        metrics_frame = pd.concat([baseline_row, other], ignore_index=True)
    return metrics_frame


def _threshold_arg(args: argparse.Namespace, name: str, loose_default: float) -> float | None:
    value = getattr(args, name)
    if value is not None:
        return float(value)
    if args.prefilter_loose:
        return loose_default
    return None


def _apply_prefilter(metrics_frame: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if metrics_frame.empty:
        return metrics_frame
    thresholds = {
        "min_cagr_rel": _threshold_arg(args, "min_cagr_rel", 0.70),
        "max_mdd_rel": _threshold_arg(args, "max_mdd_rel", 1.75),
        "min_calmar_rel": _threshold_arg(args, "min_calmar_rel", 0.50),
        "min_sharpe_rel": _threshold_arg(args, "min_sharpe_rel", 0.60),
        "max_recovery_rel": _threshold_arg(args, "max_recovery_rel", 3.00),
        "max_trades_rel": _threshold_arg(args, "max_trades_rel", 5.00),
    }
    if all(value is None for value in thresholds.values()):
        result = metrics_frame.copy()
        result["prefilter_pass"] = True
        return result
    mask = pd.Series(True, index=metrics_frame.index)
    if thresholds["min_cagr_rel"] is not None:
        mask &= pd.to_numeric(metrics_frame["cagr_rel"], errors="coerce") >= thresholds["min_cagr_rel"]
    if thresholds["max_mdd_rel"] is not None:
        mask &= pd.to_numeric(metrics_frame["mdd_rel_bad"], errors="coerce") <= thresholds["max_mdd_rel"]
    if thresholds["min_calmar_rel"] is not None:
        mask &= pd.to_numeric(metrics_frame["calmar_rel"], errors="coerce") >= thresholds["min_calmar_rel"]
    if thresholds["min_sharpe_rel"] is not None:
        mask &= pd.to_numeric(metrics_frame["sharpe_rel"], errors="coerce") >= thresholds["min_sharpe_rel"]
    if thresholds["max_recovery_rel"] is not None:
        mask &= pd.to_numeric(metrics_frame["recovery_bars_rel_bad"], errors="coerce") <= thresholds["max_recovery_rel"]
    if thresholds["max_trades_rel"] is not None:
        mask &= pd.to_numeric(metrics_frame["trades_rel_bad"], errors="coerce") <= thresholds["max_trades_rel"]
    mask |= metrics_frame["is_baseline"].astype(bool)
    result = metrics_frame.copy()
    result["prefilter_pass"] = mask
    return result[mask].reset_index(drop=True)


def _drop_duplicate_metric_rows(metrics_frame: pd.DataFrame) -> pd.DataFrame:
    if metrics_frame.empty:
        return metrics_frame
    subset = [
        column
        for column in (
            "run_name",
            "cagr",
            "mdd",
            "sharpe",
            "calmar",
            "martin",
            "recovery_bars",
            "recent_2y",
            "trades",
        )
        if column in metrics_frame.columns
    ]
    if not subset:
        return metrics_frame
    ordered = metrics_frame.sort_values("is_baseline", ascending=False)
    deduped = ordered.drop_duplicates(subset=subset, keep="first")
    return deduped.sort_index().reset_index(drop=True)


def _add_percentile_features(metrics_frame: pd.DataFrame) -> pd.DataFrame:
    frame = metrics_frame.copy()
    for metric in HIGHER_BETTER:
        if metric in frame.columns:
            values = pd.to_numeric(frame[metric], errors="coerce")
            frame[f"{metric}_pct_score"] = values.rank(pct=True, na_option="keep")
    for metric in LOWER_BETTER:
        if metric in frame.columns:
            values = pd.to_numeric(frame[metric], errors="coerce")
            frame[f"{metric}_pct_score"] = 1.0 - values.rank(pct=True, na_option="keep")
    frame["turnover_pct_score"] = frame.get("trades_pct_score", 0.5)
    for feature in PREFERENCE_FEATURES:
        if feature not in frame.columns:
            frame[feature] = 1.0
    return frame


def _metrics_from_row(row: pd.Series) -> dict[str, float]:
    mdd = abs(_first_existing(row, ["Max Drawdown [%]", "Max Drawdown"]))
    benchmark_mdd = abs(_first_existing(row, ["Benchmark Max Drawdown [%]"]))
    return {
        "total_return": _first_existing(row, ["Total Return [%]"]),
        "cagr": _first_existing(row, ["CAGR [%]"]),
        "mdd": mdd,
        "sharpe": _first_existing(row, ["Sharpe Ratio"]),
        "sortino": _first_existing(row, ["Sortino Ratio"]),
        "calmar": _first_existing(row, ["Calmar Ratio"]),
        "martin": _first_existing(row, ["Martin Ratio"]),
        "ulcer": abs(_first_existing(row, ["Ulcer Index [%]", "Ulcer Index"])),
        "cvar_sharpe": _first_existing(row, ["CVaR Sharpe Ratio"]),
        "recovery_bars": _first_existing(row, ["Longest Peak-to-Recovery Bars"]),
        "recent_1y": _first_existing(row, ["Recent 1Y Return [%]"]),
        "recent_2y": _first_existing(row, ["Recent 2Y Return [%]"]),
        "information_ratio": _first_existing(row, ["Information Ratio"]),
        "annualized_information_ratio": _first_existing(row, ["Annualized Information Ratio"]),
        "trades": _first_existing(row, ["Total Trades"]),
        "fees_paid": _first_existing(row, ["Total Fees Paid"]),
        "benchmark_cagr": _first_existing(row, ["Benchmark CAGR [%]"]),
        "benchmark_mdd": benchmark_mdd,
    }


def _distance_from_baseline(row: dict[str, Any]) -> float:
    parts = []
    for name in ("cagr_rel", "calmar_rel", "martin_rel", "mdd_improvement", "recovery_improvement", "sharpe_rel"):
        value = _finite(row.get(name), 1.0)
        parts.append(abs(value - 1.0))
    return float(sum(parts))


def _clip(value: Any, low: float = 0.0, high: float = 2.0) -> float:
    return max(low, min(high, _finite(value, 1.0)))


def _default_score(row: dict[str, Any] | pd.Series) -> float:
    return (
        0.25 * _clip(row.get("cagr_rel")) +
        0.18 * _clip(row.get("calmar_rel")) +
        0.14 * _clip(row.get("martin_rel")) +
        0.10 * _clip(row.get("sharpe_rel")) +
        0.10 * _clip(row.get("recent_2y_rel")) +
        0.10 * _clip(row.get("mdd_improvement")) +
        0.10 * _clip(row.get("recovery_improvement")) +
        0.03 * _clip(row.get("complexity_improvement"))
    )


def _out_dir(args: argparse.Namespace) -> Path:
    return Path(args.out_root) / args.survey_name


def _responses_path(out_dir: Path) -> Path:
    return out_dir / "responses.csv"


def _asked_pairs(responses: pd.DataFrame) -> set[tuple[str, str]]:
    if responses.empty:
        return set()
    pairs = set()
    for _, row in responses.iterrows():
        a = str(row.get("strategy_a", ""))
        b = str(row.get("strategy_b", ""))
        if a and b:
            pairs.add(tuple(sorted((a, b))))
    return pairs


def _load_responses(out_dir: Path) -> pd.DataFrame:
    path = _responses_path(out_dir)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _row_by_id(metrics: pd.DataFrame, strategy_id: str) -> pd.Series:
    match = metrics[metrics["strategy_id"] == strategy_id]
    if len(match) != 1:
        raise KeyError(strategy_id)
    return match.iloc[0]


def _add_pair(
    pairs: dict[tuple[str, str], Pair],
    left: str,
    right: str,
    *,
    reason: str,
    priority: float,
    asked: set[tuple[str, str]],
) -> None:
    if left == right:
        return
    key = tuple(sorted((left, right)))
    if key in asked:
        return
    old = pairs.get(key)
    pair = Pair(left=left, right=right, reason=reason, priority=priority)
    if old is None or pair.priority > old.priority:
        pairs[key] = pair


def _generate_pairs(metrics: pd.DataFrame, *, questions: int, seed: int, responses: pd.DataFrame) -> list[Pair]:
    rng = random.Random(seed)
    asked = _asked_pairs(responses)
    pairs: dict[tuple[str, str], Pair] = {}
    baseline = metrics[metrics["is_baseline"]].iloc[0]
    candidates = metrics[~metrics["is_baseline"]].copy()
    if candidates.empty:
        return []

    candidates["score_gap"] = (candidates["default_score"] - float(baseline["default_score"])).abs()
    for _, row in candidates.sort_values(["score_gap", "baseline_distance"], ascending=False).head(max(questions, 5)).iterrows():
        if str(row["label"]) == str(baseline["label"]):
            continue
        _add_pair(
            pairs,
            str(baseline["strategy_id"]),
            str(row["strategy_id"]),
            reason="baseline_vs_candidate",
            priority=float(row["score_gap"] + row["baseline_distance"]),
            asked=asked,
        )

    all_rows = metrics.to_dict("records")
    for i, left in enumerate(all_rows):
        for right in all_rows[i + 1 :]:
            if str(left.get("label", "")) == str(right.get("label", "")):
                continue
            left_score = _finite(left.get("default_score"), 1.0)
            right_score = _finite(right.get("default_score"), 1.0)
            score_gap = abs(left_score - right_score)
            metric_distance = abs(_finite(left.get("cagr_rel"), 1.0) - _finite(right.get("cagr_rel"), 1.0))
            metric_distance += abs(_finite(left.get("mdd_improvement"), 1.0) - _finite(right.get("mdd_improvement"), 1.0))
            metric_distance += abs(_finite(left.get("recovery_improvement"), 1.0) - _finite(right.get("recovery_improvement"), 1.0))
            priority = metric_distance / (0.05 + score_gap)
            reason = "ambiguous_tradeoff" if score_gap < 0.08 else "metric_tradeoff"
            left_better_return = _finite(left.get("cagr"), 0.0) > _finite(right.get("cagr"), 0.0)
            left_worse_risk = _finite(left.get("mdd"), 0.0) > _finite(right.get("mdd"), 0.0) or _finite(left.get("recovery_bars"), 0.0) > _finite(right.get("recovery_bars"), 0.0)
            right_better_return = _finite(right.get("cagr"), 0.0) > _finite(left.get("cagr"), 0.0)
            right_worse_risk = _finite(right.get("mdd"), 0.0) > _finite(left.get("mdd"), 0.0) or _finite(right.get("recovery_bars"), 0.0) > _finite(left.get("recovery_bars"), 0.0)
            if (left_better_return and left_worse_risk) or (right_better_return and right_worse_risk):
                priority += 2.0
                reason = "return_vs_risk_tradeoff"
            _add_pair(
                pairs,
                str(left["strategy_id"]),
                str(right["strategy_id"]),
                reason=reason,
                priority=priority + rng.random() * 0.001,
                asked=asked,
            )

    id_to_label = dict(zip(metrics["strategy_id"].astype(str), metrics["label"].astype(str)))
    selected: list[Pair] = []
    seen_label_pairs: set[tuple[str, str]] = set()
    for pair in sorted(pairs.values(), key=lambda item: item.priority, reverse=True):
        label_key = tuple(sorted((id_to_label.get(pair.left, pair.left), id_to_label.get(pair.right, pair.right))))
        if label_key in seen_label_pairs:
            continue
        seen_label_pairs.add(label_key)
        selected.append(pair)
        if len(selected) >= questions:
            break
    return selected


def _format_metric(value: Any, metric: str) -> str:
    value = _safe_float(value)
    if not math.isfinite(value):
        return "NA"
    if metric in {"recovery_bars", "trades"}:
        return f"{value:.0f}"
    return f"{value:.2f}"


def _metric_line(row: pd.Series) -> str:
    labels = {
        "cagr": "CAGR",
        "mdd": "MDD",
        "sharpe": "Sharpe",
        "calmar": "Calmar",
        "martin": "Martin",
        "recovery_bars": "Recovery",
        "recent_2y": "Recent2Y",
        "trades": "Trades",
    }
    chunks = [f"Scope {row.get('asset_scope', 'unknown')}"]
    for metric in DISPLAY_METRICS:
        if metric in row.index:
            chunks.append(f"{labels[metric]} {_format_metric(row[metric], metric)}")
    return " | ".join(chunks)


def _diff_line(left: pd.Series, right: pd.Series) -> str:
    parts = []
    for metric, label in (
        ("cagr", "CAGR"),
        ("mdd", "MDD"),
        ("calmar", "Calmar"),
        ("martin", "Martin"),
        ("recovery_bars", "Recovery"),
        ("recent_2y", "Recent2Y"),
    ):
        if metric not in left.index or metric not in right.index:
            continue
        a = _safe_float(left[metric])
        b = _safe_float(right[metric])
        if not math.isfinite(a) or not math.isfinite(b):
            continue
        diff = b - a
        if metric in LOWER_BETTER:
            direction = "B worse" if diff > 0 else "B better"
        else:
            direction = "B better" if diff > 0 else "B worse"
        parts.append(f"{label} {diff:+.2f} ({direction})")
    return " | ".join(parts)


def _prompt_choice(pair: Pair, left: pd.Series, right: pd.Series, index: int, total: int, *, no_reason: bool) -> dict[str, Any] | None:
    print("")
    print("=" * 80)
    print(f"Question {index}/{total} [{pair.reason}]")
    print("")
    print(f"A: {left['label']}")
    print(_metric_line(left))
    print("")
    print(f"B: {right['label']}")
    print(_metric_line(right))
    print("")
    print("B - A:")
    print(_diff_line(left, right))
    if str(left.get("asset_scope", "")) != str(right.get("asset_scope", "")):
        print("")
        print(f"Scope warning: A={left.get('asset_scope', 'unknown')} vs B={right.get('asset_scope', 'unknown')}")
    print("")
    print("1 A better | 2 B better | 3 similar | 4 reject A | 5 reject B | 6 reject both | s skip | q quit")
    while True:
        choice = input("choice> ").strip().lower()
        if choice in {"1", "2", "3", "4", "5", "6", "s", "q"}:
            break
        print("invalid choice")
    if choice == "q":
        return None
    if choice == "s":
        reason = ""
    elif no_reason:
        reason = ""
    else:
        reason = input("reason optional> ").strip()
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "question_id": str(uuid.uuid4()),
        "reason_type": pair.reason,
        "strategy_a": str(left["strategy_id"]),
        "strategy_b": str(right["strategy_id"]),
        "label_a": str(left["label"]),
        "label_b": str(right["label"]),
        "scope_a": str(left.get("asset_scope", "unknown")),
        "scope_b": str(right.get("asset_scope", "unknown")),
        "choice": choice,
        "reason_optional": reason,
        "metrics_a_json": json.dumps(_response_metrics(left), ensure_ascii=False, sort_keys=True),
        "metrics_b_json": json.dumps(_response_metrics(right), ensure_ascii=False, sort_keys=True),
    }


def _response_metrics(row: pd.Series) -> dict[str, Any]:
    keys = list(DISPLAY_METRICS) + list(PREFERENCE_FEATURES) + ["default_score", "complexity"]
    return {key: _safe_float(row.get(key, float("nan"))) for key in keys if key in row.index}


def _append_responses(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fieldnames = list(records[0].keys())
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(records)


def _choice_to_pairwise(row: pd.Series, metrics: pd.DataFrame) -> tuple[np.ndarray, float] | None:
    choice = str(row.get("choice", "")).strip().lower()
    if choice not in {"1", "2"}:
        return None
    try:
        left = _row_by_id(metrics, str(row["strategy_a"]))
        right = _row_by_id(metrics, str(row["strategy_b"]))
    except KeyError:
        return None
    diff = _preference_vector(left) - _preference_vector(right)
    if choice == "1":
        return diff, 1.0
    return -diff, 1.0


def _preference_vector(row: pd.Series) -> np.ndarray:
    return np.array([_finite(row.get(name), 1.0) for name in PREFERENCE_FEATURES], dtype=float)


def _estimate_weights(metrics: pd.DataFrame, responses: pd.DataFrame) -> pd.DataFrame:
    if responses.empty:
        weights = np.array([0.22, 0.18, 0.14, 0.10, 0.08, 0.08, 0.10, 0.06, 0.02, 0.01, 0.01], dtype=float)
    else:
        diffs = []
        for _, response in responses.iterrows():
            converted = _choice_to_pairwise(response, metrics)
            if converted is not None:
                diffs.append(converted[0])
        if diffs:
            raw = np.maximum(np.sum(np.vstack(diffs), axis=0), 0.0)
            if raw.sum() <= 0:
                weights = np.ones(len(PREFERENCE_FEATURES), dtype=float)
            else:
                weights = raw
        else:
            weights = np.ones(len(PREFERENCE_FEATURES), dtype=float)
    weights = weights / weights.sum()
    return pd.DataFrame({"feature": list(PREFERENCE_FEATURES), "weight": weights})


def _apply_preference_score(metrics: pd.DataFrame, weights: pd.DataFrame) -> pd.DataFrame:
    weight_map = dict(zip(weights["feature"], weights["weight"]))
    ranked = metrics.copy()
    score = np.zeros(len(ranked), dtype=float)
    for feature in PREFERENCE_FEATURES:
        if feature in ranked.columns:
            value = pd.to_numeric(ranked[feature], errors="coerce")
        else:
            value = pd.Series(1.0, index=ranked.index)
        value = value.replace([np.inf, -np.inf], np.nan).fillna(1.0)
        score += float(weight_map.get(feature, 0.0)) * value.clip(lower=0.0, upper=2.0).to_numpy()
    ranked["preference_score"] = score
    return ranked.sort_values("preference_score", ascending=False)


def _estimate_hard_cuts(metrics: pd.DataFrame, responses: pd.DataFrame) -> pd.DataFrame:
    if responses.empty:
        return pd.DataFrame(columns=["metric", "direction", "accepted_boundary", "rejected_boundary", "suggested_cut", "status"])
    rejected_ids: set[str] = set()
    accepted_ids: set[str] = set()
    for _, row in responses.iterrows():
        choice = str(row.get("choice", "")).strip().lower()
        a = str(row.get("strategy_a", ""))
        b = str(row.get("strategy_b", ""))
        if choice == "1":
            accepted_ids.add(a)
        elif choice == "2":
            accepted_ids.add(b)
        elif choice == "3":
            accepted_ids.update([a, b])
        elif choice == "4":
            rejected_ids.add(a)
            accepted_ids.add(b)
        elif choice == "5":
            rejected_ids.add(b)
            accepted_ids.add(a)
        elif choice == "6":
            rejected_ids.update([a, b])
    accepted = metrics[metrics["strategy_id"].isin(accepted_ids - rejected_ids)]
    rejected = metrics[metrics["strategy_id"].isin(rejected_ids)]
    rules = []
    for metric, direction in (
        ("mdd", "max"),
        ("recovery_bars", "max"),
        ("trades", "max"),
        ("ulcer", "max"),
        ("cagr", "min"),
        ("calmar", "min"),
        ("martin", "min"),
        ("recent_2y", "min"),
    ):
        if accepted.empty or rejected.empty or metric not in metrics.columns:
            continue
        acc = pd.to_numeric(accepted[metric], errors="coerce").dropna()
        rej = pd.to_numeric(rejected[metric], errors="coerce").dropna()
        if acc.empty or rej.empty:
            continue
        if direction == "max":
            acc_boundary = float(acc.max())
            rej_boundary = float(rej.min())
            suggested = (acc_boundary + rej_boundary) / 2.0
            status = "separated" if rej_boundary > acc_boundary else "overlap"
        else:
            acc_boundary = float(acc.min())
            rej_boundary = float(rej.max())
            suggested = (acc_boundary + rej_boundary) / 2.0
            status = "separated" if rej_boundary < acc_boundary else "overlap"
        rules.append(
            {
                "metric": metric,
                "direction": direction,
                "accepted_boundary": acc_boundary,
                "rejected_boundary": rej_boundary,
                "suggested_cut": suggested,
                "status": status,
                "accepted_count": int(len(acc)),
                "rejected_count": int(len(rej)),
            }
        )
    return pd.DataFrame(rules)


def _write_summary(out_dir: Path, ranked: pd.DataFrame, weights: pd.DataFrame, hard_cuts: pd.DataFrame, responses: pd.DataFrame) -> None:
    lines = [
        "# Strategy Preference Survey Summary",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Responses: {len(responses)}",
        "",
        "## Preference Weights",
        "",
        _frame_to_markdown(weights),
        "",
        "## Top Ranked Strategies",
        "",
    ]
    cols = [
        "label",
        "preference_score",
        "default_score",
        "cagr",
        "mdd",
        "sharpe",
        "calmar",
        "martin",
        "recovery_bars",
        "recent_2y",
        "trades",
    ]
    cols = [col for col in cols if col in ranked.columns]
    lines.append(_frame_to_markdown(ranked[cols].head(20)))
    lines.extend(["", "## Hard Cut Candidates", ""])
    if hard_cuts.empty:
        lines.append("No hard-cut candidates yet. Add reject responses first.")
    else:
        lines.append(_frame_to_markdown(hard_cuts))
    lines.append("")
    (out_dir / "survey_summary.md").write_text("\n".join(lines), encoding="utf-8")


def _frame_to_markdown(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_empty_"
    display = frame.copy()
    for column in display.columns:
        if pd.api.types.is_float_dtype(display[column]):
            display[column] = display[column].map(lambda value: "" if pd.isna(value) else f"{float(value):.4f}")
    headers = [str(column) for column in display.columns]
    rows = [[str(value) for value in row] for row in display.astype(object).to_numpy()]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    header_line = "| " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)) + " |"
    sep_line = "| " + " | ".join("-" * widths[index] for index in range(len(headers))) + " |"
    body = [
        "| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + " |"
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *body])


def _prepare(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.Series, Path, pd.DataFrame]:
    summary_paths = _resolve_summary_paths(args.summary_csv, args.summary_glob)
    data = _load_summary(summary_paths)
    data = _filter_data(data, args)
    baseline = _select_baseline(
        data,
        exact=args.baseline_run_name,
        contains=args.baseline_contains,
        source_contains=args.baseline_source_contains,
    )
    metrics = _extract_metrics(data, baseline, args)
    out_dir = _out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(out_dir / "strategy_metrics.csv", index=False)
    responses = _load_responses(out_dir)
    return metrics, baseline, out_dir, responses


def command_ask(args: argparse.Namespace) -> int:
    metrics, baseline, out_dir, responses = _prepare(args)
    pairs = _generate_pairs(metrics, questions=args.questions, seed=args.seed, responses=responses)
    if not pairs:
        print("No new question pairs available.")
        return command_summarize(args)
    print(f"Survey: {args.survey_name}")
    print(f"Baseline: {_clean_run_label(str(baseline['run_name']))}")
    print(f"Candidates: {len(metrics)}")
    print(f"Existing responses: {len(responses)}")
    print(f"Questions proposed: {len(pairs)}")
    records = []
    for index, pair in enumerate(pairs, start=1):
        left = _row_by_id(metrics, pair.left)
        right = _row_by_id(metrics, pair.right)
        if args.dry_run:
            print("")
            print(f"[{index}/{len(pairs)}] {pair.reason}: {left['label']} vs {right['label']}")
            print(f"  A { _metric_line(left) }")
            print(f"  B { _metric_line(right) }")
            continue
        record = _prompt_choice(pair, left, right, index, len(pairs), no_reason=args.no_reason)
        if record is None:
            break
        if record["choice"] != "s":
            records.append(record)
    if not args.dry_run:
        _append_responses(_responses_path(out_dir), records)
        print(f"Saved responses: {len(records)} -> {_responses_path(out_dir)}")
        return command_summarize(args)
    return 0


def command_summarize(args: argparse.Namespace) -> int:
    metrics, _, out_dir, responses = _prepare(args)
    weights = _estimate_weights(metrics, responses)
    ranked = _apply_preference_score(metrics, weights)
    hard_cuts = _estimate_hard_cuts(metrics, responses)
    weights.to_csv(out_dir / "preference_weights.csv", index=False)
    ranked.to_csv(out_dir / "ranked_strategies.csv", index=False)
    hard_cuts.to_csv(out_dir / "estimated_hard_cuts.csv", index=False)
    _write_summary(out_dir, ranked, weights, hard_cuts, responses)
    print(f"responses: {len(responses)}")
    print(f"wrote: {out_dir / 'preference_weights.csv'}")
    print(f"wrote: {out_dir / 'ranked_strategies.csv'}")
    print(f"wrote: {out_dir / 'estimated_hard_cuts.csv'}")
    print(f"wrote: {out_dir / 'survey_summary.md'}")
    cols = ["label", "preference_score", "cagr", "mdd", "sharpe", "calmar", "martin", "recovery_bars", "recent_2y", "trades"]
    print("")
    print(ranked[[col for col in cols if col in ranked.columns]].head(10).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    return 0


def command_export(args: argparse.Namespace) -> int:
    metrics, baseline, out_dir, _ = _prepare(args)
    print(f"baseline: {_clean_run_label(str(baseline['run_name']))}")
    print(f"wrote: {out_dir / 'strategy_metrics.csv'}")
    print(metrics[["label", "cagr", "mdd", "sharpe", "calmar", "martin", "recovery_bars", "default_score"]].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "ask":
        return command_ask(args)
    if args.command == "summarize":
        return command_summarize(args)
    if args.command == "export":
        return command_export(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
