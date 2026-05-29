import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import daily_cycle


def test_build_summary_uses_account_positions_orders_and_buying_power():
    account = {
        "credentials_present": True,
        "trading_blocked": False,
        "portfolio_value": 1200.0,
        "cash": 900.0,
        "buying_power": 900.0,
        "open_positions_count": 2,
        "open_orders_count": 1,
    }

    market_scan = {"market_scanner": {"opportunities_found": 3, "candidates": [{"symbol": "SPY"}]}}

    summary = daily_cycle.build_summary(account, market_scan=market_scan)

    cycle = summary["cycle_summary"]
    assert cycle["portfolio_value"] == 100.0
    assert cycle["available_cash"] == 100.0
    assert cycle["buying_power"] == 900.0
    assert cycle["open_positions"] == 2
    assert cycle["pending_orders"] == 1
    assert cycle["kill_switch_status"] == "clear"
    assert cycle["opportunities_found"] == 3
    assert summary["market_scan"] == market_scan
    assert summary["orders_sent"] == 0


def test_main_skips_market_scan_when_account_check_blocks(monkeypatch):
    blocked_account = {
        "credentials_present": True,
        "account_verified": True,
        "trading_blocked": True,
        "reason": "cash_buying_power_margin_incoherence",
        "cash": 10000.0,
        "buying_power": 20000.0,
        "portfolio_value": 10000.0,
        "margin_multiplier": 2.0,
        "open_positions_count": 0,
        "open_orders_count": 0,
    }
    captured = {}

    monkeypatch.setattr(daily_cycle, "run_check", lambda: (6, blocked_account))

    def fail_scan():
        raise AssertionError("market scan must not run when account check blocks")

    monkeypatch.setattr(daily_cycle, "run_live_scan", fail_scan)
    monkeypatch.setattr(daily_cycle, "write_cycle_outputs", lambda summary, timestamp: captured.setdefault("summary", summary))

    assert daily_cycle.main() == 6
    cycle = captured["summary"]["cycle_summary"]
    assert cycle["kill_switch_status"] == "triggered"
    assert cycle["validation_gate_status"] == "blocked"
    assert cycle["next_action"] == "Corriger incohérence compte avant tout scan/exécution"
    assert "market_scan" not in captured["summary"]
