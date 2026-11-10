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

from lib.specs import UniverseSpec
from lib.universe_v2 import build_universe_mask_v2
from scripts.run_vectorbt import (
    benchmark_summary,
    compute_drawdown_recovery_stats,
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


class RebalanceScheduleV2Test(unittest.TestCase):
    def test_nday_uses_fixed_buckets_and_first_observation_after_missing_boundary(self):
        from lib.weights_v2 import _rebalance_mask
        index = pd.to_datetime(["1970-01-04 12:00", "1970-01-05 03:00", "1970-01-07 23:00",
                                "1970-01-08 02:00", "1970-01-10 23:00", "1970-01-11 00:00",
                                "1970-01-17 00:00"])
        expected = pd.Series([True, True, False, True, False, True, True], index=index)
        pd.testing.assert_series_equal(_rebalance_mask(index, "3d"), expected)
        # A sliced input gets an initial allocation, but later boundaries do not move.
        sliced = _rebalance_mask(index[2:], "3d")
        self.assertTrue(sliced.iloc[0])
        pd.testing.assert_series_equal(sliced.iloc[1:], expected.iloc[3:])
        pd.testing.assert_series_equal(_rebalance_mask(index[:5], "3d"), expected.iloc[:5])

    def test_nday_daily_weekly_compatibility_across_year_boundary(self):
        from lib.weights_v2 import _rebalance_mask
        for tz in (None, "UTC"):
            index = pd.date_range("2020-12-29 18:00", periods=24 * 25, freq="h", tz=tz)
            for days, calendar in (("1d", "daily"), ("7d", "weekly")):
                pd.testing.assert_series_equal(_rebalance_mask(index, days), _rebalance_mask(index, calendar))

    def test_nday_uses_utc_across_dst_and_preserves_output_index(self):
        from lib.weights_v2 import _rebalance_mask
        index = pd.date_range("2024-03-09", periods=24 * 8, freq="h", tz="America/New_York")
        actual = _rebalance_mask(index, "3d")
        expected = _rebalance_mask(index.tz_convert("UTC"), "3d")
        np.testing.assert_array_equal(actual.to_numpy(), expected.to_numpy())
        pd.testing.assert_index_equal(actual.index, index)
        self.assertEqual(len(_rebalance_mask(index[:0], "3d")), 0)

    def test_nday_offset_shifts_bucket_start_on_global_calendar(self):
        from lib.weights_v2 import _rebalance_mask
        index = pd.date_range("2024-01-01 05:00", periods=24 * 40, freq="h")  # Monday 05:00
        index = index[index != pd.Timestamp("2024-01-18 00:00")]  # missing Thursday boundary bar
        mask = _rebalance_mask(index, "7d+3")
        later = mask.index[mask][1:]
        self.assertTrue(all(ts.dayofweek == 3 for ts in later))  # Thursdays only
        self.assertEqual([str(ts) for ts in later[:3]],
                         ["2024-01-04 00:00:00", "2024-01-11 00:00:00", "2024-01-18 01:00:00"])
        self.assertTrue(mask.iloc[0])  # initial allocation
        # Phase is fixed by the UTC calendar, not by the input start (WF slices).
        sliced = _rebalance_mask(index[50:], "7d+3")
        pd.testing.assert_series_equal(sliced.iloc[1:], mask.iloc[51:])
        # 14d+7 starts its buckets one week after 14d.
        fourteen = _rebalance_mask(index, "14d").index[_rebalance_mask(index, "14d")][1:]
        shifted = _rebalance_mask(index, "14d+7").index[_rebalance_mask(index, "14d+7")][1:]
        self.assertTrue(all((s - f) % pd.Timedelta(days=14) == pd.Timedelta(days=7)
                            for f, s in zip(fourteen, shifted) if f.hour == 0 and s.hour == 0))
        self.assertTrue(set(shifted.normalize()).isdisjoint(set(fourteen.normalize())))

    def test_nday_offset_validation(self):
        from lib.weights_v2 import _rebalance_mask
        index = pd.date_range("2024-01-01", periods=2, freq="h")
        for frequency in ("7d+0", "7d+7", "7d+8", "7d+-1", "7d+03", "7d+1.5", "weekly+3", "7d+"):
            with self.subTest(frequency=frequency), self.assertRaises(ValueError):
                _rebalance_mask(index, frequency)
        self.assertEqual(int(_rebalance_mask(index, "3d+2").sum()), 1)

    def test_nday_rejects_invalid_interval_and_nat(self):
        from lib.weights_v2 import _rebalance_mask
        index = pd.date_range("2024-01-01", periods=2, freq="h")
        for frequency in ("0d", "-3d", "1.5d", "3h", "03d"):
            with self.subTest(frequency=frequency), self.assertRaises(ValueError):
                _rebalance_mask(index, frequency)
        with self.assertRaisesRegex(ValueError, "NaT"):
            _rebalance_mask(pd.DatetimeIndex([pd.NaT]), "3d")

    def test_nday_long_short_targets_keep_quantity_between_rebalances(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("1970-01-05", periods=24 * 8, freq="h")
        scores = pd.DataFrame({"A": 1., "B": -1.}, index=index)
        scores.iloc[1:] *= -1
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal",
            position_score_feature="side", rebalance_frequency="3d"))
        actual = build_weight_frame_v2(scores.notna(), spec, {"side": scores})
        np.testing.assert_allclose(actual.iloc[0], [.5, -.5])
        self.assertTrue(actual.iloc[1:72].isna().all().all())
        np.testing.assert_allclose(actual.iloc[72], [-.5, .5])
        self.assertTrue(actual.iloc[73:144].isna().all().all())
        np.testing.assert_allclose(actual.iloc[144], [-.5, .5])


class LongShortEqualV2Test(unittest.TestCase):
    def test_side_scales_keep_weekly_schedule_and_common_multiplier(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=24 * 9, freq="h")
        scores = pd.DataFrame({"A": 1., "B": 1., "C": -1.}, index=index)
        frames = {"side": scores,
                  "long_scale": pd.DataFrame(.5, index=index, columns=scores.columns),
                  "short_scale": pd.DataFrame(0., index=index, columns=["market"]),
                  "common": pd.DataFrame(.8, index=index, columns=["market"])}
        frames["long_scale"].iloc[0] = np.nan
        frames["short_scale"].iloc[0] = np.nan
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal",
            position_score_feature="side", rebalance_frequency="weekly",
            long_exposure_feature="long_scale", short_exposure_feature="short_scale",
            gross_exposure_feature="common"))
        actual = build_weight_frame_v2(scores.notna(), spec, frames)
        np.testing.assert_allclose(actual.iloc[0], [.2, .2, -.4])
        self.assertTrue(actual.iloc[1:168].isna().all().all())
        np.testing.assert_allclose(actual.iloc[168], [.1, .1, 0.])
        reordered = scores.columns[::-1]
        permuted = build_weight_frame_v2(scores[reordered].notna(), spec, frames)
        pd.testing.assert_frame_equal(actual, permuted[actual.columns])

    def test_side_scale_lag_neutral_history_and_future_independence(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=4, freq="h")
        scores = pd.DataFrame({"A": 1., "B": -1.}, index=index)
        frames = {"side": scores,
                  "long_scale": pd.DataFrame({"market": [1., .5, 0., 2.]}, index=index),
                  "short_scale": pd.DataFrame({"market": [1., 1., .5, .5]}, index=index)}
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal",
            position_score_feature="side", rebalance_frequency="every_bar",
            long_exposure_feature="long_scale", long_exposure_lag=1,
            short_exposure_feature="short_scale"))
        actual = build_weight_frame_v2(scores.notna(), spec, frames)
        np.testing.assert_allclose(actual.A, [.5, .5, .25, 0.])
        np.testing.assert_allclose(actual.B, [-.5, -.5, -.25, -.25])
        prefix_frames = {k: v.iloc[:3] for k, v in frames.items()}
        pd.testing.assert_frame_equal(actual.iloc[:3],
            build_weight_frame_v2(scores.iloc[:3].notna(), spec, prefix_frames))
        # Multipliers above one increase just that side; they are not clipped.
        expanded = build_weight_frame_v2(scores.notna(),
            load_weight_spec_from_payload(dict(weighting="long_short_equal", position_score_feature="side",
                rebalance_frequency="every_bar", long_exposure_feature="long_scale")), frames)
        self.assertEqual(expanded.A.iloc[-1], 1.)
        self.assertEqual(expanded.B.iloc[-1], -.5)

    def test_side_scale_rejects_invalid_values_shapes_and_options(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=2, freq="h")
        scores = pd.DataFrame({"A": 1., "B": -1.}, index=index)
        base = dict(weighting="long_short_equal", position_score_feature="side",
                    rebalance_frequency="every_bar", long_exposure_feature="scale")
        for values in ([1., -1.], [1., np.inf], [1., -np.inf]):
            frames = {"side": scores, "scale": pd.DataFrame({"market": values}, index=index)}
            with self.subTest(values=values), self.assertRaises(ValueError):
                build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload(base), frames)
        with self.assertRaisesRegex(ValueError, "scalar/broadcast"):
            build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload(base),
                {"side": scores, "scale": pd.DataFrame({"A": 1., "B": .5}, index=index)})
        # Floating-point drift of a broadcast feature computed per column passes
        # (relative 1e-9); a real per-market difference still fails.
        drift = build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload(base),
            {"side": scores, "scale": pd.DataFrame({"A": .8, "B": .8 * (1 + 1e-11)}, index=index)})
        exact = build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload(base),
            {"side": scores, "scale": pd.DataFrame({"A": .8, "B": .8}, index=index)})
        pd.testing.assert_frame_equal(drift, exact)
        with self.assertRaisesRegex(ValueError, "scalar/broadcast"):
            build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload(base),
                {"side": scores, "scale": pd.DataFrame({"A": .8, "B": .8 * (1 + 1e-7)}, index=index)})
        with self.assertRaisesRegex(ValueError, "Missing long exposure"):
            build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload(base), {"side": scores})
        for change in (dict(long_exposure_lag=-1), dict(long_exposure_lag=True),
                       dict(long_exposure_lag=.5), dict(long_exposure_feature=""), dict(weighting="equal"),
                       dict(long_exposure_feature=None, long_exposure_lag=1)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_weight_frame_v2(scores.notna(), load_weight_spec_from_payload({**base, **change}),
                                     {"side": scores, "scale": scores.abs()})

    def test_side_scale_names_and_shared_retention(self):
        from lib.spec_io import load_weight_spec_from_payload
        from scripts.run_cross_section_grid_v2 import _retain_shared_feature_frames
        base = dict(weighting="long_short_equal", position_score_feature="side", rebalance_frequency="weekly")
        baseline = load_weight_spec_from_payload(base)
        scaled = load_weight_spec_from_payload({**base, "long_exposure_feature": "long_scale"})
        lagged = load_weight_spec_from_payload({**base, "long_exposure_feature": "long_scale", "long_exposure_lag": 1})
        self.assertEqual(len({baseline.resolved_name(), scaled.resolved_name(), lagged.resolved_name()}), 3)
        index = pd.date_range("2024-01-01", periods=2, freq="h")
        frames = {k: pd.DataFrame(1., index=index, columns=["A"]) for k in ("side", "long_scale", "dead")}
        config = dict(run_name_template="case_{scale}", universe_spec_template=dict(feature_column="side", mode="all"),
                      weight_spec_template={**base, "long_exposure_feature": "{scale}"})
        kept = _retain_shared_feature_frames(frames, config, [dict(scale="long_scale")], None)
        self.assertIn("long_scale", kept)
        self.assertNotIn("dead", kept)

    def test_sequential_caps_rank_inside_each_independent_cross_group(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        names = [f"V{v}R{r}_{j}" for v, r, j in itertools.product(range(1, 4), range(1, 4), range(3))]
        index = pd.date_range("2024-01-01", periods=1, freq="h")
        features = {
            "volume": pd.DataFrame([[int(n[1]) * 100 + (2 - int(n[-1])) for n in names]], index=index, columns=names),
            "return": pd.DataFrame([[int(n[3]) * 100 + int(n[-1]) for n in names]], index=index, columns=names),
        }
        for frame in features.values():
            frame["BTC"] = -1e9
        selected = pd.DataFrame(True, index=index, columns=features["volume"].columns)
        selected["BTC"] = False

        def side(v, criterion, ascending, limit):
            return dict(mode="sequential", filters=[
                dict(feature_column="volume", mode="quantile", quantiles=3, bucket_values=[v],
                     ascending=True, scope="global"),
                dict(feature_column="return", mode="quantile", quantiles=3, bucket_values=[1],
                     ascending=True, scope="global"),
                dict(feature_column=criterion, mode="top_n", top_n=limit,
                     ascending=ascending, scope="filtered")])

        criteria = list(itertools.product(("return", "volume"), (True, False)))
        for limit, long, short in itertools.product((1, 2, 5), criteria, criteria):
            payload = dict(weighting="long_short_equal", rebalance_frequency="every_bar",
                long_filter=side(1, *long, limit), short_filter=side(3, *short, limit))
            actual = build_weight_frame_v2(selected, load_weight_spec_from_payload(payload), features,
                                          base_eligible_mask=selected)
            expected = pd.Series(0., index=selected.columns)
            for v, (criterion, ascending), exposure in ((1, long, .5), (3, short, -.5)):
                # The three known members have opposite volume/return order.
                increasing_j = (criterion == "return") == ascending
                order = [0, 1, 2] if increasing_j else [2, 1, 0]
                kept = [f"V{v}R1_{j}" for j in order[:limit]]
                expected.loc[kept] = exposure / len(kept)
            with self.subTest(limit=limit, long=long, short=short):
                pd.testing.assert_series_equal(actual.iloc[0], expected, check_names=False)
            if limit == 5:
                # A nonbinding cap preserves the old AND intersection exactly.
                for key in ("long_filter", "short_filter"):
                    payload[key] = dict(mode="and", filters=payload[key]["filters"][:2])
                baseline = build_weight_frame_v2(selected, load_weight_spec_from_payload(payload), features,
                                                base_eligible_mask=selected)
                pd.testing.assert_frame_equal(actual, baseline)

    def test_sequential_cap_weekly_causality_empty_side_and_invalid_limits(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=169, freq="h")
        selected = pd.DataFrame(True, index=index, columns=list("ABCDEF"))
        features = {"side": pd.DataFrame([[1., 1., 1., 2., 2., 2.]] * len(index), index=index,
                                        columns=selected.columns),
                    "score": pd.DataFrame([[6., 5., 4., 3., 2., 1.]] * len(index), index=index,
                                         columns=selected.columns)}
        def side(bucket, limit):
            return dict(mode="sequential", filters=[
                dict(feature_column="side", mode="quantile", quantiles=2, bucket_values=[bucket],
                     ascending=True, scope="global"),
                dict(feature_column="score", mode="top_n", top_n=limit, ascending=True,
                     scope="filtered", lag=1)])
        payload = dict(weighting="long_short_equal", rebalance_frequency="weekly",
                       long_filter=side(1, 1), short_filter=side(2, 1))
        spec = load_weight_spec_from_payload(payload)
        actual = build_weight_frame_v2(selected, spec, features, base_eligible_mask=selected)
        self.assertTrue(actual.iloc[0].eq(0).all())
        self.assertTrue(actual.iloc[1:168].isna().all().all())
        self.assertEqual(actual.iloc[-1]["C"], .5)
        self.assertEqual(actual.iloc[-1]["F"], -.5)
        changed = {k: v.copy() for k, v in features.items()}
        changed["score"].iloc[-1] = list(range(6))
        pd.testing.assert_frame_equal(actual, build_weight_frame_v2(selected, spec, changed,
                                                                  base_eligible_mask=selected))
        prefix = selected.iloc[:100]
        pd.testing.assert_frame_equal(actual.iloc[:100], build_weight_frame_v2(
            prefix, spec, {k: v.iloc[:100] for k, v in features.items()}, base_eligible_mask=prefix))
        no_longs = selected.copy()
        no_longs.loc[:, list("ABC")] = False
        empty = build_weight_frame_v2(no_longs, spec, features, base_eligible_mask=selected)
        self.assertEqual(empty.iloc[-1].clip(lower=0).sum(), 0.)
        self.assertEqual(empty.iloc[-1].clip(upper=0).sum(), -.5)
        for limit in (0, -1, True, "2", None):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "top_n"):
                invalid = load_weight_spec_from_payload(dict(payload, long_filter=side(1, limit)))
                build_weight_frame_v2(selected, invalid, features, base_eligible_mask=selected)

    def test_and_filters_cover_all_72_distinct_cross_group_pairs(self):
        from lib.spec_io import load_universe_spec_from_payload, load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        names = [f"V{v}R{r}_{j}" for v, r, j in itertools.product(range(1, 4), range(1, 4), range(2))]
        index = pd.date_range("2024-01-01", periods=1, freq="h")
        features = {
            "volume": pd.DataFrame([[int(n[1]) * 100 + j for j, n in enumerate(names)]], index=index, columns=names),
            "return": pd.DataFrame([[int(n[3]) * 100 + j for j, n in enumerate(names)]], index=index, columns=names),
        }
        # An excluded extreme must not move either factor's breakpoints.
        for frame in features.values():
            frame["BTC"] = -1e9
        universe = build_universe_mask_v2(features, pd.DataFrame(index=index), load_universe_spec_from_payload(
            dict(feature_column="volume", mode="all", excluded_markets=["BTC"], lag=0, signal_lag=0)))
        def side(v, r):
            return dict(mode="and", filters=[dict(feature_column=f, mode="quantile", quantiles=3,
                bucket_values=[bucket], ascending=True, scope="global", lag=0)
                for f, bucket in (("volume", v), ("return", r))])
        cells = list(itertools.product(range(1, 4), repeat=2))
        for long, short in itertools.permutations(cells, 2):
            spec = load_weight_spec_from_payload(dict(weighting="long_short_equal", rebalance_frequency="every_bar",
                long_filter=side(*long), short_filter=side(*short)))
            actual = build_weight_frame_v2(universe.selection_mask, spec, features,
                                          base_eligible_mask=universe.base_eligible_mask)
            expected = pd.Series(0., index=actual.columns)
            for n in names:
                cell = (int(n[1]), int(n[3]))
                expected[n] = .25 if cell == long else -.25 if cell == short else 0.
            with self.subTest(long=long, short=short):
                pd.testing.assert_series_equal(actual.iloc[0], expected, check_names=False)

    def test_and_filtered_scope_is_frozen_not_sequential_and_order_does_not_matter(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        names = list("ABCDEFGHI")
        selected = pd.DataFrame(True, index=[0], columns=names)
        features = {"return": pd.DataFrame([list(range(9))], columns=names),
                    "volume": pd.DataFrame([[90., 10., 20., 40., 50., 60., 30., 80., 100.]], columns=names)}
        def side(bucket):
            return dict(mode="and", filters=[dict(feature_column=f, mode="quantile", quantiles=3,
                bucket_values=[bucket], ascending=True, scope="filtered") for f in ("volume", "return")])
        long, short = side(3), side(1)
        def build(a, b):
            return build_weight_frame_v2(selected, load_weight_spec_from_payload(dict(
                weighting="long_short_equal", rebalance_frequency="every_bar", long_filter=a, short_filter=b)), features)
        actual = build(long, short)
        expected = pd.Series(0., index=names)
        expected.loc[["H", "I"]] = .25
        expected.loc[["B", "C"]] = -.25
        pd.testing.assert_series_equal(actual.iloc[0], expected, check_names=False)
        pd.testing.assert_frame_equal(actual, build(dict(mode="and", filters=long["filters"][::-1]), short))
        # Group-of-one wrappers preserve old single-filter results and names of old specs.
        single = long["filters"][0]
        pd.testing.assert_frame_equal(build(single, short), build(dict(mode="and", filters=[single]), short))

    def test_and_filter_lags_are_causal_and_empty_side_keeps_cash(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        names = list("ABCDEFGHI")
        index = pd.date_range("2024-01-01", periods=3, freq="h")
        selected = pd.DataFrame(True, index=index, columns=names)
        features = {"return": pd.DataFrame([list(range(9))] * 3, index=index, columns=names),
                    "volume": pd.DataFrame([[90., 10., 20., 40., 50., 60., 30., 80., 100.]] * 3,
                                           index=index, columns=names)}
        def rank(feature, bucket):
            return dict(feature_column=feature, mode="quantile", quantiles=3, bucket_values=[bucket],
                        ascending=True, scope="global", lag=1)
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal", rebalance_frequency="every_bar",
            long_filter=dict(mode="and", filters=[rank("volume", 3), rank("return", 3)]),
            short_filter=dict(mode="and", filters=[rank("volume", 3), rank("return", 2)])))
        actual = build_weight_frame_v2(selected, spec, features, base_eligible_mask=selected)
        self.assertTrue(actual.iloc[0].eq(0).all())
        self.assertTrue(actual.iloc[1:].lt(0).sum().eq(0).all())
        np.testing.assert_allclose(actual.iloc[1:][["H", "I"]], .25)
        changed = {k: v.copy() for k, v in features.items()}
        changed["return"].iloc[-1] = list(reversed(range(9)))
        pd.testing.assert_frame_equal(actual, build_weight_frame_v2(selected, spec, changed, base_eligible_mask=selected))

    def test_and_filter_rejects_empty_nested_or_ambiguous_groups_and_overlap(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        rank = dict(feature_column="score", mode="quantile", quantiles=2, bucket_values=[1])
        for bad in ([], dict(filters=[]), dict(mode="or", filters=[rank]),
                    dict(filters=[dict(filters=[rank])]), dict(filters=[None]),
                    dict(feature_column="score", filters=[rank])):
            with self.subTest(group=bad), self.assertRaises(ValueError):
                load_weight_spec_from_payload(dict(weighting="long_short_equal", long_filter=bad, short_filter=rank))
        selected = pd.DataFrame(True, index=[0], columns=["A", "B"])
        grouped = dict(mode="and", filters=[rank, rank])
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal", rebalance_frequency="every_bar",
            long_filter=grouped, short_filter=rank))
        with self.assertRaisesRegex(ValueError, "overlap"):
            build_weight_frame_v2(selected, spec, {"score": pd.DataFrame([[1., 2.]], columns=selected.columns)})

    def build(self, scores, selected=None, **options):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal",
            position_score_feature="side", rebalance_frequency="every_bar", **options))
        return build_weight_frame_v2(scores.notna() if selected is None else selected,
                                    spec, {"side": scores})

    def test_separate_budgets_equalize_each_side_and_leave_empty_side_in_cash(self):
        scores = pd.DataFrame([[1., 100., -3., 0.], [0., 2., np.nan, 0.],
                               [-1., -2., -3., -4.], [0., 0., 0., 0.]])
        actual = self.build(scores, long_exposure=.8, short_exposure=.3)
        expected = pd.DataFrame([[.4, .4, -.3, 0.], [0., .8, 0., 0.],
                                 [-.075] * 4, [0.] * 4])
        pd.testing.assert_frame_equal(actual, expected)
        self.assertAlmostEqual(actual.iloc[0].abs().sum(), 1.1)
        disabled = self.build(scores, long_exposure=0., short_exposure=.2)
        self.assertFalse(disabled.gt(0).any().any())

    def test_weekly_requests_hold_quantities_and_exit_reverse_on_next_week(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=8 * 24, freq="h")
        scores = pd.DataFrame({"A": 1., "B": -1., "C": 1.}, index=index)
        scores.loc[index[1]:, "A"] = -1.
        scores.loc[index[1]:, "B"] = 0.
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal",
            position_score_feature="side", rebalance_frequency="weekly"))
        actual = build_weight_frame_v2(scores.notna(), spec, {"side": scores})
        np.testing.assert_allclose(actual.iloc[0], [.25, -.5, .25])
        self.assertTrue(actual.iloc[1:168].isna().all().all())
        np.testing.assert_allclose(actual.iloc[168], [-.5, 0., .5])
        prefix = scores.iloc[:100]
        pd.testing.assert_frame_equal(actual.iloc[:100],
            build_weight_frame_v2(prefix.notna(), spec, {"side": prefix}))

    def test_global_rank_conditions_preserve_independent_intersections_and_exclusions(self):
        from lib.spec_io import load_universe_spec_from_payload, load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=3, freq="h")
        names = list("ABCDEFGHI") + ["BTC"]
        returns = pd.DataFrame([list(range(9)) + [-1000.]] * 3, index=index, columns=names)
        turnover = pd.DataFrame([[90., 10., 20., 40., 50., 60., 30., 80., 100., 1000.]] * 3,
                                index=index, columns=names)
        features = {"return": returns, "turnover": turnover}
        warning = pd.DataFrame(index=index)
        def rank(bucket):
            return dict(feature_column="return", mode="quantile", quantiles=3,
                        bucket_values=[bucket], ascending=True, scope="global", lag=0)
        payload = dict(weighting="long_short_equal", rebalance_frequency="every_bar",
                       long_filter=rank(3), short_filter=rank(1))
        for bucket, expected in ((3, {"A": -.5, "H": .25, "I": .25}),
                                 (1, {"B": -.25, "C": -.25, "G": .5})):
            universe = build_universe_mask_v2(features, warning, load_universe_spec_from_payload(dict(
                feature_column="turnover", mode="all", allowed_markets=list("ABCDEFGHI"),
                excluded_markets=["BTC"], lag=0, signal_lag=0, rank_filters=[dict(
                    feature_column="turnover", mode="quantile", quantiles=3, bucket_values=[bucket],
                    ascending=True, scope="global")])) )
            original = {name: value.copy() for name, value in features.items()}
            actual = build_weight_frame_v2(universe.selection_mask,
                load_weight_spec_from_payload(payload), features, base_eligible_mask=universe.base_eligible_mask)
            row = pd.Series(0., index=names)
            row.loc[list(expected)] = list(expected.values())
            pd.testing.assert_series_equal(actual.iloc[0], row, check_names=False)
            reversed_sides = build_weight_frame_v2(universe.selection_mask,
                load_weight_spec_from_payload(dict(payload, long_filter=rank(1), short_filter=rank(3))),
                features, base_eligible_mask=universe.base_eligible_mask)
            pd.testing.assert_frame_equal(reversed_sides, -actual)
            for name in original:
                pd.testing.assert_frame_equal(original[name], features[name])

    def test_lag_eligibility_overlay_and_change_only(self):
        from lib.dataframes import CACHE_ELIGIBILITY_FRAME
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        index = pd.date_range("2024-01-01", periods=3, freq="h")
        scores = pd.DataFrame([[1., -1.], [-1., 1.], [1., -1.]], index=index, columns=["A", "B"])
        eligible = pd.DataFrame([[True, False], [True, True], [True, True]], index=index, columns=scores.columns)
        features = {"side": scores, CACHE_ELIGIBILITY_FRAME: eligible,
                    "scale": pd.DataFrame(.5, index=index, columns=["scale"])}
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal", position_score_feature="side",
            position_score_lag=1, rebalance_frequency="change_only", gross_exposure_feature="scale"))
        actual = build_weight_frame_v2(scores.notna(), spec, features)
        np.testing.assert_allclose(actual, [[0., 0.], [.25, -.25], [-.25, .25]])

    def test_invalid_budgets_conflicting_conditions_and_missing_population_fail(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2
        scores = pd.DataFrame([[1., -1.]])
        for value in (-.1, np.nan, np.inf, True, None, "0.5"):
            with self.subTest(budget=value), self.assertRaisesRegex(ValueError, "long_exposure"):
                self.build(scores, long_exposure=value)
        with self.assertRaisesRegex(ValueError, "gross_exposure"):
            self.build(scores, gross_exposure=2.)
        rank = dict(feature_column="side", mode="quantile", quantiles=2, bucket_values=[1], scope="global")
        spec = load_weight_spec_from_payload(dict(weighting="long_short_equal", long_filter=rank,
                                                  short_filter=rank, rebalance_frequency="every_bar"))
        with self.assertRaisesRegex(ValueError, "base_eligible_mask"):
            build_weight_frame_v2(scores.notna(), spec, {"side": scores})
        with self.assertRaisesRegex(ValueError, "overlap"):
            build_weight_frame_v2(scores.notna(), spec, {"side": scores}, base_eligible_mask=scores.notna())
        with self.assertRaisesRegex(ValueError, "infinity"):
            self.build(pd.DataFrame([[np.inf, -1.]]))
        baseline = load_weight_spec_from_payload(dict(weighting="long_short_equal", position_score_feature="side"))
        from dataclasses import replace
        self.assertNotEqual(baseline.resolved_name(), replace(baseline, short_exposure=.7).resolved_name())


class ChangeOnlyScopeV2Test(unittest.TestCase):
    def test_asset_scope_preserves_unchanged_targets_and_explicit_exits(self):
        from lib.weights_v2 import to_change_only_target_frame
        dense = pd.DataFrame({"A": [-.2, -.2, -.2, -.3, 0.],
                              "B": [-.2, 0., 0., 0., 0.]})
        actual = to_change_only_target_frame(dense, scope="asset")
        expected = pd.DataFrame({"A": [-.2, np.nan, np.nan, -.3, 0.],
                                 "B": [-.2, 0., np.nan, np.nan, np.nan]})
        pd.testing.assert_frame_equal(actual, expected)
        pd.testing.assert_frame_equal(actual.ffill(), dense)

    def test_default_portfolio_scope_remains_whole_row(self):
        from lib.weights_v2 import to_change_only_target_frame
        dense = pd.DataFrame({"A": [-.2, -.2, -.2], "B": [-.2, 0., 0.]})
        expected = pd.DataFrame({"A": [-.2, -.2, np.nan], "B": [-.2, 0., np.nan]})
        pd.testing.assert_frame_equal(to_change_only_target_frame(dense), expected)
        pd.testing.assert_frame_equal(to_change_only_target_frame(dense, scope="portfolio"), expected)

    def test_fresh_window_emits_first_targets_and_reversals(self):
        from lib.weights_v2 import to_change_only_target_frame
        dense = pd.DataFrame({"A": [0., -.2, -.2, .2, 0.], "B": [0.] * 5})
        fresh = to_change_only_target_frame(dense.iloc[2:], scope="asset")
        self.assertEqual(fresh.iloc[0].to_dict(), {"A": -.2, "B": 0.})
        self.assertEqual(fresh.A.tolist(), [-.2, .2, 0.])
        self.assertTrue(fresh.B.iloc[1:].isna().all())

    def test_scope_parser_names_and_invalid_values(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.specs import WeightSpec
        from lib.weights_v2 import build_weight_frame_v2, to_change_only_target_frame
        baseline = load_weight_spec_from_payload({})
        explicit = load_weight_spec_from_payload({"change_only_scope": "portfolio"})
        asset = load_weight_spec_from_payload({"change_only_scope": "asset"})
        self.assertEqual(baseline, explicit)
        self.assertNotEqual(asset.resolved_name(), baseline.resolved_name())
        for bad in (None, "", "market", False):
            with self.subTest(scope=bad):
                with self.assertRaisesRegex(ValueError, "change_only_scope"):
                    load_weight_spec_from_payload({"change_only_scope": bad})
                with self.assertRaisesRegex(ValueError, "change_only_scope"):
                    to_change_only_target_frame(pd.DataFrame(), scope=bad)
                with self.assertRaisesRegex(ValueError, "change_only_scope"):
                    build_weight_frame_v2(pd.DataFrame(), WeightSpec(change_only_scope=bad))

    def test_asset_scope_is_causal_order_independent_and_handles_empty_frames(self):
        from lib.weights_v2 import to_change_only_target_frame
        dense = pd.DataFrame({"A": [0., -.2, -.2, 0.], "B": [-.2, -.2, 0., -.2]})
        full = to_change_only_target_frame(dense, scope="asset")
        pd.testing.assert_frame_equal(full.iloc[:3], to_change_only_target_frame(dense.iloc[:3], scope="asset"))
        pd.testing.assert_frame_equal(full, to_change_only_target_frame(dense[["B", "A"]], scope="asset")[["A", "B"]])
        pd.testing.assert_frame_equal(to_change_only_target_frame(dense.iloc[:0], scope="asset"), dense.iloc[:0])

    def test_daily_schedule_and_cache_eligibility_keep_own_exit(self):
        from lib.spec_io import load_weight_spec_from_payload
        from lib.weights_v2 import build_weight_frame_v2, to_change_only_target_frame
        from lib.dataframes import CACHE_ELIGIBILITY_FRAME
        index = pd.date_range("2024-01-01", periods=50, freq="h")
        selected = pd.DataFrame(True, index=index, columns=["A", "B"])
        selected.loc[index[24]:, "B"] = False
        spec = load_weight_spec_from_payload(dict(weighting="fixed", fixed_weight=-.2,
            rebalance_frequency="daily", output_mode="change_only", change_only_scope="asset"))
        result = build_weight_frame_v2(selected, spec)
        self.assertTrue(pd.isna(result.loc[index[24], "A"]))
        self.assertEqual(result.loc[index[24], "B"], 0.)
        usage = selected * True
        usage.loc[index[30]:, "A"] = False
        eligible = build_weight_frame_v2(selected, spec, {CACHE_ELIGIBILITY_FRAME: usage})
        final = to_change_only_target_frame(eligible.ffill().fillna(0.), scope="asset")
        self.assertEqual(final.loc[index[30], "A"], 0.)
        self.assertTrue(final.loc[index[31]:, "A"].isna().all())

    def test_selection_statistics_count_held_targets_not_only_new_requests(self):
        from scripts.run_cross_section_grid_v2 import _selection_stats
        from lib.weights_v2 import to_change_only_target_frame
        dense = pd.DataFrame({"A": [-.2, -.2, -.2, 0.], "B": [0., -.2, 0., 0.]})
        stats = _selection_stats(to_change_only_target_frame(dense, scope="asset"))
        self.assertEqual(stats["max_selected_count"], 2)
        self.assertEqual(stats["active_rebalance_dates"], 3)


class ExecutionAvailabilityV2Test(unittest.TestCase):
    """Hand cases for deferring non-tradable requests and settling delisted holdings (vectorbt)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="execution_availability_v2_")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.index = pd.date_range("2024-01-01", periods=8, freq="D")

    def run_case(self, a_prices, a_volume, a_requests, *, enabled=True, b_requests=None):
        from lib.backtest_engine_v2 import run_backtest_v2
        from lib.vectorbt_adapter import VectorBTSpec
        nan = np.nan
        prices = pd.DataFrame({"A": a_prices, "B": [10.] * 8}, index=self.index, dtype=float)
        volume = pd.DataFrame({"A": a_volume, "B": [1.] * 8}, index=self.index, dtype=float)
        volume.to_parquet(self.directory / "vol.parquet")
        weights = pd.DataFrame({"A": a_requests, "B": b_requests or [.5] + [nan] * 7}, index=self.index, dtype=float)
        payload = {"engine": "vectorbt"}
        if enabled:
            payload["execution_availability"] = {"volume_column": "vol"}
        spec = VectorBTSpec(price_column="trade_price", init_cash=1000., fees=0., slippage=0., freq="D")
        return run_backtest_v2(prices, weights, spec, payload=payload, source_cache_dir=self.directory, timeframe="1d")

    @staticmethod
    def a_orders(portfolio):
        records = portfolio.orders.records_readable
        return records.loc[records.Column.eq("A"), ["Timestamp", "Size", "Price"]].reset_index(drop=True)

    def test_sell_during_suspension_waits_for_resumption(self):
        nan = np.nan
        prices = [10., 10., 12., 12., 12., 15., 15., 15.]
        volume = [1., 1., 0., 0., 0., 1., 1., 1.]
        requests = [.5, nan, nan, 0., nan, nan, nan, nan]
        on = self.run_case(prices, volume, requests)
        off = self.run_case(prices, volume, requests, enabled=False)
        # 50 shares bought at 10; the suspended-day exit fills at the resumption price 15.
        np.testing.assert_allclose(self.a_orders(on).Price, [10., 15.])
        self.assertAlmostEqual(on.value().iloc[-1], 1000. + 50 * 5)
        self.assertAlmostEqual(off.value().iloc[-1], 1000. + 50 * 2)  # old behaviour: stale price 12
        self.assertEqual(on.execution_availability_audit["deferred_requests"], 1)

    def test_newer_request_supersedes_a_deferred_one(self):
        nan = np.nan
        on = self.run_case([10., 10., 12., 12., 12., 15., 15., 15.], [1., 1., 0., 0., 0., 1., 1., 1.],
                           [.5, nan, .2, nan, 0., nan, nan, nan])
        audit = on.execution_availability_audit
        self.assertEqual((audit["superseded_requests"], audit["deferred_requests"]), (1, 1))
        np.testing.assert_allclose(self.a_orders(on).Price, [10., 15.])
        np.testing.assert_allclose(self.a_orders(on).Size, [50., 50.])

    def test_buy_during_suspension_fills_on_resumption(self):
        nan = np.nan
        on = self.run_case([10., 10., 12., 12., 12., 15., 15., 15.], [1., 1., 0., 0., 0., 1., 1., 1.],
                           [nan, nan, nan, .5, nan, nan, nan, nan])
        orders = self.a_orders(on)
        self.assertEqual(list(orders.Timestamp), [self.index[5]])
        self.assertAlmostEqual(orders.Price.iloc[0], 15.)
        self.assertAlmostEqual(orders.Size.iloc[0], 1000. * .5 / 15.)  # target weight kept, sized at fill equity

    def test_held_position_settles_at_last_price_when_prices_end(self):
        nan = np.nan
        prices = [10., 10., 12., nan, nan, nan, nan, nan]
        volume = [1., 1., 0., nan, nan, nan, nan, nan]
        on = self.run_case(prices, volume, [.5, nan, nan, nan, 0., nan, nan, nan])
        orders = self.a_orders(on)
        self.assertEqual(list(orders.Timestamp), [self.index[0], self.index[3]])
        np.testing.assert_allclose(orders.Price, [10., 12.])
        self.assertAlmostEqual(on.value().iloc[-1], 1000. + 50 * 2)
        self.assertTrue(np.isfinite(on.trades.records_readable.PnL).all())
        settlement, = on.execution_availability_audit["delisting_settlements"]
        self.assertEqual((settlement["market"], settlement["date"], settlement["price"]), ("A", str(self.index[3]), 12.))
        off = self.run_case(prices, volume, [.5, nan, nan, nan, 0., nan, nan, nan], enabled=False)
        self.assertTrue(np.isnan(off.trades.records_readable.loc[lambda t: t.Column.eq("A"), "PnL"]).any())

    def test_buy_into_suspension_that_ends_in_delisting_never_fills(self):
        nan = np.nan  # the 010620 pattern: bought while suspended, prices then end
        on = self.run_case([10., 10., 12., 12., nan, nan, nan, nan], [1., 1., 0., 0., nan, nan, nan, nan],
                           [nan, nan, .5, nan, nan, nan, nan, nan])
        self.assertTrue(self.a_orders(on).empty)
        audit = on.execution_availability_audit
        self.assertEqual((audit["canceled_at_delisting"], len(audit["delisting_settlements"])), (1, 0))

    def test_temporary_price_gap_defers_and_requests_on_tradable_bars_are_unchanged(self):
        nan = np.nan
        gap = self.run_case([10., 10., 11., nan, 13., 13., 13., 13.], [1., 1., 1., nan, 1., 1., 1., 1.],
                            [.5, nan, nan, 0., nan, nan, nan, nan])
        np.testing.assert_allclose(self.a_orders(gap).Price, [10., 13.])
        self.assertEqual(gap.execution_availability_audit["delisting_settlements"], [])
        clean = ([10., 11., 12., 13., 12., 11., 12., 13.], [1.] * 8, [.5, nan, .2, nan, 0., nan, .3, nan])
        on, off = self.run_case(*clean), self.run_case(*clean, enabled=False)
        pd.testing.assert_series_equal(on.value(), off.value())
        pd.testing.assert_frame_equal(on.orders.records_readable, off.orders.records_readable)

    def test_rejects_unknown_settings_and_other_engines(self):
        from lib.backtest_engine_v2 import run_backtest_v2, apply_execution_availability_v2
        frame = pd.DataFrame({"A": [1.]}, index=self.index[:1])
        with self.assertRaisesRegex(ValueError, "supports only"):
            apply_execution_availability_v2(frame, frame, {"volume_column": "vol", "x": 1}, self.directory)
        with self.assertRaisesRegex(ValueError, "volume source is missing"):
            apply_execution_availability_v2(frame, frame, {"volume_column": "nope"}, self.directory)
        with self.assertRaisesRegex(ValueError, "only by the vectorbt engine"):
            run_backtest_v2(frame, frame, None, payload={"engine": "numba_futures",
                            "execution_availability": {"volume_column": "vol"}},
                            source_cache_dir=self.directory, timeframe="1d")


class CompactBacktestAnalysisV2Test(unittest.TestCase):
    def make_portfolio(self, profits=(60., -20., 30., -20.)):
        from types import SimpleNamespace
        trades = pd.DataFrame(dict(trade_id=[1, 2, 3, 4], pair=["A", "A", "B", "C"],
            is_short=[False, False, True, True], profit_abs=profits,
            close_date=["2023-12-31"] * 4, fees_paid=[99.] * 4))
        return SimpleNamespace(trades=trades)

    def test_hand_annual_returns_and_signed_profit_concentration(self):
        from lib.backtest_engine_v2 import build_backtest_analysis_v2
        portfolio = self.make_portfolio()
        before = portfolio.trades.copy(deep=True)
        equity = pd.Series([110., 120., 90., 150.], index=pd.to_datetime(
            ["2022-12-30", "2022-12-31", "2023-01-01", "2023-12-31"]))
        tables, report, metrics = build_backtest_analysis_v2(portfolio, equity_curve=equity, init_cash=100.)
        np.testing.assert_allclose(tables["annual_returns"].return_pct, [20., 25.])
        self.assertAlmostEqual(metrics["Asset Top 1 Net Profit Share [%]"], 80.)
        self.assertAlmostEqual(metrics["Asset Top 5 Net Profit Share [%]"], 140.)
        self.assertAlmostEqual(metrics["Trade Top 1 Net Profit Share [%]"], 120.)
        self.assertAlmostEqual(metrics["Long Net PnL"], 40.)
        self.assertAlmostEqual(metrics["Short Net PnL"], 10.)
        self.assertEqual(report["trade_net_pnl"], 50.)  # Do not subtract fees_paid again.
        self.assertTrue(report["reconciled"])
        pd.testing.assert_frame_equal(portfolio.trades, before)

    def test_loss_zero_and_empty_books_write_strict_json(self):
        from lib.backtest_engine_v2 import write_backtest_analysis_v2
        for profits in ((10., -20., 0., 0.), (10., -10., 0., 0.), ()):
            portfolio = self.make_portfolio(profits) if profits else self.make_portfolio()
            if not profits:
                portfolio.trades = pd.DataFrame()
            equity = pd.Series([100., 100. + sum(profits)], index=pd.date_range("2024-01-01", periods=2))
            with self.subTest(profits=profits), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                metrics = write_backtest_analysis_v2(portfolio, path, equity_curve=equity, init_cash=100.)
                report = json.loads((path / "analysis_summary.json").read_text(encoding="utf-8"))
                self.assertIsNone(report["metrics"]["Asset Top 1 Net Profit Share [%]"])
                self.assertTrue(report["reconciled"])
                self.assertEqual(len(list(path.iterdir())), 5)
                self.assertTrue(pd.isna(metrics["Trade Top 5 Net Profit Share [%]"]))
                self.assertEqual(pd.read_csv(path / "side_contribution.csv").side.tolist(), ["long", "short"])

    def test_vectorbt_closed_and_open_marked_pnl_are_supported(self):
        from lib.backtest_engine_v2 import build_backtest_analysis_v2
        from lib.vectorbt_adapter import VectorBTSpec, run_portfolio_from_target_weights
        index = pd.date_range("2024-01-01", periods=3)
        prices = pd.DataFrame({"ETF": [100., 110., 120.]}, index=index)
        for close in (0., np.nan):
            with self.subTest(close=close):
                weights = pd.DataFrame({"ETF": [1., np.nan, close]}, index=index)
                portfolio = run_portfolio_from_target_weights(prices, weights,
                    VectorBTSpec(init_cash=1000., fees=.001, freq="D"))
                _, report, _ = build_backtest_analysis_v2(portfolio, equity_curve=portfolio.value(), init_cash=1000.)
                self.assertTrue(report["reconciled"], report)
                self.assertEqual(report["open_trade_records"], int(pd.isna(close)))


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


class DrawdownRecoveryV2Test(unittest.TestCase):
    def test_completed_recoveries_and_open_drawdown_count_observed_bars(self):
        index = pd.to_datetime([
            "2024-01-01", "2024-01-03", "2024-02-01", "2024-03-01", "2024-03-02",
            "2024-04-01", "2024-04-03", "2024-06-01", "2024-07-01",
        ], utc=True)
        curve = pd.Series([100., 90., 95., 100., 100., 80., 90., 100., 95.], index=index)
        actual = compute_drawdown_recovery_stats(curve)
        expected = {
            "Longest Peak-to-Recovery Bars": 3., "Second Longest Peak-to-Recovery Bars": 3.,
            "Longest Drawdown Duration Bars": 3., "Drawdown Duration P90 Bars": 3.,
            "Mean Top-5 Drawdown Duration Bars": 7 / 3, "Current Drawdown [%]": -5.,
            "Current Drawdown Bars": 1., "Underwater Time [%]": 500 / 9, "CDaR 95% [%]": 20.,
        }
        pd.testing.assert_series_equal(actual, pd.Series(expected), rtol=1e-12, atol=1e-12)

    def test_equal_highs_and_coerced_missing_values_preserve_bar_counts(self):
        for values, duration, underwater in [
            ([100., 100., 90., 100., 100., 100., 90.], 2., 200 / 7),
            ([np.nan, 100., "90", "missing", 100., 80.], 2., 50.),
        ]:
            with self.subTest(values=values):
                actual = compute_drawdown_recovery_stats(pd.Series(
                    values, index=pd.date_range("2024-01-01", periods=len(values), freq="h")))
                self.assertEqual(actual["Longest Peak-to-Recovery Bars"], duration)
                self.assertTrue(math.isnan(actual["Second Longest Peak-to-Recovery Bars"]))
                self.assertEqual(actual["Current Drawdown Bars"], 1.)
                self.assertAlmostEqual(actual["Underwater Time [%]"], underwater)

    def test_empty_flat_and_never_recovered_curves(self):
        for values in ([], [np.nan], [100.], [100., 100., 100.], [100., 90., 80.]):
            with self.subTest(values=values):
                actual = compute_drawdown_recovery_stats(pd.Series(
                    values, index=pd.date_range("2024-01-01", periods=len(values)), dtype=float))
                self.assertEqual(len(actual), 9)
                self.assertTrue(math.isnan(actual["Longest Peak-to-Recovery Bars"]))
                if not values or all(pd.isna(value) for value in values):
                    self.assertTrue(actual.isna().all())
                else:
                    expected_duration = 2. if values[-1] == 80. else 0.
                    self.assertEqual(actual["Current Drawdown Bars"], expected_duration)

    def test_duplicate_and_unordered_timestamps_keep_label_slice_semantics(self):
        for dates, duration in [
            (["2024-01-01", "2024-01-01", "2024-01-02"], 2.),
            (["2024-01-02", "2024-01-03", "2024-01-01"], 1.),
        ]:
            with self.subTest(dates=dates):
                actual = compute_drawdown_recovery_stats(pd.Series(
                    [100., 100., 90.], index=pd.to_datetime(dates)))
                self.assertEqual(actual["Current Drawdown Bars"], duration)


class UniverseMaskV2Test(unittest.TestCase):
    def test_allowed_excluded_lag_and_warning_masks_preserve_market_order(self):
        index = pd.date_range("2024-01-01", periods=4, tz="UTC")
        scores = pd.DataFrame({"ETH": [9., 9., 4., 9.], "BTC": [8., np.nan, 8., 8.],
                               "SOL": [7., 7., 7., 7.]}, index=index)
        original = scores.copy(deep=True)
        warnings = pd.DataFrame("NONE", index=index, columns=scores.columns)
        warnings.loc[index[-1], "BTC"] = "CAUTION"
        spec = UniverseSpec(feature_column="score", lag=1, mode="all", exclude_warnings=True,
                            allowed_markets=("ETH", "BTC"), excluded_markets=("ETH",))
        actual = build_universe_mask_v2({"score": scores}, warnings, spec)
        expected = pd.DataFrame(False, index=index, columns=sorted(scores.columns))
        expected.loc[index[1], "BTC"] = True
        pd.testing.assert_frame_equal(actual.base_eligible_mask, expected)
        pd.testing.assert_frame_equal(actual.selection_mask, expected)
        actual.base_eligible_mask.iloc[0, 0] = True
        self.assertFalse(actual.base_eligible_mask.iloc[2, 0])
        pd.testing.assert_frame_equal(scores, original)

    def test_empty_axes_and_cross_section_start(self):
        for rows, columns in ((0, ["BTC", "ETH"]), (3, []), (0, [])):
            with self.subTest(rows=rows, columns=columns):
                scores = pd.DataFrame(1., index=pd.date_range("2024-01-01", periods=rows), columns=columns)
                actual = build_universe_mask_v2({"score": scores}, pd.DataFrame(),
                                               UniverseSpec(feature_column="score", lag=0, mode="all"))
                pd.testing.assert_frame_equal(actual.selection_mask, scores.notna())
                pd.testing.assert_frame_equal(actual.base_eligible_mask, scores.notna())
        scores = pd.DataFrame({"BTC": [np.nan, 1., 1., np.nan], "ETH": [np.nan, np.nan, 2., 2.]},
                              index=pd.date_range("2024-01-01", periods=4))
        actual = build_universe_mask_v2({"score": scores}, pd.DataFrame(), UniverseSpec(
            feature_column="score", lag=0, mode="all", start_min_cross_section_size=2))
        expected = pd.DataFrame({"BTC": [False, False, True, False], "ETH": [False, False, True, True]},
                                index=scores.index)
        pd.testing.assert_frame_equal(actual.selection_mask, expected)


class HoldingSelectionV2Test(unittest.TestCase):
    def make_case(self, rows=9, **holding):
        from lib.spec_io import load_universe_spec_from_payload
        index = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
        scores = pd.DataFrame({"A": 5., "B": 4., "C": 3., "D": 2., "E": 1.}, index=index)
        frames = {"score": scores, "exit": scores * 0, "entry": scores * 0 + 1}
        payload = dict(feature_column="score", lag=0, mode="top_n", top_n=2,
                       value_filters=[dict(feature_column="entry", operator="eq", value=1)],
                       holding=dict(exit_feature="exit", **holding))
        return frames, load_universe_spec_from_payload(payload)

    def build(self, frames, spec, **kwargs):
        return build_universe_mask_v2(frames, pd.DataFrame(), spec, **kwargs).selection_mask

    def test_four_slots_keep_incumbents_then_fill_one_from_still_valid_candidates(self):
        from dataclasses import replace
        frames, spec = self.make_case()
        spec = replace(spec, top_n=4)
        frames["score"].loc[frames["score"].index[1]:, "E"] = 100.
        frames["entry"].loc[frames["score"].index[1]:, ["A", "B", "C", "D"]] = 0.
        frames["exit"].iloc[3, 2] = 1.
        result = self.build(frames, spec)
        self.assertEqual(result.iloc[0][lambda row: row].index.tolist(), ["A", "B", "C", "D"])
        pd.testing.assert_series_equal(result.iloc[1], result.iloc[0], check_names=False)
        self.assertEqual(result.iloc[3][lambda row: row].index.tolist(), ["A", "B", "D", "E"])
        self.assertTrue(result.sum(axis=1).eq(4).all())

    def test_timeout_is_per_admission_not_first_candidate_and_cooldown_counts_source_rows(self):
        frames, spec = self.make_case(max_holding_bars=4, cooldown_bars=1)
        frames["score"].loc[frames["score"].index[1]:, "C"] = 9.
        frames["exit"].iloc[2, 0] = 1.
        result = self.build(frames, spec)
        expected = [["A", "B"], ["A", "B"], ["B", "C"], ["B", "C"],
                    ["A", "C"], ["A", "C"], ["A", "B"], ["A", "B"], ["B", "C"]]
        for row, wanted in enumerate(expected):
            self.assertEqual(result.iloc[row][lambda value: value].index.tolist(), wanted)
        # C was eligible from row 0, but entered at row 2 and expired at row 6.

    def test_exit_true_blocks_entry_and_same_row_exit_cannot_reenter(self):
        frames, spec = self.make_case()
        frames["exit"].loc[:, ["C", "D", "E"]] = 1.
        frames["exit"].iloc[2:5, 0] = 1.
        result = self.build(frames, spec)
        self.assertFalse(result.A.iloc[2:5].any())
        self.assertTrue(result.A.iloc[5])
        timed_frames, timed_spec = self.make_case(max_holding_bars=1)
        timed_frames["exit"].loc[:, ["B", "C", "D", "E"]] = 1.
        self.assertEqual(self.build(timed_frames, timed_spec).A.tolist(), [True, False, True, False, True, False, True, False, True])

    def test_entry_screen_rank_and_warning_changes_do_not_exit_but_data_end_does(self):
        from lib.dataframes import CACHE_ELIGIBILITY_FRAME
        from lib.universe_v2 import ENTRY_ELIGIBILITY_FRAME
        from dataclasses import replace
        frames, spec = self.make_case()
        spec = replace(spec, exclude_warnings=True)
        entry_screen = frames["score"].notna()
        entry_screen.loc[entry_screen.index[1]:, "A"] = False
        frames[ENTRY_ELIGIBILITY_FRAME] = entry_screen
        frames["score"].loc[entry_screen.index[1]:, "A"] = np.nan
        usage = entry_screen * False + True
        usage.loc[usage.index[4]:, "A"] = False
        frames[CACHE_ELIGIBILITY_FRAME] = usage
        warnings = pd.DataFrame("NONE", index=entry_screen.index, columns=entry_screen.columns)
        warnings.loc[warnings.index[1]:, "B"] = "CAUTION"
        result = build_universe_mask_v2(frames, warnings, spec).selection_mask
        self.assertTrue(result.A.iloc[:4].all())
        self.assertFalse(result.A.iloc[4:].any())
        self.assertTrue(result.B.all())

    def test_missing_exit_is_not_treated_as_false_and_known_timeout_can_close(self):
        frames, spec = self.make_case()
        frames["exit"].iloc[0, 0] = np.nan
        self.assertFalse(self.build(frames, spec).A.iloc[0])
        frames["exit"].iloc[0, 0] = 0.
        frames["exit"].iloc[2, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "holding exit signal for A"):
            self.build(frames, spec)
        frames, spec = self.make_case(max_holding_bars=2)
        frames["exit"].iloc[2, 0] = np.nan
        self.assertFalse(self.build(frames, spec).A.iloc[2])

    def test_signal_lag_applies_once_and_prefix_and_column_order_do_not_change_selection(self):
        from dataclasses import replace
        frames, spec = self.make_case()
        spec = replace(spec, signal_lag=1)
        frames["exit"].iloc[2, 0] = 1.
        result = self.build(frames, spec)
        self.assertFalse(result.iloc[0].any())
        self.assertTrue(result.A.iloc[1:3].all())
        self.assertFalse(result.A.iloc[3])
        pd.testing.assert_frame_equal(result.iloc[:5], self.build({k: v.iloc[:5] for k, v in frames.items()}, spec))
        tied = {k: v.copy() for k, v in frames.items()}
        tied["score"].loc[:, :] = 1.
        original = self.build(tied, spec)
        permuted = self.build({k: v.loc[:, list(reversed(v.columns))] for k, v in tied.items()}, spec)
        pd.testing.assert_frame_equal(original, permuted.reindex(columns=original.columns))

    def test_fresh_window_starts_empty_without_removing_lag_warmup(self):
        from dataclasses import replace
        frames, spec = self.make_case(max_holding_bars=4)
        spec = replace(spec, signal_lag=1)
        start = frames["score"].index[5]
        result = self.build(frames, spec, selection_start=start)
        self.assertFalse(result.iloc[:5].any().any())
        self.assertTrue(result.A.iloc[5:].all())
        self.assertTrue(result.B.iloc[5:].all())

    def test_config_validation_and_incompatible_double_clocks(self):
        from lib.spec_io import load_universe_spec_from_payload, load_weight_spec_from_payload
        from lib.universe_v2 import validate_holding_selection_config
        frames, spec = self.make_case()
        base = dict(feature_column="score", lag=0, mode="top_n", top_n=2)
        for change in ({}, {"exit_feature": "exit", "cooldown_bars": True},
                       {"exit_feature": "exit", "max_holding_bars": 0},
                       {"exit_feature": "exit", "max_holding_bars": "4"},
                       {"exit_feature": "exit", "exit_lag": -1}, {"exit_feature": "exit", "typo": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_universe_spec_from_payload(dict(base, holding=change))
        for change in (dict(mode="all"), dict(top_n=True), dict(lag=-1), dict(scope="global")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_universe_spec_from_payload(dict(base, **change, holding=dict(exit_feature="exit")))
        good = dict(weighting="fixed", fixed_weight=-.25, rebalance_frequency="change_only")
        validate_holding_selection_config(spec, load_weight_spec_from_payload(good), {})
        for change in (dict(weighting="equal"), dict(position_score_feature="held"),
                       dict(rebalance_frequency="daily"), dict(gross_exposure_update_frequency="daily")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_holding_selection_config(spec, load_weight_spec_from_payload(dict(good, **change)), {})
        with self.assertRaisesRegex(ValueError, "max_holding_hours"):
            validate_holding_selection_config(spec, load_weight_spec_from_payload(good), {"numba_futures": {"max_holding_hours": 24}})

    def test_entry_preselection_preserves_actual_data_mask_and_cache_tracks_start(self):
        from lib.dataframes import CACHE_ELIGIBILITY_FRAME
        from lib.universe_v2 import ENTRY_ELIGIBILITY_FRAME
        from scripts.run_cross_section_grid_v2 import _preselection_universe_frames, _universe_cache_key
        frames, spec = self.make_case()
        usage = frames["score"].notna()
        frames[CACHE_ELIGIBILITY_FRAME] = usage
        screen = usage.copy()
        screen.iloc[1:, 0] = False
        projected = _preselection_universe_frames(frames, spec, {"mask": screen})
        pd.testing.assert_frame_equal(projected[CACHE_ELIGIBILITY_FRAME], usage)
        pd.testing.assert_frame_equal(projected[ENTRY_ELIGIBILITY_FRAME], screen)
        self.assertTrue(self.build(projected, spec).A.all())
        payload = dict(feature_column="score", holding=dict(exit_feature="exit"))
        first = _universe_cache_key(payload, {}, (), usage.index,
                                    dict(trim_start_mode="timestamp", trim_start_timestamp=str(usage.index[0])))
        later = _universe_cache_key(payload, {}, (), usage.index,
                                    dict(trim_start_mode="timestamp", trim_start_timestamp=str(usage.index[4])))
        self.assertNotEqual(first, later)

    def test_rank_cache_does_not_mix_holding_entry_validation_with_snapshot_filters(self):
        from dataclasses import replace
        from lib.specs import ValueFilterSpec, RankFilterSpec
        frames, spec = self.make_case()
        frames["entry"].loc[:, "A"] = np.nan
        spec = replace(spec, top_n=1, value_filters=(ValueFilterSpec("entry", "ne", 0.),),
                       rank_filters=(RankFilterSpec("score", top_n=1),))
        cache = {}
        build_universe_mask_v2(frames, pd.DataFrame(), replace(spec, holding=None),
                               rank_cache=cache, rank_cache_namespace=("shared",))
        actual = build_universe_mask_v2(frames, pd.DataFrame(), spec,
                                        rank_cache=cache, rank_cache_namespace=("shared",)).selection_mask
        self.assertTrue(actual.B.all())
        self.assertFalse(actual.A.any())


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
