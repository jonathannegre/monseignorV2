import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts.backtester import backtest_setup, fetch_yahoo_daily_bars, recommend_setup_rotation, run_backtest, walk_forward_backtest
from scripts.catalyst_agent import CatalystAgent, NewsItem, score_catalysts_for_symbols
from scripts.decision_replay import build_decision_snapshot, write_decision_snapshot
from scripts.intraday_execution import IntradayExecutionConfig, confirm_intraday_entry, reprice_stale_order
from scripts.portfolio_constructor import construct_portfolio
from scripts.position_manager import PositionPlan, build_position_plan, evaluate_position_action
from scripts.trade_validation_pipeline import compose_trade_validation_pipeline


class CatalystAgentTests(unittest.TestCase):
    def test_positive_recent_news_boosts_symbol_and_negative_news_vetoes(self):
        now = dt.datetime(2026, 6, 3, 12, tzinfo=dt.timezone.utc)
        items = [
            NewsItem(symbol="AAPL", headline="Apple beats earnings and raises guidance", source="Reuters", published_at=now - dt.timedelta(hours=2)),
            NewsItem(symbol="AAPL", headline="Apple announces major AI partnership", source="Bloomberg", published_at=now - dt.timedelta(hours=3)),
            NewsItem(symbol="TSLA", headline="Tesla faces fraud investigation after guidance cut", source="Reuters", published_at=now - dt.timedelta(hours=1)),
        ]
        agent = CatalystAgent(news_items=items, now=now)

        scored = score_catalysts_for_symbols(["AAPL", "TSLA", "SPY"], agent)

        self.assertGreaterEqual(scored["AAPL"]["score"], 8.0)
        self.assertTrue(scored["AAPL"]["trade_allowed"])
        self.assertEqual(scored["TSLA"]["catalyst_status"], "negative_news_veto")
        self.assertFalse(scored["TSLA"]["trade_allowed"])
        self.assertEqual(scored["SPY"]["catalyst_status"], "macro_context_only")


class BacktesterTests(unittest.TestCase):
    def _bars(self, count=80):
        bars = []
        for i in range(count):
            close = 100 + i * 0.5
            bars.append({"t": f"2026-01-{(i % 28) + 1:02d}", "o": close - 0.2, "h": close + 1.5, "l": close - 1.0, "c": close, "v": 2_000_000 + i * 1000})
        return bars

    def test_backtest_and_walk_forward_emit_expectancy_and_policy_rotation(self):
        bars_by_symbol = {"SPY": self._bars(90), "MSFT": self._bars(90)}

        result = backtest_setup("etf_trend_following", bars_by_symbol, min_trades=1)
        walk = walk_forward_backtest(bars_by_symbol, setups=["etf_trend_following"], window=40, step=20)
        rotation = recommend_setup_rotation([result], min_samples=1)
        full = run_backtest(bars_by_symbol, setups=["etf_trend_following"], min_trades=1)

        self.assertEqual(result["setup"], "etf_trend_following")
        self.assertGreater(result["trades"], 0)
        self.assertIn("avg_r", result)
        self.assertGreaterEqual(walk["folds"], 1)
        self.assertIn("boosted_setups", rotation)
        self.assertEqual(full["symbols_loaded"]["SPY"], 90)
        self.assertIn("recommendation", full)

    def test_yahoo_fetcher_normalizes_adjusted_bars(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1735689600, 1735776000],
                        "indicators": {
                            "quote": [{"open": [100, 102], "high": [104, 106], "low": [99, 101], "close": [103, 105], "volume": [1000, 1100]}],
                            "adjclose": [{"adjclose": [51.5, 105]}],
                        },
                    }
                ]
            }
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            bars = fetch_yahoo_daily_bars("AAPL", "2025-01-01", "2025-01-02")

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["c"], 51.5)
        self.assertEqual(bars[0]["o"], 50.0)
        self.assertEqual(bars[1]["v"], 1100)

class PositionAndExecutionTests(unittest.TestCase):
    def test_position_manager_promotes_stop_and_partial_profit(self):
        position = {"symbol": "AAPL", "qty": "10", "avg_entry_price": "100", "current_price": "116", "market_value": "1160"}
        quote = {"price": 116}
        plan = build_position_plan(position, quote, {"entry": 100, "stop_loss": 94, "take_profit": 118, "atr14": 3})

        action = evaluate_position_action(plan)

        self.assertIsInstance(plan, PositionPlan)
        self.assertGreaterEqual(plan.stop_price, 100.0)
        self.assertIn("take_partial_profit", action["actions"])
        self.assertIn("ensure_stop_order", action["actions"])

    def test_intraday_confirmation_rejects_early_wide_spread_and_reprices_stale_order(self):
        bars = [{"c": 100.0, "v": 1000}, {"c": 101.2, "v": 1800}, {"c": 101.8, "v": 2200}]
        wide = confirm_intraday_entry("AAPL", bars, {"bp": 100.0, "ap": 101.2}, IntradayExecutionConfig(max_spread_bps=20, avoid_first_minutes=True), minutes_after_open=3)
        ok = confirm_intraday_entry("AAPL", bars, {"bp": 101.7, "ap": 101.75}, IntradayExecutionConfig(max_spread_bps=20), minutes_after_open=35)
        repriced = reprice_stale_order({"limit_price": 100, "created_at": "2026-06-03T13:30:00Z"}, {"bp": 101.0, "ap": 101.2}, now=dt.datetime(2026, 6, 3, 13, 50, tzinfo=dt.timezone.utc), max_age_minutes=10, max_chase_bps=150)

        self.assertFalse(wide["confirmed"])
        self.assertTrue(ok["confirmed"])
        self.assertEqual(repriced["action"], "replace")
        self.assertGreater(repriced["new_limit_price"], 100)


class PortfolioAndReplayTests(unittest.TestCase):
    def test_portfolio_constructor_caps_sector_and_prioritizes_conviction(self):
        proposals = [
            {"symbol": "NVDA", "confidence": 9.5, "risk_reward": 2.0, "agent_rationale": {"cash": {"suggested_notional_usd": 800}}, "sector": "Technology", "order_intent": {"qty": 4, "limit_price": 200}},
            {"symbol": "AMD", "confidence": 9.0, "risk_reward": 2.0, "agent_rationale": {"cash": {"suggested_notional_usd": 700}}, "sector": "Technology", "order_intent": {"qty": 7, "limit_price": 100}},
            {"symbol": "XLF", "confidence": 8.0, "risk_reward": 1.8, "agent_rationale": {"cash": {"suggested_notional_usd": 500}}, "sector": "Financials", "order_intent": {"qty": 10, "limit_price": 50}},
        ]
        result = construct_portfolio(proposals, account={"portfolio_value": 2000, "cash": 2000}, policy={"portfolio_construction": {"max_sector_exposure_pct": 40, "max_new_orders": 3}})

        selected = [p["symbol"] for p in result["selected_proposals"]]
        rejected = [r["symbol"] for r in result["rejected_proposals"]]
        self.assertIn("NVDA", selected)
        self.assertIn("AMD", rejected)
        self.assertIn("sector_cap", result["rejected_proposals"][0]["reason"])

    def test_decision_snapshot_contains_replayable_inputs_outputs_and_file_write(self):
        snapshot = build_decision_snapshot(
            timestamp="2026-06-03T12:00:00Z",
            account={"cash": 1000},
            policy={"project": "monseignor-alpaca-paper"},
            market_scan={"market_scanner": {"candidates": [{"symbol": "SPY"}]}},
            trade_validation={"proposals": [{"symbol": "SPY"}]},
            portfolio_plan={"selected_proposals": [{"symbol": "SPY"}]},
            execution={"orders_sent": 1, "results": [{"order_id": "abc"}]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_decision_snapshot(snapshot, pathlib.Path(tmp))
            loaded = json.loads(path.read_text())

        self.assertEqual(snapshot["schema"], "monseignor_decision_v1")
        self.assertEqual(loaded["final_allocation"]["orders_sent"], 1)
        self.assertEqual(loaded["submitted_order_ids"], ["abc"])


class PipelineIntegrationTests(unittest.TestCase):
    def test_trade_pipeline_applies_negative_catalyst_veto_and_portfolio_construction(self):
        policy = {
            "trading_enabled": True,
            "no_orders_before_full_pipeline": False,
            "allow_etf": True,
            "risk_mode": {"min_confidence": 4.5, "min_risk_reward": 1.0, "max_risk_per_trade_pct": 2.0, "max_total_exposure_pct": 80.0},
            "hard_rules": {"minimum_cash_buffer_usd": 5, "prefer_bracket_orders": True},
            "portfolio_construction": {"max_sector_exposure_pct": 50, "max_new_orders": 2},
        }
        account = {"account_verified": True, "trading_blocked": False, "cash": 1000, "portfolio_value": 1000}
        watchlist = [
            {"symbol": "BAD", "asset_class": "EQUITY", "entry": 50, "stop_loss": 47, "take_profit": 56, "risk_reward": 2.0, "technical_score": 8, "confidence": 8, "catalyst_score": 1, "catalyst_analysis": {"trade_allowed": False, "catalyst_status": "negative_news_veto", "summary": "fraud investigation"}},
            {"symbol": "GOOD", "asset_class": "EQUITY", "entry": 100, "stop_loss": 95, "take_profit": 112, "risk_reward": 2.4, "technical_score": 8, "confidence": 8, "catalyst_score": 8, "sector": "Technology"},
        ]

        result = compose_trade_validation_pipeline(policy, account, manual_validation=True, watchlist=watchlist)

        self.assertEqual([p["symbol"] for p in result["proposals"]], ["GOOD"])
        votes = result["agent_outputs"]["Strategy Committee Agent"]["votes"]
        self.assertTrue(any(v["symbol"] == "BAD" and v["vote"] == "reject" for v in votes))
        self.assertEqual(result["agent_outputs"]["Portfolio Construction Agent"]["selected_count"], 1)


if __name__ == "__main__":
    unittest.main()
