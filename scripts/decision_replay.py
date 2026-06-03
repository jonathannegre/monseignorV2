#!/usr/bin/env python3
"""Replayable decision snapshots for Monseignor cycles."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, Mapping


def build_decision_snapshot(*, timestamp: str, account: Mapping[str, Any], policy: Mapping[str, Any], market_scan: Mapping[str, Any] | None, trade_validation: Mapping[str, Any] | None, portfolio_plan: Mapping[str, Any] | None, execution: Mapping[str, Any] | None, position_management: Mapping[str, Any] | None = None) -> dict[str, Any]:
    candidates = []
    if isinstance(market_scan, Mapping):
        candidates = list(market_scan.get("market_scanner", {}).get("candidates", [])) if isinstance(market_scan.get("market_scanner"), Mapping) else []
    proposals = list(trade_validation.get("proposals", [])) if isinstance(trade_validation, Mapping) else []
    selected = list(portfolio_plan.get("selected_proposals", [])) if isinstance(portfolio_plan, Mapping) else proposals
    exec_results = list(execution.get("results", [])) if isinstance(execution, Mapping) else []
    order_ids = [str(row.get("order_id")) for row in exec_results if row.get("order_id")]
    return {
        "schema": "monseignor_decision_v1",
        "timestamp": timestamp,
        "inputs": {
            "account": dict(account),
            "policy_key_limits": {
                "project": policy.get("project"),
                "risk_mode": policy.get("risk_mode"),
                "portfolio_construction": policy.get("portfolio_construction"),
                "execution_authorization": policy.get("execution_authorization"),
            },
        },
        "candidate_scores": candidates,
        "vetoes": _extract_vetoes(trade_validation, portfolio_plan),
        "trade_validation": dict(trade_validation or {}),
        "portfolio_plan": dict(portfolio_plan or {}),
        "position_management": dict(position_management or {}),
        "final_allocation": {
            "selected_symbols": [row.get("symbol") for row in selected],
            "orders_sent": int(execution.get("orders_sent", 0)) if isinstance(execution, Mapping) else 0,
        },
        "submitted_order_ids": order_ids,
        "execution": dict(execution or {}),
    }


def _extract_vetoes(trade_validation: Mapping[str, Any] | None, portfolio_plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    vetoes: list[dict[str, Any]] = []
    if isinstance(trade_validation, Mapping):
        outputs = trade_validation.get("agent_outputs", {}) if isinstance(trade_validation.get("agent_outputs"), Mapping) else {}
        committee = outputs.get("Strategy Committee Agent", {}) if isinstance(outputs.get("Strategy Committee Agent"), Mapping) else {}
        for vote in committee.get("votes", []) if isinstance(committee.get("votes"), list) else []:
            if isinstance(vote, Mapping) and vote.get("vote") == "reject":
                vetoes.append({"symbol": vote.get("symbol"), "source": "strategy_committee", "reason": vote.get("reason")})
    if isinstance(portfolio_plan, Mapping):
        for row in portfolio_plan.get("rejected_proposals", []) if isinstance(portfolio_plan.get("rejected_proposals"), list) else []:
            if isinstance(row, Mapping):
                vetoes.append({"symbol": row.get("symbol"), "source": "portfolio_construction", "reason": row.get("reason")})
    return vetoes


def write_decision_snapshot(snapshot: Mapping[str, Any], directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    ts = str(snapshot.get("timestamp") or dt.datetime.now(dt.timezone.utc).isoformat())
    safe = ts.replace(":", "").replace("+", "Z").replace(".", "_")
    path = directory / f"decision_{safe}.json"
    path.write_text(json.dumps(dict(snapshot), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
