#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.strategy.target_weights import DEFAULT_PROFILE_JSON, _resolve_path
from us_stock_live.active_profile import clear_pending_switch, resolve_profile_reference
from us_stock_live.trading.state import read_json, state_path_for_profile, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scheduler-safe stock-live auto rebalance runner.")
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="Stock-live profile JSON path.")
    parser.add_argument("--state-json", default="", help="Optional rebalance state JSON path.")
    parser.add_argument("--run-dir", default="", help="Optional output dir. Defaults to us_stock_live/.cache/runs/<profile>/auto.")
    parser.add_argument("--equity-usd", type=float, default=None, help="Optional equity override for plan generation.")
    parser.add_argument("--cash-usd", type=float, default=None, help="Optional cash override for plan generation.")
    parser.add_argument("--min-order-usd", type=float, default=50.0)
    parser.add_argument(
        "--bootstrap-policy",
        choices=["empty-account", "always", "never"],
        default="empty-account",
        help="Initial state handling. Use always only for an explicit profile switch or manual alignment.",
    )
    parser.add_argument("--execute", action="store_true", help="Submit real KIS orders. Omit for dry-run.")
    parser.add_argument("--confirm-live", action="store_true", help="Required by execute_plan.py for KIS_ENV=live.")
    parser.add_argument("--ignore-market-hours", action="store_true")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.0)
    parser.add_argument("--sleep-seconds", type=float, default=1.2)
    parser.add_argument("--fill-lookback-days", type=int, default=3)
    parser.add_argument("--skip-cache-update", action="store_true")
    parser.add_argument(
        "--auto-finalize",
        action="store_true",
        help="After a submitted buy phase is fully filled, mark the signal change executed.",
    )
    parser.add_argument(
        "--reset-pending",
        action="store_true",
        help="Clear auto pending state before running. Use only after manual inspection.",
    )
    parser.add_argument(
        "--continue-after-sell",
        action="store_true",
        help="After submitting sells, wait for fills and continue to buy in the same run when fully filled.",
    )
    parser.add_argument(
        "--continue-after-buy",
        action="store_true",
        help="After submitting buys, wait for fills and auto-finalize in the same run when fully filled.",
    )
    parser.add_argument("--fill-wait-seconds", type=float, default=20.0)
    parser.add_argument("--fill-retry-count", type=int, default=2)
    return parser


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def _state_json(profile_json: Path, explicit: str) -> Path:
    return _resolve_path(explicit) if explicit else state_path_for_profile(profile_json)


def _run_dir(profile_json: Path, explicit: str) -> Path:
    if explicit:
        return _resolve_path(explicit)
    return ROOT_DIR / "us_stock_live" / ".cache" / "runs" / profile_json.stem / "auto"


def _auto_state_path(run_dir: Path) -> Path:
    return run_dir / "auto_state.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_auto_state(path: Path, payload: dict[str, Any]) -> None:
    payload.setdefault("type", "stock_live_auto_state_v1")
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(path, payload)


def _py_command(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]


def _run(command: list[str], *, allow_exit_2: bool = False) -> int:
    result = subprocess.run(command, cwd=ROOT_DIR, capture_output=True, text=True)
    if result.returncode == 2 and allow_exit_2:
        _print_command_summary(result)
        return result.returncode
    if result.returncode == 0:
        _print_command_summary(result)
    else:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
    result.check_returncode()
    return result.returncode


def _run_execution_command(command: list[str], *, report_path: Path, execute: bool) -> int:
    result = subprocess.run(command, cwd=ROOT_DIR, capture_output=True, text=True)
    if result.returncode in {0, 2}:
        _print_command_summary(result)
        return result.returncode
    if result.returncode == 1 and report_path.exists():
        try:
            report = read_json(report_path)
        except (OSError, json.JSONDecodeError):
            report = {}
        submitted = _submitted_orders(report) if execute else _dry_or_submitted_orders(report)
        if submitted:
            _print_command_summary(result)
            return result.returncode
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    result.check_returncode()
    return result.returncode


def _try_last_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            return json.loads(text[index:])
        except json.JSONDecodeError:
            continue
    return None


def _print_command_summary(result: subprocess.CompletedProcess[str]) -> None:
    payload = _try_last_json(result.stdout or "")
    if payload:
        print(_summary_line(payload))
        return
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if lines:
        print(lines[-1])
    if result.stderr:
        err_lines = [line for line in result.stderr.splitlines() if line.strip()]
        if err_lines:
            print(err_lines[-1], file=sys.stderr)


def _summary_line(payload: dict[str, Any]) -> str:
    payload_type = payload.get("type")
    if payload_type == "stock_live_rebalance_plan_v1":
        return (
            "plan "
            f"phase={payload.get('phase')} "
            f"allowed={payload.get('rebalance_allowed')}({payload.get('rebalance_reason')}) "
            f"orders={len(_nonzero_plan_orders(payload))} "
            f"buy=${float(payload.get('planned_buy_notional_usd') or 0):.2f} "
            f"sell=${float(payload.get('planned_sell_notional_usd') or 0):.2f}"
        )
    if payload_type == "stock_live_execution_report_v1":
        return (
            "execution "
            f"phase={payload.get('phase')} "
            f"execute={payload.get('execute')} "
            f"orders={len(payload.get('orders', []))} "
            f"submitted={len(payload.get('submitted_orders', []))} "
            f"blocked={payload.get('blocked_reasons') or payload.get('blocked_reason')}"
        )
    if payload_type == "stock_live_fill_report_v1":
        return (
            "fills "
            f"execution={payload.get('execution_report_id')} "
            f"submitted={payload.get('submitted_order_count')} "
            f"full={payload.get('fully_filled_count')} "
            f"all_full={payload.get('all_submitted_orders_fully_filled')} "
            f"weak={payload.get('weak_match_used')}"
        )
    if payload_type == "stock_live_auto_report_v1":
        return f"auto action={payload.get('action')} reason={payload.get('reason')} phase={payload.get('phase')}"
    return json.dumps(payload, ensure_ascii=False)


def _maybe_num_arg(command: list[str], flag: str, value: float | None) -> None:
    if value is not None:
        command.extend([flag, str(value)])


def _plan_command(
    *,
    phase: str,
    profile_json: Path,
    state_json: Path,
    plan_path: Path,
    args: argparse.Namespace,
) -> list[str]:
    command = _py_command(
        "us_stock_live/trading/rebalance_plan.py",
        "--profile-json",
        str(profile_json),
        "--state-json",
        str(state_json),
        "--phase",
        phase,
        "--min-order-usd",
        str(args.min_order_usd),
        "--bootstrap-policy",
        args.bootstrap_policy,
        "--sleep-seconds",
        str(args.sleep_seconds),
        "--output-json",
        str(plan_path),
        "--format",
        "json",
    )
    if args.execute:
        command.extend(["--save-plan", str(plan_path)])
    _maybe_num_arg(command, "--equity-usd", args.equity_usd)
    _maybe_num_arg(command, "--cash-usd", args.cash_usd)
    return command


def _execute_command(
    *,
    plan_path: Path,
    state_json: Path,
    report_path: Path,
    args: argparse.Namespace,
) -> list[str]:
    command = _py_command(
        "us_stock_live/trading/execute_plan.py",
        "--plan-json",
        str(plan_path),
        "--state-json",
        str(state_json),
        "--output-json",
        str(report_path),
        "--limit-buffer-pct",
        str(args.limit_buffer_pct),
        "--sleep-seconds",
        str(args.sleep_seconds),
    )
    if args.execute:
        command.append("--execute")
    if args.confirm_live:
        command.append("--confirm-live")
    if args.ignore_market_hours:
        command.append("--ignore-market-hours")
    return command


def _fill_command(*, execution_path: Path, output_path: Path, args: argparse.Namespace) -> list[str]:
    return _py_command(
        "us_stock_live/trading/check_fills.py",
        "--execution-json",
        str(execution_path),
        "--fill-filter",
        "01",
        "--lookback-days",
        str(args.fill_lookback_days),
        "--sleep-seconds",
        str(args.sleep_seconds),
        "--output-json",
        str(output_path),
    )


def _nonzero_plan_orders(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in plan.get("rows", [])
        if row.get("action") in {"buy", "sell"} and int(row.get("order_qty") or 0) > 0
    ]


def _submitted_orders(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        order
        for order in report.get("submitted_orders", [])
        if str(order.get("status") or "").lower() == "submitted"
    ]


def _dry_or_submitted_orders(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        order
        for order in report.get("submitted_orders", [])
        if str(order.get("status") or "").lower() in {"submitted", "dry_run"}
    ]


def _update_cache(profile_json: Path, *, skip: bool) -> None:
    if skip:
        print("skip_cache_update=True")
        return
    _run(_py_command("us_stock_live/data/update_cache.py", "--profile-json", str(profile_json)))


def _build_plan(phase: str, *, profile_json: Path, state_json: Path, run_dir: Path, args: argparse.Namespace) -> Path:
    path = run_dir / f"{_stamp()}_{phase}_plan.json"
    _run(_plan_command(phase=phase, profile_json=profile_json, state_json=state_json, plan_path=path, args=args))
    return path


def _execute_plan(phase: str, *, plan_path: Path, state_json: Path, run_dir: Path, args: argparse.Namespace) -> Path:
    path = run_dir / f"{_stamp()}_{phase}_execution.json"
    _run_execution_command(
        _execute_command(plan_path=plan_path, state_json=state_json, report_path=path, args=args),
        report_path=path,
        execute=bool(args.execute),
    )
    return path


def _check_fills(phase: str, *, execution_path: Path, run_dir: Path, args: argparse.Namespace) -> Path:
    path = run_dir / f"{_stamp()}_{phase}_fills.json"
    _run(_fill_command(execution_path=execution_path, output_path=path, args=args), allow_exit_2=True)
    return path


def _write_report(run_dir: Path, payload: dict[str, Any]) -> None:
    payload.setdefault("type", "stock_live_auto_report_v1")
    payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    report_path = run_dir / "latest_auto_report.json"
    write_json(report_path, payload)
    print(_summary_line(payload))
    print(f"report={_rel(report_path)}")


def _pending_execution_path(auto_state: dict[str, Any], phase: str) -> Path | None:
    key = f"{phase}_execution"
    value = auto_state.get(key)
    if not value:
        return None
    return _resolve_path(str(value))


def _mark_done(
    *,
    plan_path: Path,
    state_json: Path,
    args: argparse.Namespace,
) -> None:
    command = _py_command(
        "us_stock_live/trading/mark_executed.py",
        "--plan-json",
        str(plan_path),
        "--state-json",
        str(state_json),
        "--confirm",
        "--note",
        "auto_rebalance_filled",
    )
    _run(command)


def _handle_pending(
    *,
    phase: str,
    auto_state: dict[str, Any],
    auto_state_path: Path,
    run_dir: Path,
    state_json: Path,
    args: argparse.Namespace,
) -> str:
    execution_path = _pending_execution_path(auto_state, phase)
    if execution_path is None or not execution_path.exists():
        auto_state["phase"] = "idle"
        auto_state["reason"] = f"missing_{phase}_execution_report"
        _write_auto_state(auto_state_path, auto_state)
        _write_report(run_dir, {"action": "blocked", "phase": phase, "reason": auto_state["reason"]})
        return "stop"

    fills_path = _check_fills(phase, execution_path=execution_path, run_dir=run_dir, args=args)
    fills = read_json(fills_path)
    auto_state[f"{phase}_fills"] = _rel(fills_path)
    if not fills.get("all_submitted_orders_fully_filled") or fills.get("weak_match_used"):
        auto_state["reason"] = f"{phase}_orders_not_confirmed_filled"
        _write_auto_state(auto_state_path, auto_state)
        _write_report(
            run_dir,
            {
                "action": "wait",
                "phase": phase,
                "reason": auto_state["reason"],
                "fills": _rel(fills_path),
            },
        )
        return "stop"

    if str(auto_state.get(f"{phase}_submit_status") or "") == "partial_submitted":
        auto_state["phase"] = "idle"
        auto_state["reason"] = f"{phase}_partial_filled_replan_required"
        _write_auto_state(auto_state_path, auto_state)
        _write_report(
            run_dir,
            {
                "action": "replan",
                "phase": phase,
                "reason": auto_state["reason"],
                "fills": _rel(fills_path),
            },
        )
        return "stop"

    if phase == "sell":
        auto_state["phase"] = "idle"
        auto_state["reason"] = "sell_filled"
        _write_auto_state(auto_state_path, auto_state)
        return "continue_buy"

    if phase == "buy":
        if not args.auto_finalize:
            auto_state["reason"] = "buy_filled_waiting_auto_finalize_flag"
            _write_auto_state(auto_state_path, auto_state)
            _write_report(
                run_dir,
                {
                    "action": "wait",
                    "phase": phase,
                    "reason": auto_state["reason"],
                    "fills": _rel(fills_path),
                },
            )
            return "stop"
        buy_plan = _resolve_path(str(auto_state.get("buy_plan") or ""))
        _mark_done(plan_path=buy_plan, state_json=state_json, args=args)
        auto_state["phase"] = "done"
        auto_state["reason"] = "buy_filled_marked_executed"
        _write_auto_state(auto_state_path, auto_state)
        _write_report(run_dir, {"action": "finalized", "phase": phase, "reason": auto_state["reason"]})
        return "stop"

    return "stop"


def _handle_pending_with_retries(
    *,
    phase: str,
    auto_state: dict[str, Any],
    auto_state_path: Path,
    run_dir: Path,
    state_json: Path,
    args: argparse.Namespace,
    wait_before_first: bool,
) -> str:
    attempts = max(int(args.fill_retry_count), 1)
    for attempt in range(attempts):
        if args.fill_wait_seconds > 0 and (wait_before_first or attempt > 0):
            time.sleep(args.fill_wait_seconds)
        result = _handle_pending(
            phase=phase,
            auto_state=auto_state,
            auto_state_path=auto_state_path,
            run_dir=run_dir,
            state_json=state_json,
            args=args,
        )
        if result != "stop":
            return result
        if str(auto_state.get("reason") or "") != f"{phase}_orders_not_confirmed_filled":
            return result
    return "stop"


def _maybe_continue_buy_after_submission(
    *,
    auto_state: dict[str, Any],
    auto_state_path: Path,
    run_dir: Path,
    state_json: Path,
    args: argparse.Namespace,
) -> None:
    if args.execute and args.continue_after_buy and str(auto_state.get("phase") or "") == "buy_submitted":
        _handle_pending_with_retries(
            phase="buy",
            auto_state=auto_state,
            auto_state_path=auto_state_path,
            run_dir=run_dir,
            state_json=state_json,
            args=args,
            wait_before_first=True,
        )


def _run_new_phase(
    *,
    phase: str,
    profile_json: Path,
    state_json: Path,
    auto_state: dict[str, Any],
    auto_state_path: Path,
    run_dir: Path,
    args: argparse.Namespace,
) -> bool:
    plan_path = _build_plan(phase, profile_json=profile_json, state_json=state_json, run_dir=run_dir, args=args)
    plan = read_json(plan_path)
    auto_state[f"{phase}_plan"] = _rel(plan_path)

    orders = _nonzero_plan_orders(plan)
    if not plan.get("rebalance_allowed"):
        auto_state["phase"] = "idle"
        auto_state["reason"] = str(plan.get("rebalance_reason"))
        _write_auto_state(auto_state_path, auto_state)
        _write_report(run_dir, {"action": "noop", "phase": phase, "reason": auto_state["reason"]})
        return True
    if not orders:
        auto_state["reason"] = f"no_{phase}_orders"
        _write_auto_state(auto_state_path, auto_state)
        return False

    execution_path = _execute_plan(phase, plan_path=plan_path, state_json=state_json, run_dir=run_dir, args=args)
    execution = read_json(execution_path)
    auto_state[f"{phase}_execution"] = _rel(execution_path)
    submitted = _submitted_orders(execution) if args.execute else _dry_or_submitted_orders(execution)
    errors = [
        order
        for order in execution.get("submitted_orders", [])
        if str(order.get("status") or "").lower() == "error"
    ]
    auto_state[f"{phase}_submitted_count"] = len(submitted)
    auto_state[f"{phase}_error_count"] = len(errors)
    if not submitted:
        auto_state["phase"] = "idle"
        auto_state["reason"] = execution.get("blocked_reason") or "no_orders_submitted"
        auto_state[f"{phase}_submit_status"] = "blocked"
        _write_auto_state(auto_state_path, auto_state)
        _write_report(
            run_dir,
            {
                "action": "blocked",
                "phase": phase,
                "reason": auto_state["reason"],
                "execution": _rel(execution_path),
            },
        )
        return True

    if args.execute:
        auto_state["phase"] = f"{phase}_submitted"
        if errors:
            auto_state[f"{phase}_submit_status"] = "partial_submitted"
            auto_state["reason"] = f"{phase}_orders_partial_submitted"
        else:
            auto_state[f"{phase}_submit_status"] = "submitted"
            auto_state["reason"] = f"{phase}_orders_submitted"
    else:
        auto_state["phase"] = "dry_run"
        auto_state[f"{phase}_submit_status"] = "dry_run"
        auto_state["reason"] = f"{phase}_orders_dry_run"
    _write_auto_state(auto_state_path, auto_state)
    _write_report(
        run_dir,
        {
            "action": "submitted" if args.execute else "dry_run",
            "phase": phase,
            "reason": auto_state["reason"],
            "execution": _rel(execution_path),
        },
    )
    return True


def main() -> int:
    args = build_parser().parse_args()
    profile_reference = _resolve_path(args.profile_json)
    profile_json, active_profile = resolve_profile_reference(profile_reference)
    state_json = _state_json(profile_json, args.state_json)
    if active_profile:
        pending_switch = active_profile.get("pending_switch") or {}
        pending_profile_json = str(pending_switch.get("profile_json") or "")
        if pending_profile_json:
            pending_path = _resolve_path(pending_profile_json)
            if pending_path == profile_json:
                existing_state = _load_json(state_json)
                if existing_state.get("last_executed_change_timestamp"):
                    clear_pending_switch(profile_reference)
                elif args.bootstrap_policy == "empty-account":
                    args.bootstrap_policy = str(pending_switch.get("bootstrap_policy") or "always")
    run_dir = _run_dir(profile_json, args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    auto_state_path = _auto_state_path(run_dir)
    auto_state = {} if args.reset_pending else _load_json(auto_state_path)
    auto_state.update(
        {
            "profile_json": _rel(profile_json),
            "profile_reference": _rel(profile_reference),
            "state_json": _rel(state_json),
            "run_dir": _rel(run_dir),
            "execute": bool(args.execute),
        }
    )

    phase = str(auto_state.get("phase") or "idle")
    if args.execute and phase == "sell_submitted":
        pending_result = _handle_pending_with_retries(
            phase="sell",
            auto_state=auto_state,
            auto_state_path=auto_state_path,
            run_dir=run_dir,
            state_json=state_json,
            args=args,
            wait_before_first=False,
        )
        if pending_result == "stop":
            return 0
        _update_cache(profile_json, skip=args.skip_cache_update)
        stopped = _run_new_phase(
            phase="buy",
            profile_json=profile_json,
            state_json=state_json,
            auto_state=auto_state,
            auto_state_path=auto_state_path,
            run_dir=run_dir,
            args=args,
        )
        if not stopped:
            auto_state["phase"] = "idle"
            auto_state["reason"] = "sell_filled_no_buy_orders"
            _write_auto_state(auto_state_path, auto_state)
            _write_report(run_dir, {"action": "noop", "phase": "buy", "reason": auto_state["reason"]})
        else:
            _maybe_continue_buy_after_submission(
                auto_state=auto_state,
                auto_state_path=auto_state_path,
                run_dir=run_dir,
                state_json=state_json,
                args=args,
            )
        return 0
    elif args.execute and phase == "buy_submitted":
        _handle_pending_with_retries(
            phase="buy",
            auto_state=auto_state,
            auto_state_path=auto_state_path,
            run_dir=run_dir,
            state_json=state_json,
            args=args,
            wait_before_first=False,
        )
        return 0

    _update_cache(profile_json, skip=args.skip_cache_update)

    # Sell first. If there is nothing to sell, the runner can immediately size buys
    # with current cash; if sells are submitted, the next scheduler tick waits for fills.
    stopped = _run_new_phase(
        phase="sell",
        profile_json=profile_json,
        state_json=state_json,
        auto_state=auto_state,
        auto_state_path=auto_state_path,
        run_dir=run_dir,
        args=args,
    )
    if stopped:
        if args.execute and args.continue_after_sell and str(auto_state.get("phase") or "") == "sell_submitted":
            pending_result = _handle_pending_with_retries(
                phase="sell",
                auto_state=auto_state,
                auto_state_path=auto_state_path,
                run_dir=run_dir,
                state_json=state_json,
                args=args,
                wait_before_first=True,
            )
            if pending_result == "stop":
                return 0
            _update_cache(profile_json, skip=args.skip_cache_update)
            stopped = _run_new_phase(
                phase="buy",
                profile_json=profile_json,
                state_json=state_json,
                auto_state=auto_state,
                auto_state_path=auto_state_path,
                run_dir=run_dir,
                args=args,
            )
            if not stopped:
                auto_state["phase"] = "idle"
                auto_state["reason"] = "sell_filled_no_buy_orders"
                _write_auto_state(auto_state_path, auto_state)
                _write_report(run_dir, {"action": "noop", "phase": "buy", "reason": auto_state["reason"]})
            else:
                _maybe_continue_buy_after_submission(
                    auto_state=auto_state,
                    auto_state_path=auto_state_path,
                    run_dir=run_dir,
                    state_json=state_json,
                    args=args,
                )
            return 0
        return 0

    stopped = _run_new_phase(
        phase="buy",
        profile_json=profile_json,
        state_json=state_json,
        auto_state=auto_state,
        auto_state_path=auto_state_path,
        run_dir=run_dir,
        args=args,
    )
    if stopped:
        _maybe_continue_buy_after_submission(
            auto_state=auto_state,
            auto_state_path=auto_state_path,
            run_dir=run_dir,
            state_json=state_json,
            args=args,
        )
        return 0
    if not stopped:
        auto_state["phase"] = "idle"
        auto_state["reason"] = "no_sell_or_buy_orders"
        _write_auto_state(auto_state_path, auto_state)
        _write_report(run_dir, {"action": "noop", "phase": "buy", "reason": auto_state["reason"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
