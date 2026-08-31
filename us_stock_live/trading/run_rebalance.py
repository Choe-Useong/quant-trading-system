#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from us_stock_live.strategy.target_weights import DEFAULT_PROFILE_JSON, _resolve_path
from us_stock_live.trading.state import read_json, state_path_for_profile, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage-based stock-live rebalance runner.")
    parser.add_argument(
        "--stage",
        choices=["prepare", "sell", "buy", "fills", "finalize"],
        required=True,
        help="prepare builds plans; sell/buy execute one phase; fills queries fills; finalize marks executed.",
    )
    parser.add_argument("--profile-json", default=str(DEFAULT_PROFILE_JSON), help="Stock-live profile JSON path.")
    parser.add_argument("--state-json", default="", help="Optional state JSON path.")
    parser.add_argument("--run-dir", default="", help="Optional run output dir. Defaults to us_stock_live/.cache/runs/<profile>.")
    parser.add_argument("--equity-usd", type=float, default=None, help="Optional equity override for plan generation.")
    parser.add_argument("--cash-usd", type=float, default=None, help="Optional cash override for plan generation.")
    parser.add_argument("--min-order-usd", type=float, default=50.0)
    parser.add_argument(
        "--bootstrap-policy",
        choices=["empty-account", "always", "never"],
        default="empty-account",
        help="Initial state handling. Use always only for an explicit profile switch or manual alignment.",
    )
    parser.add_argument("--execute", action="store_true", help="Submit orders in sell/buy stages. Omit for dry-run.")
    parser.add_argument("--confirm-live", action="store_true", help="Required by execute_plan.py for live execution.")
    parser.add_argument("--ignore-market-hours", action="store_true")
    parser.add_argument("--limit-buffer-pct", type=float, default=0.0)
    parser.add_argument("--note", default="", help="Finalize execution note.")
    parser.add_argument(
        "--force-finalize",
        action="store_true",
        help="Allow finalize without fill checks. Intended only for explicit manual confirmation.",
    )
    return parser


def _run_dir(profile_json: Path, explicit: str) -> Path:
    if explicit:
        return _resolve_path(explicit)
    return ROOT_DIR / "us_stock_live" / ".cache" / "runs" / profile_json.stem


def _state_json(profile_json: Path, explicit: str) -> Path:
    return _resolve_path(explicit) if explicit else state_path_for_profile(profile_json)


def _latest_stage_file(run_dir: Path, phase: str, suffix: str) -> Path:
    matches = sorted(run_dir.glob(f"*_{phase}_{suffix}.json"))
    if not matches:
        raise FileNotFoundError(f"No {phase} {suffix} file found in {run_dir}")
    return matches[-1]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _py_command(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]


def _run(command: list[str], *, allow_exit_2: bool = False) -> int:
    print(f"> {' '.join(command)}")
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
        _print_payload_summary(payload)
        return
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if lines:
        print(lines[-1])
    if result.stderr:
        err_lines = [line for line in result.stderr.splitlines() if line.strip()]
        if err_lines:
            print(err_lines[-1], file=sys.stderr)


def _print_payload_summary(payload: dict[str, Any]) -> None:
    payload_type = payload.get("type")
    if payload_type == "stock_live_rebalance_plan_v1":
        nonzero = [
            row for row in payload.get("rows", [])
            if row.get("action") in {"buy", "sell"} and int(row.get("order_qty") or 0) > 0
        ]
        print(
            "plan "
            f"phase={payload.get('phase')} "
            f"allowed={payload.get('rebalance_allowed')}({payload.get('rebalance_reason')}) "
            f"orders={len(nonzero)} "
            f"buy=${float(payload.get('planned_buy_notional_usd') or 0):.2f} "
            f"sell=${float(payload.get('planned_sell_notional_usd') or 0):.2f} "
            f"path_state={payload.get('state_json')}"
        )
    elif payload_type == "stock_live_execution_report_v1":
        submitted = payload.get("submitted_orders", [])
        print(
            "execution "
            f"phase={payload.get('phase')} "
            f"execute={payload.get('execute')} "
            f"orders={len(payload.get('orders', []))} "
            f"submitted={len(submitted)} "
            f"blocked={payload.get('blocked_reasons') or payload.get('blocked_reason')}"
        )
    elif payload_type == "stock_live_fill_report_v1":
        print(
            "fills "
            f"execution={payload.get('execution_report_id')} "
            f"rows={payload.get('row_count')} "
            f"matched={payload.get('matched_count')}"
        )
    elif "last_executed_change_timestamp" in payload:
        print(
            "state "
            f"last_executed_change={payload.get('last_executed_change_timestamp')} "
            f"plan_id={payload.get('last_executed_plan_id')}"
        )
    else:
        print(json.dumps(payload, ensure_ascii=False))


def _maybe_num_arg(command: list[str], flag: str, value: float | None) -> None:
    if value is not None:
        command.extend([flag, str(value)])


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    existing = {}
    if path.exists():
        try:
            existing = read_json(path)
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(payload)
    write_json(path, existing)


def _plan_command(
    *,
    phase: str,
    profile_json: Path,
    state_json: Path,
    plan_path: Path,
    equity_usd: float | None,
    cash_usd: float | None,
    min_order_usd: float,
    bootstrap_policy: str,
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
        str(min_order_usd),
        "--bootstrap-policy",
        bootstrap_policy,
        "--save-plan",
        str(plan_path),
        "--output-json",
        str(plan_path),
        "--format",
        "json",
    )
    _maybe_num_arg(command, "--equity-usd", equity_usd)
    _maybe_num_arg(command, "--cash-usd", cash_usd)
    return command


def stage_prepare(args: argparse.Namespace, profile_json: Path, state_json: Path, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    manifest_path = run_dir / "latest_manifest.json"

    _run(_py_command("us_stock_live/data/update_cache.py", "--profile-json", str(profile_json)))

    plan_paths = {}
    for phase in ["full", "sell", "buy"]:
        plan_path = run_dir / f"{stamp}_{phase}_plan.json"
        _run(
            _plan_command(
                phase=phase,
                profile_json=profile_json,
                state_json=state_json,
                plan_path=plan_path,
                equity_usd=args.equity_usd,
                cash_usd=args.cash_usd,
                min_order_usd=args.min_order_usd,
                bootstrap_policy=args.bootstrap_policy,
            )
        )
        plan_paths[phase] = str(plan_path.relative_to(ROOT_DIR) if plan_path.is_relative_to(ROOT_DIR) else plan_path)

    _write_manifest(
        manifest_path,
        {
            "profile_json": str(profile_json.relative_to(ROOT_DIR) if profile_json.is_relative_to(ROOT_DIR) else profile_json),
            "state_json": str(state_json.relative_to(ROOT_DIR) if state_json.is_relative_to(ROOT_DIR) else state_json),
            "stamp": stamp,
            "plans": plan_paths,
        },
    )
    print(f"manifest={manifest_path}")


def _execute_phase(args: argparse.Namespace, profile_json: Path, state_json: Path, run_dir: Path, phase: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    plan_path = run_dir / f"{stamp}_{phase}_plan.json"
    report_path = run_dir / f"{stamp}_{phase}_execution.json"
    _run(
        _plan_command(
            phase=phase,
            profile_json=profile_json,
            state_json=state_json,
            plan_path=plan_path,
            equity_usd=args.equity_usd,
            cash_usd=args.cash_usd,
            min_order_usd=args.min_order_usd,
            bootstrap_policy=args.bootstrap_policy,
        )
    )
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
    )
    if args.execute:
        command.append("--execute")
    if args.confirm_live:
        command.append("--confirm-live")
    if args.ignore_market_hours:
        command.append("--ignore-market-hours")
    _run(command, allow_exit_2=not args.execute)
    _write_manifest(
        run_dir / "latest_manifest.json",
        {
            f"{phase}_plan": str(plan_path.relative_to(ROOT_DIR) if plan_path.is_relative_to(ROOT_DIR) else plan_path),
            f"{phase}_execution": str(report_path.relative_to(ROOT_DIR) if report_path.is_relative_to(ROOT_DIR) else report_path),
        },
    )


def stage_fills(args: argparse.Namespace, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    outputs = {}
    for phase in ["sell", "buy"]:
        try:
            execution_path = _latest_stage_file(run_dir, phase, "execution")
        except FileNotFoundError:
            continue
        output_path = run_dir / f"{stamp}_{phase}_fills.json"
        _run(
            _py_command(
                "us_stock_live/trading/check_fills.py",
                "--execution-json",
                str(execution_path),
                "--output-json",
                str(output_path),
            )
        )
        outputs[f"{phase}_fills"] = str(output_path.relative_to(ROOT_DIR) if output_path.is_relative_to(ROOT_DIR) else output_path)
    if not outputs:
        raise FileNotFoundError(f"No execution reports found in {run_dir}")
    _write_manifest(run_dir / "latest_manifest.json", outputs)


def stage_finalize(args: argparse.Namespace, profile_json: Path, state_json: Path, run_dir: Path) -> None:
    plan_path = _latest_stage_file(run_dir, "buy", "plan")
    if not args.force_finalize:
        fill_path = _latest_stage_file(run_dir, "buy", "fills")
        fill_report = read_json(fill_path)
        execution_path = _latest_stage_file(run_dir, "buy", "execution")
        execution_report = read_json(execution_path)
        submitted = [
            order
            for order in execution_report.get("submitted_orders", [])
            if order.get("status") == "submitted"
        ]
        if submitted and int(fill_report.get("matched_count") or 0) <= 0:
            raise RuntimeError(
                "Refusing to finalize: submitted buy orders exist but latest buy fill report has no matched rows. "
                "Use --force-finalize only after manual confirmation."
            )
        if not submitted:
            raise RuntimeError(
                "Refusing to finalize: latest buy execution has no submitted orders. "
                "Use --force-finalize only after explicit manual confirmation."
            )
    command = _py_command(
        "us_stock_live/trading/mark_executed.py",
        "--plan-json",
        str(plan_path),
        "--state-json",
        str(state_json),
        "--confirm",
    )
    if args.note:
        command.extend(["--note", args.note])
    _run(command)


def main() -> int:
    args = build_parser().parse_args()
    profile_json = _resolve_path(args.profile_json)
    state_json = _state_json(profile_json, args.state_json)
    run_dir = _run_dir(profile_json, args.run_dir)

    if args.stage == "prepare":
        stage_prepare(args, profile_json, state_json, run_dir)
    elif args.stage == "sell":
        _execute_phase(args, profile_json, state_json, run_dir, "sell")
    elif args.stage == "buy":
        _execute_phase(args, profile_json, state_json, run_dir, "buy")
    elif args.stage == "fills":
        stage_fills(args, run_dir)
    elif args.stage == "finalize":
        stage_finalize(args, profile_json, state_json, run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
