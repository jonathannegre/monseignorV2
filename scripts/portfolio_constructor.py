#!/usr/bin/env python3
"""Portfolio construction layer for proposal-level allocation."""

from __future__ import annotations

from typing import Any, Mapping

SECTOR_MAP = {
    "NVDA": "Technology", "AMD": "Technology", "AAPL": "Technology", "MSFT": "Technology", "QQQ": "Technology", "XLK": "Technology",
    "XLF": "Financials", "JPM": "Financials", "BAC": "Financials",
    "XLE": "Energy", "XLY": "Consumer Discretionary", "XLV": "Healthcare", "SPY": "Broad Market", "IWM": "Small Caps", "EEM": "Emerging Markets",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _notional(proposal: Mapping[str, Any]) -> float:
    cash = proposal.get("agent_rationale", {}).get("cash", {}) if isinstance(proposal.get("agent_rationale"), Mapping) else {}
    suggested = _f(cash.get("suggested_notional_usd")) if isinstance(cash, Mapping) else 0.0
    if suggested > 0:
        return suggested
    order = proposal.get("order_intent", {}) if isinstance(proposal.get("order_intent"), Mapping) else {}
    return _f(order.get("qty")) * _f(order.get("limit_price"))


def _resize_order(row: dict[str, Any], target_notional: float) -> dict[str, Any]:
    """Return a proposal resized to target_notional while keeping entry/stop/TP intact."""
    resized = dict(row)
    order = dict(resized.get("order_intent", {})) if isinstance(resized.get("order_intent"), Mapping) else {}
    limit_price = _f(order.get("limit_price"))
    if limit_price > 0:
        order["qty"] = round(target_notional / limit_price, 4)
        resized["order_intent"] = order
    rationale = dict(resized.get("agent_rationale", {})) if isinstance(resized.get("agent_rationale"), Mapping) else {}
    cash = dict(rationale.get("cash", {})) if isinstance(rationale.get("cash"), Mapping) else {}
    cash["suggested_notional_usd"] = round(target_notional, 2)
    cash["portfolio_constructor_resized"] = True
    rationale["cash"] = cash
    resized["agent_rationale"] = rationale
    resized["requested_notional_usd"] = round(target_notional, 2)
    resized["portfolio_constructor_resized"] = True
    return resized


def _sector(proposal: Mapping[str, Any]) -> str:
    explicit = proposal.get("sector") or proposal.get("asset_sector")
    if explicit:
        return str(explicit)
    return SECTOR_MAP.get(str(proposal.get("symbol", "")).upper(), "Other")


def _conviction(proposal: Mapping[str, Any]) -> float:
    confidence = _f(proposal.get("confidence"))
    rr = _f(proposal.get("risk_reward"))
    catalyst = _f(proposal.get("catalyst_score"), confidence)
    return round(confidence * 0.55 + min(rr, 4.0) * 1.25 + catalyst * 0.2, 4)


def construct_portfolio(proposals: list[Mapping[str, Any]], account: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    cfg = policy.get("portfolio_construction", {}) if isinstance(policy.get("portfolio_construction"), Mapping) else {}
    portfolio_value = max(_f(account.get("portfolio_value"), _f(account.get("cash"))), 1.0)
    cash = max(_f(account.get("cash")), 0.0)
    max_sector_pct = _f(cfg.get("max_sector_exposure_pct"), 100.0)
    max_new_orders = int(cfg.get("max_new_orders", policy.get("risk_mode", {}).get("max_open_positions", 10)) or 10)
    resize_to_caps = bool(cfg.get("resize_to_caps", False))
    min_resized_notional = _f(cfg.get("min_resized_notional_usd"), 50.0)
    regime = str(cfg.get("regime", "risk_on" if policy.get("autonomous_mode") else "neutral"))
    regime_budget_pct = {"risk_off": 35.0, "neutral": 65.0, "risk_on": _f(policy.get("risk_mode", {}).get("max_total_exposure_pct"), 85.0)}.get(regime, 65.0)
    total_budget = min(cash, portfolio_value * regime_budget_pct / 100.0)
    sector_cap = portfolio_value * max_sector_pct / 100.0

    ranked = []
    for proposal in proposals:
        row = dict(proposal)
        row["sector"] = _sector(row)
        row["conviction_score"] = _conviction(row)
        row["requested_notional_usd"] = round(_notional(row), 2)
        ranked.append(row)
    ranked.sort(key=lambda item: (item["conviction_score"], item["requested_notional_usd"]), reverse=True)

    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    sector_usage: dict[str, float] = {}
    total_used = 0.0
    for row in ranked:
        symbol = str(row.get("symbol", ""))
        requested = _f(row.get("requested_notional_usd"))
        sector = str(row.get("sector", "Other"))
        if len(selected) >= max_new_orders:
            rejected.append({"symbol": symbol, "reason": "max_new_orders", "conviction_score": row["conviction_score"]})
            continue
        if requested <= 0:
            rejected.append({"symbol": symbol, "reason": "zero_notional", "conviction_score": row["conviction_score"]})
            continue
        sector_remaining = max(sector_cap - sector_usage.get(sector, 0.0), 0.0)
        budget_remaining = max(total_budget - total_used, 0.0)
        clipped = min(requested, sector_remaining, budget_remaining)
        if resize_to_caps and clipped < requested and clipped >= min_resized_notional:
            row = _resize_order(row, clipped)
            requested = clipped
        if sector_usage.get(sector, 0.0) + requested > sector_cap:
            rejected.append({"symbol": symbol, "reason": f"sector_cap:{sector}", "conviction_score": row["conviction_score"], "requested_notional_usd": round(requested, 2)})
            continue
        if total_used + requested > total_budget:
            rejected.append({"symbol": symbol, "reason": "portfolio_budget", "conviction_score": row["conviction_score"], "requested_notional_usd": round(requested, 2)})
            continue
        selected.append(row)
        sector_usage[sector] = sector_usage.get(sector, 0.0) + requested
        total_used += requested

    return {
        "agent": "Portfolio Construction Agent",
        "regime": regime,
        "portfolio_budget_usd": round(total_budget, 2),
        "sector_cap_usd": round(sector_cap, 2),
        "selected_count": len(selected),
        "selected_proposals": selected,
        "rejected_proposals": rejected,
        "sector_usage_usd": {k: round(v, 2) for k, v in sorted(sector_usage.items())},
        "cash_redeployment": "replace_low_expectancy_positions_when_candidate_conviction_is_higher",
    }
