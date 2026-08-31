from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from live_common.env import load_dotenv


@dataclass(frozen=True)
class NotifyResult:
    sent: bool
    reason: str
    response: dict[str, Any] | None = None


def _provider() -> str:
    load_dotenv()
    return os.getenv("NOTIFY_PROVIDER", "").strip().lower()


def _telegram_credentials() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def send_notification(message: str, *, timeout: float = 10.0) -> NotifyResult:
    provider = _provider()
    if not provider:
        return NotifyResult(sent=False, reason="provider_disabled")
    if provider != "telegram":
        return NotifyResult(sent=False, reason=f"unsupported_provider:{provider}")
    return send_telegram_message(message, timeout=timeout)


def send_telegram_message(message: str, *, timeout: float = 10.0) -> NotifyResult:
    token, chat_id = _telegram_credentials()
    if not token or not chat_id:
        return NotifyResult(sent=False, reason="telegram_credentials_missing")

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body}
    if not payload.get("ok"):
        return NotifyResult(sent=False, reason="telegram_api_error", response=payload)
    return NotifyResult(sent=True, reason="sent", response=payload)
