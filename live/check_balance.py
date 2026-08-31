#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
UPBIT_BASE_URL = "https://api.upbit.com"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _b64url_json(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_bytes(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _encode_jwt_hs512(payload: dict[str, object], secret_key: str) -> str:
    header = {"alg": "HS512", "typ": "JWT"}
    signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha512).digest()
    return f"{signing_input.decode('ascii')}.{_b64url_bytes(signature)}"


def _authorized_get(path: str) -> list[dict[str, object]]:
    access_key = os.environ.get("UPBIT_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("UPBIT_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise RuntimeError("Missing UPBIT_ACCESS_KEY or UPBIT_SECRET_KEY")

    token = _encode_jwt_hs512(
        {
            "access_key": access_key,
            "nonce": str(uuid.uuid4()),
        },
        secret_key,
    )
    req = urllib.request.Request(
        f"{UPBIT_BASE_URL}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    _load_dotenv(ENV_PATH)
    try:
        balances = _authorized_get("/v1/accounts")
    except Exception as exc:
        print(f"Balance check failed: {exc}", file=sys.stderr)
        return 1

    nonzero = []
    for item in balances:
        balance = float(item.get("balance", 0.0) or 0.0)
        locked = float(item.get("locked", 0.0) or 0.0)
        avg_buy_price = float(item.get("avg_buy_price", 0.0) or 0.0)
        total = balance + locked
        if total <= 0.0:
            continue
        nonzero.append(
            {
                "currency": str(item.get("currency", "")),
                "balance": balance,
                "locked": locked,
                "total": total,
                "avg_buy_price": avg_buy_price,
                "avg_buy_price_modified": bool(item.get("avg_buy_price_modified", False)),
                "unit_currency": str(item.get("unit_currency", "")),
            }
        )

    print(json.dumps(nonzero, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
