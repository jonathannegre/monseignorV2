#!/usr/bin/env python3
"""Picsou Alpaca Paper bracket/limit order executor.

SAFETY INVARIANTS:
- Only submits to paper-api.alpaca.markets (hard check).
- Only limit or bracket orders (no market orders).
- Only BUY side (long only).
- All critical veto gates must pass before execution.
- Journals BEFORE submitting the order.
- Never uses margin/buying_power; only real cash minus buffer.
- Refuses if stop_loss or take_profit are missing.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

BASE = pathlib.Path(__file__).resolve().parents[1]
POLICY_PATH = BASE / "config" / "policy.json"
JOURNAL = BASE / "journal" / "events.jsonl"


def _headers() -> Dict[str, str]:
    key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or ""
    secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or ""
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _trading_base() -> str:
    return (os.getenv("APCA_API_BASE_URL") or "https://paper-api.alpaca.markets").rstrip("/")


def _post_json(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**_headers(), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_policy() -> Dict[str, Any]:
    return json.loads(POLICY_PATH.read_text()) if POLICY_PATH.exists() else {}


def _journal_entry(entry: Dict[str, Any]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ExecutionRefused(Exception):
    """Raised when a safety gate prevents order submission."""


def preflight_checks(policy: Mapping[str, Any], proposal: Mapping[str, Any]) -> List[str]:
    """Return list of blocking reasons. Empty list means go."""
    reasons: List[str] = []
    base = _trading_base()

    # Must be paper
    if "paper-api.alpaca.markets" not in base:
        reasons.append("FATAL: not paper endpoint")

    # Policy must authorize
    auth = policy.get("execution_authorization", {})
    if not auth.get("alpaca_paper_orders_after_full_pipeline"):
        reasons.append("execution_authorization not granted in policy")
    if not auth.get("authorized_by_user"):
        reasons.append("no user authorization")

    # Hard rules
    hard = policy.get("hard_rules", {})
    if hard.get("allow_margin"):
        reasons.append("margin is enabled — refusing")
    if hard.get("allow_short_selling"):
        reasons.append("short selling enabled — refusing")

    # Proposal checks
    order_intent = proposal.get("order_intent", {})
    if not order_intent:
        reasons.append("no order_intent in proposal")
    if order_intent.get("order_type") == "market" or (not hard.get("allow_market_orders", False) and order_intent.get("order_type") == "market"):
        reasons.append("market orders forbidden")
    if float(order_intent.get("qty", 0)) <= 0:
        reasons.append("qty is zero or negative")
    if float(order_intent.get("limit_price", 0)) <= 0:
        reasons.append("limit_price missing or zero")
    if float(order_intent.get("stop_loss_price", 0)) <= 0:
        reasons.append("stop_loss_price missing")
    if float(order_intent.get("take_profit_price", 0)) <= 0:
        reasons.append("take_profit_price missing")

    return reasons


def verify_cash_available(policy: Mapping[str, Any], proposal: Mapping[str, Any]) -> Dict[str, Any]:
    """Check live account cash vs proposed order. Returns status dict."""
    base = _trading_base()
    account = _get_json(base + "/v2/account")
    broker_cash = float(account.get("cash", 0))
    allocation_cap = float(policy.get("allocated_capital_usd", 0))
    effective_cash = min(broker_cash, allocation_cap) if allocation_cap > 0 else broker_cash
    buffer = float(policy.get("hard_rules", {}).get("minimum_cash_buffer_usd", 5))
    usable = max(effective_cash - buffer, 0)

    order_intent = proposal.get("order_intent", {})
    notional = float(order_intent.get("qty", 0)) * float(order_intent.get("limit_price", 0))

    # Check open orders reserved cash
    open_orders = _get_json(base + "/v2/orders?status=open")
    reserved = 0.0
    if isinstance(open_orders, list):
        for o in open_orders:
            if o.get("side") == "buy":
                oqty = float(o.get("qty", 0) or o.get("notional", 0))
                oprice = float(o.get("limit_price", 0) or o.get("filled_avg_price", 0))
                reserved += oqty * oprice if oprice > 0 else float(o.get("notional", 0))

    available_after_reserved = max(usable - reserved, 0)

    return {
        "broker_cash": broker_cash,
        "effective_cash": effective_cash,
        "buffer": buffer,
        "usable": usable,
        "reserved_by_open_orders": round(reserved, 2),
        "available_after_reserved": round(available_after_reserved, 2),
        "proposed_notional": round(notional, 2),
        "sufficient": available_after_reserved >= notional,
        "open_positions": int(account.get("position_qty", 0) if "position_qty" in account else len(_get_json(base + "/v2/positions") or [])),
    }


def _is_whole_share(qty: float) -> bool:
    """Check if qty is effectively a whole number."""
    return abs(qty - round(qty)) < 0.0001


def submit_order(proposal: Mapping[str, Any], policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Submit order to Alpaca Paper. Uses bracket for whole shares, simple limit for fractional.

    Alpaca constraint: fractional orders cannot use bracket order_class.
    Strategy:
    - If qty >= 1 and is whole: use bracket order (limit + TP + SL in one).
    - If qty < 1 or fractional: use simple limit order. Stop/TP managed by Position Manager.
    """
    base = _trading_base()
    order_intent = proposal.get("order_intent", {})
    symbol = proposal["symbol"]
    qty = float(order_intent["qty"])
    limit_price = float(order_intent["limit_price"])
    tp = float(order_intent["take_profit_price"])
    sl = float(order_intent["stop_loss_price"])

    use_bracket = _is_whole_share(qty) and qty >= 1.0

    if use_bracket:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "qty": str(int(round(qty))),
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(limit_price, 2)),
            "order_class": "bracket",
            "take_profit": {"limit_price": str(round(tp, 2))},
            "stop_loss": {"stop_price": str(round(sl, 2))},
        }
    else:
        # Fractional: simple limit order, Position Manager handles exits
        body = {
            "symbol": symbol,
            "qty": str(round(qty, 4)),
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(limit_price, 2)),
        }

    response = _post_json(base + "/v2/orders", body)
    response["_picsou_order_mode"] = "bracket" if use_bracket else "fractional_simple"
    response["_picsou_planned_tp"] = tp
    response["_picsou_planned_sl"] = sl
    return response


def execute_proposals(
    proposals: List[Mapping[str, Any]],
    policy: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute approved proposals through full safety chain.

    Returns a summary with executed/rejected counts and details.
    """
    policy = policy or load_policy()
    results: List[Dict[str, Any]] = []
    executed = 0
    rejected = 0
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    max_positions = int(policy.get("risk_mode", {}).get("max_open_positions", 2))

    for proposal in proposals:
        symbol = proposal.get("symbol", "?")
        entry: Dict[str, Any] = {"symbol": symbol, "timestamp": timestamp}

        # Gate 1: preflight
        blocking = preflight_checks(policy, proposal)
        if blocking:
            entry["status"] = "rejected_preflight"
            entry["reasons"] = blocking
            results.append(entry)
            rejected += 1
            _journal_entry({"agent": "Execution Trader Agent", "event_type": "order_rejected", "symbol": symbol, "timestamp": timestamp, "summary": f"Preflight failed: {blocking}", "data": entry})
            continue

        # Gate 2: live cash verification
        try:
            cash_check = verify_cash_available(policy, proposal)
        except Exception as e:
            entry["status"] = "rejected_cash_check_error"
            entry["reason"] = str(e)
            results.append(entry)
            rejected += 1
            _journal_entry({"agent": "Execution Trader Agent", "event_type": "order_rejected", "symbol": symbol, "timestamp": timestamp, "summary": f"Cash check error: {e}", "data": entry})
            continue

        if not cash_check["sufficient"]:
            entry["status"] = "rejected_insufficient_cash"
            entry["cash_check"] = cash_check
            results.append(entry)
            rejected += 1
            _journal_entry({"agent": "Execution Trader Agent", "event_type": "order_rejected", "symbol": symbol, "timestamp": timestamp, "summary": f"Insufficient cash: need {cash_check['proposed_notional']} have {cash_check['available_after_reserved']}", "data": entry})
            continue

        # Gate 3: max positions
        if cash_check["open_positions"] >= max_positions:
            entry["status"] = "rejected_max_positions"
            entry["open_positions"] = cash_check["open_positions"]
            entry["max_positions"] = max_positions
            results.append(entry)
            rejected += 1
            _journal_entry({"agent": "Execution Trader Agent", "event_type": "order_rejected", "symbol": symbol, "timestamp": timestamp, "summary": f"Max positions {max_positions} reached", "data": entry})
            continue

        # Journal BEFORE execution
        _journal_entry({
            "agent": "Execution Trader Agent",
            "event_type": "order_pre_submit",
            "symbol": symbol,
            "timestamp": timestamp,
            "summary": f"{'DRY RUN ' if dry_run else ''}About to submit bracket order for {symbol}",
            "data": {"proposal": dict(proposal), "cash_check": cash_check, "dry_run": dry_run},
        })

        if dry_run:
            entry["status"] = "dry_run_approved"
            entry["cash_check"] = cash_check
            entry["would_submit"] = proposal.get("order_intent")
            results.append(entry)
            executed += 1
            continue

        # Gate 4: actual submission
        try:
            order_response = submit_order(proposal, policy)
            entry["status"] = "submitted"
            entry["order_id"] = order_response.get("id")
            entry["order_status"] = order_response.get("status")
            entry["order_response"] = {
                "id": order_response.get("id"),
                "status": order_response.get("status"),
                "symbol": order_response.get("symbol"),
                "qty": order_response.get("qty"),
                "limit_price": order_response.get("limit_price"),
                "order_class": order_response.get("order_class"),
                "created_at": order_response.get("created_at"),
            }
            entry["cash_check"] = cash_check
            results.append(entry)
            executed += 1
            _journal_entry({
                "agent": "Execution Trader Agent",
                "event_type": "order_submitted",
                "symbol": symbol,
                "timestamp": timestamp,
                "summary": f"Bracket order submitted: {order_response.get('id')} status={order_response.get('status')}",
                "data": entry,
            })
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass
            entry["status"] = "submission_error"
            entry["http_code"] = e.code
            entry["error"] = error_body
            results.append(entry)
            rejected += 1
            _journal_entry({"agent": "Execution Trader Agent", "event_type": "order_error", "symbol": symbol, "timestamp": timestamp, "summary": f"HTTP {e.code}: {error_body[:200]}", "data": entry})
        except Exception as e:
            entry["status"] = "submission_exception"
            entry["error"] = str(e)
            results.append(entry)
            rejected += 1
            _journal_entry({"agent": "Execution Trader Agent", "event_type": "order_error", "symbol": symbol, "timestamp": timestamp, "summary": str(e), "data": entry})

    return {
        "agent": "Execution Trader Agent",
        "timestamp": timestamp,
        "dry_run": dry_run,
        "proposals_received": len(proposals),
        "executed": executed,
        "rejected": rejected,
        "orders_sent": executed if not dry_run else 0,
        "results": results,
    }


if __name__ == "__main__":
    import sys
    # Standalone test: dry-run with a fake proposal
    policy = load_policy()
    test_proposal = {
        "symbol": "SPY",
        "side": "buy",
        "status": "proposed_for_manual_review",
        "execution_allowed": False,
        "confidence": 8.0,
        "risk_reward": 2.2,
        "order_intent": {
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": 730.0,
            "qty": 0.13,
            "bracket_order_preferred": True,
            "take_profit_price": 736.0,
            "stop_loss_price": 727.0,
        },
    }
    result = execute_proposals([test_proposal], policy, dry_run=True)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["rejected"] == 0 else 1)
