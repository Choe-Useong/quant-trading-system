from __future__ import annotations


def _rescale_rows_to_target_total(rows: list[dict[str, str]], target_total: float) -> list[dict[str, str]]:
    if not rows:
        return rows
    current_total = sum(float(row["target_weight"]) for row in rows)
    if current_total <= 0.0:
        return rows
    scale = target_total / current_total
    scaled_rows: list[dict[str, str]] = []
    for row in rows:
        next_row = dict(row)
        next_row["target_weight"] = f"{float(row['target_weight']) * scale:.12g}"
        scaled_rows.append(next_row)
    return scaled_rows


def _apply_market_caps(rows: list[dict[str, str]], market_caps: dict[str, float], overflow_mode: str) -> list[dict[str, str]]:
    if not rows or not market_caps:
        return rows
    if overflow_mode not in {"keep_cash", "redistribute"}:
        raise ValueError(f"Unsupported cap_overflow_mode: {overflow_mode}")

    base_weights = {str(row["market"]): float(row["target_weight"]) for row in rows}
    if overflow_mode == "keep_cash":
        adjusted_rows: list[dict[str, str]] = []
        for row in rows:
            market = str(row["market"])
            next_row = dict(row)
            limit = market_caps.get(market)
            weight = base_weights[market]
            next_row["target_weight"] = f"{min(weight, limit) if limit is not None else weight:.12g}"
            adjusted_rows.append(next_row)
        return adjusted_rows

    caps = {market: float(limit) for market, limit in market_caps.items()}
    target_total = sum(base_weights.values())
    result: dict[str, float] = {}
    free_markets = set(base_weights)
    remaining_total = target_total

    while free_markets:
        base_sum = sum(base_weights[market] for market in free_markets)
        if base_sum <= 0.0:
            break
        scaled = {market: remaining_total * base_weights[market] / base_sum for market in free_markets}
        breached = [market for market, weight in scaled.items() if market in caps and weight > caps[market]]
        if not breached:
            for market, weight in scaled.items():
                result[market] = weight
            break
        for market in breached:
            capped_weight = caps[market]
            result[market] = capped_weight
            remaining_total -= capped_weight
            free_markets.remove(market)

    adjusted_rows: list[dict[str, str]] = []
    for row in rows:
        market = str(row["market"])
        next_row = dict(row)
        next_row["target_weight"] = f"{result.get(market, 0.0):.12g}"
        adjusted_rows.append(next_row)
    return adjusted_rows


def postprocess_latest_weight_rows(execution_config: dict[str, object], latest_weight_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = [dict(row) for row in latest_weight_rows]
    if execution_config["strategy_type"] == "sleeve_portfolio":
        portfolio_inactive_mode = str(execution_config.get("portfolio_inactive_mode", "keep_cash"))
        if portfolio_inactive_mode == "redistribute":
            target_total = min(sum(float(item["capital_weight"]) for item in execution_config["sleeves"]), 1.0)
            rows = _rescale_rows_to_target_total(rows, target_total)
        elif portfolio_inactive_mode != "keep_cash":
            raise ValueError(f"Unsupported portfolio_inactive_mode: {portfolio_inactive_mode}")
    rows = _apply_market_caps(
        rows,
        dict(execution_config.get("market_caps", {})),
        str(execution_config.get("cap_overflow_mode", "keep_cash")),
    )
    return rows
