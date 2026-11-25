#!/usr/bin/env python3
"""Telegram notification for a scheduled task (price cache update, hourly futures step).

With --only-on-failure the task is silent while it succeeds. --dedupe-state keeps the last
notified failure so an hourly task that keeps failing the same way notifies once, and the
first success after a notified failure sends one recovery line.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.notify import send_notification
from live_common.notify_live_result import _last_error, _log_segment, _short_error, _system_name

TASK_NAMES = {
    "cache_update": "데이터 갱신",
    "futures_paper": "페이퍼 계좌·데이터 갱신",
    "futures_demo": "데모 주문",
}

# (pattern in the error line, title, explanation, action) for errors the futures step raises.
KNOWN_ERRORS = [
    ("STOP file present", "수동 정지 중", "STOP 파일이 있어 실행하지 않았습니다.", "재개하려면 STOP 파일 삭제"),
    ("is not the current bar", "데이터가 최신이 아님", "라이브 캐시가 현재 시각 봉까지 갱신되지 않아 주문하지 않았습니다.\n페이퍼 실행(데이터 갱신)이 실패했거나 늦었을 수 있습니다.", "계속되면 페이퍼 로그 확인"),
    ("was already processed", "같은 봉 중복 실행", "이미 처리한 봉이라 건너뛰었습니다.", "없음"),
    ("hedge position mode", "계좌 모드 오류", "계좌가 헤지 모드입니다. 단방향 모드에서만 실행합니다.", "바이낸스에서 단방향 모드로 변경"),
    ("multi-assets mode", "계좌 모드 오류", "계좌가 멀티에셋 모드입니다. 단일 자산 모드에서만 실행합니다.", "바이낸스에서 단일 자산 모드로 변경"),
    ("without current contract rules", "계약 정보 없는 보유 종목", "보유 종목 중 현재 계약 정보가 없는 종목이 있어 멈췄습니다.", "보유 종목·상장폐지 여부 확인"),
    ("rate limit", "바이낸스 요청 한도 초과", "바이낸스 API 요청 한도에 걸렸습니다.\n다음 실행에서 다시 시도합니다.", "계속되면 확인"),
    ("Missing API key", "API 키 없음", "API 키 또는 시크릿을 찾지 못했습니다.", ".env 설정 확인"),
    ("BinanceAPIError", "바이낸스 API 오류", "바이낸스가 오류를 돌려줬습니다.\n다음 실행에서 다시 시도합니다.", "계속되면 확인"),
    ("UnknownOrderStatus", "주문 상태 확인 불가", "보낸 주문의 상태를 확인하지 못했습니다.", "바이낸스에서 주문·포지션 확인"),
    ("timed out", "네트워크 오류", "응답 시간이 초과됐습니다.\n다음 실행에서 다시 시도합니다.", "계속되면 확인"),
    ("URLError", "네트워크 오류", "서버에 연결하지 못했습니다.\n다음 실행에서 다시 시도합니다.", "계속되면 확인"),
    ("ConnectionError", "네트워크 오류", "서버에 연결하지 못했습니다.\n다음 실행에서 다시 시도합니다.", "계속되면 확인"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a simple Telegram notification for scheduled maintenance tasks.")
    parser.add_argument("--system-label", required=True)
    parser.add_argument("--task-label", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--run-ts", default="", help="yyyy-MM-dd HH:mm:ss (local, or UTC with --utc).")
    parser.add_argument("--utc", action="store_true", help="--run-ts is UTC; the message shows Korea time.")
    parser.add_argument("--log-file", default="")
    parser.add_argument("--log-marker", default="", help="Line prefix that starts this run in the log. Default: [run-ts].")
    parser.add_argument("--log-lines", type=int, default=2000, help="Log lines scanned for this run's error.")
    parser.add_argument("--only-on-failure", action="store_true", help="Send nothing on success (except a recovery line).")
    parser.add_argument("--ignore-exit-codes", default="", help="Comma list of exit codes notified elsewhere; treated as success.")
    parser.add_argument("--dedupe-state", default="", help="JSON file remembering the last notified failure.")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _when(args: argparse.Namespace) -> str:
    try:
        moment = datetime.strptime(args.run_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    if args.utc:
        moment += timedelta(hours=9)
    return moment.strftime("%m/%d %H:%M")


def _ignored_codes(text: str) -> set[int]:
    return {int(part) for part in text.split(",") if part.strip().lstrip("-").isdigit()}


def _signature(error: str) -> str:
    # Bars and timestamps change every hour; the same failure should still match.
    return re.sub(r"\d+", "#", error)[:300]


def _read_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _failure_message(args: argparse.Namespace, error: str) -> str:
    system = _system_name(args.system_label)
    task = TASK_NAMES.get(args.task_label, args.task_label)
    title, explanation, todo, show_log = f"{task} 실패", "", "", True
    for pattern, known_title, known_explanation, known_todo in KNOWN_ERRORS:
        if pattern.lower() in error.lower():
            title, explanation, todo, show_log = known_title, known_explanation, known_todo, False
            break
    if not explanation:
        explanation = _short_error(error) if error else "오류 내용을 로그에서 찾지 못했습니다."
        todo = "다음 실행 전에 확인" if args.task_label == "cache_update" else "로그 확인"
        if args.task_label == "cache_update":
            todo += " (갱신되지 않은 데이터로 신호가 계산될 수 있음)"
    icon = "⚠️" if todo == "없음" else "❌"
    lines = [f"{icon} {system} · {title}"]
    when = _when(args)
    if when:
        lines.append(when)
    lines.extend(["", explanation, "", f"할 일  {todo}"])
    if show_log and args.log_file:
        lines.append(f"로그  {Path(args.log_file).name}")
    return "\n".join(lines)[:3900]


def build_message(args: argparse.Namespace) -> str | None:
    """The message to send, or None when this run needs no notification."""
    system = _system_name(args.system_label)
    task = TASK_NAMES.get(args.task_label, args.task_label)
    when = _when(args)
    succeeded = args.exit_code == 0 or args.exit_code in _ignored_codes(args.ignore_exit_codes)
    state_path = Path(args.dedupe_state) if args.dedupe_state else None
    state = _read_state(state_path) if state_path else {}

    if succeeded:
        if state.get("signature"):
            if state_path and not args.dry_run:
                _write_state(state_path, {})
            since = state.get("since_text") or ""
            return f"✅ {system} · {task} 정상 복구" + (f" ({when}, {since}부터 실패)" if since else f" ({when})")
        if args.only_on_failure:
            return None
        headline = f"✅ {system} · {task} 완료"
        return f"{headline} ({when})" if when else headline

    marker = args.log_marker or (f"[{args.run_ts}]" if args.run_ts else "")
    error = _last_error(_log_segment(args.log_file, marker, args.log_lines))
    signature = _signature(error or f"exit {args.exit_code}")
    if state_path:
        if state.get("signature") == signature:
            return None
        if not args.dry_run:
            _write_state(state_path, {"signature": signature, "since_text": state.get("since_text") or when})
    return _failure_message(args, error)


def main() -> int:
    args = build_parser().parse_args()
    message = build_message(args)
    if message is None:
        print("notification_sent=False reason=no_event")
        return 0
    if args.dry_run:
        print(message)
        return 0
    try:
        result = send_notification(message)
    except Exception as exc:  # Notifications must not change task exit codes.
        print(f"notification_error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    print(f"notification_sent={result.sent} reason={result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
