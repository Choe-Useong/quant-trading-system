#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze max drawdown timing and characteristics.")
    parser.add_argument("--curve-csv", required=True, help="CSV containing equity curves")
    parser.add_argument("--curve-column", required=True, help="Strategy curve column name")
    parser.add_argument("--date-column", default="date_utc", help="Date column name")
    parser.add_argument("--benchmark-column", help="Optional benchmark curve column name")
    return parser


def _load_curve_frame(path: Path, date_column: str) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if date_column not in frame.columns:
        raise SystemExit(f"Missing date column: {date_column}")
    frame[date_column] = pd.to_datetime(frame[date_column])
    frame = frame.sort_values(date_column).set_index(date_column)
    return frame


def _analyze_drawdown(curve: pd.Series) -> dict[str, object]:
    running_max = curve.cummax()
    drawdown = curve / running_max - 1.0
    trough_time = pd.Timestamp(drawdown.idxmin())
    trough_drawdown = float(drawdown.loc[trough_time])

    peak_slice = curve.loc[:trough_time]
    peak_time = pd.Timestamp(peak_slice.idxmax())
    peak_value = float(curve.loc[peak_time])
    trough_value = float(curve.loc[trough_time])

    recovery_candidates = curve.loc[trough_time:]
    recovered = recovery_candidates[recovery_candidates >= peak_value]
    recovery_time = None if recovered.empty else pd.Timestamp(recovered.index[0])

    peak_to_trough_bars = int(curve.loc[peak_time:trough_time].shape[0] - 1)
    trough_to_recovery_bars = None
    if recovery_time is not None:
        trough_to_recovery_bars = int(curve.loc[trough_time:recovery_time].shape[0] - 1)

    return {
        "peak_time": peak_time,
        "peak_value": peak_value,
        "trough_time": trough_time,
        "trough_value": trough_value,
        "max_drawdown_pct": trough_drawdown * 100.0,
        "peak_to_trough_bars": peak_to_trough_bars,
        "recovery_time": recovery_time,
        "trough_to_recovery_bars": trough_to_recovery_bars,
    }


def _drawdown_episodes(curve: pd.Series) -> list[dict[str, object]]:
    running_max = float(curve.iloc[0])
    running_max_time = pd.Timestamp(curve.index[0])
    in_drawdown = False
    trough_value = float(curve.iloc[0])
    trough_time = pd.Timestamp(curve.index[0])
    episodes: list[dict[str, object]] = []

    for timestamp, value in curve.items():
        current_time = pd.Timestamp(timestamp)
        current_value = float(value)

        if current_value >= running_max:
            if in_drawdown:
                peak_to_recovery_bars = int(curve.loc[running_max_time:current_time].shape[0] - 1)
                peak_to_trough_bars = int(curve.loc[running_max_time:trough_time].shape[0] - 1)
                trough_to_recovery_bars = int(curve.loc[trough_time:current_time].shape[0] - 1)
                episodes.append(
                    {
                        "peak_time": running_max_time,
                        "trough_time": trough_time,
                        "recovery_time": current_time,
                        "drawdown_pct": (trough_value / running_max - 1.0) * 100.0,
                        "peak_to_trough_bars": peak_to_trough_bars,
                        "trough_to_recovery_bars": trough_to_recovery_bars,
                        "peak_to_recovery_bars": peak_to_recovery_bars,
                    }
                )
                in_drawdown = False

            running_max = current_value
            running_max_time = current_time
            trough_value = current_value
            trough_time = current_time
            continue

        if not in_drawdown:
            in_drawdown = True
            trough_value = current_value
            trough_time = current_time
            continue

        if current_value < trough_value:
            trough_value = current_value
            trough_time = current_time

    return episodes


def _window_return_pct(curve: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float:
    window = curve.loc[start:end]
    if window.empty:
        return float("nan")
    start_value = float(window.iloc[0])
    end_value = float(window.iloc[-1])
    if start_value == 0:
        return float("nan")
    return (end_value / start_value - 1.0) * 100.0


def main() -> None:
    args = build_parser().parse_args()

    frame = _load_curve_frame(Path(args.curve_csv), args.date_column)
    if args.curve_column not in frame.columns:
        raise SystemExit(f"Missing curve column: {args.curve_column}")

    curve = frame[args.curve_column].dropna()
    if curve.empty:
        raise SystemExit("Strategy curve is empty")

    stats = _analyze_drawdown(curve)
    peak_time = stats["peak_time"]
    trough_time = stats["trough_time"]
    recovery_time = stats["recovery_time"]

    print(f"curve_column: {args.curve_column}")
    print(f"peak_time: {pd.Timestamp(peak_time)}")
    print(f"trough_time: {pd.Timestamp(trough_time)}")
    print(f"max_drawdown_pct: {stats['max_drawdown_pct']:.4f}")
    print(f"peak_to_trough_bars: {stats['peak_to_trough_bars']}")
    print(f"strategy_return_peak_to_trough_pct: {_window_return_pct(curve, peak_time, trough_time):.4f}")

    if recovery_time is None:
        print("recovery_time: not_recovered")
        print("trough_to_recovery_bars: not_recovered")
    else:
        print(f"recovery_time: {pd.Timestamp(recovery_time)}")
        print(f"trough_to_recovery_bars: {stats['trough_to_recovery_bars']}")
        print(
            "peak_to_recovery_bars: "
            f"{stats['peak_to_trough_bars'] + int(stats['trough_to_recovery_bars'])}"
        )
        print(f"strategy_return_trough_to_recovery_pct: {_window_return_pct(curve, trough_time, recovery_time):.4f}")

    if args.benchmark_column:
        if args.benchmark_column not in frame.columns:
            raise SystemExit(f"Missing benchmark column: {args.benchmark_column}")
        benchmark = frame[args.benchmark_column].dropna()
        print(f"benchmark_column: {args.benchmark_column}")
        print(
            "benchmark_return_peak_to_trough_pct: "
            f"{_window_return_pct(benchmark, peak_time, trough_time):.4f}"
        )
        if recovery_time is not None:
            print(
                "benchmark_return_trough_to_recovery_pct: "
                f"{_window_return_pct(benchmark, trough_time, recovery_time):.4f}"
            )

    episodes = _drawdown_episodes(curve)
    episodes = [episode for episode in episodes if episode["peak_to_recovery_bars"] > 0]
    episodes.sort(key=lambda item: int(item["peak_to_recovery_bars"]), reverse=True)
    if episodes:
        top_episode = episodes[0]
        print("longest_recovery_episode_rank: 1")
        print(f"longest_recovery_peak_time: {pd.Timestamp(top_episode['peak_time'])}")
        print(f"longest_recovery_trough_time: {pd.Timestamp(top_episode['trough_time'])}")
        print(f"longest_recovery_recovery_time: {pd.Timestamp(top_episode['recovery_time'])}")
        print(f"longest_recovery_drawdown_pct: {float(top_episode['drawdown_pct']):.4f}")
        print(f"longest_recovery_peak_to_recovery_bars: {int(top_episode['peak_to_recovery_bars'])}")
    if len(episodes) >= 2:
        second_episode = episodes[1]
        print("second_longest_recovery_episode_rank: 2")
        print(f"second_longest_recovery_peak_time: {pd.Timestamp(second_episode['peak_time'])}")
        print(f"second_longest_recovery_trough_time: {pd.Timestamp(second_episode['trough_time'])}")
        print(f"second_longest_recovery_recovery_time: {pd.Timestamp(second_episode['recovery_time'])}")
        print(f"second_longest_recovery_drawdown_pct: {float(second_episode['drawdown_pct']):.4f}")
        print(
            "second_longest_recovery_peak_to_recovery_bars: "
            f"{int(second_episode['peak_to_recovery_bars'])}"
        )


if __name__ == "__main__":
    main()
