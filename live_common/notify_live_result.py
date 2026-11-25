#!/usr/bin/env python3
"""Telegram notification for one KR/US stock live run, written for a person to read at a glance.

The message names what happened, the orders of this run, current holdings, and what the
operator has to do. It deliberately leaves out account value, cash, prices and weights,
so returns cannot be read from the message.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.notify import send_notification

KR_ETF_MASTER = ROOT_DIR / "data" / "kr_etfs" / "raw" / "etf_master.parquet"
STAMP_RE = re.compile(r"^(\d{8}_\d{6})_(sell|buy)_(execution|fills)\.json$")
ERROR_LINE_RE = re.compile(r"^[\w.]*(Error|Exception)\b.*?:\s*(.*)$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a live-run Telegram notification from saved reports.")
    parser.add_argument("--system-label", required=True, help="Human-readable system label, for example KR ETF.")
    parser.add_argument("--runner", choices=["kr", "us"], required=True)
    parser.add_argument("--mode", default="", help="Runner mode, for example preview or live.")
    parser.add_argument("--profile-json", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--snapshot-exit-code", type=int, default=None)
    parser.add_argument("--run-ts", default="", help="Local run timestamp formatted as yyyy-MM-dd HH:mm:ss.")
    parser.add_argument("--log-file", default="")
    parser.add_argument("--log-lines", type=int, default=400, help="Log lines scanned for this run's error.")
    parser.add_argument("--notify-preview-ok", action="store_true", help="Also notify successful preview runs.")
    parser.add_argument("--dry-run", action="store_true", help="Print the message instead of sending it.")
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _profile_path(runner: str, profile_ref: Path) -> Path:
    active_profile_types = {
        "kr": "kr_stock_live_active_profile_v1",
        "us": "stock_live_active_profile_v1",
    }
    payload = _read_json(profile_ref) or {}
    if payload.get("type") != active_profile_types.get(runner):
        return profile_ref
    profile_json = payload.get("profile_json")
    if not profile_json:
        return profile_ref
    return _resolve_path(str(profile_json))


def _base_dir(runner: str) -> Path:
    return ROOT_DIR / ("kr_stock_live" if runner == "kr" else "us_stock_live")


def _run_dir(runner: str, profile_json: Path) -> Path:
    return _base_dir(runner) / ".cache" / "runs" / profile_json.stem / "auto"


def _latest_snapshot_path(runner: str, profile_json: Path) -> Path:
    return _base_dir(runner) / ".cache" / "performance" / profile_json.stem / "latest_account_snapshot.json"


def _parse_run_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _is_stale(path: Path, run_ts: datetime | None) -> bool:
    if not path.exists() or run_ts is None:
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return modified < run_ts - timedelta(seconds=60)


def _system_name(system_label: str) -> str:
    return {
        "KR ETF": "국내 ETF",
        "US Stock": "해외 주식",
        "Binance Futures Demo": "선물 데모",
        "Binance Futures Live": "선물 라이브",
    }.get(system_label, system_label)


# --- names -------------------------------------------------------------------------------

_KR_NAMES: dict[str, str] | None = None


def _kr_names() -> dict[str, str]:
    global _KR_NAMES
    if _KR_NAMES is None:
        _KR_NAMES = {}
        try:
            import pandas as pd

            master = pd.read_parquet(KR_ETF_MASTER, columns=["Symbol", "Name"])
            _KR_NAMES = {str(symbol): str(name) for symbol, name in zip(master["Symbol"], master["Name"])}
        except Exception:  # Names are cosmetic; fall back to codes.
            _KR_NAMES = {}
    return _KR_NAMES


def _display_symbol(runner: str, symbol: str) -> str:
    symbol = str(symbol or "?").strip()
    if runner == "kr":
        return _kr_names().get(symbol, symbol)
    return symbol


# --- this run's orders ---------------------------------------------------------------------


def _qty_text(value: Any) -> str:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return f"{value}주"
    return f"{int(number)}주" if number.is_integer() else f"{number:g}주"


def _run_files(run_dir: Path, run_ts: datetime | None, kind: str) -> list[tuple[str, str, Path]]:
    """(stamp, phase, path) of execution or fills reports written during this run."""
    if not run_dir.exists():
        return []
    # Runs are daily; a 6-hour window holds one run's reports even with fill retries.
    start = (run_ts - timedelta(seconds=60)).strftime("%Y%m%d_%H%M%S") if run_ts else ""
    end = (run_ts + timedelta(hours=6)).strftime("%Y%m%d_%H%M%S") if run_ts else ""
    found = []
    for path in run_dir.iterdir():
        match = STAMP_RE.match(path.name)
        if not match or match.group(3) != kind:
            continue
        if start and not (start <= match.group(1) <= end):
            continue
        found.append((match.group(1), match.group(2), path))
    return sorted(found)


def _fill_states(run_dir: Path, run_ts: datetime | None) -> dict[str, str]:
    """order_no -> filled / closed_unfilled / open / not_found from this run's latest fill checks."""
    states: dict[str, str] = {}
    for _, _, path in _run_files(run_dir, run_ts, "fills"):
        report = _read_json(path) or {}
        for item in report.get("order_fill_status") or []:
            order_no = str(item.get("order_no") or "").strip()
            if not order_no:
                continue
            state = str(item.get("order_state") or "")
            if not state:
                state = "filled" if item.get("fully_filled") else "open"
            states[order_no] = state
    return states


def _orders_from_execution(path: Path) -> list[dict[str, Any]]:
    report = _read_json(path) or {}
    return [
        order
        for order in report.get("submitted_orders") or []
        if str(order.get("status") or "").lower() in {"submitted", "dry_run"}
    ]


def _run_orders(run_dir: Path, run_ts: datetime | None) -> list[dict[str, Any]]:
    orders = []
    for _, phase, path in _run_files(run_dir, run_ts, "execution"):
        for order in _orders_from_execution(path):
            orders.append({**order, "side": str(order.get("side") or phase).lower()})
    return orders


def _order_lines(runner: str, orders: list[dict[str, Any]], fill_states: dict[str, str]) -> list[str]:
    lines = []
    for side, label in (("sell", "매도"), ("buy", "매수")):
        parts = []
        for order in orders:
            if order.get("side") != side:
                continue
            text = f"{_display_symbol(runner, order.get('symbol'))} {_qty_text(order.get('qty'))}"
            state = fill_states.get(str(order.get("order_no") or "").strip())
            if state == "closed_unfilled":
                text += " (미체결 종료)"
            elif state in {"open", "not_found"}:
                text += " (체결 대기)"
            parts.append(text)
        if parts:
            lines.append(f"{label}  " + " · ".join(parts))
    return lines


def _holdings_line(runner: str, snapshot: dict[str, Any] | None, stale: bool) -> str | None:
    if not snapshot or stale:
        return None
    positions = [item for item in snapshot.get("positions") or [] if isinstance(item, dict)]
    names = [_display_symbol(runner, item.get("symbol")) for item in positions if float(item.get("qty") or 0) > 0]
    return "보유  " + (" · ".join(names) if names else "없음")


# --- this run's error -------------------------------------------------------------------------


def _decode_line(raw: bytes) -> str:
    # Python on Windows writes the log in the console codepage; KIS messages are Korean.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace")


def _log_segment(log_file: str, marker: str, max_lines: int) -> list[str]:
    """This run's main output: from the last line starting with marker up to "[exit_code=".

    Without a marker found, the whole scanned window is returned uncut.
    """
    if not log_file:
        return []
    path = _resolve_path(log_file)
    try:
        raw_lines = path.read_bytes().splitlines()[-max_lines:]
    except OSError:
        return []
    lines = [_decode_line(raw).rstrip() for raw in raw_lines]
    starts = [index for index, line in enumerate(lines) if marker and line.startswith(marker)]
    if not starts:
        return lines
    lines = lines[starts[-1]:]
    # Keep the runner's own output; the account snapshot and notifier run after "[exit_code=".
    for index, line in enumerate(lines):
        if line.startswith("[exit_code="):
            return lines[:index]
    return lines


def _run_log_lines(log_file: str, run_ts_text: str, max_lines: int) -> list[str]:
    return _log_segment(log_file, f"[{run_ts_text}]" if run_ts_text else "", max_lines)


def _last_error(lines: list[str]) -> str:
    # The runner re-raises a child script's failure as CalledProcessError; the child's own
    # error printed above it names the cause.
    fallback = ""
    for line in reversed(lines):
        stripped = line.strip()
        if not ERROR_LINE_RE.match(stripped):
            continue
        if "CalledProcessError" in stripped:
            fallback = fallback or stripped
            continue
        return stripped
    return fallback


def _explain_error(error: str, phase_word: str) -> tuple[str, str, str, bool]:
    """(icon, title, explanation, action) for a known failure; bool = show log location."""
    text = error.lower()
    if "not confirmed fully filled" in text:
        return (
            "⚠️",
            f"{phase_word} 체결 대기",
            f"{phase_word} 주문의 체결이 아직 확인되지 않았습니다.\n장 마감 후 주문이 끝나면 다음 실행에서\n남은 수량을 자동으로 다시 주문합니다.",
            "없음",
        )
    if "ended unfilled after" in text:
        return (
            "❌",
            "재주문 한도 초과",
            f"{phase_word} 주문을 3번 다시 냈지만 체결되지 않았습니다.\n자동 진행을 멈췄습니다.",
            "한투 앱에서 주문·보유를 확인한 뒤 대기 상태 초기화(--reset-pending)",
        )
    if "상품이 없습니다" in error:
        return (
            "❌",
            "종목 조회 실패",
            "한투가 종목을 찾지 못했습니다.\n종목의 거래소 설정이 빠졌을 수 있습니다.",
            "config의 kis.symbols 거래소 설정 확인",
        )
    if any(key in text for key in ("kis http", "egw0", "mci", "서버", "timed out", "urlerror", "timeout", "connection")):
        return (
            "❌",
            "한투 서버 응답 오류",
            "한투 API가 응답하지 않거나 오류를 돌려줬습니다.\n다음 실행에서 자동으로 다시 시도합니다.",
            "다음 실행도 실패하면 확인",
        )
    return ("❌", "실행 실패", "", "로그 확인")


def _short_error(error: str, limit: int = 160) -> str:
    match = ERROR_LINE_RE.match(error)
    detail = match.group(2).strip() if match else error
    return detail if len(detail) <= limit else detail[: limit - 1] + "…"


# --- message ------------------------------------------------------------------------------------


def _auto_state_phase_word(run_dir: Path) -> str:
    state = _read_json(run_dir / "auto_state.json") or {}
    phase = str(state.get("phase") or "")
    return "매수" if phase.startswith("buy") else "매도"


def _pending_orders(run_dir: Path) -> list[dict[str, Any]]:
    state = _read_json(run_dir / "auto_state.json") or {}
    phase = str(state.get("phase") or "")
    if not phase.endswith("_submitted"):
        return []
    side = phase.split("_", 1)[0]
    execution = state.get(f"{side}_execution")
    if not execution:
        return []
    return [{**order, "side": side} for order in _orders_from_execution(_resolve_path(str(execution)))]


def build_message(args: argparse.Namespace) -> str:
    profile_json = _profile_path(args.runner, _resolve_path(args.profile_json))
    run_dir = _run_dir(args.runner, profile_json)
    report_path = run_dir / "latest_auto_report.json"
    snapshot_path = _latest_snapshot_path(args.runner, profile_json)
    run_ts = _parse_run_ts(args.run_ts)
    report = _read_json(report_path)
    report_stale = report is None or _is_stale(report_path, run_ts)
    snapshot = _read_json(snapshot_path)
    snapshot_stale = snapshot is None or _is_stale(snapshot_path, run_ts)
    snapshot_failed = args.snapshot_exit_code not in (None, 0)

    system = _system_name(args.system_label)
    if str(args.mode).lower() == "preview":
        system += " [미리보기]"
    when = run_ts.strftime("%m/%d %H:%M") if run_ts else ""

    orders = _run_orders(run_dir, run_ts)
    fill_states = _fill_states(run_dir, run_ts)
    action = str((report or {}).get("action") or "").lower() if not report_stale else ""
    reason = str((report or {}).get("reason") or "") if not report_stale else ""

    icon, title, explanation, todo, show_log = "✅", "", "", "없음", False
    if args.exit_code != 0:
        error = _last_error(_run_log_lines(args.log_file, args.run_ts, args.log_lines))
        icon, title, explanation, todo = _explain_error(error, _auto_state_phase_word(run_dir))
        if title == "실행 실패":
            explanation = _short_error(error) if error else "오류 내용을 로그에서 찾지 못했습니다."
            show_log = True
        if not orders:
            orders = _pending_orders(run_dir)
    elif report_stale:
        icon, title = "❌", "실행 결과 없음"
        explanation = "이번 실행의 결과 파일이 만들어지지 않았습니다."
        todo, show_log = "로그 확인", True
    elif action == "finalized":
        title = "리밸런싱 완료"
    elif action == "noop":
        title = "매매 없음"
    elif action == "blocked" and reason.startswith("outside_"):
        weekend = run_ts is not None and run_ts.weekday() >= 5 and args.runner == "kr"
        title = "휴장일" if weekend else "장 시간 아님 · 주문은 다음 실행에서"
    elif action == "blocked":
        icon, title = "⚠️", "주문 보류"
        explanation = f"주문이 나가지 않았습니다 ({reason})."
        todo = "사유 확인"
    elif action == "replan":
        icon, title = "⚠️", "미체결 주문 종료"
        explanation = "체결되지 않고 끝난 주문이 있어 다음 실행에서 다시 주문합니다."
    elif action == "wait":
        icon, title = "⚠️", "체결 대기"
        explanation = "체결이 아직 확인되지 않았습니다. 다음 실행에서 다시 확인합니다."
    elif action in {"submitted", "dry_run"}:
        title = "주문 제출" if action == "submitted" else "주문 미리보기"
    else:
        title = "실행 완료"

    if snapshot_failed:
        if icon == "✅":
            icon = "⚠️"
        note = "계좌 조회도 실패했습니다 (주문 처리와는 별개)." if args.exit_code != 0 else "계좌 조회에 실패했습니다 (매매에는 영향 없음)."
        explanation = f"{explanation}\n{note}".strip()

    headline = f"{icon} {system} · {title}"
    quiet = icon == "✅" and not orders and not explanation
    if quiet:
        return f"{headline} ({when})" if when else headline

    lines = [headline]
    if when:
        lines.append(when)
    order_lines = _order_lines(args.runner, orders, fill_states)
    if order_lines or explanation:
        lines.append("")
    lines.extend(order_lines)
    if explanation:
        if order_lines:
            lines.append("")
        lines.append(explanation)
    lines.append("")
    holdings = _holdings_line(args.runner, snapshot, snapshot_stale or snapshot_failed)
    if holdings and icon == "✅":
        lines.append(holdings)
    lines.append(f"할 일  {todo}")
    if show_log and args.log_file:
        lines.append(f"로그  {Path(args.log_file).name}")
    return "\n".join(lines)[:3900]


def main() -> int:
    args = build_parser().parse_args()
    message = build_message(args)
    if str(args.mode).lower() == "preview" and message.startswith("✅") and not args.notify_preview_ok:
        print("notification_sent=False reason=preview_ok_suppressed")
        return 0
    if args.dry_run:
        print(message)
        return 0
    try:
        result = send_notification(message)
    except Exception as exc:  # Notifications must not change trading exit codes.
        print(f"notification_error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    print(f"notification_sent={result.sent} reason={result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
