#!/usr/bin/env python3
"""Intraday confirmation and stale-order repricing helpers."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Mapping


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclasses.dataclass(frozen=True)
class IntradayExecutionConfig:
    max_spread_bps: float = 20.0
    min_relative_volume: float = 1.05
    max_chase_bps: float = 35.0
    avoid_first_minutes: bool = True
    first_minutes_guard: int = 10


def _spread_bps(quote: Mapping[str, Any]) -> tuple[float, float, float]:
    bid = _f(quote.get("bp") or quote.get("bid_price"))
    ask = _f(quote.get("ap") or quote.get("ask_price"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return bid, ask, float("inf")
    mid = (bid + ask) / 2
    return bid, ask, (ask - bid) / mid * 10_000


def confirm_intraday_entry(symbol: str, bars_5m: list[Mapping[str, Any]], quote: Mapping[str, Any], config: IntradayExecutionConfig | None = None, *, minutes_after_open: int = 30) -> dict[str, Any]:
    config = config or IntradayExecutionConfig()
    bid, ask, spread = _spread_bps(quote)
    reasons: list[str] = []
    if config.avoid_first_minutes and minutes_after_open < config.first_minutes_guard:
        reasons.append("opening_spread_guard")
    if spread > config.max_spread_bps:
        reasons.append("spread_too_wide")
    closes = [_f(bar.get("c") or bar.get("close")) for bar in bars_5m if _f(bar.get("c") or bar.get("close")) > 0]
    volumes = [_f(bar.get("v") or bar.get("volume")) for bar in bars_5m if _f(bar.get("v") or bar.get("volume")) >= 0]
    momentum_ok = len(closes) >= 2 and closes[-1] >= closes[-2]
    avg_volume = sum(volumes[:-1]) / max(len(volumes[:-1]), 1) if len(volumes) > 1 else 0.0
    rel_volume = volumes[-1] / avg_volume if avg_volume > 0 and volumes else 1.0
    if not momentum_ok:
        reasons.append("intraday_momentum_not_confirmed")
    if rel_volume < config.min_relative_volume and len(volumes) > 1:
        reasons.append("intraday_volume_not_confirmed")
    confirmed = not reasons
    return {
        "agent": "Intraday Execution Agent",
        "symbol": symbol.upper(),
        "confirmed": confirmed,
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread_bps": round(spread, 3) if spread != float("inf") else spread,
        "relative_volume": round(rel_volume, 3),
        "reasons": reasons,
        "suggested_limit_price": round(ask, 2) if confirmed else None,
    }


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def reprice_stale_order(order: Mapping[str, Any], quote: Mapping[str, Any], *, now: dt.datetime | None = None, max_age_minutes: int = 15, max_chase_bps: float = 35.0) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    created = _parse_time(order.get("created_at") or order.get("submitted_at"))
    if created is None:
        return {"action": "hold", "reason": "unknown_order_age"}
    age = (now - created).total_seconds() / 60.0
    if age < max_age_minutes:
        return {"action": "hold", "reason": "not_stale", "age_minutes": round(age, 1)}
    bid, ask, spread = _spread_bps(quote)
    old_limit = _f(order.get("limit_price"))
    if ask <= 0 or old_limit <= 0:
        return {"action": "cancel", "reason": "stale_no_valid_quote", "age_minutes": round(age, 1)}
    chase_bps = (ask - old_limit) / old_limit * 10_000
    if chase_bps > max_chase_bps:
        return {"action": "cancel", "reason": "stale_chase_limit_exceeded", "age_minutes": round(age, 1), "chase_bps": round(chase_bps, 2)}
    return {"action": "replace", "reason": "stale_reprice_within_bounds", "age_minutes": round(age, 1), "old_limit_price": round(old_limit, 2), "new_limit_price": round(ask, 2), "spread_bps": round(spread, 3)}
