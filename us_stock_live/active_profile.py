from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from us_stock_live.trading.state import read_json, write_json


ROOT_DIR = Path(__file__).resolve().parents[1]
ACTIVE_PROFILE_PATH = ROOT_DIR / "us_stock_live" / "configs" / "active_profile.json"
ACTIVE_PROFILE_TYPE = "stock_live_active_profile_v1"


def _resolve_path(path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def is_active_profile_payload(payload: dict[str, Any]) -> bool:
    return payload.get("type") == ACTIVE_PROFILE_TYPE and bool(payload.get("profile_json"))


def load_active_profile(path: Path | None = None) -> dict[str, Any]:
    active_path = path or ACTIVE_PROFILE_PATH
    payload = read_json(active_path)
    if not is_active_profile_payload(payload):
        raise ValueError(f"Invalid active profile file: {active_path}")
    return payload


def resolve_profile_reference(path: Path) -> tuple[Path, dict[str, Any] | None]:
    payload = read_json(path)
    if not is_active_profile_payload(payload):
        return path, None
    profile_path = _resolve_path(str(payload["profile_json"]))
    return profile_path, payload


def write_active_profile(
    *,
    profile_json: Path,
    pending_switch: bool,
    path: Path | None = None,
) -> Path:
    active_path = path or ACTIVE_PROFILE_PATH
    payload: dict[str, Any] = {
        "type": ACTIVE_PROFILE_TYPE,
        "profile_json": _relative(profile_json),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if pending_switch:
        payload["pending_switch"] = {
            "profile_json": _relative(profile_json),
            "bootstrap_policy": "always",
            "requested_at": datetime.now().isoformat(timespec="seconds"),
        }
    write_json(active_path, payload)
    return active_path


def clear_pending_switch(path: Path | None = None) -> None:
    active_path = path or ACTIVE_PROFILE_PATH
    payload = load_active_profile(active_path)
    if "pending_switch" not in payload:
        return
    payload.pop("pending_switch", None)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(active_path, payload)
