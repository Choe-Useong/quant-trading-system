#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.notify import send_notification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a simple Telegram notification for scheduled maintenance tasks.")
    parser.add_argument("--system-label", required=True)
    parser.add_argument("--task-label", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--run-ts", default="")
    parser.add_argument("--log-file", default="")
    parser.add_argument("--log-lines", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _tail_log(path: Path, max_lines: int) -> str:
    if not path.exists() or max_lines <= 0:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    tail = [line for line in lines[-max_lines:] if line.strip()]
    return "\n".join(tail)


def _clip(message: str, limit: int = 3900) -> str:
    if len(message) <= limit:
        return message
    marker = "\n...[truncated]..."
    return message[: limit - len(marker)] + marker


def main() -> int:
    args = build_parser().parse_args()
    status = "정상: 작업 완료" if args.exit_code == 0 else "실패: 작업 오류"
    lines = [
        f"[{args.system_label}] {status}",
        f"task={args.task_label} exit_code={args.exit_code}",
    ]
    if args.run_ts:
        lines.append(f"run_ts={args.run_ts}")
    if args.exit_code != 0 and args.log_file:
        tail = _tail_log(_resolve_path(args.log_file), args.log_lines)
        if tail:
            lines.append("log_tail:")
            lines.append(tail)
    message = _clip("\n".join(lines))
    if args.dry_run:
        print(message)
        return 0
    result = send_notification(message)
    print(f"notification_sent={result.sent} reason={result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
