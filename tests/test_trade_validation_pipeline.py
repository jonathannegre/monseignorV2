import json
import pathlib
import tempfile
import unittest

from scripts.trade_validation_pipeline import (
    compose_trade_validation_pipeline,
    append_trade_validation_journal,
)


POLICY = {
    "trading_enabled": False,
    "no_orders_before_full_pipeline": True,
    "selected_risk_mode": "B",
    "risk_mode": {
        "name": "équilibré",
        "max_risk_per_trade_pct": 1.0,
        "max_total_exposure_pct": 50,
        "max_open_positions": 2,
        "min_confidence": 7.5,
        "min_risk_reward": 2.0,
    },
    "allow_etf": True,
    "hard_rules": {
        "minimum_cash_buffer_usd": 5,
        "allow_market_orders": False,
        "prefer_bracket_orders": True,
    },
}


class TradeValidationPipelineTest(unittest.TestCase):
    def test_composes_all_validation_agents_without_orders_or_auto_approval(self):
        account = {
            "account_verified": True,
            "trading_blocked": False,
            "cash": 1000.0,
            "portfolio_value": 1000.0,
        }

        result = compose_trade_validation_pipeline(POLICY, account, manual_validation=False)

        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["execution_gate"]["status"], "manual_validation_required")
        self.assertFalse(result["execution_gate"]["manual_validation_present"])
        self.assertEqual(result["trades_approved"], 0)
        self.assertEqual(result["trades_executed"], 0)
        self.assertEqual(result["proposals_count"], len(result["proposals"]))
        self.assertGreater(result["proposals_count"], 0)

        required_agents = {
            "Technical Analyst Agent",
            "Catalyst & News Agent",
            "Risk Manager Agent",
            "Cash Control Agent",
            "Strategy Committee Agent",
            "Compliance & Kill-Switch Agent",
            "CEO Agent",
        }
        self.assertTrue(required_agents.issubset(result["agent_outputs"].keys()))
        for proposal in result["proposals"]:
            self.assertEqual(proposal["status"], "approved_for_execution")
            self.assertTrue(proposal["execution_allowed"])
            self.assertEqual(proposal["order_intent"]["order_type"], "limit")
            self.assertIsNone(proposal["execution_block_reason"])

    def test_kill_switch_blocks_when_account_or_policy_blocks_trading(self):
        account = {"account_verified": False, "trading_blocked": True, "reason": "missing_credentials"}

        result = compose_trade_validation_pipeline(POLICY, account, manual_validation=False)

        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["proposals_count"], 0)
        self.assertEqual(result["execution_gate"]["status"], "blocked")
        self.assertIn("missing_credentials", result["execution_gate"]["reasons"])
        self.assertIn("policy_trading_disabled", result["execution_gate"]["reasons"])

    def test_scanner_candidate_with_technical_analysis_is_mapped_into_pipeline(self):
        account = {"account_verified": True, "trading_blocked": False, "cash": 1000.0, "portfolio_value": 1000.0}
        watchlist = [{
            "symbol": "XLK",
            "asset_type": "etf",
            "price": 250.0,
            "technical_analysis": {
                "technical_score": 8.0,
                "confidence": 8.0,
                "risk_reward": 2.2,
                "setup": "etf_trend_following",
                "entry": 250.0,
                "stop_loss": 245.0,
                "take_profit": 261.0,
                "technical_summary": "ETF trend following",
            },
            "catalyst_analysis": {"score": 6.0, "catalyst_status": "no_verified_catalyst", "summary": "No verified catalyst"},
        }]

        result = compose_trade_validation_pipeline(POLICY, account, manual_validation=False, watchlist=watchlist)

        tech = result["agent_outputs"]["Technical Analyst Agent"]["evaluations"][0]
        self.assertEqual(tech["entry"], 250.0)
        self.assertEqual(tech["setup"], "etf_trend_following")
        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["proposals_count"], 1)
        self.assertEqual(result["proposals"][0]["order_intent"]["stop_loss_price"], 245.0)

    def test_append_trade_validation_journal_writes_jsonl_entry(self):
        result = compose_trade_validation_pipeline(
            POLICY,
            {"account_verified": True, "trading_blocked": False, "cash": 1000.0, "portfolio_value": 1000.0},
            manual_validation=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = pathlib.Path(tmp) / "events.jsonl"
            append_trade_validation_journal(journal, result, timestamp="2026-05-18 20:00:00")
            line = journal.read_text(encoding="utf-8").strip()

        entry = json.loads(line)
        self.assertEqual(entry["agent"], "Memory / Journal Agent")
        self.assertEqual(entry["event_type"], "trade_validation_proposals")
        self.assertEqual(entry["data"]["orders_sent"], 0)
        self.assertEqual(entry["data"]["execution_gate"]["status"], "manual_validation_required")


if __name__ == "__main__":
    unittest.main()
