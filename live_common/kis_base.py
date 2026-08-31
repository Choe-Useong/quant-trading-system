#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KIS_REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
TOKEN_EXPIRY_SAFETY_SECONDS = 300
KIS_RETRYABLE_MSG_CODES = {"EGW00201", "EGW00215"}
KIS_RETRY_DELAYS_SECONDS = (1.5, 3.0, 5.0)


@dataclass(frozen=True)
class KISConfig:
    env: str
    app_key: str
    app_secret: str
    account_no: str
    account_product_code: str
    base_url: str


class KISError(RuntimeError):
    pass


def redacted_account(config: KISConfig) -> str:
    return f"{config.account_no[:2]}******-{config.account_product_code}"


def _is_retryable_kis_payload(payload: dict[str, Any]) -> bool:
    msg_code = str(payload.get("msg_cd") or "").strip()
    return msg_code in KIS_RETRYABLE_MSG_CODES


def _is_retryable_kis_body(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return any(code in body for code in KIS_RETRYABLE_MSG_CODES)
    return isinstance(payload, dict) and _is_retryable_kis_payload(payload)


def _read_json_response(req: urllib.request.Request, timeout: int = 30) -> dict[str, Any]:
    method = req.get_method().upper()
    retry_get = method == "GET"
    for attempt in range(len(KIS_RETRY_DELAYS_SECONDS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if retry_get and attempt < len(KIS_RETRY_DELAYS_SECONDS) and _is_retryable_kis_body(body):
                time.sleep(KIS_RETRY_DELAYS_SECONDS[attempt])
                continue
            raise KISError(f"KIS HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise KISError(f"KIS request failed: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KISError(f"KIS returned non-JSON response: {raw[:500]}") from exc
        if str(payload.get("rt_cd", "0")) not in {"0", ""}:
            if retry_get and attempt < len(KIS_RETRY_DELAYS_SECONDS) and _is_retryable_kis_payload(payload):
                time.sleep(KIS_RETRY_DELAYS_SECONDS[attempt])
                continue
            message = payload.get("msg1") or payload.get("msg_cd") or payload
            raise KISError(f"KIS API error: {message}")
        return payload
    raise KISError("KIS request failed after retries")


class KISBaseClient:
    def __init__(self, config: KISConfig):
        self.config = config
        self._access_token: str | None = None
        self._access_token_expires_at: float | None = None

    @property
    def is_live(self) -> bool:
        return self.config.env == "live"

    def _token_cache_path(self) -> Path:
        raise NotImplementedError("KISBaseClient subclasses must define _token_cache_path")

    def _load_cached_token(self) -> str | None:
        cache_path = self._token_cache_path()
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = str(payload.get("access_token", "")).strip()
        expires_at = float(payload.get("expires_at", 0.0) or 0.0)
        if not token or expires_at <= time.time() + TOKEN_EXPIRY_SAFETY_SECONDS:
            return None
        self._access_token = token
        self._access_token_expires_at = expires_at
        return token

    def _write_cached_token(self, token: str, expires_at: float) -> None:
        cache_path = self._token_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "env": self.config.env,
            "account": redacted_account(self.config),
            "access_token": token,
            "expires_at": expires_at,
            "saved_at": time.time(),
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def issue_access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._load_cached_token()
            if cached:
                return cached

        body = json.dumps(
            {
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "appsecret": self.config.app_secret,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.base_url}/oauth2/tokenP",
            data=body,
            headers={
                "content-type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        payload = _read_json_response(req)
        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise KISError("KIS token response did not include access_token")
        self._access_token = token
        expires_in = float(payload.get("expires_in", 86400) or 86400)
        expires_at = time.time() + max(expires_in, 0.0)
        self._access_token_expires_at = expires_at
        self._write_cached_token(token, expires_at)
        return token

    def _token(self) -> str:
        if (
            not self._access_token
            or not self._access_token_expires_at
            or self._access_token_expires_at <= time.time() + TOKEN_EXPIRY_SAFETY_SECONDS
        ):
            return self.issue_access_token()
        return self._access_token

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.config.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url, headers=self._headers(tr_id), method="GET")
        return _read_json_response(req)

    def post(self, path: str, tr_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        data = json.dumps(
            {key: value for key, value in body.items() if value is not None},
            separators=(",", ":"),
        ).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(tr_id), method="POST")
        return _read_json_response(req)
