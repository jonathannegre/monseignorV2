"""Tests for order_executor module."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from order_executor import preflight_checks, execute_proposals, load_policy


def _base_policy():
    return {
        "allocated_capital_usd": 100,
        "trading_enabled": True,
        "no_orders_before_full_pipeline": False,
        "risk_mode": {"max_risk_per_trade_pct": 1.0, "max_open_positions": 2},
        "hard_rules": {
            "allow_margin": False,
            "allow_short_selling": False,
            "allow_market_orders": False,
            "minimum_cash_buffer_usd": 5,
            "prefer_bracket_orders": True,
        },
        "execution_authorization": {
            "alpaca_paper_orders_after_full_pipeline": True,
            "authorized_by_user": True,
        },
    }


def _base_proposal():
    return {
        "symbol": "SPY",
        "side": "buy",
        "order_intent": {
            "order_type": "limit",
            "limit_price": 730.0,
            "qty": 0.13,
            "take_profit_price": 736.0,
            "stop_loss_price": 727.0,
        },
    }


def test_preflight_passes_with_valid_inputs(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    policy = _base_policy()
    proposal = _base_proposal()
    reasons = preflight_checks(policy, proposal)
    assert reasons == [], f"Expected no reasons, got: {reasons}"


def test_preflight_blocks_non_paper(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    policy = _base_policy()
    proposal = _base_proposal()
    reasons = preflight_checks(policy, proposal)
    assert any("not paper" in r for r in reasons)


def test_preflight_blocks_no_authorization(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    policy = _base_policy()
    policy["execution_authorization"]["authorized_by_user"] = False
    proposal = _base_proposal()
    reasons = preflight_checks(policy, proposal)
    assert any("no user authorization" in r for r in reasons)


def test_preflight_blocks_missing_stop_loss(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    policy = _base_policy()
    proposal = _base_proposal()
    proposal["order_intent"]["stop_loss_price"] = 0
    reasons = preflight_checks(policy, proposal)
    assert any("stop_loss" in r for r in reasons)


def test_preflight_blocks_market_order(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    policy = _base_policy()
    proposal = _base_proposal()
    proposal["order_intent"]["order_type"] = "market"
    reasons = preflight_checks(policy, proposal)
    assert any("market" in r for r in reasons)


def test_preflight_blocks_zero_qty(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    policy = _base_policy()
    proposal = _base_proposal()
    proposal["order_intent"]["qty"] = 0
    reasons = preflight_checks(policy, proposal)
    assert any("qty" in r for r in reasons)
