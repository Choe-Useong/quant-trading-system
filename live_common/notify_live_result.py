#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from live_common.notify import send_notification


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
    parser.add_argument("--log-lines", type=int, default=30)
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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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


def _latest_report_path(runner: str, profile_json: Path) -> Path:
    return _base_dir(runner) / ".cache" / "runs" / profile_json.stem / "auto" / "latest_auto_report.json"


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


def _is_expected_kr_weekend_block(report: dict[str, Any]) -> bool:
    if str(report.get("action") or "").lower() != "blocked":
        return False
    if str(report.get("reason") or "").lower() != "outside_krx_regular_market_hours":
        return False
    try:
        created_at = datetime.fromisoformat(str(report.get("created_at") or ""))
    except ValueError:
        return False
    return created_at.weekday() >= 5


def _status(
    *,
    exit_code: int,
    snapshot_exit_code: int | None,
    report: dict[str, Any] | None,
    report_stale: bool,
) -> str:
    if exit_code != 0:
        return "FAIL"
    if snapshot_exit_code is not None and snapshot_exit_code != 0:
        return "WARN_PARTIAL"
    if report is None or report_stale:
        return "STALE"
    if _is_expected_kr_weekend_block(report):
        return "OK_MARKET_CLOSED"
    action = str(report.get("action") or "").lower()
    if action == "noop":
        return "OK_NOOP"
    if action in {"submitted", "finalized", "dry_run"}:
        return "OK_ORDER"
    if action in {"blocked", "wait", "replan"}:
        return "WARN_PARTIAL"
    return "OK"


def _display_system_label(system_label: str) -> str:
    labels = {
        "KR ETF": "국내 ETF",
        "US Stock": "해외 주식",
    }
    return labels.get(system_label, system_label)


def _status_headline(status: str, system_label: str) -> str:
    labels = {
        "OK_NOOP": "정상: 주문 없음",
        "OK_MARKET_CLOSED": "정상: 휴장일 주문 없음",
        "OK_ORDER": "정상: 주문 제출",
        "WARN_PARTIAL": "경고: 확인 필요",
        "FAIL": "실패: 실행 오류",
        "STALE": "경고: 최신 리포트 없음",
        "OK": "정상: 실행 완료",
    }
    return f"[{_display_system_label(system_label)}] {labels.get(status, status)}"


# Override the old mojibake headings with escaped Korean strings so Windows
# codepages cannot corrupt the live notification headline.
def _display_system_label(system_label: str) -> str:
    labels = {
        "KR ETF": "\uad6d\ub0b4 ETF",
        "US Stock": "\ud574\uc678 \uc8fc\uc2dd",
    }
    return labels.get(system_label, system_label)


def _status_headline(status: str, system_label: str) -> str:
    labels = {
        "OK_NOOP": "\uc815\uc0c1: \uc8fc\ubb38 \uc5c6\uc74c",
        "OK_MARKET_CLOSED": "\uc815\uc0c1: \ud734\uc7a5\uc77c \uc8fc\ubb38 \uc5c6\uc74c",
        "OK_ORDER": "\uc815\uc0c1: \uc8fc\ubb38 \uc81c\ucd9c",
        "WARN_PARTIAL": "\uacbd\uace0: \ud655\uc778 \ud544\uc694",
        "FAIL": "\uc2e4\ud328: \uc2e4\ud589 \uc624\ub958",
        "STALE": "\uacbd\uace0: \ucd5c\uc2e0 \ub9ac\ud3ec\ud2b8 \uc5c6\uc74c",
        "OK": "\uc815\uc0c1: \uc2e4\ud589 \uc644\ub8cc",
    }
    return f"[{_display_system_label(system_label)}] {labels.get(status, status)}"


def _tail_log(path: Path, max_lines: int) -> str:
    if not path.exists() or max_lines <= 0:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    tail = [line for line in lines[-max_lines:] if line.strip()]
    return "\n".join(tail)


def _fmt_money(value: Any, suffix: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"? {suffix}"
    if suffix == "USD":
        return f"{number:,.2f} {suffix}"
    return f"{number:,.0f} {suffix}"


def _snapshot_lines(runner: str, snapshot: dict[str, Any] | None) -> list[str]:
    if not snapshot:
        return ["snapshot=missing"]
    holdings_count = snapshot.get("holdings_count")
    if runner == "kr":
        lines = [
            f"total={_fmt_money(snapshot.get('total_eval_krw'), 'KRW')}",
            f"cash={_fmt_money(snapshot.get('orderable_cash_krw'), 'KRW')}",
            f"holdings={holdings_count}",
        ]
    else:
        lines = [
            f"total={_fmt_money(snapshot.get('estimated_total_usd'), 'USD')}",
            f"cash={_fmt_money(snapshot.get('orderable_cash_usd'), 'USD')}",
            f"holdings={holdings_count}",
        ]
    positions = snapshot.get("positions") or []
    if isinstance(positions, list) and positions:
        summary = []
        for item in positions[:5]:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "?")
            qty = item.get("qty")
            weight = item.get("weight")
            try:
                weight_text = f"{float(weight) * 100:.1f}%"
            except (TypeError, ValueError):
                weight_text = "?"
            summary.append(f"{symbol}:{qty}@{weight_text}")
        if summary:
            lines.append("positions=" + ", ".join(summary))
    return lines


def _report_lines(report: dict[str, Any] | None, *, stale: bool, report_path: Path) -> list[str]:
    if not report:
        return [f"report=missing path={report_path}"]
    if stale:
        return [
            f"report=stale path={report_path}",
            f"stale_report_created_at={report.get('created_at')}",
        ]
    lines = [
        f"action={report.get('action')}",
        f"phase={report.get('phase')}",
        f"reason={report.get('reason')}",
        f"report_created_at={report.get('created_at')}",
    ]
    return lines


def _profile_name(profile_json: Path) -> str:
    payload = _read_json(profile_json) or {}
    return str(payload.get("name") or profile_json.stem)


def _clip_message(message: str, limit: int = 3900) -> str:
    if len(message) <= limit:
        return message
    marker = "\n...[truncated]..."
    return message[: limit - len(marker)] + marker


def build_message(args: argparse.Namespace) -> str:
    profile_ref = _resolve_path(args.profile_json)
    profile_json = _profile_path(args.runner, profile_ref)
    report_path = _latest_report_path(args.runner, profile_json)
    snapshot_path = _latest_snapshot_path(args.runner, profile_json)
    report = _read_json(report_path)
    snapshot = _read_json(snapshot_path)
    run_ts = _parse_run_ts(args.run_ts)
    report_stale = _is_stale(report_path, run_ts)
    status = _status(
        exit_code=args.exit_code,
        snapshot_exit_code=args.snapshot_exit_code,
        report=report,
        report_stale=report_stale,
    )

    lines = [
        _status_headline(status, args.system_label),
        f"mode={args.mode or '?'} exit_code={args.exit_code} snapshot_exit_code={args.snapshot_exit_code}",
        f"profile={_profile_name(profile_json)}",
    ]
    lines.extend(_report_lines(report, stale=report_stale, report_path=report_path))
    lines.extend(_snapshot_lines(args.runner, snapshot))

    if status in {"FAIL", "WARN_PARTIAL", "STALE"} and args.log_file:
        tail = _tail_log(_resolve_path(args.log_file), args.log_lines)
        if tail:
            lines.append("log_tail:")
            lines.append(tail)
    return _clip_message("\n".join(str(line) for line in lines))


def main() -> int:
    args = build_parser().parse_args()
    message = build_message(args)
    status = _status(
        exit_code=args.exit_code,
        snapshot_exit_code=args.snapshot_exit_code,
        report=_read_json(_latest_report_path(args.runner, _profile_path(args.runner, _resolve_path(args.profile_json)))),
        report_stale=_is_stale(
            _latest_report_path(args.runner, _profile_path(args.runner, _resolve_path(args.profile_json))),
            _parse_run_ts(args.run_ts),
        ),
    )
    if args.mode.lower() == "preview" and status.startswith("OK_") and not args.notify_preview_ok:
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
