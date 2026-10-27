from __future__ import annotations

import math
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from scripts.run_vectorbt import (
    benchmark_summary,
    compute_sortino_ratio,
    compute_cvar_sharpe_ratio,
    compute_martin_ratio,
    compute_sharpe_ratio,
    resolve_periods_per_year,
    strategy_performance_summary,
)
from scripts.research.analyze_pbo import (
    _build_subperiods,
    _build_cscv_splits,
    _rank_percentile_desc,
    _ReturnBlockScorer,
    _returns_from_result,
    _score_returns,
    _split_selection,
    main as pbo_main,
)
from scripts.walkforward.run_cross_section_grid_walkforward_v2 import (
    _build_folds,
    _trim_validation_timestamps,
)


class PerformanceAnnualizationV2Test(unittest.TestCase):
    def test_default_periods_follow_market_schedule(self) -> None:
        self.assertEqual(resolve_periods_per_year("daily"), 252)
        self.assertEqual(resolve_periods_per_year("60m"), 8760)
        self.assertEqual(resolve_periods_per_year("240m"), 2190)

    def test_explicit_periods_override_default(self) -> None:
        self.assertEqual(resolve_periods_per_year("60m", 6048), 6048)
        for invalid in (0, -1, 12.5, True, "invalid"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    resolve_periods_per_year("daily", invalid)

    def test_sortino_uses_standard_downside_deviation(self) -> None:
        returns = pd.Series([0.10, -0.05, 0.00, -0.10])
        downside_deviation = math.sqrt((0.0**2 + (-0.05) ** 2 + 0.0**2 + (-0.10) ** 2) / 4.0)
        expected = (returns.mean() / downside_deviation) * math.sqrt(4.0)
        actual = compute_sortino_ratio(returns, annualization_factor=4)
        self.assertTrue(math.isclose(actual, expected, rel_tol=1e-12))

    def test_strategy_and_benchmark_use_identical_annualization(self) -> None:
        curve = pd.Series(
            [100.0, 110.0, 99.0, 121.0],
            index=pd.date_range("2024-01-01", periods=4, freq="D"),
        )
        strategy = strategy_performance_summary(curve, annualization_factor=3)
        benchmark = benchmark_summary(
            curve,
            init_cash=100.0,
            benchmark_label="TEST",
            annualization_factor=3,
        )

        expected_cagr = ((121.0 / 100.0) ** (3.0 / 3.0) - 1.0) * 100.0
        expected_mdd = (99.0 / 110.0 - 1.0) * 100.0
        self.assertTrue(math.isclose(strategy["CAGR [%]"], expected_cagr, rel_tol=1e-12))
        self.assertTrue(
            math.isclose(
                strategy["Calmar Ratio"],
                (expected_cagr / 100.0) / abs(expected_mdd / 100.0),
                rel_tol=1e-12,
            )
        )
        for strategy_key, benchmark_key in (
            ("CAGR [%]", "Benchmark CAGR [%]"),
            ("Sharpe Ratio", "Benchmark Sharpe Ratio"),
            ("Sortino Ratio", "Benchmark Sortino Ratio"),
            ("Calmar Ratio", "Benchmark Calmar Ratio"),
        ):
            strategy_value = float(strategy[strategy_key])
            benchmark_value = float(benchmark[benchmark_key])
            if math.isnan(strategy_value) and math.isnan(benchmark_value):
                continue
            self.assertTrue(
                math.isclose(
                    strategy_value,
                    benchmark_value,
                    rel_tol=1e-12,
                )
            )


class ValidationStartV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.timestamps = pd.date_range("2017-09-25 03:00:00", "2026-10-27 03:00:00", freq="D")
        self.payload = {
            "trim_start_mode": "timestamp",
            "trim_start_timestamp": "2019-09-25 03:00:00",
        }

    def test_pbo_periods_exclude_warmup_for_both_anchors(self) -> None:
        timestamps = _trim_validation_timestamps(self.timestamps, [self.payload])
        for offset, expected_start, count in (
            (0, "2019-09-25 03:00:00", 14),
            (3, "2019-12-25 03:00:00", 13),
        ):
            with self.subTest(offset=offset):
                periods = _build_subperiods(timestamps, 6, anchor_month_offset=offset)
                self.assertEqual(periods[0]["start"], pd.Timestamp(expected_start))
                self.assertEqual(len(periods), count)
                self.assertTrue(all(period["end"] == period["start"] + pd.DateOffset(months=6) for period in periods))
        self.assertEqual(self.timestamps.min(), pd.Timestamp("2017-09-25 03:00:00"))

    def test_wf_uses_active_is_months_and_preserves_oos_dates(self) -> None:
        timestamps = _trim_validation_timestamps(self.timestamps, [self.payload])
        previous = _build_folds(self.timestamps, 48, 12, 12, "expanding")
        current = _build_folds(timestamps, 24, 12, 12, "expanding")
        self.assertEqual(len(current), 5)
        self.assertEqual(
            [(fold["oos_start"], fold["oos_end"]) for fold in current],
            [(fold["oos_start"], fold["oos_end"]) for fold in previous],
        )
        self.assertTrue(all(fold["is_start"] == pd.Timestamp(self.payload["trim_start_timestamp"]) for fold in current))
        rolling = _build_folds(timestamps, 24, 12, 12, "rolling")
        self.assertTrue(all(fold["is_end"] == fold["is_start"] + pd.DateOffset(months=24) for fold in rolling))

    def test_candidates_share_latest_configured_start(self) -> None:
        later = dict(self.payload, trim_start_timestamp="2020-01-01 00:00:00")
        timestamps = _trim_validation_timestamps(self.timestamps, [self.payload, {}, later])
        self.assertEqual(timestamps.min(), pd.Timestamp("2020-01-01 03:00:00"))

    def test_other_modes_keep_existing_timestamp_basis(self) -> None:
        for payload in ({}, {"trim_start_mode": "none"}, {"trim_start_mode": "first_weight"}):
            with self.subTest(payload=payload):
                pd.testing.assert_index_equal(_trim_validation_timestamps(self.timestamps, [payload]), self.timestamps)

    def test_missing_or_invalid_configured_start_fails(self) -> None:
        for value in (None, "NaT", "invalid"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _trim_validation_timestamps(self.timestamps, [dict(self.payload, trim_start_timestamp=value)])

    def test_start_after_data_produces_no_validation_periods(self) -> None:
        timestamps = _trim_validation_timestamps(
            self.timestamps, [dict(self.payload, trim_start_timestamp="2030-01-01")]
        )
        self.assertEqual(_build_subperiods(timestamps, 6), [])
        self.assertEqual(_build_folds(timestamps, 24, 12, 12, "expanding"), [])


class PboReturnScoringV2Test(unittest.TestCase):
    def test_all_combinations_include_reverse_direction(self) -> None:
        splits = _build_cscv_splits(list("abcdef"), 0, 42)
        self.assertEqual(len(splits), 20)
        self.assertEqual([tuple(i) for i, _ in splits], list(itertools.combinations(range(6), 3)))
        for left, right in splits:
            self.assertIn((right, left), splits)
            self.assertEqual(set(left) | set(right), set(range(6)))
            self.assertFalse(set(left) & set(right))

    def test_sampled_splits_preserve_pairs_and_seed(self) -> None:
        splits = _build_cscv_splits(list("abcdefgh"), 8, 123)
        self.assertEqual(len(splits), 8)
        self.assertEqual(splits, _build_cscv_splits(list("abcdefgh"), 8, 123))
        for left, right in splits:
            self.assertIn((right, left), splits)
        for periods, cap in ((list("abcde"), 0), (list("abcdef"), 3), (list("abcdef"), -2)):
            with self.assertRaises(ValueError):
                _build_cscv_splits(periods, cap, 42)

    def test_rank_median_and_ties_follow_cscv_convention(self) -> None:
        scores = pd.Series([1.0, 2.0, 3.0], index=list("abc"))
        selection = _split_selection(pd.Series([1.0, 3.0, 2.0], index=list("abc")), scores)
        self.assertEqual(selection["is_best_label"], "b")
        self.assertEqual(selection["lambda"], 0.0)
        self.assertTrue(selection["is_overfit"])
        tied = pd.Series([1.0, 2.0, 2.0, 4.0], index=list("abcd"))
        self.assertEqual(_rank_percentile_desc(tied, "b"), 2.5 / 5.0)
        self.assertEqual(_split_selection(tied, tied)["oos_rank_desc"], 1.0)
        self.assertEqual(_split_selection(scores * 0, scores)["is_best_label"], "a")

    def test_undefined_scores_do_not_silently_change_family(self) -> None:
        scores = pd.Series([1.0, np.nan, 3.0], index=list("abc"))
        with self.assertRaisesRegex(ValueError, "candidate family fixed"):
            _split_selection(scores, scores)

    def test_sufficient_statistics_equal_pooled_returns_for_unequal_blocks(self) -> None:
        rng = np.random.default_rng(20261027)
        blocks = [rng.normal(mu, sigma, (n, 3)) for mu, sigma, n in
                  ((0.01, 0.02, 5), (-0.02, 0.08, 17), (0.03, 0.01, 9), (0.001, 0.03, 12))]
        factors = np.array([252.0, 365.0, 8760.0])
        for metric in ("air", "sharpe", "martin", "calmar", "simple_return", "log_return", "cvar_sharpe"):
            scorer = _ReturnBlockScorer(blocks, metric, factors)
            for indices in ([0, 1], [0, 2], [1, 3], [3, 1]):
                expected = _score_returns(np.concatenate([blocks[i] for i in sorted(indices)]), metric, factors)
                np.testing.assert_allclose(scorer.score(indices), expected, rtol=1e-12, atol=1e-12)
        old_average = np.mean([_score_returns(block, "air", factors) for block in blocks[:2]], axis=0)
        self.assertFalse(np.allclose(old_average, _ReturnBlockScorer(blocks, "air", factors).score([0, 1])))

    def test_metrics_match_backtest_helpers_on_same_return_intervals(self) -> None:
        values = np.array([0.01, -0.02, 0.03, -0.04, 0.005, 0.02])
        returns = pd.Series(values)
        curve = pd.Series(np.r_[1.0, np.cumprod(1 + values)])
        stats = strategy_performance_summary(curve, annualization_factor=252)
        expected = {
            "air": compute_sharpe_ratio(returns, 252),
            "sharpe": compute_sharpe_ratio(returns, 252),
            "cvar_sharpe": compute_cvar_sharpe_ratio(returns, 252),
            "martin": compute_martin_ratio(curve, 252),
            "calmar": stats["Calmar Ratio"],
            "log_return": math.log(curve.iloc[-1]),
            "simple_return": curve.iloc[-1] - 1,
        }
        for metric, score in expected.items():
            with self.subTest(metric=metric):
                actual = _score_returns(values[:, None], metric, np.array([252.0]))[0]
                self.assertTrue(math.isclose(actual, score, rel_tol=1e-11, abs_tol=1e-11))

    def test_excess_returns_computed_before_date_alignment(self) -> None:
        index = pd.date_range("2020-01-01", periods=5)
        result = {"status": "ok", "_equity_curve": pd.Series([100, 110, 121, 133.1, 146.41], index=index),
                  "_benchmark_curve": pd.Series([100, 105, 110.25], index=index[2:])}
        actual = _returns_from_result(result, "air")
        self.assertEqual(actual.index[0], index[2])
        np.testing.assert_allclose(actual, [0.1, 0.05, 0.05], atol=1e-12)
        self.assertEqual(len(_returns_from_result(result, "martin")), 5)

    def test_bad_curves_fail_instead_of_zero_filling(self) -> None:
        for values in ([100, np.nan, 102], [100, 0, 102]):
            with self.assertRaises(ValueError):
                _returns_from_result({"status": "ok", "_equity_curve": pd.Series(values)}, "sharpe")

    def test_cli_backtests_once_per_candidate_and_reports_coverage(self) -> None:
        index = pd.date_range("2020-01-01", "2022-03-15")
        prices = pd.DataFrame({"TEST": np.arange(len(index)) + 100.0}, index=index)
        payloads = {label: ({"allowed_markets": ["TEST"]}, {}, {}, ["TEST"], {}, pd.DataFrame())
                    for label in ("a", "b", "c")}
        rng = np.random.default_rng(42)
        results = [{"status": "ok", "Periods Per Year": 252,
                    "_equity_curve": pd.Series(100 * np.cumprod(1 + rng.normal(0.001, 0.01, len(index))), index=index)}
                   for _ in payloads]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "grid.json"
            config.write_text("{}", encoding="utf-8")
            argv = ["analyze_pbo", "--grid-config-json", str(config), "--out-dir", str(root),
                    "--score-metric", "sharpe", "--subperiod-months", "6"]
            with patch("sys.argv", argv), \
                    patch("scripts.research.analyze_pbo._build_candidate_frames",
                          return_value=(root, root, "TEST", [{}], payloads)), \
                    patch("scripts.research.analyze_pbo.load_price_frame", return_value=prices), \
                    patch("scripts.research.analyze_pbo._run_v2_backtest_window", side_effect=results) as backtest:
                self.assertEqual(pbo_main(), 0)
                self.assertEqual(backtest.call_count, 3)
                self.assertTrue(all(call.kwargs["include_curves"] for call in backtest.call_args_list))
            summary = json.loads((root / "pbo_summary.json").read_text())
            splits = pd.read_csv(root / "pbo_split_results.csv")
            self.assertEqual(summary["split_count"], 6)
            self.assertEqual(summary["period_count"], 4)
            self.assertGreater(summary["coverage_by_anchor"]["0"]["excluded_tail_observations"], 0)
            self.assertEqual(summary["pbo"], float((splits["lambda"] <= 0).mean()))
            self.assertEqual(len(pd.read_parquet(root / "pbo_period_score_matrix.parquet")), 4)


if __name__ == "__main__":
    unittest.main()
