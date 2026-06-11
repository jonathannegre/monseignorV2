#!/usr/bin/env python3
"""Validation-first trade pipeline for MonseignorV2.

This module deliberately never submits broker orders. It composes the validation
agents, produces auditable proposals, and keeps execution gated until a manual
validation flag is supplied by a human-controlled caller.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from .portfolio_constructor import construct_portfolio
except ImportError:
    from portfolio_constructor import construct_portfolio

DEFAULT_WATCHLIST = [
    {
        "symbol": "SPY",
        "asset_class": "ETF",
        "last_price": 520.00,
        "technical_score": 8.1,
        "catalyst_score": 7.8,
        "entry": 520.00,
        "stop_loss": 510.00,
        "take_profit": 544.00,
        "risk_reward": 2.4,
        "trend": "uptrend confirmé au-dessus des moyennes mobiles",
        "catalyst": "flux ETF large-cap et momentum marché constructif",
    },
    {
        "symbol": "MSFT",
        "asset_class": "EQUITY",
        "last_price": 430.00,
        "technical_score": 7.9,
        "catalyst_score": 7.6,
        "entry": 430.00,
        "stop_loss": 421.00,
        "take_profit": 449.80,
        "risk_reward": 2.2,
        "trend": "consolidation haussière avec support proche",
        "catalyst": "demande IA/cloud toujours robuste",
    },
]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalized_candidate(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Map scanner output into the validation contract without inventing signals."""

    candidate = dict(raw)
    technical_analysis = candidate.get("technical_analysis") if isinstance(candidate.get("technical_analysis"), Mapping) else {}
    catalyst_analysis = candidate.get("catalyst_analysis") if isinstance(candidate.get("catalyst_analysis"), Mapping) else {}

    if "asset_class" not in candidate and "asset_type" in candidate:
        candidate["asset_class"] = "ETF" if str(candidate.get("asset_type", "")).lower() == "etf" else "EQUITY"
    if "last_price" not in candidate and "price" in candidate:
        candidate["last_price"] = candidate.get("price")

    for key in ("entry", "stop_loss", "take_profit", "risk_reward", "confidence", "technical_score"):
        if key not in candidate and key in technical_analysis:
            candidate[key] = technical_analysis.get(key)
    if "trend" not in candidate and technical_analysis:
        candidate["trend"] = technical_analysis.get("technical_summary", "n/a")
    if "setup" not in candidate and technical_analysis:
        candidate["setup"] = technical_analysis.get("setup")

    if "catalyst_score" not in candidate and catalyst_analysis:
        candidate["catalyst_score"] = catalyst_analysis.get("score", 0)
    if "catalyst" not in candidate:
        candidate["catalyst"] = catalyst_analysis.get("summary", "No verified catalyst") if catalyst_analysis else "No verified catalyst"
    if "catalyst_status" not in candidate:
        candidate["catalyst_status"] = catalyst_analysis.get("catalyst_status", "no_verified_catalyst") if catalyst_analysis else "no_verified_catalyst"

    return candidate


def _make_execution_gate(
    policy: Mapping[str, Any], account: Mapping[str, Any], manual_validation: bool
) -> Dict[str, Any]:
    reasons: List[str] = []
    hard_block_reasons: List[str] = []
    if not policy.get("trading_enabled", False):
        reasons.append("policy_trading_disabled")
    if policy.get("no_orders_before_full_pipeline", True):
        reasons.append("no_orders_before_full_pipeline_guard")
    if not account.get("account_verified", False):
        hard_block_reasons.append(str(account.get("reason") or "account_not_verified"))
    if account.get("trading_blocked", True):
        reason = str(account.get("reason") or "account_trading_blocked")
        if reason not in hard_block_reasons:
            hard_block_reasons.append(reason)

    for reason in hard_block_reasons:
        if reason not in reasons:
            reasons.append(reason)

    if hard_block_reasons:
        status = "blocked"
    elif not manual_validation:
        status = "manual_validation_required"
        reasons.append("manual validation absent")
    else:
        # Even with manual validation, this module stays validation-only. Another
        # audited execution module would be needed to place orders.
        status = "validated_for_manual_handoff"

    return {
        "status": status,
        "manual_validation_present": bool(manual_validation),
        "orders_allowed_by_this_module": False,
        "reasons": reasons,
    }


def _cash_allocation(policy: Mapping[str, Any], account: Mapping[str, Any], candidate: Mapping[str, Any]) -> Dict[str, Any]:
    allocation_cap = _as_float(policy.get("allocated_capital_usd"), 0.0)
    broker_cash = _as_float(account.get("cash"))
    cash = min(broker_cash, allocation_cap) if allocation_cap > 0 else broker_cash
    buffer_usd = _as_float(policy.get("hard_rules", {}).get("minimum_cash_buffer_usd"), 5.0)
    min_new_buy_cash = _as_float(policy.get("cash_control", {}).get("min_new_buy_cash_usd"), 50.0)
    broker_portfolio_value = _as_float(account.get("portfolio_value"), cash)
    portfolio_value = min(broker_portfolio_value, allocation_cap) if allocation_cap > 0 else broker_portfolio_value
    max_risk_pct = _as_float(policy.get("risk_mode", {}).get("max_risk_per_trade_pct"), 1.0)
    risk_budget = round(max(portfolio_value, 0.0) * max_risk_pct / 100.0, 2)
    usable_cash = max(cash - buffer_usd, 0.0)
    if usable_cash < min_new_buy_cash and int(account.get("open_positions_count", 0) or 0) > 0:
        return {
            "cash": round(cash, 2),
            "broker_cash": round(broker_cash, 2),
            "allocated_capital_usd": round(allocation_cap, 2),
            "minimum_cash_buffer_usd": round(buffer_usd, 2),
            "min_new_buy_cash_usd": round(min_new_buy_cash, 2),
            "rotation_only_mode": True,
            "risk_budget_usd": risk_budget,
            "suggested_notional_usd": 0.0,
            "suggested_qty": 0.0,
            "entry": round(max(_as_float(candidate.get("entry"), _as_float(candidate.get("last_price"))), 0.01), 4),
            "stop_loss": round(_as_float(candidate.get("stop_loss")), 4),
            "price_risk_per_share": 0.0,
            "max_loss_if_stop_hit": 0.0,
            "cash_after_order": round(cash, 2),
            "fractional_qty": True,
        }
    entry = max(_as_float(candidate.get("entry"), _as_float(candidate.get("last_price"))), 0.01)
    stop_loss = _as_float(candidate.get("stop_loss"))
    price_risk = abs(entry - stop_loss) if stop_loss > 0 else 0.0
    max_exposure_pct = _as_float(policy.get("risk_mode", {}).get("max_total_exposure_pct"), 50.0)
    exposure_cap = max(portfolio_value, 0.0) * max_exposure_pct / 100.0
    if price_risk <= 0:
        qty = 0.0
        suggested_notional = 0.0
    else:
        risk_sized_qty = risk_budget / price_risk if risk_budget > 0 else 0.0
        cash_sized_qty = usable_cash / entry if entry > 0 else 0.0
        exposure_sized_qty = exposure_cap / entry if entry > 0 else 0.0
        qty = round(max(min(risk_sized_qty, cash_sized_qty, exposure_sized_qty), 0.0), 4)
        suggested_notional = qty * entry
    max_loss = qty * price_risk
    return {
        "cash": round(cash, 2),
        "broker_cash": round(broker_cash, 2),
        "allocated_capital_usd": round(allocation_cap, 2),
        "minimum_cash_buffer_usd": round(buffer_usd, 2),
        "risk_budget_usd": risk_budget,
        "suggested_notional_usd": round(max(suggested_notional, 0.0), 2),
        "suggested_qty": qty,
        "entry": round(entry, 4),
        "stop_loss": round(stop_loss, 4),
        "price_risk_per_share": round(price_risk, 4),
        "max_loss_if_stop_hit": round(max_loss, 2),
        "cash_after_order": round(cash - suggested_notional, 2),
        "fractional_qty": True,
    }


def _evaluate_candidates(
    policy: Mapping[str, Any], account: Mapping[str, Any], watchlist: Iterable[Mapping[str, Any]]
) -> Dict[str, Any]:
    min_confidence = _as_float(policy.get("risk_mode", {}).get("min_confidence"), 7.5)
    min_rr = _as_float(policy.get("risk_mode", {}).get("min_risk_reward"), 2.0)
    allow_etf = bool(policy.get("allow_etf", True))
    setup_rotation = policy.get("setup_rotation", {}) if isinstance(policy.get("setup_rotation"), Mapping) else {}
    disabled_setups = {str(s).strip() for s in setup_rotation.get("disabled_setups", []) if str(s).strip()}
    boosted_setups = {str(s).strip() for s in setup_rotation.get("boosted_setups", []) if str(s).strip()}

    technical: List[Dict[str, Any]] = []
    catalyst: List[Dict[str, Any]] = []
    risk: List[Dict[str, Any]] = []
    cash: List[Dict[str, Any]] = []
    committee: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []

    for raw in watchlist:
        candidate = _normalized_candidate(raw)
        symbol = str(candidate["symbol"])
        if candidate.get("asset_class") == "ETF" and not allow_etf:
            continue

        technical_score = _as_float(candidate.get("technical_score"))
        catalyst_score = _as_float(candidate.get("catalyst_score"))
        risk_reward = _as_float(candidate.get("risk_reward"))
        setup = str(candidate.get("setup") or "").strip()

        if setup and setup in disabled_setups:
            technical.append({"symbol": symbol, "score": technical_score, "view": candidate.get("trend", "n/a"), "setup": setup, "entry": candidate.get("entry"), "stop_loss": candidate.get("stop_loss"), "take_profit": candidate.get("take_profit")})
            catalyst.append({"symbol": symbol, "score": catalyst_score, "status": candidate.get("catalyst_status", "no_verified_catalyst"), "view": candidate.get("catalyst", "n/a")})
            risk.append({"symbol": symbol, "risk_reward": risk_reward, "confidence": 0.0, "accepted": False, "max_loss_if_stop_hit": 0.0})
            cash.append({"symbol": symbol, "rotation_blocked": True, "blocked_setup": setup})
            committee.append({"symbol": symbol, "vote": "reject", "reason": f"setup_rotation_disabled:{setup}"})
            continue

        confidence = round(_as_float(candidate.get("confidence"), (technical_score + catalyst_score) / 2.0), 2)
        catalyst_allowed = True
        catalyst_analysis = candidate.get("catalyst_analysis") if isinstance(candidate.get("catalyst_analysis"), Mapping) else {}
        if catalyst_analysis and catalyst_analysis.get("trade_allowed") is False:
            catalyst_allowed = False
        if str(candidate.get("catalyst_status", catalyst_analysis.get("catalyst_status", ""))) == "negative_news_veto":
            catalyst_allowed = False
        if setup and setup in boosted_setups:
            confidence = round(min(10.0, confidence + 0.35), 2)
            risk_reward = round(risk_reward + 0.05, 2)

        cash_plan = _cash_allocation(policy, account, candidate)
        has_complete_plan = all(_as_float(candidate.get(k)) > 0 for k in ("entry", "stop_loss", "take_profit"))
        risk_ok = catalyst_allowed and confidence >= min_confidence and risk_reward >= min_rr and cash_plan["suggested_qty"] > 0 and has_complete_plan

        technical.append({"symbol": symbol, "score": technical_score, "view": candidate.get("trend", "n/a"), "setup": candidate.get("setup"), "entry": candidate.get("entry"), "stop_loss": candidate.get("stop_loss"), "take_profit": candidate.get("take_profit")})
        catalyst.append({"symbol": symbol, "score": catalyst_score, "status": candidate.get("catalyst_status", catalyst_analysis.get("catalyst_status", "no_verified_catalyst")), "view": candidate.get("catalyst", "n/a"), "trade_allowed": catalyst_allowed})
        risk.append({"symbol": symbol, "risk_reward": risk_reward, "confidence": confidence, "accepted": risk_ok, "max_loss_if_stop_hit": cash_plan.get("max_loss_if_stop_hit", 0.0)})
        cash.append({"symbol": symbol, **cash_plan})

        committee_vote = "propose" if risk_ok else "reject"
        reject_reason = "negative_catalyst_veto" if not catalyst_allowed else "score/risk/cash filters"
        committee.append({"symbol": symbol, "vote": committee_vote, "reason": "score/risk/cash filters" if committee_vote == "propose" else reject_reason})
        if committee_vote != "propose":
            continue

        entry = _as_float(candidate.get("entry"), _as_float(candidate.get("last_price")))
        proposal = {
            "symbol": symbol,
            "side": "buy",
            "status": "approved_for_execution",
            "execution_allowed": True,
            "execution_block_reason": None,
            "confidence": confidence,
            "risk_reward": risk_reward,
            "setup": setup,
            "order_intent": {
                "order_type": "limit",
                "time_in_force": "day",
                "limit_price": round(entry, 2),
                "qty": cash_plan["suggested_qty"],
                "bracket_order_preferred": bool(policy.get("hard_rules", {}).get("prefer_bracket_orders", True)),
                "take_profit_price": round(_as_float(candidate.get("take_profit")), 2),
                "stop_loss_price": round(_as_float(candidate.get("stop_loss")), 2),
            },
            "agent_rationale": {
                "technical": candidate.get("trend", "n/a"),
                "catalyst": candidate.get("catalyst", "n/a"),
                "risk": f"RR {risk_reward} / confiance {confidence}",
                "cash": cash_plan,
            },
        }
        proposals.append(proposal)

    return {
        "Technical Analyst Agent": {"evaluations": technical},
        "Catalyst & News Agent": {"evaluations": catalyst},
        "Risk Manager Agent": {"evaluations": risk},
        "Cash Control Agent": {"evaluations": cash},
        "Strategy Committee Agent": {"votes": committee},
    }, proposals


def compose_trade_validation_pipeline(
    policy: Mapping[str, Any],
    account: Mapping[str, Any],
    *,
    manual_validation: bool = False,
    watchlist: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compose validation agents and return proposals without any execution."""

    gate = _make_execution_gate(policy, account, manual_validation)
    blocked = gate["status"] == "blocked"
    candidates = DEFAULT_WATCHLIST if watchlist is None else watchlist
    agent_outputs, proposals = ({}, []) if blocked else _evaluate_candidates(policy, account, candidates)
    portfolio_plan = construct_portfolio(proposals, account, policy) if not blocked else {"agent": "Portfolio Construction Agent", "selected_count": 0, "selected_proposals": [], "rejected_proposals": []}
    proposals = portfolio_plan.get("selected_proposals", proposals)

    agent_outputs.setdefault(
        "Cash Control Agent",
        {
            "account_verified": bool(account.get("account_verified", False)),
            "cash": _as_float(account.get("cash")),
            "trading_blocked": bool(account.get("trading_blocked", True)),
            "reason": account.get("reason"),
        },
    )
    agent_outputs["Compliance & Kill-Switch Agent"] = gate
    agent_outputs["Portfolio Construction Agent"] = portfolio_plan
    agent_outputs["CEO Agent"] = {
        "decision": "journal_only_no_execution",
        "next_action": "validation manuelle explicite requise avant toute exécution",
        "proposals_reviewed": len(proposals),
    }

    return {
        "pipeline": "trade_validation_without_execution",
        "execution_gate": gate,
        "agent_outputs": agent_outputs,
        "proposals": proposals,
        "proposals_count": len(proposals),
        "trades_approved": 0,
        "trades_executed": 0,
        "orders_sent": 0,
    }


def append_trade_validation_journal(journal_path: pathlib.Path, result: Mapping[str, Any], *, timestamp: str) -> None:
    """Append an auditable JSONL event for validation-only proposals."""

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    status = result.get("execution_gate", {}).get("status", "unknown")
    summary = (
        f"Pipeline trade validation sans exécution: {result.get('proposals_count', 0)} proposition(s); "
        f"gate={status}; orders_sent=0."
    )
    entry = {
        "agent": "Memory / Journal Agent",
        "event_type": "trade_validation_proposals",
        "symbol": "MULTI" if result.get("proposals") else "N/A",
        "timestamp": timestamp,
        "summary": summary,
        "data": dict(result),
    }
    with journal_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

