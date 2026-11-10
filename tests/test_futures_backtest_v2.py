from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from lib.backtest_engine_v2 import (
    FuturesBankruptcyError, normalize_artifact_format, read_backtest_curve,
    resolve_backtest_artifact, run_backtest_v2, write_backtest_frame,
)
from lib.vectorbt_adapter import VectorBTSpec, run_portfolio_from_target_weights
from scripts.run_cross_section_grid_v2 import _render_backtest_payload


def write_futures_fixture(directory: Path, *, periods: int = 26, markets=None):
    """Synthetic prices and settlement events, never a market-performance test."""
    markets = markets or ["BTC/USDT:USDT"]
    index = pd.date_range("2024-01-01 23:00", periods=periods, freq="h")
    opening = pd.DataFrame({market: 100.0 + i * 10 for i, market in enumerate(markets)}, index=index)
    frames = {
        "opening_price": opening, "trade_price": opening,
        "high_price": opening + 0.5, "low_price": opening - 0.5,
        "candle_acc_trade_volume": opening * 100, "mark_price": opening,
    }
    funding = opening * np.nan
    funding.loc[funding.index.hour % 8 == 0] = 0.001
    frames["funding_rate"] = funding
    for name, frame in frames.items():
        frame.to_parquet(directory / f"{name}.parquet")
    return frames


def offline_binance(config, *args, **kwargs):
    """Real Binance/FT accounting with deterministic, synthetic contract metadata."""
    from freqtrade.exchange import Binance
    exchange = Binance(config, validate=False, load_leverage_tiers=False)
    markets = {}
    for pair in config["exchange"]["pair_whitelist"]:
        base = pair.split("/")[0]
        markets[pair] = {
            "id": base + "USDT", "symbol": pair, "base": base, "quote": "USDT", "settle": "USDT",
            "active": True, "type": "swap", "spot": False, "margin": False,
            "swap": True, "future": False, "contract": True, "linear": True,
            "inverse": False, "contractSize": 1.0,
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {"amount": {"min": 0.001, "max": 1e9}, "cost": {"min": 0.01, "max": None}, "price": {"min": 0.01, "max": None}, "leverage": {"min": 1, "max": 125}},
            "maker": 0.0005, "taker": 0.0005,
            "info": {"orderTypes": ["MARKET", "LIMIT", "STOP_MARKET"]},
        }
        exchange._leverage_tiers[pair] = [{"minNotional": 0.0, "maxNotional": 1e12, "maintenanceMarginRate": 0.004, "maxLeverage": 125.0, "maintAmt": 0.0}]
    exchange._markets = markets
    exchange._api.markets = markets
    exchange._api_async.markets = markets
    return exchange


class BacktestDispatchV2Test(unittest.TestCase):
    def test_bankruptcy_is_not_a_nonfinite_account_error(self):
        for value in (float("nan"), float("inf"), 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                FuturesBankruptcyError("2024-01-01", value)
            self.assertNotIsInstance(raised.exception, FuturesBankruptcyError)

    def test_default_preserves_vectorbt(self):
        prices = pd.DataFrame({"TEST": [100., 110., 90., 120.]}, index=pd.date_range("2024-01-01", periods=4))
        weights = pd.DataFrame({"TEST": [1., np.nan, np.nan, 0.]}, index=prices.index)
        spec = VectorBTSpec(init_cash=1000, fees=0.001, freq="1D")
        expected = run_portfolio_from_target_weights(prices, weights, spec)
        actual = run_backtest_v2(prices, weights, spec, payload={}, source_cache_dir=Path("unused"), timeframe="daily")
        pd.testing.assert_series_equal(actual.value(), expected.value())

    def test_engine_and_config_errors(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            run_backtest_v2(pd.DataFrame(), pd.DataFrame(), VectorBTSpec(), payload={"engine": "typo"}, source_cache_dir=Path("unused"), timeframe="daily")
        with self.assertRaisesRegex(ValueError, "not both"):
            _render_backtest_payload({"backtest_spec_template": {}, "vectorbt_spec_template": {}}, {})
        payload = {"engine": "freqtrade", "freqtrade": {"leverage": "{leverage}"}}
        self.assertEqual(_render_backtest_payload({"backtest_spec_template": payload}, {"leverage": 3})["freqtrade"]["leverage"], 3)


class ArtifactStorageV2Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v2_artifacts_")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.curve = pd.Series(
            1000 + np.sin(np.arange(400) / 5) * 5 + np.arange(400) * .1,
            index=pd.date_range("2024-01-01", periods=400, freq="D", name="date_utc"),
            name="portfolio_value",
        )

    def test_csv_and_zstd_parquet_preserve_float_values_and_timestamps(self):
        import pyarrow.parquet as pq
        for artifact_format in ("csv", "parquet"):
            with self.subTest(artifact_format=artifact_format):
                curve = self.curve.copy()
                curve.index = curve.index.tz_localize("UTC")
                curve.iloc[5] = np.nan
                path = write_backtest_frame(self.directory / "curve.csv", curve, artifact_format=artifact_format)
                actual = read_backtest_curve(path)
                pd.testing.assert_frame_equal(actual, curve.to_frame(), check_exact=True, check_freq=False)
                if artifact_format == "parquet":
                    self.assertEqual(pq.ParquetFile(path).metadata.row_group(0).column(0).compression, "ZSTD")
        self.assertEqual(normalize_artifact_format("PARQUET"), "parquet")
        default_path = write_backtest_frame(self.directory / "default.csv", self.curve)
        self.assertEqual(default_path.suffix, ".parquet")
        self.assertFalse(default_path.with_suffix(".csv").exists())
        self.assertEqual(pq.ParquetFile(default_path).metadata.row_group(0).column(0).compression, "ZSTD")
        with self.assertRaisesRegex(ValueError, "artifact_format"):
            write_backtest_frame(self.directory / "bad.csv", self.curve, artifact_format="pickle")
        self.assertFalse((self.directory / "bad.csv").exists())

    def test_indexed_account_with_multiple_columns_roundtrips_exactly(self):
        account = pd.DataFrame({"equity": self.curve, "funding_pnl": -.001, "wallet_balance": self.curve - 5})
        account.index.name = None
        path = write_backtest_frame(self.directory / "futures_account.csv", account,
                                    artifact_format="parquet", index_label="date_utc")
        pd.testing.assert_frame_equal(read_backtest_curve(path), account.rename_axis("date_utc"),
                                      check_exact=True, check_freq=False)

    def test_summary_selects_current_format_and_never_falls_back_to_stale_output(self):
        path = self.directory / "equity_curve.csv"
        write_backtest_frame(path, self.curve, artifact_format="csv")
        write_backtest_frame(path, self.curve * 2, artifact_format="parquet")
        pd.Series({"Artifact Format": "parquet"}).to_frame("value").to_csv(self.directory / "summary.csv")
        self.assertEqual(resolve_backtest_artifact(path, prefer_summary=True).suffix, ".parquet")
        pd.testing.assert_series_equal(read_backtest_curve(path, prefer_summary=True).iloc[:, 0],
                                       self.curve * 2, check_exact=True, check_freq=False)
        pd.testing.assert_series_equal(read_backtest_curve(path).iloc[:, 0], self.curve,
                                       check_exact=True, check_freq=False)
        pd.Series({"Artifact Format": "csv"}).to_frame("value").to_csv(self.directory / "summary.csv")
        self.assertEqual(resolve_backtest_artifact(path, prefer_summary=True), path)
        # A declared current format missing on disk must not silently use the older CSV.
        write_backtest_frame(self.directory / "benchmark_curve.csv", self.curve, artifact_format="csv")
        pd.Series({"Artifact Format": "parquet"}).to_frame("value").to_csv(self.directory / "summary.csv")
        with self.assertRaisesRegex(FileNotFoundError, "declared"):
            resolve_backtest_artifact(self.directory / "benchmark_curve.csv", prefer_summary=True)

    def test_analysis_readers_follow_summary_when_old_and_current_formats_coexist(self):
        from scripts.research import analyze_strategy_blend_v2 as blend
        from scripts.research import analyze_strategy_uncertainty_v2 as uncertainty
        from scripts.research import analyze_market_regime_transitions_v2 as regimes

        readers = (
            lambda path: uncertainty._read_curve(path, "strategy"),
            lambda path: blend._read_equity_curve(blend.SourceSpec("strategy", path)),
            lambda path: regimes._read_equity_curve(path, value_column="portfolio_value"),
        )
        path = self.directory / "equity_curve.csv"
        write_backtest_frame(path, self.curve, artifact_format="csv")
        write_backtest_frame(path, self.curve * 2, artifact_format="parquet")
        for current_format, multiplier in (("parquet", 2), ("csv", 1)):
            pd.Series({"Artifact Format": current_format}).to_frame("value").to_csv(self.directory / "summary.csv")
            for read in readers:
                for requested_format in ("csv", "parquet"):
                    with self.subTest(current=current_format, reader=read, requested=requested_format):
                        pd.testing.assert_series_equal(
                            read(path.with_suffix("." + requested_format)), self.curve * multiplier,
                            check_exact=True, check_freq=False, check_names=False,
                        )
        # Missing declared output is an error even when another format remains on disk.
        missing = self.directory / "missing" / "equity_curve.csv"
        write_backtest_frame(missing, self.curve, artifact_format="csv")
        pd.Series({"Artifact Format": "parquet"}).to_frame("value").to_csv(missing.parent / "summary.csv")
        for read in readers:
            with self.subTest(reader=read), self.assertRaisesRegex(FileNotFoundError, "declared"):
                read(missing)

    def test_existing_v2_readers_accept_csv_parquet_and_missing_suffix_fallback(self):
        from scripts.research import analyze_strategy_blend_v2 as blend
        from scripts.research import analyze_strategy_uncertainty_v2 as uncertainty
        from scripts.research import analyze_market_regime_transitions_v2 as regimes
        curves = {}
        for artifact_format in ("csv", "parquet"):
            path = write_backtest_frame(self.directory / artifact_format / "curve.csv", self.curve,
                                        artifact_format=artifact_format)
            curves[artifact_format] = (
                uncertainty._read_curve(path, "strategy"),
                blend._read_equity_curve(blend.SourceSpec("strategy", path)),
                regimes._read_equity_curve(path, value_column="portfolio_value"),
            )
        for csv, parquet in zip(curves["csv"], curves["parquet"]):
            pd.testing.assert_series_equal(csv, parquet, check_exact=True, check_freq=False)
        fallback = self.directory / "parquet" / "curve.csv"
        pd.testing.assert_frame_equal(read_backtest_curve(fallback), self.curve.to_frame(),
                                      check_exact=True, check_freq=False)
        # Preserve each reader's existing checks instead of accepting bad curves.
        duplicates = pd.concat([self.curve.iloc[:3], self.curve.iloc[:1]])
        for artifact_format in ("csv", "parquet"):
            path = write_backtest_frame(self.directory / artifact_format / "duplicates.csv", duplicates,
                                        artifact_format=artifact_format)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                uncertainty._read_curve(path, "strategy")


@unittest.skipUnless(importlib.util.find_spec("freqtrade"), "optional freqtrade dependencies not installed")
class NativeFuturesV2Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v2_futures_")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.frames = write_futures_fixture(self.directory)
        self.prices = self.frames["opening_price"].iloc[1:]
        self.weights = self.prices * np.nan
        self.spec = VectorBTSpec(price_column="opening_price", init_cash=1000, fees=0.0005, freq="h")
        self.settings = {"metadata_dir": str(self.directory / "metadata")}
        self.exchange_patch = patch("freqtrade.resolvers.ExchangeResolver.load_exchange", side_effect=offline_binance)
        self.exchange_patch.start()
        self.addCleanup(self.exchange_patch.stop)

    def run_futures(self):
        return run_backtest_v2(self.prices, self.weights, self.spec, payload={"engine": "freqtrade", "freqtrade": self.settings}, source_cache_dir=self.directory, timeframe="60m")

    def test_long_short_partial_adjustments_and_account(self):
        self.weights.iloc[0, 0] = 0.5
        self.weights.iloc[3, 0] = 1.0
        self.weights.iloc[6, 0] = 0.25
        self.weights.iloc[10, 0] = -0.5
        self.weights.iloc[17, 0] = 0.0
        result = self.run_futures()
        self.assertEqual(result.summary["Execution Engine"], "freqtrade")
        self.assertEqual(set(result.trades["is_short"]), {False, True})
        self.assertEqual(result.orders["date_utc"].min().replace(tzinfo=None), self.prices.index[0])
        self.assertEqual(len(result.orders), 6)
        self.assertEqual(result.equity.iloc[0], self.spec.init_cash)
        self.assertTrue(np.isfinite(result.equity).all())
        self.assertGreater(result.orders["fee"].sum(), 0)
        self.assertTrue(np.allclose(result.equity.iloc[18:], result.equity.iloc[-1]))
        result.save_artifacts(self.directory / "results")
        self.assertTrue((self.directory / "results" / "futures_account.parquet").exists())
        self.assertFalse((self.directory / "results" / "futures_account.csv").exists())

    def test_mark_valuation_and_funding_before_rebalance(self):
        marks = self.frames["mark_price"].copy()
        marks.iloc[3, 0] = 110.0
        marks.to_parquet(self.directory / "mark_price.parquet")
        self.weights.iloc[0, 0] = 0.5
        self.weights.iloc[10, 0] = 0.0
        result = self.run_futures()
        self.assertAlmostEqual(result.equity.iloc[2], 1049.75, places=6)
        self.assertAlmostEqual(result.account["funding_pnl"].iloc[8], -0.5, places=6)

    def test_funding_at_resize_and_reversal_is_charged_once(self):
        self.weights.iloc[0, 0] = 0.5
        self.weights.iloc[8, 0] = 0.25
        self.weights.iloc[16, 0] = -0.5
        self.weights.iloc[23, 0] = 0.0
        result = self.run_futures()
        # First entry at UTC00 is after settlement. At UTC08 the original
        # quantity pays once; UTC16 pays once before the position reverses.
        self.assertAlmostEqual(result.account["funding_pnl"].iloc[8], -0.5, places=7)
        partial_exit = result.orders[(result.orders["date_utc"] == pd.Timestamp(self.prices.index[8], tz="UTC")) & (result.orders["side"] == "sell")].iloc[0]
        remaining = 5.0 - partial_exit["amount"]
        self.assertAlmostEqual(result.account["funding_pnl"].iloc[16], -0.5 - remaining * 100 * .001, places=7)
        self.assertAlmostEqual(result.summary["Funding PnL"], -0.5 - remaining * 100 * .001, places=7)

    def test_partial_exit_uses_quantity_at_changed_price(self):
        for name in ("opening_price", "trade_price", "mark_price", "high_price", "low_price"):
            frame = self.frames[name].copy()
            frame.iloc[4:] += 20
            frame.to_parquet(self.directory / f"{name}.parquet")
        self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
        self.weights.iloc[0, 0] = .5
        self.weights.iloc[3, 0] = .25
        self.weights.iloc[10, 0] = 0
        result = self.run_futures()
        at_resize = result.orders[result.orders["date_utc"] == pd.Timestamp(self.prices.index[3], tz="UTC")]
        self.assertEqual(len(at_resize), 1)
        self.assertAlmostEqual(at_resize.iloc[0]["amount"], 2.709, places=9)
        # Buy 5 @100; total sell 5 @120; fees .25+.30; funding once
        # on the remaining 2.291 @120: hand-computed final equity.
        self.assertAlmostEqual(result.equity.iloc[-1], 1099.17508, places=7)

    def test_resize_then_intrabar_liquidation_does_not_reenter_at_open(self):
        self.weights.iloc[0, 0] = .5
        self.weights.iloc[3, 0] = .25
        low = self.frames["low_price"].copy()
        low.iloc[4, 0] = 40
        low.to_parquet(self.directory / "low_price.parquet")
        result = self.run_futures()
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["exit_reason"], "liquidation")
        self.assertEqual(list(result.orders["side"]), ["buy", "sell", "sell"])

    def test_market_exit_uses_historical_open_at_candle_boundary(self):
        # Today's 0.01 price tick would round a long exit above the historical
        # candle high, or a short exit below its low. Both must still fill at open.
        for direction, rate in [(1, 100.006), (-1, 100.004)]:
            with self.subTest(direction=direction):
                for name in ("opening_price", "trade_price", "mark_price"):
                    frame = self.frames[name].copy()
                    frame.iloc[4, 0] = rate
                    frame.to_parquet(self.directory / f"{name}.parquet")
                high = self.frames["high_price"].copy()
                low = self.frames["low_price"].copy()
                (high if direction > 0 else low).iloc[4, 0] = rate
                high.to_parquet(self.directory / "high_price.parquet")
                low.to_parquet(self.directory / "low_price.parquet")
                self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
                self.weights[:] = np.nan
                self.weights.iloc[0, 0] = direction * .5
                self.weights.iloc[3, 0] = 0
                result = self.run_futures()
                self.assertEqual(len(result.orders), 2)
                exit_order = result.orders.iloc[-1]
                self.assertEqual(exit_order["date_utc"], pd.Timestamp(self.prices.index[3], tz="UTC"))
                self.assertEqual(exit_order["price"], rate)
                self.assertAlmostEqual(result.equity.iloc[-1], 1000 + direction * 5 * (rate - 100) - .0005 * 5 * (100 + rate), places=8)

    def test_final_force_exit_uses_unrounded_historical_open(self):
        for name in ("opening_price", "trade_price", "mark_price", "high_price"):
            frame = self.frames[name].copy()
            frame.iloc[-1, 0] = 100.006
            frame.to_parquet(self.directory / f"{name}.parquet")
        self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
        self.weights.iloc[0, 0] = .5
        result = self.run_futures()
        self.assertEqual(result.trades.iloc[0]["exit_reason"], "force_exit")
        self.assertEqual(result.orders.iloc[-1]["price"], 100.006)
        self.assertEqual(result.orders.iloc[-1]["date_utc"], pd.Timestamp(self.prices.index[-1], tz="UTC"))

    def test_quantity_survives_native_stake_roundtrip_without_topup(self):
        rate = 47882.14
        for name in ("opening_price", "trade_price", "mark_price", "high_price", "low_price"):
            frame = self.frames[name] * 0 + rate
            if name == "high_price":
                frame += 1
            if name == "low_price":
                frame -= 1
            frame.to_parquet(self.directory / f"{name}.parquet")
        self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
        self.spec = VectorBTSpec(price_column="opening_price", init_cash=11830, fees=.0005, freq="h")
        self.weights.iloc[0, 0] = 1
        self.weights.iloc[3, 0] = 0
        # .247 -> stake -> .24699999999999997 previously caused a .001 top-up.
        for step in (.001, .247):
            with self.subTest(quantity_step=step):
                def stepped_exchange(config, *args, **kwargs):
                    exchange = offline_binance(config, *args, **kwargs)
                    market = exchange._markets["BTC/USDT:USDT"]
                    market["precision"]["amount"] = step
                    market["limits"]["amount"]["min"] = 0
                    return exchange

                with patch("freqtrade.resolvers.ExchangeResolver.load_exchange", side_effect=stepped_exchange):
                    result = self.run_futures()
                self.assertEqual(list(result.orders["amount"]), [.247, .247])
                self.assertEqual(result.trades.iloc[0]["nr_of_successful_entries"], 1)
                self.assertEqual(result.trades.iloc[0]["nr_of_successful_exits"], 1)
                self.assertAlmostEqual(result.summary["Total Fees Paid"], 2 * .247 * rate * .0005, places=8)

    def test_pending_entry_retries_fixed_quantity_without_resizing_holdings(self):
        def limited_exchange(config, *args, **kwargs):
            exchange = offline_binance(config, *args, **kwargs)
            exchange._markets["BTC/USDT:USDT"]["limits"]["cost"]["min"] = 50
            return exchange

        # Target .1 BTC is initially below minimum notional. When price rises,
        # retry .1 BTC, not .01*equity/new_price. NaN must preserve that request.
        for name in ("opening_price", "trade_price", "mark_price", "high_price", "low_price"):
            frame = self.frames[name].copy()
            frame.iloc[5:] += 900
            frame.to_parquet(self.directory / f"{name}.parquet")
        self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
        self.weights.iloc[0, 0] = .01
        self.weights.iloc[10, 0] = 0
        with patch("freqtrade.resolvers.ExchangeResolver.load_exchange", side_effect=limited_exchange):
            result = self.run_futures()
        self.assertEqual(len(result.orders), 2)
        self.assertEqual(list(result.orders["amount"]), [.1, .1])
        self.assertEqual(result.orders.iloc[0]["date_utc"], pd.Timestamp(self.prices.index[4], tz="UTC"))
        self.assertGreater(result.summary["Rejected Below Minimum"], 0)
        self.assertEqual(result.summary["Unfilled Target Requests"], 0)

    def test_new_cash_target_cancels_pending_entry(self):
        def limited_exchange(config, *args, **kwargs):
            exchange = offline_binance(config, *args, **kwargs)
            exchange._markets["BTC/USDT:USDT"]["limits"]["cost"]["min"] = 50
            return exchange

        self.weights.iloc[0, 0] = .045
        self.weights.iloc[3, 0] = 0
        with patch("freqtrade.resolvers.ExchangeResolver.load_exchange", side_effect=limited_exchange):
            result = self.run_futures()
        # Native validation used to raise this to the minimum (+~23% exposure).
        self.assertTrue(result.orders.empty)
        self.assertEqual(result.summary["Rejected Below Minimum"], 3)
        self.assertEqual(result.summary["Rejected Signals"], 3)
        self.assertEqual(result.summary["Unfilled Target Requests"], 1)

    def test_below_quantity_precision_is_visible(self):
        self.weights.iloc[0, 0] = 1e-8
        self.weights.iloc[3, 0] = 0
        result = self.run_futures()
        self.assertTrue(result.orders.empty)
        self.assertEqual(result.summary["Rejected Below Quantity Precision"], 1)
        self.assertEqual(result.summary["Rejected Signals"], 1)
        self.assertEqual(result.summary["Unfilled Target Requests"], 1)

    def test_rejected_request_is_counted_at_end(self):
        def limited_exchange(config, *args, **kwargs):
            exchange = offline_binance(config, *args, **kwargs)
            exchange._markets["BTC/USDT:USDT"]["limits"]["cost"]["min"] = 50
            return exchange

        self.weights.iloc[0, 0] = .01
        with patch("freqtrade.resolvers.ExchangeResolver.load_exchange", side_effect=limited_exchange):
            result = self.run_futures()
        self.assertTrue(result.orders.empty)
        self.assertGreater(result.summary["Rejected Below Minimum"], 0)
        self.assertEqual(result.summary["Unfilled Target Requests"], 1)

    def test_insufficient_margin_does_not_silently_shrink_target(self):
        self.weights.iloc[0, 0] = .5  # 5 BTC @100 requires 250 margin at 2x.
        self.weights.iloc[3, 0] = 0
        # Model margin reserved elsewhere without changing the native fill or
        # validation code: a 250 request must not silently become a 100 stake.
        with patch("freqtrade.wallets.Wallets.get_available_stake_amount", return_value=100.0):
            result = self.run_futures()
        self.assertTrue(result.orders.empty)
        self.assertEqual(result.summary["Rejected Insufficient Margin"], 3)
        self.assertEqual(result.summary["Unfilled Target Requests"], 1)

    def test_rejected_reduction_is_counted_once_per_bar(self):
        self.weights.iloc[0, 0] = .5
        # Leave .001 BTC, below the native remaining-position reserve limit.
        self.weights.iloc[3, 0] = .0001001
        self.weights.iloc[7, 0] = 0
        result = self.run_futures()
        self.assertEqual(list(result.orders["amount"]), [5.0, 5.0])
        self.assertEqual(result.summary["Rejected Reduction Below Minimum"], 4)
        self.assertEqual(result.summary["Unfilled Target Requests"], 1)

    def test_missing_prices_and_funding_fail(self):
        self.weights.iloc[0, 0] = 0.5
        funding = self.frames["funding_rate"].copy()
        funding.iloc[9, 0] = np.nan  # scheduled UTC08 settlement
        funding.to_parquet(self.directory / "funding_rate.parquet")
        with self.assertRaisesRegex(ValueError, "Missing scheduled funding"):
            self.run_futures()
        self.frames["funding_rate"].to_parquet(self.directory / "funding_rate.parquet")
        (self.directory / "mark_price.parquet").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "mark_price"):
            self.run_futures()

    def variable_funding_fixture(self):
        self.settings["funding_schedule"] = [
            {"start": "2024-01-02T00:00:00Z", "interval_hours": 8},
            {"start": "2024-01-02T12:00:00Z", "interval_hours": 4},
            {"start": "2024-01-02T18:00:00Z", "interval_hours": 2},
            {"start": "2024-01-03T00:00:00Z", "interval_hours": 8},
        ]
        events = pd.DatetimeIndex([
            "2024-01-02 00:00", "2024-01-02 08:00", "2024-01-02 12:00", "2024-01-02 16:00",
            "2024-01-02 18:00", "2024-01-02 20:00", "2024-01-02 22:00", "2024-01-03 00:00",
        ])
        funding = self.frames["funding_rate"] * np.nan
        funding.loc[events] = .001
        funding.to_parquet(self.directory / "funding_rate.parquet")
        return events, funding

    def test_variable_funding_matches_signed_quantity_before_resize_reversal_and_exit(self):
        events, _ = self.variable_funding_fixture()
        self.weights.iloc[0, 0] = .5
        self.weights.iloc[12, 0] = .25
        self.weights.iloc[18, 0] = -.5
        self.weights.iloc[22, 0] = 0
        result = self.run_futures()
        total = 0.0
        for event in events:
            # Funding is before same-time orders, including initial entry and final exit.
            prior = result.orders[result.orders.date_utc < event.tz_localize("UTC")]
            quantity = prior.amount.where(prior.side.eq("buy"), -prior.amount).sum()
            total -= quantity * 100 * .001
            self.assertAlmostEqual(result.account.loc[event, "funding_pnl"], total, places=7)
        self.assertAlmostEqual(result.summary["Funding PnL"], total, places=7)
        self.assertEqual(result.summary["Funding Schedule Mode"], "segments")
        self.assertEqual(result.summary["Funding Settlement Events"], 8)

    def test_variable_funding_missing_extra_and_coarse_bars_are_rejected(self):
        _, original = self.variable_funding_fixture()
        self.weights.iloc[0, 0] = .5
        missing = original.copy()
        missing.loc["2024-01-02 20:00"] = np.nan
        missing.to_parquet(self.directory / "funding_rate.parquet")
        with self.assertRaisesRegex(ValueError, "Missing scheduled funding"):
            self.run_futures()
        extra = original.copy()
        extra.loc["2024-01-02 14:00"] = 0
        extra.to_parquet(self.directory / "funding_rate.parquet")
        with self.assertRaisesRegex(ValueError, "Unexpected funding"):
            self.run_futures()
        original.to_parquet(self.directory / "funding_rate.parquet")
        prices = self.frames["opening_price"].iloc[5::4]
        weights = prices * np.nan
        weights.iloc[0] = .5
        with self.assertRaisesRegex(ValueError, "align with"):
            run_backtest_v2(prices, weights, self.spec,
                            payload={"engine": "freqtrade", "freqtrade": self.settings},
                            source_cache_dir=self.directory, timeframe="240m")

    def test_fixed_interval_rejects_unexpected_funding(self):
        funding = self.frames["funding_rate"].copy()
        funding.loc["2024-01-02 04:00"] = 0
        funding.to_parquet(self.directory / "funding_rate.parquet")
        with self.assertRaisesRegex(ValueError, "Unexpected funding"):
            self.run_futures()

    def test_multiasset_reduction_releases_margin_before_increase(self):
        self.frames = write_futures_fixture(self.directory, markets=["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.prices = self.frames["opening_price"].iloc[1:]
        self.weights = self.prices * np.nan
        self.weights.iloc[0] = [0.9, 0.9]
        self.weights.iloc[3] = [1.7, 0.1]
        self.weights.iloc[6] = [0.0, 0.0]
        result = self.run_futures()
        at_rebalance = result.orders[result.orders["date_utc"] == pd.Timestamp(self.prices.index[3], tz="UTC")]
        self.assertEqual(list(at_rebalance["side"]), ["sell", "buy"])
        self.assertEqual(result.summary["Rejected Signals"], 0)

    def test_native_liquidation_and_cross_mode(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                self.weights[:] = np.nan
                self.weights.iloc[0, 0] = direction * .5
                low = self.frames["low_price"].copy()
                high = self.frames["high_price"].copy()
                if direction > 0:
                    low.iloc[4, 0] = 40
                else:
                    # Even a candle crossing both a nominal strategy stop and
                    # liquidation must use the native liquidation price.
                    high.iloc[4, 0] = 10000
                low.to_parquet(self.directory / "low_price.parquet")
                high.to_parquet(self.directory / "high_price.parquet")
                result = self.run_futures()
                self.assertEqual(result.trades.iloc[0]["exit_reason"], "liquidation")
                self.assertLess(result.equity.iloc[-1], 800)
        for name in ("low_price", "high_price"):
            self.frames[name].to_parquet(self.directory / f"{name}.parquet")
        self.settings["margin_mode"] = "cross"
        result = self.run_futures()
        self.assertEqual(result.summary["Margin Mode"], "cross")

    def test_cross_wallet_costs_are_charged_once_through_resizes_and_reversal(self):
        self.settings.update(margin_mode="cross", leverage=5)
        self.weights.iloc[0, 0] = .5
        self.weights.iloc[3, 0] = .25
        self.weights.iloc[5, 0] = .75
        self.weights.iloc[10, 0] = -.5
        self.weights.iloc[17, 0] = 0
        result = self.run_futures()
        # Prices stay at 100. Independently reconstruct cash costs from actual
        # fills and scheduled funding, including funding before same-time exits.
        orders = result.orders
        expected_funding = 0.0
        for i, timestamp in enumerate(self.prices.index):
            utc = pd.Timestamp(timestamp, tz="UTC")
            prior = orders.loc[orders["date_utc"] < utc]
            quantity = float((prior["amount"] * prior["side"].map({"buy": 1, "sell": -1})).sum())
            if timestamp.hour % 8 == 0:
                expected_funding -= quantity * 100 * .001
            included = orders if i == len(self.prices) - 1 else prior
            expected = 1000 - included["fee"].sum() + expected_funding
            self.assertAlmostEqual(result.account.loc[timestamp, "wallet_balance"], expected, places=6)
            self.assertAlmostEqual(result.equity.loc[timestamp], expected, places=6)
        self.assertEqual(len(result.orders), 6)

    def test_cross_funding_moves_liquidation_before_another_order(self):
        self.settings.update(margin_mode="cross", leverage=5)
        for direction in (1, -1):
            with self.subTest(direction=direction):
                funding = self.frames["funding_rate"].copy()
                funding.loc[funding.notna().any(axis=1)] = direction * .01
                funding.to_parquet(self.directory / "funding_rate.parquet")
                low, high = self.frames["low_price"].copy(), self.frames["high_price"].copy()
                # After entry fee 2 and funding 40, WB=958, amount=40.
                expected_liq = round((4000 - direction * 958) / (40 * (1 - direction * .004)), 2)
                if direction > 0:
                    low.iloc[9, 0] = 76  # old liquidation was ~75.35
                else:
                    high.iloc[9, 0] = 124  # old liquidation was ~124.45
                low.to_parquet(self.directory / "low_price.parquet")
                high.to_parquet(self.directory / "high_price.parquet")
                self.weights[:] = np.nan
                self.weights.iloc[0, 0] = direction * 4
                result = self.run_futures()
                trade = result.trades.iloc[0]
                self.assertEqual(trade["exit_reason"], "liquidation")
                self.assertEqual(pd.Timestamp(trade["close_date"], tz="UTC"), pd.Timestamp(self.prices.index[8], tz="UTC"))
                self.assertAlmostEqual(trade["close_rate"], expected_liq, places=8)
                expected_end = 1000 + direction * 40 * (expected_liq - 100) - 2 - 40 - 40 * expected_liq * .0005
                self.assertAlmostEqual(result.equity.iloc[-1], expected_end, places=6)
                self.assertEqual(len(result.orders), 2)

    def test_gap_liquidation_precedes_sizing_and_records_real_bankruptcy(self):
        self.settings.update(margin_mode="cross", leverage=5)
        for direction, gap in ((1, 70.0033), (-1, 130.0033)):
            with self.subTest(direction=direction):
                frames = write_futures_fixture(self.directory)
                for name in ("opening_price", "trade_price", "mark_price", "high_price", "low_price"):
                    frame = frames[name].copy()
                    offset = .5 if name == "high_price" else -.5 if name == "low_price" else 0
                    frame.iloc[4:, 0] = gap + offset
                    frame.to_parquet(self.directory / f"{name}.parquet")
                self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
                self.weights = self.prices * np.nan
                self.weights.iloc[0, 0] = direction * 4
                self.weights.iloc[3, 0] = 0  # cannot escape liquidation via a new cash target
                with self.assertRaises(FuturesBankruptcyError) as raised:
                    self.run_futures()
                expected = 1000 + direction * 40 * (gap - 100) - 2 - 40 * gap * .0005
                self.assertAlmostEqual(raised.exception.equity, expected, places=6)
                self.assertEqual(raised.exception.timestamp, pd.Timestamp(self.prices.index[3], tz="UTC"))

    def test_cross_nonfinite_and_accounting_errors_remain_fatal(self):
        from lib.freqtrade_adapter_v2 import _EquityLedger
        self.settings.update(margin_mode="cross", leverage=5)
        with patch.object(_EquityLedger, "capture", return_value={"equity": np.nan, "funding_pnl": 0}):
            with self.assertRaisesRegex(ValueError, "Nonfinite") as raised:
                self.run_futures()
            self.assertNotIsInstance(raised.exception, FuturesBankruptcyError)
        original = _EquityLedger.capture

        def wrong_equity(ledger, timestamp, marks):
            result = original(ledger, timestamp, marks)
            result["equity"] += 1
            return result

        with patch.object(_EquityLedger, "capture", wrong_equity):
            with self.assertRaisesRegex(RuntimeError, "open-account reconciliation failed"):
                self.run_futures()

    def test_missing_liquidation_is_an_engine_error_not_a_bankrupt_candidate(self):
        from lib.freqtrade_adapter_v2 import _TargetWeightBacktesting
        self.settings.update(margin_mode="cross", leverage=5)
        for name in ("opening_price", "trade_price", "mark_price", "high_price", "low_price"):
            frame = self.frames[name].copy()
            offset = .5 if name == "high_price" else -.5 if name == "low_price" else 0
            frame.iloc[4:, 0] = 70 + offset
            frame.to_parquet(self.directory / f"{name}.parquet")
        self.prices = pd.read_parquet(self.directory / "opening_price.parquet").iloc[1:]
        self.weights = self.prices * np.nan
        self.weights.iloc[0, 0] = 4
        with patch.object(_TargetWeightBacktesting, "_liquidate_at_open", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "unliquidated positions"):
                self.run_futures()

    def test_existing_cli_grid_pbo_spa_and_walkforward(self):
        from scripts import run_cross_section_grid_v2 as grid
        from scripts import run_cross_section_backtest_v2 as single
        from scripts.research import analyze_pbo, analyze_spa, analyze_spa_arch
        from scripts.walkforward import run_cross_section_grid_walkforward_v2 as wf

        count = len(pd.date_range("2024-01-01 23:00", "2024-05-03", freq="h"))
        frames = write_futures_fixture(self.directory, periods=count)
        n = np.arange(count)
        opening = frames["opening_price"] + (np.sin(n / 48) * 2 + n * .001)[:, None]
        for name, frame in {"opening_price": opening, "trade_price": opening, "mark_price": opening, "high_price": opening + .5, "low_price": opening - .5}.items():
            frame.to_parquet(self.directory / f"{name}.parquet")
        direction = opening * 0 + np.where((n // 96) % 2 == 0, 1, -1)[:, None]
        direction.to_parquet(self.directory / "direction.parquet")
        features = [{"source": "trade_price", "column_name": "price"}, {"source": "custom:direction", "column_name": "direction"}]
        universe = {"feature_column": "price", "lag": 0, "signal_lag": 1, "mode": "top_n", "top_n": 1, "start_min_cross_section_size": 1, "exclude_warnings": False, "allowed_markets": ["BTC/USDT:USDT"]}
        weight = {"weighting": "fixed", "fixed_weight": "{weight}", "rebalance_frequency": "daily", "position_score_feature": "direction", "position_score_lag": 1}
        backtest = {"engine": "freqtrade", "timeframe": "60m", "price_column": "opening_price", "init_cash": 1000, "fees": .0005, "slippage": 0, "periods_per_year": 8760, "benchmark_market": "BTC/USDT:USDT", "trim_start_mode": "timestamp", "trim_start_timestamp": "2024-01-02 00:00", "freqtrade": self.settings}
        config = {"candle_dir": str(self.directory), "source_cache_dir": str(self.directory), "out_dir": str(self.directory / "grid"), "run_name_template": "weight_{weight}", "save_run_artifacts": True, "feature_spec_template": features, "universe_spec_template": universe, "weight_spec_template": weight, "backtest_spec_template": backtest, "grid": {"weight": [.5, .8]}}
        config_path = self.directory / "grid.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        def invoke(module, args):
            output = io.StringIO()
            with patch("sys.argv", [module.__name__, *map(str, args)]), contextlib.redirect_stdout(output):
                code = module.main()
            self.assertIn(code, (None, 0), output.getvalue())

        invoke(grid, ["--config-json", config_path])
        results = pd.read_csv(self.directory / "grid" / "summary_results.csv")
        self.assertEqual(set(results["status"]), {"ok"}, results.to_string())
        self.assertEqual(set(results["Execution Engine"]), {"freqtrade"})
        self.assertEqual(set(results["Artifact Format"]), {"parquet"})
        candidate_dir = self.directory / "grid" / "weight_0.5" / "backtest"
        for name in ("equity_curve", "benchmark_curve", "excess_equity_curve", "futures_account"):
            self.assertTrue((candidate_dir / (name + ".parquet")).is_file())
            self.assertFalse((candidate_dir / (name + ".csv")).exists())
        # CSV remains an explicit grid option despite the new Parquet default.
        csv_config = dict(config, artifact_format="csv", out_dir=str(self.directory / "grid_csv"), grid={"weight": [.5]})
        csv_config_path = self.directory / "grid_csv.json"
        csv_config_path.write_text(json.dumps(csv_config), encoding="utf-8")
        invoke(grid, ["--config-json", csv_config_path])
        csv_candidate_dir = self.directory / "grid_csv" / "weight_0.5" / "backtest"
        self.assertEqual(set(pd.read_csv(self.directory / "grid_csv" / "summary_results.csv")["Artifact Format"]), {"csv"})
        for name in ("equity_curve", "benchmark_curve", "excess_equity_curve", "futures_account"):
            self.assertTrue((csv_candidate_dir / (name + ".csv")).is_file())
            self.assertFalse((csv_candidate_dir / (name + ".parquet")).exists())
        pd.testing.assert_frame_equal(
            read_backtest_curve(candidate_dir / "equity_curve.parquet"),
            read_backtest_curve(csv_candidate_dir / "equity_curve.csv"), check_exact=True, check_freq=False,
        )
        self.assertTrue(np.isfinite(results["CAGR [%]"]).all())
        invoke(analyze_pbo, ["--grid-config-json", config_path, "--out-dir", self.directory / "pbo", "--subperiod-months", 1, "--anchor-month-offsets", 0])
        pbo = json.loads((self.directory / "pbo" / "pbo_summary.json").read_text())
        self.assertEqual(pbo["candidate_count"], 2)
        self.assertEqual(pbo["split_count"], 6)
        self.assertTrue(0 <= pbo["pbo"] <= 1)
        invoke(analyze_spa, ["--grid-config-json", config_path, "--out-dir", self.directory / "spa"])
        matrix_path = self.directory / "spa" / "spa_excess_return_matrix.parquet"
        matrix = pd.read_parquet(matrix_path)
        self.assertEqual(matrix.shape[1], 2)
        self.assertTrue(np.isfinite(matrix.to_numpy()).all())
        self.assertEqual(matrix.index.min(), pd.Timestamp("2024-01-02"))
        invoke(analyze_spa_arch, ["--excess-return-matrix-parquet", matrix_path, "--bootstrap-reps", 50, "--block-size", 24, "--out-dir", self.directory / "spa_arch"])
        spa = json.loads((self.directory / "spa_arch" / "spa_summary.json").read_text())
        self.assertTrue(0 <= spa["arch_spa_p_value"] <= 1)
        invoke(wf, ["--grid-config-json", config_path, "--out-dir", self.directory / "wf", "--is-months", 2, "--oos-months", 1, "--step-months", 1, "--window-mode", "rolling"])
        folds = pd.read_csv(self.directory / "wf" / "fold_candidate_results.csv")
        self.assertEqual(set(folds["oos_status"]), {"ok"})
        self.assertEqual(set(folds["oos_Execution Engine"]), {"freqtrade"})
        self.assertEqual(len(pd.read_csv(self.directory / "wf" / "fold_winners.csv")), 2)
        self.assertTrue((self.directory / "wf" / "stitched_oos_equity_curve.csv").exists())

        paths = {}
        for name, payload in {"features": features, "universe": universe, "weights": dict(weight, fixed_weight=.5), "freqtrade": self.settings}.items():
            paths[name] = self.directory / f"{name}.json"
            paths[name].write_text(json.dumps(payload), encoding="utf-8")
        invoke(single, ["--candle-dir", self.directory, "--source-cache-dir", self.directory, "--feature-spec-json", paths["features"], "--universe-spec-json", paths["universe"], "--weight-spec-json", paths["weights"], "--portfolio-dir", self.directory / "portfolio", "--backtest-out-dir", self.directory / "single", "--engine", "freqtrade", "--freqtrade-spec-json", paths["freqtrade"], "--init-cash", 1000, "--fees", .0005, "--execution-price-column", "opening_price", "--timeframe", "60m", "--periods-per-year", 8760, "--benchmark-market", "BTC/USDT:USDT", "--trim-start-mode", "first_weight"])
        grid_curve = read_backtest_curve(candidate_dir / "equity_curve.parquet")
        single_curve = read_backtest_curve(self.directory / "single" / "equity_curve.parquet")
        self.assertFalse((self.directory / "single" / "equity_curve.csv").exists())
        self.assertTrue((self.directory / "single" / "futures_account.parquet").exists())
        pd.testing.assert_frame_equal(grid_curve, single_curve, check_exact=True, check_freq=False)
        # Exercise the public CSV override as well as the single-run Parquet default.
        csv_args = ["--candle-dir", self.directory, "--source-cache-dir", self.directory,
                        "--feature-spec-json", paths["features"], "--universe-spec-json", paths["universe"],
                        "--weight-spec-json", paths["weights"], "--portfolio-dir", self.directory / "portfolio",
                        "--backtest-out-dir", self.directory / "single_csv", "--engine", "freqtrade",
                        "--freqtrade-spec-json", paths["freqtrade"], "--init-cash", 1000, "--fees", .0005,
                        "--execution-price-column", "opening_price", "--timeframe", "60m",
                        "--periods-per-year", 8760, "--benchmark-market", "BTC/USDT:USDT",
                        "--trim-start-mode", "first_weight", "--artifact-format", "csv"]
        invoke(single, csv_args)
        pd.testing.assert_frame_equal(
            read_backtest_curve(self.directory / "single_csv" / "equity_curve.csv"), single_curve,
            check_exact=True, check_freq=False,
        )
        self.assertTrue((self.directory / "single_csv" / "futures_account.csv").is_file())
        self.assertFalse((self.directory / "single_csv" / "equity_curve.parquet").exists())
        from scripts.research import analyze_strategy_uncertainty_v2 as uncertainty
        invoke(uncertainty, ["--backtest-dir", candidate_dir, "--out-dir", self.directory / "uncertainty",
                             "--bootstrap-reps", 20, "--block-size", 24])
        metadata = json.loads((self.directory / "uncertainty" / "run_metadata.json").read_text())
        self.assertTrue(metadata["candidates"][0]["equity_curve_path"].endswith(".parquet"))


if __name__ == "__main__":
    unittest.main()
