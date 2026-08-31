from __future__ import annotations

import math
import unittest

import pandas as pd

from scripts.run_vectorbt import (
    benchmark_summary,
    compute_sortino_ratio,
    resolve_periods_per_year,
    strategy_performance_summary,
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


if __name__ == "__main__":
    unittest.main()
