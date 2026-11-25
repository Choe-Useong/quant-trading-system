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

    def test_rows_repeated_across_exchange_queries_count_once(self) -> None:
        from us_stock_live.trading import check_fills

        # 2026-11-20: the NASD query also returned the AMEX ERX order, so it appeared twice.
        # A 2-of-3 partial fill that then expired must stay closed_unfilled, not count as 4 filled.
        partial = _kis_row("0030000001", "ERX", ord_qty=3, ccld_qty=2, nccs_qty=0, status="완료", reject="DFD 주문종료 취소")
        rows = check_fills._dedupe_rows([partial, dict(partial)])
        self.assertEqual(len(rows), 1)
        status = check_fills._summarize_order_fill(rows, _order("0030000001", "ERX", 3), fill_filter="00")
        self.assertEqual(status["filled_qty"], 2.0)
        self.assertEqual(status["order_state"], "closed_unfilled")
        self.assertFalse(status["fully_filled"])

        # Distinct rows of the same order are kept.
        other = dict(partial, ft_ccld_qty="1")
        self.assertEqual(len(check_fills._dedupe_rows([partial, other])), 2)

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


def _kr_row(odno: str, pdno: str, *, ord_dt: str, ord_qty: int, ccld_qty: int, rmn_qty: int, cncl: int = 0, rjct: int = 0) -> dict:
    # Field names and values as returned by KIS inquire-daily-ccld (TTTC0081R) for 2026-08-04.
    return {
        "ord_dt": ord_dt,
        "odno": odno,
        "orgn_odno": "",
        "pdno": pdno,
        "ord_qty": str(ord_qty),
        "tot_ccld_qty": str(ccld_qty),
        "rmn_qty": str(rmn_qty),
        "cncl_cfrm_qty": str(cncl),
        "rjct_qty": str(rjct),
        "cncl_yn": "",
    }


class KrFillStateTest(unittest.TestCase):
    """LIVE-001 (domestic): ended day orders with unfilled quantity must lead to a replan."""

    def test_order_states(self) -> None:
        from kr_stock_live.trading import check_fills

        today = "20260805"
        rows = [
            _kr_row("0005482700", "395270", ord_dt="20260804", ord_qty=23, ccld_qty=23, rmn_qty=0),
            # Previous-day order left with remaining quantity: a regular-session day order has ended.
            _kr_row("0005484600", "446690", ord_dt="20260804", ord_qty=10, ccld_qty=4, rmn_qty=6),
            # Same-day order still working.
            _kr_row("0005490000", "069500", ord_dt=today, ord_qty=5, ccld_qty=0, rmn_qty=5),
            # Same-day order cancelled or rejected with nothing remaining.
            _kr_row("0005490001", "069500", ord_dt=today, ord_qty=5, ccld_qty=1, rmn_qty=0, cncl=4),
            _kr_row("0005490002", "069500", ord_dt=today, ord_qty=5, ccld_qty=0, rmn_qty=0, rjct=5),
            # Same-day, nothing remaining, no cancel/reject reported: not enough to call it ended.
            _kr_row("0005490003", "069500", ord_dt=today, ord_qty=5, ccld_qty=0, rmn_qty=0),
            # Earlier order of the same symbol that must not be counted for 0005499999.
            _kr_row("0005400000", "446690", ord_dt="20260803", ord_qty=10, ccld_qty=10, rmn_qty=0),
        ]
        cases = {
            ("0005482700", "395270", 23): "filled",
            ("0005484600", "446690", 10): "closed_unfilled",
            ("0005490000", "069500", 5): "open",
            ("0005490001", "069500", 5): "closed_unfilled",
            ("0005490002", "069500", 5): "closed_unfilled",
            ("0005490003", "069500", 5): "open",
            ("0005499999", "446690", 10): "not_found",
        }
        for (order_no, symbol, qty), state in cases.items():
            with self.subTest(order_no=order_no):
                status = check_fills._summarize_order_fill(rows, _order(order_no, symbol, qty), fill_filter="00", today=today)
                self.assertEqual(status["order_state"], state)
                self.assertEqual(status["fully_filled"], state == "filled")
                self.assertFalse(status["weak_symbol_match"])

    def test_fill_query_starts_at_order_date(self) -> None:
        args = SimpleNamespace(sleep_seconds=1.2, fill_lookback_days=3)
        command = kr_auto._fill_command(
            execution_path=Path("x/20260804_092426_sell_execution.json"), output_path=Path("x/o.json"), args=args
        )
        self.assertEqual(command[command.index("--start-date") + 1], "20260804")
        self.assertEqual(command[command.index("--fill-filter") + 1], "00")

    def _run_pending(self, directory: str, fills: dict, auto_state: dict) -> tuple[str, dict]:
        run_dir = Path(directory)
        execution = run_dir / "20260804_092426_sell_execution.json"
        write_json(execution, {"submitted_orders": []})
        fills_path = run_dir / "fills.json"
        write_json(fills_path, fills)
        state_path = run_dir / "auto_state.json"
        auto_state = {"phase": "sell_submitted", "sell_execution": str(execution), **auto_state}
        with patch.object(kr_auto, "_check_fills", return_value=fills_path):
            result = kr_auto._handle_pending(
                phase="sell",
                auto_state=auto_state,
                auto_state_path=state_path,
                run_dir=run_dir,
                state_json=run_dir / "state.json",
                args=SimpleNamespace(auto_finalize=True),
            )
        return result, read_json(state_path)

    def test_pending_outcomes(self) -> None:
        closed = {"all_submitted_orders_fully_filled": False, "all_submitted_orders_closed": True, "weak_match_used": False}
        working = {"all_submitted_orders_fully_filled": False, "all_submitted_orders_closed": False, "weak_match_used": False}
        with tempfile.TemporaryDirectory() as directory:
            result, state = self._run_pending(directory, closed, {})
        self.assertEqual((result, state["phase"], state["sell_closed_unfilled_replans"]), ("replan", "idle", 1))
        with tempfile.TemporaryDirectory() as directory:
            result, state = self._run_pending(directory, working, {})
        self.assertEqual((result, state["phase"]), ("stop", "sell_submitted"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self._run_pending(directory, closed, {"sell_closed_unfilled_replans": kr_auto.MAX_CLOSED_UNFILLED_REPLANS})
            self.assertEqual(read_json(Path(directory) / "auto_state.json")["phase"], "sell_submitted")

    def test_main_replans_after_closed_buys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(run_dir / "auto_state.json", {"phase": "buy_submitted"})
            phases: list[str] = []

            def fake_new_phase(*, phase: str, **_: object) -> bool:
                phases.append(phase)
                return False

            argv = ["run_auto_rebalance.py", "--execute", "--run-dir", str(run_dir), "--state-json", str(run_dir / "s.json")]
            with patch.object(sys, "argv", argv), patch.object(
                kr_auto, "resolve_profile_reference", return_value=(run_dir / "profile.json", None)
            ), patch.object(kr_auto, "_handle_pending_with_retries", return_value="replan"), patch.object(
                kr_auto, "_update_cache"
            ), patch.object(kr_auto, "_run_new_phase", side_effect=fake_new_phase), patch.object(kr_auto, "_write_report"):
                self.assertEqual(kr_auto.main(), 0)
        self.assertEqual(phases, ["sell", "buy"])


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


class LiveNotificationMessageTest(unittest.TestCase):
    """Telegram messages: readable summary, no account value, cash, prices or weights."""

    RUN_TS = "2026-11-20 23:40:00"

    def _message(self, directory: str, *, runner: str = "us", exit_code: int = 0, snapshot_exit_code: int = 0,
                 report: dict | None = None, log_text: str = "", files: dict | None = None) -> str:
        from live_common import notify_live_result as notify

        root = Path(directory)
        run_dir = root / "run"
        run_dir.mkdir()
        if report is not None:
            write_json(run_dir / "latest_auto_report.json", report)
        for name, payload in (files or {}).items():
            write_json(run_dir / name, payload)
        snapshot_path = root / "snapshot.json"
        write_json(
            snapshot_path,
            {
                "estimated_total_usd": 578.5,
                "orderable_cash_usd": 86.93,
                "positions": [
                    {"symbol": "ERX", "qty": 3.0, "price_usd": 95.81, "value_usd": 287.43, "pnl_pct": -0.3, "weight": 0.495},
                    {"symbol": "IBB", "qty": 1.0, "price_usd": 204.96, "value_usd": 204.96, "pnl_pct": 0.2, "weight": 0.354},
                ],
            },
        )
        log_path = root / "run.log"
        log_path.write_bytes((f"[{self.RUN_TS}] mode=live\n" + log_text).encode("cp949"))
        args = notify.build_parser().parse_args(
            [
                "--system-label", "US Stock" if runner == "us" else "KR ETF",
                "--runner", runner,
                "--mode", "live",
                "--profile-json", str(root / "profile.json"),
                "--exit-code", str(exit_code),
                "--snapshot-exit-code", str(snapshot_exit_code),
                "--run-ts", self.RUN_TS,
                "--log-file", str(log_path),
            ]
        )
        with patch.object(notify, "_run_dir", return_value=run_dir), patch.object(
            notify, "_latest_snapshot_path", return_value=snapshot_path
        ):
            return notify.build_message(args)

    def assert_no_money(self, message: str) -> None:
        for token in ("578", "86.93", "95.81", "204.96", "287", "USD", "$", "%", "원", "KRW"):
            self.assertNotIn(token, message)

    def test_no_trade_day_is_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            message = self._message(directory, report={"action": "noop", "reason": "no_sell_or_buy_orders"})
        self.assertEqual(message, "✅ 해외 주식 · 매매 없음 (11/20 23:40)")

    def test_rebalance_lists_orders_and_holdings_without_money(self) -> None:
        files = {
            "20261120_234109_sell_execution.json": {
                "execution_report_id": "e1",
                "submitted_orders": [{"symbol": "UYM", "side": "sell", "qty": 9, "order_no": "1", "status": "submitted", "limit_price": "26.82"}],
            },
            "20261120_234135_sell_fills.json": {"execution_report_id": "e1", "order_fill_status": [{"order_no": "1", "order_state": "filled"}]},
            "20261120_234210_buy_execution.json": {
                "execution_report_id": "e2",
                "submitted_orders": [
                    {"symbol": "ERX", "side": "buy", "qty": 3, "order_no": "2", "status": "submitted", "limit_price": "97.97"},
                    {"symbol": "IBB", "side": "buy", "qty": 1, "order_no": "3", "status": "submitted", "limit_price": "208.72"},
                ],
            },
            "20261120_234240_buy_fills.json": {
                "execution_report_id": "e2",
                "order_fill_status": [{"order_no": "2", "order_state": "filled"}, {"order_no": "3", "order_state": "filled"}],
            },
            # A later run's report must not appear in this run's message.
            "20261121_234109_sell_execution.json": {
                "submitted_orders": [{"symbol": "ROM", "side": "sell", "qty": 2, "order_no": "9", "status": "submitted"}],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            message = self._message(directory, report={"action": "finalized", "reason": "buy_filled_marked_executed"}, files=files)
        self.assertEqual(
            message,
            "✅ 해외 주식 · 리밸런싱 완료\n11/20 23:40\n\n매도  UYM 9주\n매수  ERX 3주 · IBB 1주\n\n보유  ERX · IBB\n할 일  없음",
        )
        self.assert_no_money(message)

    def test_fill_wait_is_explained(self) -> None:
        log = "auto action=wait reason=sell_orders_not_confirmed_filled phase=sell\nTraceback (most recent call last):\n    raise RuntimeError(\nRuntimeError: sell orders were not confirmed fully filled after 5 attempts; pending order state was preserved\n[exit_code=1]\n"
        files = {
            "20261120_234109_sell_execution.json": {
                "submitted_orders": [{"symbol": "UYM", "side": "sell", "qty": 9, "order_no": "1", "status": "submitted"}],
            },
            "20261120_234135_sell_fills.json": {"order_fill_status": [{"order_no": "1", "order_state": "open"}]},
        }
        with tempfile.TemporaryDirectory() as directory:
            message = self._message(directory, exit_code=1, report={"action": "wait"}, log_text=log, files=files)
        self.assertTrue(message.startswith("⚠️ 해외 주식 · 매도 체결 대기"))
        self.assertIn("매도  UYM 9주 (체결 대기)", message)
        self.assertIn("할 일  없음", message)
        self.assertNotIn("Traceback", message)
        self.assert_no_money(message)

    def test_kis_error_in_cp949_log_beats_wrapper_error(self) -> None:
        log = (
            'live_common.kis_base.KISError: KIS HTTP 403: {"error_description":"서버 에러가 발생했습니다.","error_code":"EGW00002"}\n'
            "Traceback (most recent call last):\n"
            "subprocess.CalledProcessError: Command '['python.exe', 'rebalance_plan.py']' returned non-zero exit status 1.\n"
            "[exit_code=1]\n"
            "live_common.kis_base.KISError: KIS API error: 상품이 없습니다\n"  # snapshot error after the run
        )
        with tempfile.TemporaryDirectory() as directory:
            message = self._message(directory, runner="kr", exit_code=1, snapshot_exit_code=1, report={"action": "noop"}, log_text=log)
        self.assertTrue(message.startswith("❌ 국내 ETF · 한투 서버 응답 오류"))
        self.assertIn("계좌 조회도 실패", message)
        self.assertNotIn("CalledProcessError", message)

    def test_unknown_error_shows_one_line_and_log(self) -> None:
        log = "ValueError: Profile data.tickers must not be empty\n[exit_code=1]\n"
        with tempfile.TemporaryDirectory() as directory:
            message = self._message(directory, exit_code=1, report={"action": "noop"}, log_text=log)
        self.assertTrue(message.startswith("❌ 해외 주식 · 실행 실패"))
        self.assertIn("Profile data.tickers must not be empty", message)
        self.assertIn("로그  run.log", message)

    def test_kr_codes_show_etf_names(self) -> None:
        from live_common import notify_live_result as notify

        with patch.object(notify, "_KR_NAMES", {"395270": "HANARO Fn K-반도체"}):
            self.assertEqual(notify._display_symbol("kr", "395270"), "HANARO Fn K-반도체")
            self.assertEqual(notify._display_symbol("kr", "999999"), "999999")
            self.assertEqual(notify._display_symbol("us", "ERX"), "ERX")


class FuturesNotificationTest(unittest.TestCase):
    """Futures demo alerts only on problems; a failure repeated every hour alerts once."""

    PAPER_RUN = "2026-11-21 05:00:12"

    def _args(self, directory: str, exit_code: int, log_text: str, *, extra: list[str] | None = None):
        from live_common import notify_task_result as task

        root = Path(directory)
        log_path = root / "demo.log"
        log_path.write_text(
            "[2026-11-21 04:00:10 UTC] demo run (paper exit_code=0)\nRuntimeError: an older run's error\n[exit_code=1]\n"
            f"[{self.PAPER_RUN} UTC] demo run (paper exit_code=0)\n{log_text}[exit_code={exit_code}]\n",
            encoding="utf-8",
        )
        return task.build_parser().parse_args(
            [
                "--system-label", "Binance Futures Demo",
                "--task-label", "futures_demo",
                "--exit-code", str(exit_code),
                "--run-ts", self.PAPER_RUN,
                "--utc",
                "--log-file", str(log_path),
                "--log-marker", f"[{self.PAPER_RUN} UTC] demo run",
                "--only-on-failure",
                "--ignore-exit-codes", "2",
                "--dedupe-state", str(root / "state.json"),
                *(extra or []),
            ]
        )

    def test_success_and_handled_exit_are_silent(self) -> None:
        from live_common import notify_task_result as task

        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(task.build_message(self._args(directory, 0, "")))
            self.assertIsNone(task.build_message(self._args(directory, 2, "")))

    def test_known_error_once_then_recovery(self) -> None:
        from live_common import notify_task_result as task

        stale = "StaleCacheError: Live cache pending bar 2026-11-21 05:00:00+00:00 is not the current bar\n"
        with tempfile.TemporaryDirectory() as directory:
            first = task.build_message(self._args(directory, 1, stale))
            self.assertEqual(
                first,
                "❌ 선물 데모 · 데이터가 최신이 아님\n11/21 14:00\n\n"
                "라이브 캐시가 현재 시각 봉까지 갱신되지 않아 주문하지 않았습니다.\n"
                "페이퍼 실행(데이터 갱신)이 실패했거나 늦었을 수 있습니다.\n\n할 일  계속되면 페이퍼 로그 확인",
            )
            # Same failure an hour later (different bar timestamp): no second alert.
            later = stale.replace("05:00:00", "06:00:00")
            self.assertIsNone(task.build_message(self._args(directory, 1, later)))
            # A different failure alerts again.
            other = task.build_message(self._args(directory, 1, "RuntimeError: Account is in hedge position mode; switch to one-way mode\n"))
            self.assertTrue(other.startswith("❌ 선물 데모 · 계좌 모드 오류"))
            # First success afterwards: one recovery line, then silence.
            recovered = task.build_message(self._args(directory, 0, ""))
            self.assertEqual(recovered, "✅ 선물 데모 · 데모 주문 정상 복구 (11/21 14:00, 11/21 14:00부터 실패)")
            self.assertIsNone(task.build_message(self._args(directory, 0, "")))

    def test_unknown_error_uses_this_run_only(self) -> None:
        from live_common import notify_task_result as task

        with tempfile.TemporaryDirectory() as directory:
            message = task.build_message(self._args(directory, 1, "KeyError: 'marginBalance'\n"))
        self.assertIn("'marginBalance'", message)
        self.assertNotIn("older run", message)
        self.assertIn("로그  demo.log", message)


if __name__ == "__main__":
    unittest.main()
