from __future__ import annotations

from typing import Any


CHANGE_ALREADY_EXECUTED = "change_event_already_executed"
CASH_TOPUP_REASON = "cash_topup"
CASH_TOPUP_WAITING_BUY_REASON = "cash_topup_waiting_buy_phase"


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def cash_topup_config(profile: dict[str, Any]) -> tuple[bool, float]:
    trading = profile.get("trading") if isinstance(profile.get("trading"), dict) else {}
    enabled = parse_bool(trading.get("cash_topup_enabled"), default=False)
    try:
        min_cash_pct = float(trading.get("cash_topup_min_cash_pct") or 0.0)
    except (TypeError, ValueError):
        min_cash_pct = 0.0
    return enabled, max(min_cash_pct, 0.0)


def evaluate_cash_topup(
    *,
    phase: str,
    rebalance_reason: str,
    cash: float,
    equity: float,
    enabled: bool,
    min_cash_pct: float,
) -> dict[str, Any]:
    cash_pct = float(cash) / float(equity) if float(equity) > 0 else 0.0
    needed = (
        bool(enabled)
        and rebalance_reason == CHANGE_ALREADY_EXECUTED
        and float(equity) > 0
        and cash_pct >= float(min_cash_pct)
    )
    return {
        "enabled": bool(enabled),
        "min_cash_pct": float(min_cash_pct),
        "cash_pct": cash_pct,
        "needed": needed,
        "buy_allowed": needed and phase in {"buy", "full"},
        "reason": CASH_TOPUP_REASON if needed else "",
    }
