#!/usr/bin/env python3
"""Dynamic position management primitives for Monseignor."""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclasses.dataclass(frozen=True)
class PositionPlan:
    symbol: str
    qty: float
    entry_price: float
    current_price: float
    stop_price: float
    target_price: float
    initial_risk: float
    open_r: float
    partial_qty: float
    reason: str


def build_position_plan(position: Mapping[str, Any], quote: Mapping[str, Any] | None = None, setup_plan: Mapping[str, Any] | None = None, *, atr_multiplier: float = 1.5) -> PositionPlan:
    quote = quote or {}
    setup_plan = setup_plan or {}
    symbol = str(position.get("symbol", setup_plan.get("symbol", ""))).upper()
    qty = abs(_f(position.get("qty")))
    entry = _f(position.get("avg_entry_price") or setup_plan.get("entry"))
    current = _f(quote.get("price") or quote.get("last") or position.get("current_price") or position.get("market_value"))
    if current > 0 and qty > 0 and current > entry * 3 and _f(position.get("market_value")) == current:
        current = current / qty
    if current <= 0:
        current = entry
    original_stop = _f(setup_plan.get("stop_loss")) or entry * 0.97
    target = _f(setup_plan.get("take_profit")) or entry + abs(entry - original_stop) * 2
    atr = _f(setup_plan.get("atr14"))
    initial_risk = max(entry - original_stop, entry * 0.005, 0.01)
    open_r = (current - entry) / initial_risk if initial_risk > 0 else 0.0

    trailing_stop = current - atr * atr_multiplier if atr > 0 else current - initial_risk
    stop = max(original_stop, trailing_stop)
    reason_parts = ["atr_trailing" if atr > 0 else "risk_trailing"]
    if open_r >= 1.0:
        stop = max(stop, entry)
        reason_parts.append("breakeven_after_1r")
    if current <= entry and open_r < 0:
        stop = max(original_stop, current - initial_risk * 0.75)
    stop = round(min(stop, current * 0.995), 2) if current > 0 else round(stop, 2)
    partial_qty = round(qty * 0.5, 4) if open_r >= 1.5 else 0.0
    return PositionPlan(symbol=symbol, qty=qty, entry_price=round(entry, 4), current_price=round(current, 4), stop_price=stop, target_price=round(target, 2), initial_risk=round(initial_risk, 4), open_r=round(open_r, 3), partial_qty=partial_qty, reason=";".join(reason_parts))


def evaluate_position_action(plan: PositionPlan, *, has_live_stop: bool = False, age_bars: int = 0, max_stale_bars: int = 8, regime: str = "neutral") -> dict[str, Any]:
    actions: list[str] = []
    if not has_live_stop:
        actions.append("ensure_stop_order")
    if plan.partial_qty > 0:
        actions.append("take_partial_profit")
    if age_bars >= max_stale_bars and plan.open_r < 0.25:
        actions.append("time_stop_exit")
    if regime == "risk_off" and plan.open_r < 1.0:
        actions.append("derisk_regime_flip")
    if not actions:
        actions.append("hold")
    return {
        "agent": "Position Manager Agent",
        "symbol": plan.symbol,
        "actions": actions,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "partial_qty": plan.partial_qty,
        "open_r": plan.open_r,
        "reason": plan.reason,
    }
