from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kr_stock_live.trading import run_auto_rebalance as kr_auto
from live_common.state import load_state, read_json, write_json
from us_stock_live.trading import run_auto_rebalance as us_auto


class LiveStateSafetyV2Test(unittest.TestCase):
    def test_atomic_write_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            payload = {"version": 1, "events": {"event": {"status": "submitted"}}, "plans": {}}

            write_json(path, payload)

            self.assertEqual(read_json(path), payload)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_replace_failure_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = {"version": 1, "events": {"original": {}}, "plans": {}}
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch("live_common.state.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_json(path, {"version": 2})

            self.assertEqual(read_json(path), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_missing_state_initializes_but_corrupt_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(load_state(path), {"version": 1, "events": {}, "plans": {}})

            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_state(path)

            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(path)

    def test_corrupt_auto_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto_state.json"
            path.write_text("{broken", encoding="utf-8")

            for loader in (kr_auto._load_json, us_auto._load_json):
                with self.subTest(loader=loader.__module__):
                    with self.assertRaises(json.JSONDecodeError):
                        loader(path)


def _kis_row(odno: str, pdno: str, *, ord_qty: int, ccld_qty: int, nccs_qty: int, status: str, reject: str = "") -> dict:
    # Field names and values as returned by KIS inquire-ccnl (TTTS3035R) on 2026-11-19.
    return {
        "ord_dt": "20261113",
        "odno": odno,
        "orgn_odno": "",
        "pdno": pdno,
        "ft_ord_qty": str(ord_qty),
        "ft_ccld_qty": str(ccld_qty),
        "nccs_qty": str(nccs_qty),
        "prcs_stat_name": status,
        "rjct_rson_name": reject,
    }


def _order(order_no: str, symbol: str, qty: int) -> dict:
    return {"symbol": symbol, "side": "sell", "qty": qty, "order_no": order_no, "status": "submitted"}


class UsFillStateTest(unittest.TestCase):
    """LIVE-001: orders that ended unfilled at day end must not leave the runner waiting forever."""

    def test_order_states_from_kis_rows(self) -> None:
        from us_stock_live.trading import check_fills

        rows = [
            # 2026-11-13 real case: BIB filled, UYM expired at day end with nothing filled.
            _kis_row("0031136421", "BIB", ord_qty=2, ccld_qty=2, nccs_qty=0, status="완료"),
            _kis_row("0031136453", "UYM", ord_qty=9, ccld_qty=0, nccs_qty=0, status="완료", reject="DFD 주문종료 취소"),
            _kis_row("0000000003", "IYW", ord_qty=5, ccld_qty=2, nccs_qty=0, status="완료", reject="DFD 주문종료 취소"),
            _kis_row("0000000004", "ROM", ord_qty=4, ccld_qty=1, nccs_qty=3, status="접수"),
            # An earlier UYM order in the same window must not be counted for 0031136453.
            _kis_row("0031130000", "UYM", ord_qty=9, ccld_qty=9, nccs_qty=0, status="완료"),
        ]
        cases = {
            ("0031136421", "BIB", 2): ("filled", True),
            ("0031136453", "UYM", 9): ("closed_unfilled", False),
            ("0000000003", "IYW", 5): ("closed_unfilled", False),  # partial fill, then expired
            ("0000000004", "ROM", 4): ("open", False),
            ("0099999999", "UYM", 9): ("not_found", False),  # no symbol fallback with an order number
        }
        for (order_no, symbol, qty), (state, full) in cases.items():
            with self.subTest(order_no=order_no):
                status = check_fills._summarize_order_fill(rows, _order(order_no, symbol, qty), fill_filter="00")
                self.assertEqual(status["order_state"], state)
                self.assertEqual(status["fully_filled"], full)
                self.assertFalse(status["weak_symbol_match"])
        uym = check_fills._summarize_order_fill(rows, _order("0031136453", "UYM", 9), fill_filter="00")
        self.assertEqual(uym["filled_qty"], 0.0)
        self.assertEqual(uym["matched_rows"], 1)

    def test_unknown_status_with_zero_open_is_not_closed(self) -> None:
        from us_stock_live.trading import check_fills

        rows = [_kis_row("1", "UYM", ord_qty=9, ccld_qty=0, nccs_qty=0, status="")]
        status = check_fills._summarize_order_fill(rows, _order("1", "UYM", 9), fill_filter="00")
        self.assertEqual(status["order_state"], "open")

    def test_fill_query_starts_at_order_date(self) -> None:
        args = SimpleNamespace(sleep_seconds=1.2, fill_lookback_days=3)
        command = us_auto._fill_command(
            execution_path=Path("x/20261113_234125_sell_execution.json"),
            output_path=Path("x/out.json"),
            args=args,
        )
        self.assertEqual(command[command.index("--start-date") + 1], "20261113")
        self.assertEqual(command[command.index("--fill-filter") + 1], "00")
        self.assertNotIn("--lookback-days", command)

        fallback = us_auto._fill_command(execution_path=Path("x/exec.json"), output_path=Path("x/o.json"), args=args)
        self.assertEqual(fallback[fallback.index("--lookback-days") + 1], "3")

    def _run_pending(self, directory: str, fills: dict, auto_state: dict) -> tuple[str, dict]:
        run_dir = Path(directory)
        execution = run_dir / "20261113_234125_sell_execution.json"
        write_json(execution, {"submitted_orders": []})
        fills_path = run_dir / "fills.json"
        write_json(fills_path, fills)
        state_path = run_dir / "auto_state.json"
        auto_state = {"phase": "sell_submitted", "sell_execution": str(execution), **auto_state}
        with patch.object(us_auto, "_check_fills", return_value=fills_path):
            result = us_auto._handle_pending(
                phase="sell",
                auto_state=auto_state,
                auto_state_path=state_path,
                run_dir=run_dir,
                state_json=run_dir / "state.json",
                args=SimpleNamespace(auto_finalize=True),
            )
        return result, read_json(state_path)

    def test_closed_unfilled_orders_replan(self) -> None:
        fills = {
            "all_submitted_orders_fully_filled": False,
            "all_submitted_orders_closed": True,
            "weak_match_used": False,
            "closed_unfilled_count": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            result, state = self._run_pending(directory, fills, {})
        self.assertEqual(result, "replan")
        self.assertEqual(state["phase"], "idle")
        self.assertEqual(state["sell_closed_unfilled_replans"], 1)

    def test_replan_limit_keeps_pending_state(self) -> None:
        fills = {
            "all_submitted_orders_fully_filled": False,
            "all_submitted_orders_closed": True,
            "weak_match_used": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self._run_pending(directory, fills, {"sell_closed_unfilled_replans": us_auto.MAX_CLOSED_UNFILLED_REPLANS})
            state = read_json(Path(directory) / "auto_state.json")
        self.assertEqual(state["phase"], "sell_submitted")
        self.assertEqual(state["reason"], "sell_orders_closed_unfilled_replan_limit")

    def test_working_orders_still_wait(self) -> None:
        fills = {
            "all_submitted_orders_fully_filled": False,
            "all_submitted_orders_closed": False,
            "weak_match_used": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            result, state = self._run_pending(directory, fills, {})
        self.assertEqual(result, "stop")
        self.assertEqual(state["phase"], "sell_submitted")
        self.assertEqual(state["reason"], "sell_orders_not_confirmed_filled")

    def test_filled_sells_clear_replan_count(self) -> None:
        fills = {"all_submitted_orders_fully_filled": True, "all_submitted_orders_closed": True, "weak_match_used": False}
        with tempfile.TemporaryDirectory() as directory:
            result, state = self._run_pending(directory, fills, {"sell_closed_unfilled_replans": 2})
        self.assertEqual(result, "continue_buy")
        self.assertNotIn("sell_closed_unfilled_replans", state)

    def test_main_replans_sell_then_buy_after_closed_sells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(run_dir / "auto_state.json", {"phase": "sell_submitted"})
            phases: list[str] = []

            def fake_new_phase(*, phase: str, auto_state: dict, **_: object) -> bool:
                phases.append(phase)
                return False

            argv = ["run_auto_rebalance.py", "--execute", "--run-dir", str(run_dir), "--state-json", str(run_dir / "s.json")]
            with patch.object(sys, "argv", argv), patch.object(
                us_auto, "resolve_profile_reference", return_value=(run_dir / "profile.json", {})
            ), patch.object(us_auto, "_handle_pending_with_retries", return_value="replan"), patch.object(
                us_auto, "_update_cache"
            ), patch.object(us_auto, "_run_new_phase", side_effect=fake_new_phase), patch.object(us_auto, "_write_report"):
                self.assertEqual(us_auto.main(), 0)
        self.assertEqual(phases, ["sell", "buy"])

    def test_main_stops_while_orders_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(run_dir / "auto_state.json", {"phase": "buy_submitted"})
            argv = ["run_auto_rebalance.py", "--execute", "--run-dir", str(run_dir), "--state-json", str(run_dir / "s.json")]
            with patch.object(sys, "argv", argv), patch.object(
                us_auto, "resolve_profile_reference", return_value=(run_dir / "profile.json", {})
            ), patch.object(us_auto, "_handle_pending_with_retries", return_value="stop"), patch.object(
                us_auto, "_run_new_phase"
            ) as new_phase:
                self.assertEqual(us_auto.main(), 0)
        new_phase.assert_not_called()


class UsBuyLimitBufferSizingTest(unittest.TestCase):
    """Buys must fit in cash at the buffered, cent-rounded limit price the broker reserves."""

    @staticmethod
    def _buy_row(symbol: str, price: float, qty: int, target: float) -> dict:
        return {
            "symbol": symbol,
            "price_usd": price,
            "action": "buy",
            "raw_action": "buy",
            "order_qty": qty,
            "raw_order_qty": qty,
            "order_notional_usd": qty * price,
            "raw_order_notional_usd": qty * price,
            "target_value_usd": target,
        }

    def test_buffered_limit_notional_fits_cash(self) -> None:
        from us_stock_live.trading import execute_plan, rebalance_plan

        # 2026-11-09 case: $455.50 cash, BIB 2 and IBB 1 planned for $454.60 at last price.
        for buffer_pct in (0.0, 0.5, 2.0, 3.0):
            with self.subTest(buffer_pct=buffer_pct):
                rows = [self._buy_row("BIB", 120.00, 2, 227.75), self._buy_row("IBB", 214.60, 1, 227.75)]
                cash = 455.50
                capacity = rebalance_plan._buy_cash_capacity(rows, cash, buffer_pct)
                rebalance_plan._reduce_buy_orders_to_cash(rows, capacity)
                reserved = sum(
                    int(row["order_qty"]) * float(execute_plan._limit_price(row, buffer_pct=buffer_pct))
                    for row in rows
                    if row["action"] == "buy"
                )
                self.assertLessEqual(reserved, cash)
        # At 2% the full order ($454.60 x 1.02 = $463.69) does not fit, so one share is dropped.
        rows = [self._buy_row("BIB", 120.00, 2, 227.75), self._buy_row("IBB", 214.60, 1, 227.75)]
        rebalance_plan._reduce_buy_orders_to_cash(rows, rebalance_plan._buy_cash_capacity(rows, 455.50, 2.0))
        self.assertEqual(sum(int(row["order_qty"]) for row in rows), 2)

    def test_zero_buffer_keeps_previous_sizing(self) -> None:
        from us_stock_live.trading import rebalance_plan

        rows = [self._buy_row("UYM", 30.0, 9, 270.0)]
        self.assertAlmostEqual(rebalance_plan._buy_cash_capacity(rows, 300.0, 0.0), 300.0 - 0.045)

    def test_plan_command_passes_buy_buffer(self) -> None:
        args = SimpleNamespace(
            min_order_usd=50.0,
            bootstrap_policy="empty-account",
            sleep_seconds=1.2,
            limit_buffer_pct=2.0,
            execute=False,
            equity_usd=None,
            cash_usd=None,
        )
        command = us_auto._plan_command(
            phase="buy", profile_json=Path("p.json"), state_json=Path("s.json"), plan_path=Path("x.json"), args=args
        )
        self.assertEqual(command[command.index("--buy-limit-buffer-pct") + 1], "2.0")


if __name__ == "__main__":
    unittest.main()
