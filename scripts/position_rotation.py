#!/usr/bin/env python3
"""Hold/trim/exit/replace scoring for existing positions."""
from __future__ import annotations
from typing import Any, Mapping, Sequence


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ''):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def score_position(position: Mapping[str, Any], catalyst: Mapping[str, Any] | None = None, technical: Mapping[str, Any] | None = None, *, age_days: float = 0.0, portfolio_value: float = 1.0) -> dict[str, Any]:
    catalyst = catalyst or {}; technical = technical or {}
    symbol = str(position.get('symbol', '')).upper()
    market_value = abs(_f(position.get('market_value')))
    qty = abs(_f(position.get('qty')))
    entry = _f(position.get('avg_entry_price'))
    current = _f(position.get('current_price') or position.get('market_price') or (market_value / qty if qty else entry))
    unrealized_pct = _f(position.get('unrealized_plpc')) * 100.0
    if unrealized_pct == 0 and entry > 0:
        unrealized_pct = (current - entry) / entry * 100.0
    catalyst_score = _f(catalyst.get('score'), 5.0)
    tech_score = _f(technical.get('technical_score') or technical.get('score'), 5.0)
    rr_remaining = _f(technical.get('risk_reward'), 1.0)
    exposure_pct = market_value / max(portfolio_value, 1.0) * 100.0
    negative_veto = catalyst.get('trade_allowed') is False or catalyst.get('catalyst_status') == 'negative_news_veto'
    score = tech_score * 0.45 + catalyst_score * 0.35 + max(min(unrealized_pct, 10), -10) * 0.08 + min(rr_remaining, 4.0) * 0.45 - min(age_days, 20) * 0.04
    if negative_veto:
        score -= 4.0
    action = 'HOLD'; reasons = []
    if negative_veto:
        action = 'EXIT'; reasons.append('negative_catalyst_veto')
    elif age_days >= 8 and unrealized_pct < 0.5:
        action = 'EXIT'; reasons.append('stale_flat_or_loser')
    elif exposure_pct > 45 and score < 7.5:
        action = 'TRIM'; reasons.append('oversized_without_high_conviction')
    elif unrealized_pct >= 4 and score < 6.5:
        action = 'TRIM'; reasons.append('protect_gain_with_fading_score')
    else:
        reasons.append('position_still_acceptable')
    return {'symbol': symbol, 'score': round(score, 3), 'action': action, 'reasons': reasons, 'unrealized_pct': round(unrealized_pct, 3), 'exposure_pct': round(exposure_pct, 2), 'catalyst_status': catalyst.get('catalyst_status')}


def plan_rotation(positions: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]], account: Mapping[str, Any], policy: Mapping[str, Any], catalyst_by_symbol: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    catalyst_by_symbol = catalyst_by_symbol or {}
    portfolio_value = _f(account.get('portfolio_value') or account.get('equity') or account.get('cash'), 1.0)
    position_scores = [score_position(p, catalyst_by_symbol.get(str(p.get('symbol', '')).upper()), portfolio_value=portfolio_value) for p in positions]
    ranked_candidates = []
    for c in candidates:
        symbol = str(c.get('symbol', '')).upper()
        confidence = _f(c.get('confidence'), _f(c.get('technical_score'), 5.0))
        rr = _f(c.get('risk_reward'), 1.0)
        raw_ca = c.get('catalyst_analysis')
        ca: Mapping[str, Any] = raw_ca if isinstance(raw_ca, Mapping) else {}
        catalyst_score = _f(c.get('catalyst_score'), _f(ca.get('score'), 5.0))
        ranked_candidates.append({'symbol': symbol, 'score': round(confidence * 0.55 + catalyst_score * 0.30 + min(rr, 4.0) * 0.8, 3), 'raw': dict(c)})
    ranked_candidates.sort(key=lambda x: x['score'], reverse=True)
    weakest = sorted(position_scores, key=lambda x: x['score'])
    replacements = []
    for pos, cand in zip(weakest, ranked_candidates):
        if cand['score'] >= pos['score'] + _f(policy.get('position_rotation', {}).get('replace_score_margin'), 1.0):
            replacements.append({'exit_symbol': pos['symbol'], 'buy_symbol': cand['symbol'], 'score_delta': round(cand['score'] - pos['score'], 3), 'action': 'REPLACE_WITH'})
    cash = _f(account.get('cash'))
    min_buy_cash = _f(policy.get('cash_control', {}).get('min_new_buy_cash_usd'), 50.0)
    mode = 'rotation_only' if cash < min_buy_cash and positions else 'new_buys_allowed'
    return {'agent': 'Position Rotation Agent', 'mode': mode, 'position_scores': position_scores, 'ranked_candidates': [{k: v for k, v in row.items() if k != 'raw'} for row in ranked_candidates], 'replacement_plan': replacements, 'cash_gate': {'cash': round(cash, 2), 'min_new_buy_cash_usd': min_buy_cash, 'micro_orders_blocked': cash < min_buy_cash}}
