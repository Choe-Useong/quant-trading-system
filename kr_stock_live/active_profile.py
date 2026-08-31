from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from kr_stock_live.trading.state import read_json, write_json


ROOT_DIR = Path(__file__).resolve().parents[1]
ACTIVE_PROFILE_PATH = ROOT_DIR / "kr_stock_live" / "configs" / "active_profile.json"
ACTIVE_PROFILE_TYPE = "kr_stock_live_active_profile_v1"


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
        raise ValueError(f"Invalid KR active profile file: {active_path}")
    return payload


def resolve_profile_reference(path: Path) -> tuple[Path, dict[str, Any] | None]:
    payload = read_json(path)
    if not is_active_profile_payload(payload):
        return path, None

    profile_path = _resolve_path(str(payload["profile_json"]))
    target_payload = read_json(profile_path)
    if is_active_profile_payload(target_payload):
        raise ValueError(f"Nested KR active profile is not supported: {profile_path}")
    return profile_path, payload


def write_active_profile(*, profile_json: Path, path: Path | None = None) -> Path:
    active_path = path or ACTIVE_PROFILE_PATH
    payload: dict[str, Any] = {
        "type": ACTIVE_PROFILE_TYPE,
        "profile_json": _relative(profile_json),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json(active_path, payload)
    return active_path
