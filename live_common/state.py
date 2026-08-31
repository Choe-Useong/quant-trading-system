#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "events": {}, "plans": {}}
    try:
        state = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "events": {}, "plans": {}}
    if not isinstance(state.get("events"), dict):
        state["events"] = {}
    if not isinstance(state.get("plans"), dict):
        state["plans"] = {}
    state.setdefault("version", 1)
    return state


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def short_hash(payload: Any, *, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:length]


def build_plan_id(payload: dict[str, Any]) -> str:
    seed = {
        "profile_json": payload.get("profile_json"),
        "target_timestamp": payload.get("target_timestamp"),
        "last_change_timestamp": payload.get("last_change_timestamp"),
        "rows": [
            {
                "symbol": row.get("symbol"),
                "action": row.get("action"),
                "order_qty": row.get("order_qty"),
                "target_weight": row.get("target_weight"),
            }
            for row in payload.get("rows", [])
        ],
    }
    return f"plan_{short_hash(seed)}"


def now_unix() -> float:
    return time.time()


def register_plan(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(payload["plan_id"])
    change_id = payload.get("last_change_timestamp")
    rows = payload.get("rows", [])
    nonzero_orders = [
        {
            "symbol": row.get("symbol"),
            "action": row.get("action"),
            "order_qty": row.get("order_qty"),
            "order_notional_usd": row.get("order_notional_usd"),
        }
        for row in rows
        if row.get("action") in {"buy", "sell"} and float(row.get("order_qty") or 0) > 0
    ]
    state.setdefault("plans", {})[plan_id] = {
        "status": "planned",
        "change_id": change_id,
        "profile_json": payload.get("profile_json"),
        "target_timestamp": payload.get("target_timestamp"),
        "created_at": now_unix(),
        "plan_hash": payload.get("plan_hash"),
        "rebalance_allowed": payload.get("rebalance_allowed"),
        "rebalance_reason": payload.get("rebalance_reason"),
        "nonzero_orders": nonzero_orders,
    }
    if change_id:
        event = state.setdefault("events", {}).setdefault(str(change_id), {"status": "detected"})
        event["last_plan_id"] = plan_id
        event["last_planned_at"] = now_unix()
    return state


def mark_plan_executed(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    execution_note: str = "",
) -> dict[str, Any]:
    plan_id = str(payload.get("plan_id") or "")
    change_id = str(payload.get("last_change_timestamp") or "")
    if not plan_id:
        raise ValueError("Plan payload does not include plan_id")
    if not change_id:
        raise ValueError("Plan payload does not include last_change_timestamp")
    if not payload.get("rebalance_allowed"):
        raise ValueError(f"Plan was not allowed to rebalance: {payload.get('rebalance_reason')}")

    state = register_plan(state, payload)
    state["last_executed_change_timestamp"] = change_id
    state["last_executed_plan_id"] = plan_id
    state["last_executed_at"] = now_unix()
    state["last_target_timestamp"] = payload.get("target_timestamp")
    event = state.setdefault("events", {}).setdefault(change_id, {})
    event.update(
        {
            "status": "executed",
            "executed_plan_id": plan_id,
            "executed_at": now_unix(),
            "execution_note": execution_note,
        }
    )
    state.setdefault("plans", {}).setdefault(plan_id, {})["status"] = "executed"
    state["plans"][plan_id]["executed_at"] = now_unix()
    state["plans"][plan_id]["execution_note"] = execution_note
    return state


def mark_plan_submitted(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    execution_report: dict[str, Any],
) -> dict[str, Any]:
    plan_id = str(payload.get("plan_id") or "")
    change_id = str(payload.get("last_change_timestamp") or "")
    if not plan_id:
        raise ValueError("Plan payload does not include plan_id")
    state = register_plan(state, payload)
    state.setdefault("plans", {}).setdefault(plan_id, {})["status"] = "submitted"
    state["plans"][plan_id]["submitted_at"] = now_unix()
    state["plans"][plan_id]["execution_report_id"] = execution_report.get("execution_report_id")
    state["plans"][plan_id]["submitted_orders"] = execution_report.get("submitted_orders", [])
    state["last_submitted_plan_id"] = plan_id
    if change_id:
        event = state.setdefault("events", {}).setdefault(change_id, {})
        event["status"] = "submitted"
        event["last_submitted_plan_id"] = plan_id
        event["last_submitted_at"] = now_unix()
    return state
