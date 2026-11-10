"""Research-only target-weight bridge to Freqtrade 2026.9.

No exchange credentials, trading commands, or installed-library modifications.
Version-pinned subclasses coordinate rebalances, marked equity and cost-aware
cross collateral; Freqtrade still supplies order execution and liquidation math.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from freqtrade.configuration.config_validation import validate_config_schema
from freqtrade.enums import CandleType, ExitType, MarginMode, RunMode, TradingMode
from freqtrade.leverage.liquidation_price import update_liquidation_prices
from freqtrade.optimize.backtesting import Backtesting, CLOSE_IDX, DATE_IDX, HIGH_IDX, LOW_IDX, OPEN_IDX
from freqtrade.persistence import LocalTrade
from freqtrade.strategy import IStrategy
from freqtrade.strategy.interface import ExitCheckTuple
from freqtrade.wallets import Wallets

from lib.backtest_engine_v2 import BacktestResultV2, FuturesBankruptcyError
from lib.funding_schedule_v2 import funding_settlement_index, normalize_funding_schedule
from lib.vectorbt_adapter import VectorBTSpec


def _quantity_difference(left: float, right: float) -> float:
    # Both operands are exchange-quantized quantities. Float subtraction must
    # not turn one whole contract step into slightly less than one step.
    return float(Decimal(str(left)) - Decimal(str(right)))


class V2TargetWeightStrategy(IStrategy):
    """Generic IStrategy entrypoint. Inputs are already lagged execution targets."""

    INTERFACE_VERSION = 3
    can_short = True
    startup_candle_count = 0
    minimal_roi = {}
    # FT requires a negative setting. ft_stoploss_reached below deliberately
    # retains only native liquidation, leaving strategy exits to v2 targets.
    stoploss = -1.0
    trailing_stop = False
    position_adjustment_enable = True
    max_entry_position_adjustment = -1
    process_only_new_candles = True
    order_types = {"entry": "market", "exit": "market", "stoploss": "market", "stoploss_on_exchange": False}
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def ft_stoploss_reached(self, current_rate, trade, current_time, current_profit, force_stoploss, low=None, high=None, bound_profit=None):
        liquidation = trade.liquidation_price
        reached = bool(liquidation) and (
            liquidation <= (high or current_rate) if trade.is_short
            else liquidation >= (low or current_rate)
        )
        return ExitCheckTuple(ExitType.LIQUIDATION if reached else ExitType.NONE)

    def populate_entry_trend(self, dataframe, metadata):
        # FT shifts signals one candle. Targets name the execution-open timestamp,
        # so put each signal on the preceding candle, without shifting callbacks.
        dates = pd.DatetimeIndex(dataframe["date"]) + self._bar_delta
        targets = self._weights[metadata["pair"]].reindex(dates).to_numpy()
        dataframe["enter_long"] = (targets > 0).astype(int)
        dataframe["enter_short"] = (targets < 0).astype(int)
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dates = pd.DatetimeIndex(dataframe["date"]) + self._bar_delta
        targets = self._weights[metadata["pair"]].reindex(dates).to_numpy()
        dataframe["exit_long"] = (targets <= 0).astype(int)
        dataframe["exit_short"] = (targets >= 0).astype(int)
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return self._leverage

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake, leverage, entry_tag, side, **kwargs):
        quantity = self._quantities.get(pair)
        if quantity is None or (quantity < 0) != (side == "short"):
            return 0.0
        return abs(quantity) * current_rate / leverage

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake, **kwargs):
        target = self._quantities.get(trade.pair)
        if target is None or target == 0 or (target < 0) != trade.is_short:
            return None  # full exits and direction changes use native exit signals
        difference = _quantity_difference(abs(target), trade.amount)
        if abs(difference) * current_rate <= self._rebalance_tolerance:
            return None
        if difference > 0:
            return difference * current_rate / trade.leverage
        # FT reductions use the trade's historical stake/quantity, not current price.
        return difference / trade.amount * trade.stake_amount


class _EquityLedger:
    """Value native fills without inventing spot cash or charging future exit fees."""

    def __init__(self, init_cash: float, fee: float):
        self.cash = init_cash
        self.fee = fee
        self.positions: dict[str, float] = {}
        self.seen_orders: set[tuple[int, str]] = set()
        self.closed_count = 0
        self.closed_funding = 0.0
        self.orders: list[dict[str, Any]] = []

    def capture(self, timestamp, marks: dict[str, float]) -> dict[str, float]:
        closed = LocalTrade.bt_trades[self.closed_count:]
        opened = list(LocalTrade.bt_trades_open)
        for trade in [*closed, *opened]:
            for order in trade.orders:
                key = (trade.id, order.order_id)
                if key in self.seen_orders or order.ft_is_open or not order.filled:
                    continue
                self.seen_orders.add(key)
                amount = float(order.safe_amount_after_fee)
                price = float(order.safe_price)
                side = 1 if order.ft_order_side == "buy" else -1
                fee = amount * price * self.fee
                self.cash -= side * amount * price + fee
                self.positions[trade.pair] = self.positions.get(trade.pair, 0.0) + side * amount
                self.orders.append({
                    "date_utc": order.order_filled_utc, "pair": trade.pair,
                    "trade_id": trade.id, "order_id": int(order.order_id), "side": order.ft_order_side,
                    "amount": amount, "price": price, "fee": fee,
                })
        self.closed_funding += sum(float(t.funding_fees or 0.0) for t in closed)
        self.closed_count = len(LocalTrade.bt_trades)
        funding = self.closed_funding + sum(float(t.funding_fees or 0.0) for t in opened)
        position_value = sum(quantity * marks[pair] for pair, quantity in self.positions.items())
        return {"equity": self.cash + position_value + funding, "funding_pnl": funding}


class _CostAwareCrossWallets(Wallets):
    """Keep native realized PnL, adding only costs not yet realized on an exit.

    Do not put unrealized mark PnL in wallet balance: native liquidation already
    accounts for it. Isolated collateral allocation retains the native model.
    """

    def _update_dry(self):
        super()._update_dry()
        if self._config["margin_mode"] != MarginMode.CROSS:
            return
        adjustment = 0.0
        for trade in LocalTrade.bt_trades_open:
            filled = [o for o in trade.orders if not o.ft_is_open and o.filled]
            last_exit = next((i for i in range(len(filled) - 1, -1, -1)
                              if filled[i].ft_order_side == trade.exit_side), -1)
            # Earlier funding is already inside realized_profit. Adding all of
            # trade.funding_fees here would charge it twice after partial exits.
            realized_funding = sum(float(o.funding_fee or 0.0) for o in filled[:last_exit + 1])
            adjustment += float(trade.funding_fees or 0.0) - realized_funding
            # stake*leverage preserves the unrounded remaining entry cost basis.
            adjustment -= trade.stake_amount * trade.leverage * trade.fee_open
        wallet = self._wallets[self._stake_currency]
        self._wallets[self._stake_currency] = wallet._replace(
            free=wallet.free + adjustment, total=wallet.total + adjustment,
        )


class _TargetWeightBacktesting(Backtesting):
    """Pin-dependent integration point, covered by native-engine integration tests."""

    def _reject_target(self, pair, reason):
        self._target_rejections[reason] += 1

    def _refresh_liquidation_prices(self):
        # Native fill callbacks can precede the wallet/partial-exit update.
        # Refresh again after native state is settled, and after funding alone.
        self.wallets.update()
        opened = list(LocalTrade.bt_trades_open)
        if self.margin_mode == MarginMode.CROSS:
            update_liquidation_prices(exchange=self.exchange, wallets=self.wallets,
                                      stake_currency=self.config["stake_currency"], dry_run=True)
        else:
            for trade in opened:
                update_liquidation_prices(trade, exchange=self.exchange, wallets=self.wallets,
                                          stake_currency=self.config["stake_currency"], dry_run=True)

    def _capture_account(self, timestamp, marks):
        account = self._ledger.capture(timestamp, marks)
        if not np.isfinite(account["equity"]):
            raise ValueError(f"Nonfinite futures account equity at {timestamp}; validation stopped")
        if self.margin_mode == MarginMode.CROSS:
            wallet = self.wallets.get_collateral()
            unrealized = sum(
                (-1 if t.is_short else 1) * (t.amount * marks[t.pair] - t.stake_amount * t.leverage)
                for t in LocalTrade.bt_trades_open if t.has_open_position
            )
            if not np.isfinite(wallet) or not np.isclose(account["equity"], wallet + unrealized, rtol=1e-8, atol=1e-6):
                raise RuntimeError(f"Futures open-account reconciliation failed at {timestamp}: "
                                   f"ledger={account['equity']}, wallet_plus_unrealized={wallet + unrealized}")
            account["wallet_balance"] = wallet
        if account["equity"] <= 0:
            if any(t.has_open_position for t in LocalTrade.bt_trades_open):
                raise RuntimeError(f"Non-positive futures equity with unliquidated positions at {timestamp}; validation stopped")
            wallet = self.wallets.get_total(self.config["stake_currency"])
            if not np.isfinite(wallet) or not np.isclose(account["equity"], wallet, rtol=1e-8, atol=1e-6):
                raise RuntimeError(f"Futures bankruptcy reconciliation failed at {timestamp}: "
                                   f"ledger={account['equity']}, Freqtrade={wallet}")
            raise FuturesBankruptcyError(timestamp, account["equity"])
        return account

    def _liquidate_at_open(self, current_time, current_rows, marks):
        # Funding or an opening gap can cross liquidation before target sizing.
        # Use open-only rows here; never look at this candle's future H/L.
        for trade in list(LocalTrade.bt_trades_open):
            liquidation = trade.liquidation_price
            if not trade.has_open_position or not liquidation:
                continue
            row = current_rows[trade.pair][1]
            opening, mark = float(row[OPEN_IDX]), marks[trade.pair]
            reached = max(opening, mark) >= liquidation if trade.is_short else min(opening, mark) <= liquidation
            if not reached:
                continue
            point = list(row)
            for column in (OPEN_IDX, HIGH_IDX, LOW_IDX, CLOSE_IDX):
                point[column] = opening
            self._boundary_liquidations.add(trade.id)
            try:
                trade.exit_reason = ExitType.LIQUIDATION.value
                self._exit_trade(trade, point, opening, trade.amount, ExitType.LIQUIDATION.value)
                self._process_exit_order(trade.orders[-1], trade, current_time, point, trade.pair)
            finally:
                self._boundary_liquidations.discard(trade.id)

    def _process_exit_order(self, order, trade, current_time, row, pair):
        super()._process_exit_order(order, trade, current_time, row, pair)
        if order is not None and not order.ft_is_open:
            self._refresh_liquidation_prices()

    def get_valid_entry_price_and_stake(
        self, pair, row, propose_rate, stake_amount, direction, current_time,
        entry_tag, trade, order_type, price_precision, precision_mode_price,
    ):
        rate, stake, leverage, minimum = super().get_valid_entry_price_and_stake(
            pair, row, propose_rate, stake_amount, direction, current_time,
            entry_tag, trade, order_type, price_precision, precision_mode_price,
        )
        target = self.strategy._quantities.get(pair)
        if target is None or target == 0 or (target < 0) != (direction == "short"):
            return rate, 0.0, leverage, minimum
        if rate <= 0 or leverage <= 0:
            self._reject_target(pair, "insufficient_margin")
            return rate, 0.0, leverage, minimum
        quantity = _quantity_difference(abs(target), trade.amount if trade else 0.0)
        if quantity <= 0:
            return rate, 0.0, leverage, minimum
        requested = quantity * rate / leverage
        if requested < minimum and not np.isclose(requested, minimum, rtol=1e-12, atol=0):
            self._reject_target(pair, "below_minimum")
            return rate, 0.0, leverage, minimum
        maximum = self.exchange.get_max_pair_stake_amount(pair, rate, leverage=leverage)
        if trade:
            maximum -= trade.stake_amount
        available = min(maximum, self.wallets.get_available_stake_amount())
        if requested > available and not np.isclose(requested, available, rtol=1e-12, atol=0):
            self._reject_target(pair, "insufficient_margin")
            return rate, 0.0, leverage, minimum
        if not stake:
            self._reject_target(pair, "native_entry_rejected")
            return rate, 0.0, leverage, minimum
        # Native validation may raise a small stake to its minimum, or cap it
        # to available margin. Neither may silently change a v2 target quantity.
        if not np.isclose(stake, requested, rtol=1e-12, atol=0):
            self._reject_target(pair, "native_stake_adjustment")
            return rate, 0.0, leverage, minimum
        # A one-step order can otherwise truncate to zero before a native Order
        # even exists. Move the stake by representable float units only until
        # the native inverse conversion yields exactly the validated quantity.
        safe_stake = requested
        for _ in range(8):
            converted = self.exchange.amount_to_contract_precision(pair, safe_stake / rate * leverage)
            if converted == quantity:
                return rate, safe_stake, leverage, minimum
            if converted > quantity:
                break
            safe_stake = float(np.nextafter(safe_stake, np.inf))
        raise RuntimeError("Cannot preserve exchange-quantized futures quantity through stake conversion")

    def _try_close_open_order(self, order, trade, current_date, row):
        if order is not None and order.ft_is_open and order.order_type == "market":
            if order.ft_order_tag != ExitType.LIQUIDATION.value or trade.id in self._boundary_liquidations:
                # v2 market fills use observed historical opens. Applying today's
                # price tick to old OHLC can otherwise put a market exit outside
                # that candle and make it behave like an unfilled limit order.
                rate = float(row[OPEN_IDX])
                target = self.strategy._quantities.get(trade.pair)
                if order.ft_order_side == trade.entry_side and target is not None:
                    quantity = _quantity_difference(abs(target), trade.amount)
                elif order.ft_order_tag == ExitType.PARTIAL_EXIT.value and target is not None:
                    quantity = _quantity_difference(trade.amount, abs(target))
                elif order.ft_order_side == trade.exit_side and order.ft_order_tag in {
                    "v2_rebalance", ExitType.FORCE_EXIT.value, ExitType.LIQUIDATION.value,
                }:
                    quantity = trade.amount
                else:
                    quantity = order.amount
                if quantity <= 0:
                    raise RuntimeError("Non-positive pending futures market quantity")
                # The requested quantity was quantized and its margin validated
                # already. Restore it after native stake->amount float conversion,
                # before any fill, fee, wallet or liquidation accounting occurs.
                order.amount = quantity
                order.remaining = quantity
                order.ft_price = order.price = order.average = rate
                order.cost = quantity * rate * (1 + self.fee)
                if trade.nr_of_successful_entries == 0:
                    trade.amount_requested = quantity
        filled = super()._try_close_open_order(order, trade, current_date, row)
        if filled:
            if order.ft_order_tag == ExitType.LIQUIDATION.value:
                # A completed request must never be retried after intrabar liquidation.
                if trade.pair in self._pending_targets:
                    self._unfilled_target_requests += 1
                    self._pending_targets.pop(trade.pair)
            else:
                entered = sum((Decimal(str(o.safe_amount_after_fee)) for o in trade.select_filled_orders(trade.entry_side)), Decimal(0))
                exited = sum((Decimal(str(o.safe_amount_after_fee)) for o in trade.select_filled_orders(trade.exit_side)), Decimal(0))
                actual = float(entered - exited) * (-1 if trade.is_short else 1)
                target = self._pending_targets.get(trade.pair)
                if target is not None and abs(actual - target) <= 1e-12:
                    self._pending_targets.pop(trade.pair)
            if order.ft_order_side == trade.entry_side:
                self._refresh_liquidation_prices()
        return filled

    def _check_adjust_trade_for_candle(self, trade, row, current_time):
        # Reductions are processed before increases across all pairs. Native FT
        # also calls this later in the same candle; do not submit the same resize twice.
        if trade.pair in self._adjusted_pairs:
            return trade
        self._adjusted_pairs.add(trade.pair)
        target = self.strategy._quantities.get(trade.pair)
        before = len(trade.orders)
        result = super()._check_adjust_trade_for_candle(trade, row, current_time)
        if target is not None and target * (-1 if trade.is_short else 1) > 0:
            difference = abs(_quantity_difference(abs(target), trade.amount))
            if len(trade.orders) == before and difference * row[OPEN_IDX] > self.strategy._rebalance_tolerance:
                # Entry refusals are counted by get_valid_entry_price_and_stake.
                if abs(target) < trade.amount:
                    self._reject_target(trade.pair, "reduction_below_minimum")
        return result

    def _run_funding_fees(self, trade, current_time, force=False):
        # Settlement precedes orders. FT's inclusive lower bound would charge
        # an entry at the settlement instant, and can charge it again on resize.
        # Keep native funding arithmetic, with an exclusive last-fill boundary.
        trade.set_funding_fees(self.exchange.calculate_funding_fees(
            self.futures_data[trade.pair], amount=trade.amount, is_short=trade.is_short,
            open_date=trade.date_last_filled_utc + timedelta(microseconds=1), close_date=current_time,
        ))

    def time_pair_generator(self, start_date, end_date, pairs, data):
        rows = {pair: {pd.Timestamp(row[DATE_IDX]): (i, row) for i, row in enumerate(values)} for pair, values in data.items()}
        # Align once; constructing Series and converting them to dictionaries
        # on every bar costs more than accessing these prepared numeric arrays.
        timeline = pd.date_range(start_date + self.timeframe_td, end_date, freq=self.timeframe_td)
        timestamps = tuple(timeline)
        mark_values = self._marks.loc[timeline, pairs].to_numpy(dtype=float, copy=False)
        weight_values = self.strategy._weights.loc[timeline, pairs].to_numpy(dtype=float, copy=False)
        final_timestamp = pd.Timestamp(end_date)
        for offset, current_time in enumerate(self._time_generator(start_date, end_date)):
            self.check_abort()
            timestamp = timestamps[offset]
            current_rows = {pair: values[timestamp] for pair, values in rows.items()}
            for pair, (index, _) in current_rows.items():
                self.dataprovider._set_dataframe_max_index(pair, index)
            self.dataprovider._set_dataframe_max_date(current_time)
            for trade in list(LocalTrade.bt_trades_open):
                self._run_funding_fees(trade, current_time, force=True)
            marks = dict(zip(pairs, map(float, mark_values[offset])))
            self._refresh_liquidation_prices()
            self._liquidate_at_open(current_time, current_rows, marks)
            account = self._capture_account(timestamp, marks)
            self._account_rows[timestamp] = account
            if timestamp == final_timestamp:
                # The final observation is an open-time boundary. Native
                # handle_left_open closes at that open, without seeing its H/L.
                continue
            self._adjusted_pairs = set()
            for pair, weight in zip(pairs, weight_values[offset]):
                if pd.notna(weight):
                    if pair in self._pending_targets:
                        self._unfilled_target_requests += 1
                    quantity = float(weight) * account["equity"] / current_rows[pair][1][OPEN_IDX]
                    target = np.sign(quantity) * self.exchange.amount_to_contract_precision(pair, abs(quantity))
                    self._pending_targets[pair] = target
                    if quantity != 0 and target == 0:
                        self._reject_target(pair, "below_quantity_precision")
                        self._unfilled_target_requests += 1
            # Pending quantities are fixed at the request's original sizing equity
            # and price. NaN retries only the remainder; it never resizes a filled
            # holding to keep a percentage constant as its value changes.
            self.strategy._quantities = dict(self._pending_targets)
            for pair, target in list(self._pending_targets.items()):
                trades = LocalTrade.bt_trades_open_pp[pair]
                signed = sum(-t.amount if t.is_short else t.amount for t in trades)
                if abs(signed - target) <= 1e-12 or (
                    signed * target > 0
                    and abs(signed - target) * current_rows[pair][1][OPEN_IDX] <= self.strategy._rebalance_tolerance
                ):
                    self._pending_targets.pop(pair)
                    self.strategy._quantities.pop(pair)
            # Release margin across ALL pairs before processing any new exposure.
            # Native FT then handles entries and direction changes normally.
            for trade in list(LocalTrade.bt_trades_open):
                target = self.strategy._quantities.get(trade.pair)
                signed = -trade.amount if trade.is_short else trade.amount
                if target is not None and (target * signed <= 0 or abs(target) < abs(signed)):
                    row = current_rows[trade.pair][1]
                    if target * signed <= 0:
                        # Close at the open; do not inspect this candle's future
                        # H/L in the reduction pass and then reopen at its open.
                        self._get_exit_for_signal(trade, row, ExitCheckTuple(ExitType.EXIT_SIGNAL, "v2_rebalance"), current_time)
                        order = trade.select_order(trade.exit_side, is_open=True)
                        if order is not None:
                            self._process_exit_order(order, trade, current_time, row, trade.pair)
                    else:
                        self._check_adjust_trade_for_candle(trade, row, current_time)
            for pair in pairs:
                row = current_rows[pair][1]
                target = self.strategy._quantities.get(pair, 0)
                direction = ("short" if target < 0 else "long") if target else None
                yield current_time, pair, row, timestamp == pd.Timestamp(end_date), direction


def _load_frame(directory: Path, name: str, columns: list[str]) -> pd.DataFrame:
    path = directory / f"{name}.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"Futures backtest requires {path}")
    frame = pd.read_parquet(path)
    frame.index = pd.to_datetime(frame.index, utc=True)
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError(f"Futures data timestamps must be unique and sorted: {path}")
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing futures markets in {path}: {sorted(missing)}")
    return frame[columns].astype(float)


def _timeframe(timeframe: str) -> tuple[str, pd.Timedelta]:
    if timeframe == "daily":
        return "1d", pd.Timedelta(days=1)
    if not timeframe.endswith("m") or not timeframe[:-1].isdigit():
        raise ValueError("Futures timeframe must be 'daily' or minutes such as '60m'")
    minutes = int(timeframe[:-1])
    if minutes <= 0 or 1440 % minutes:
        raise ValueError("Futures timeframe must divide one UTC day")
    return (f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"), pd.Timedelta(minutes=minutes)


def run_futures_backtest_v2(price_frame, target_weight_frame, spec: VectorBTSpec, *, source_cache_dir: Path, timeframe: str, settings: dict[str, Any]):
    if version("freqtrade") != "2026.9":
        raise RuntimeError("The v2 adapter is tested against freqtrade==2026.9; install requirements-futures.txt")
    allowed = {"exchange", "margin_mode", "leverage", "pair_map", "funding_interval_hours", "funding_schedule", "rebalance_tolerance", "liquidation_buffer", "metadata_dir"}
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"Unknown freqtrade settings: {sorted(unknown)}")
    if spec.price_column != "opening_price":
        raise ValueError("Freqtrade targets execute at candle open; set price_column='opening_price' and lag signals explicitly")
    if spec.slippage != 0:
        raise ValueError("This Freqtrade adapter does not model proportional slippage; set slippage=0 explicitly")
    if not np.isfinite(spec.init_cash) or spec.init_cash <= 0 or not 0 <= spec.fees < 1:
        raise ValueError("Futures init_cash must be positive and fees must be in [0, 1)")
    ft_timeframe, delta = _timeframe(timeframe)
    index = pd.DatetimeIndex(pd.to_datetime(price_frame.index, utc=True))
    if len(index) < 2 or index.has_duplicates or not index.equals(pd.date_range(index[0], index[-1], freq=delta)):
        raise ValueError("Futures evaluation prices need at least two complete, regular UTC bars")
    weights = target_weight_frame.reindex(index=price_frame.index).copy()
    if not np.isfinite(weights.to_numpy()[weights.notna().to_numpy()]).all():
        raise ValueError("Futures weights must be finite or NaN (hold)")
    weights.index = index
    markets = list(weights.columns[weights.fillna(0).abs().sum().gt(0)])
    if not markets:
        markets = list(weights.columns[:1])
    if not markets:
        raise ValueError("Futures weights have no markets")
    pair_map = settings.get("pair_map", {})
    pairs = [str(pair_map.get(market, market)) for market in markets]
    if len(set(pairs)) != len(pairs) or any("/" not in pair or not pair.endswith(":USDT") for pair in pairs):
        raise ValueError("Use unique USDT-settled pairs such as BTC/USDT:USDT, directly or via freqtrade.pair_map")
    leverage = float(settings.get("leverage", 2))
    if not np.isfinite(leverage) or leverage < 1 or not leverage.is_integer():
        raise ValueError("Futures exchange leverage must be a positive integer; target weights may be fractional")
    if weights.ffill().fillna(0).abs().sum(axis=1).gt(leverage + 1e-12).any():
        raise ValueError("Requested gross exposure exceeds the configured exchange leverage")
    interval = float(settings.get("funding_interval_hours", 8))
    if not np.isfinite(interval) or interval <= 0 or 24 % interval:
        raise ValueError("funding_interval_hours must divide one UTC day")
    funding_schedule = normalize_funding_schedule(settings.get("funding_schedule"))
    if funding_schedule is None and delta > pd.Timedelta(hours=interval):
        raise ValueError("Use bars no longer than the funding interval to preserve settlement timestamps")
    margin_mode = str(settings.get("margin_mode", "isolated"))
    if margin_mode not in {"isolated", "cross"}:
        raise ValueError("margin_mode must be isolated or cross")
    tolerance = float(settings.get("rebalance_tolerance", 0.01))
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError("rebalance_tolerance must be finite and non-negative")
    required_index = index.insert(0, index[0] - delta)
    mapping = {"open": "opening_price", "high": "high_price", "low": "low_price", "close": "trade_price", "volume": "candle_acc_trade_volume", "mark": "mark_price"}
    frames = {key: _load_frame(source_cache_dir, column, markets).reindex(required_index) for key, column in mapping.items()}
    for name, frame in frames.items():
        values = frame.to_numpy()
        if not np.isfinite(values).all() or (values < 0 if name == "volume" else values <= 0).any():
            raise ValueError(f"Incomplete/invalid futures {name} data, including the preceding warmup bar")
    if (frames["high"] < frames["low"]).any().any() or (frames["high"] < frames["open"]).any().any() or (frames["high"] < frames["close"]).any().any() or (frames["low"] > frames["open"]).any().any() or (frames["low"] > frames["close"]).any().any():
        raise ValueError("Invalid futures OHLC range")
    if not np.allclose(price_frame[markets].to_numpy(), frames["open"].loc[index].to_numpy(), rtol=1e-12, atol=0):
        raise ValueError("Execution price frame differs from futures opening_price cache")
    funding = _load_frame(source_cache_dir, "funding_rate", markets)
    if np.isinf(funding.to_numpy()).any():
        raise ValueError("Funding rates must be finite or NaN outside settlements")
    # NaN is not silently treated as zero on an expected settlement timestamp.
    settlement_index = funding_settlement_index(
        index[0], index[-1] + pd.Timedelta(nanoseconds=1), interval, funding_schedule,
    )
    if not settlement_index.isin(index).all():
        raise ValueError("Funding settlement timestamps must align with the futures execution bars")
    if not np.isfinite(funding.reindex(settlement_index).to_numpy()).all():
        raise ValueError("Missing scheduled funding observations; correct funding_interval_hours/funding_schedule or refresh data")
    observed = funding.loc[(funding.index >= index[0]) & (funding.index <= index[-1])].dropna(how="all")
    if not observed.index.isin(settlement_index).all():
        raise ValueError("Unexpected funding observations; correct funding_interval_hours/funding_schedule")
    funding = funding.loc[(funding.index >= required_index[0]) & (funding.index <= index[-1])]
    if any(timestamp not in required_index for timestamp in funding.dropna(how="all").index):
        raise ValueError("Funding settlement timestamps must align with the futures execution bars")

    metadata_dir = Path(settings.get("metadata_dir", "data/research/freqtrade_metadata_v2"))
    metadata_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "runmode": RunMode.BACKTEST, "dry_run": True, "dry_run_wallet": spec.init_cash,
        "trading_mode": TradingMode.FUTURES, "margin_mode": MarginMode(margin_mode),
        "timeframe": ft_timeframe, "strategy": "V2TargetWeightStrategy",
        "strategy_path": str(Path(__file__).parent), "user_data_dir": metadata_dir,
        "datadir": metadata_dir, "dataformat_ohlcv": "feather", "candle_type_def": CandleType.FUTURES,
        "stake_currency": "USDT", "stake_amount": "unlimited", "tradable_balance_ratio": 1.0,
        "max_open_trades": len(pairs), "fee": spec.fees, "backtest_cache": "none",
        "exchange": {"name": str(settings.get("exchange", "binance")), "key": "", "secret": "", "pair_whitelist": pairs, "pair_blacklist": [], "ccxt_config": {"enableRateLimit": True}, "ccxt_async_config": {"enableRateLimit": True}},
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "unfilledtimeout": {"entry": 1, "exit": 1, "unit": "minutes"},
        "liquidation_buffer": float(settings.get("liquidation_buffer", 0)),
        "export": "none", "internals": {}, "config_files": [], "original_config": {},
    }
    validate_config_schema(config, preliminary=True)
    backend = None
    try:
        backend = _TargetWeightBacktesting(config, progress_callback=lambda task: None)
        strategy = backend.strategylist[0]
        strategy._weights = weights[markets].set_axis(pairs, axis=1)
        strategy._bar_delta = delta
        strategy._leverage = leverage
        strategy._rebalance_tolerance = tolerance
        strategy._quantities = {}
        backend.wallets = _CostAwareCrossWallets(config, backend.exchange, is_backtest=True)
        backend._set_strategy(strategy)
        backend._ledger = _EquityLedger(spec.init_cash, spec.fees)
        backend._account_rows = {}
        backend._pending_targets = {}
        backend._target_rejections = Counter()
        backend._unfilled_target_requests = 0
        backend._adjusted_pairs = set()
        backend._boundary_liquidations = set()
        backend._marks = frames["mark"].set_axis(pairs, axis=1)
        backend.funding_fee_timeframe_secs = int(delta.total_seconds())
        processed = {}
        for market, pair in zip(markets, pairs):
            processed[pair] = pd.DataFrame({"date": required_index, **{name: frame[market].to_numpy() for name, frame in frames.items() if name != "mark"}})
            rates = funding[market].dropna().rename("funding_rate").rename_axis("date").reset_index()
            marks = frames["mark"][market].rename("open").rename_axis("date").reset_index()
            backend.futures_data[pair] = backend.exchange.combine_funding_and_mark(rates, marks)
            max_leverage = backend.exchange.get_max_leverage(pair, spec.init_cash)
            if leverage > max_leverage:
                raise ValueError(f"Configured leverage {leverage} exceeds {pair} maximum {max_leverage}")
        native = backend.backtest(processed, required_index[0].to_pydatetime(), index[-1].to_pydatetime())
        final = backend._capture_account(index[-1], backend._marks.loc[index[-1]].to_dict())
        if not np.isclose(final["equity"], native["final_balance"], rtol=1e-8, atol=1e-6):
            raise RuntimeError(f"Futures equity reconciliation failed: ledger={final['equity']}, Freqtrade={native['final_balance']}")
        backend._account_rows[index[-1]] = final
        account = pd.DataFrame.from_dict(backend._account_rows, orient="index").reindex(index)
        account.index = price_frame.index
        equity = account["equity"].rename("portfolio_value")
        trades = pd.DataFrame([trade.to_json() for trade in LocalTrade.bt_trades])
        orders = pd.DataFrame(backend._ledger.orders, columns=["date_utc", "pair", "trade_id", "order_id", "side", "amount", "price", "fee"])
        if not orders.empty:
            orders = orders.sort_values(["date_utc", "order_id"]).reset_index(drop=True)
        summary = pd.Series({
            "Start": equity.index[0], "End": equity.index[-1],
            "Start Value": spec.init_cash, "End Value": float(equity.iloc[-1]),
            "Total Return [%]": (equity.iloc[-1] / spec.init_cash - 1) * 100,
            "Max Drawdown [%]": float((1 - equity / equity.cummax()).max() * 100),
            "Total Trades": len(trades), "Total Orders": len(orders),
            "Total Fees Paid": float(orders["fee"].sum()) if len(orders) else 0.0,
            "Execution Engine": "freqtrade", "Freqtrade Version": version("freqtrade"),
            "Futures Execution Model": "market_open_fixed_quantity",
            "Margin Mode": margin_mode, "Exchange Leverage": leverage,
            "Funding PnL": final["funding_pnl"],
            "Funding Schedule Mode": "segments" if funding_schedule is not None else "fixed",
            "Funding Settlement Events": len(settlement_index),
            "Rejected Signals": native["rejected_signals"] + sum(backend._target_rejections.values()),
            "Native Rejected Signals": native["rejected_signals"],
            "Rejected Below Minimum": backend._target_rejections["below_minimum"],
            "Rejected Below Quantity Precision": backend._target_rejections["below_quantity_precision"],
            "Rejected Insufficient Margin": backend._target_rejections["insufficient_margin"],
            "Rejected Native Stake Adjustment": backend._target_rejections["native_stake_adjustment"],
            "Rejected Native Entry": backend._target_rejections["native_entry_rejected"],
            "Rejected Reduction Below Minimum": backend._target_rejections["reduction_below_minimum"],
            "Unfilled Target Requests": backend._unfilled_target_requests + len(backend._pending_targets),
            "Equity Timing": "pre-rebalance mark after opening liquidations; final bar force-closed at trade open",
        })
        if (equity <= 0).any() or not np.isfinite(equity).all():
            raise ValueError("Invalid futures equity; validation stopped")
        return BacktestResultV2(equity, summary, trades, orders, account, price_frame, spec.init_cash)
    finally:
        Backtesting.cleanup()
        if backend is not None:
            backend.exchange.close()
