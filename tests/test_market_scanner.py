import unittest

from scripts.market_scanner import MarketScannerConfig, analyze_technicals, choose_asset_universe, scan_market_universe


class MarketScannerTests(unittest.TestCase):
    def test_prudent_scan_filters_for_liquid_low_spread_us_equities_and_etfs(self):
        assets = [
            {"symbol": "AAPL", "status": "active", "tradable": True, "class": "us_equity", "exchange": "NASDAQ", "fractionable": True},
            {"symbol": "SPY", "status": "active", "tradable": True, "class": "us_equity", "exchange": "ARCA", "fractionable": True},
            {"symbol": "WIDE", "status": "active", "tradable": True, "class": "us_equity", "exchange": "NYSE", "fractionable": True},
            {"symbol": "ILLIQ", "status": "active", "tradable": True, "class": "us_equity", "exchange": "NYSE", "fractionable": True},
            {"symbol": "BTCUSD", "status": "active", "tradable": True, "class": "crypto", "exchange": "CRYPTO", "fractionable": True},
            {"symbol": "HALT", "status": "inactive", "tradable": True, "class": "us_equity", "exchange": "NYSE", "fractionable": True},
        ]
        bars = {
            "AAPL": {"c": 200.0, "v": 50_000_000},
            "SPY": {"c": 520.0, "v": 70_000_000},
            "WIDE": {"c": 100.0, "v": 10_000_000},
            "ILLIQ": {"c": 30.0, "v": 100_000},
        }
        quotes = {
            "AAPL": {"bp": 199.99, "ap": 200.01},
            "SPY": {"bp": 519.98, "ap": 520.02},
            "WIDE": {"bp": 99.00, "ap": 101.00},
            "ILLIQ": {"bp": 29.99, "ap": 30.01},
        }

        result = scan_market_universe(assets, bars, quotes, MarketScannerConfig(max_spread_bps=5, min_dollar_volume=20_000_000, top_n=10))

        symbols = [candidate["symbol"] for candidate in result["market_scanner"]["candidates"]]
        self.assertEqual(symbols, ["SPY", "AAPL"])
        self.assertEqual(result["agent"], "Market Scanner Agent")
        self.assertEqual(result["orders_sent"], 0)
        self.assertTrue(all(candidate["fractionable"] for candidate in result["market_scanner"]["candidates"]))
        self.assertEqual(result["market_scanner"]["rejected_counts"]["spread_too_wide"], 1)
        self.assertEqual(result["market_scanner"]["rejected_counts"]["liquidity_too_low"], 1)
        self.assertEqual(result["market_scanner"]["universe"]["allowed_asset_classes"], ["us_equity"])

    def test_non_approved_etfs_are_rejected_even_when_alpaca_reports_us_equity(self):
        assets = [
            {"symbol": "TQQQ", "name": "ProShares UltraPro QQQ ETF", "status": "active", "tradable": True, "class": "us_equity", "exchange": "NASDAQ", "fractionable": True},
            {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "status": "active", "tradable": True, "class": "us_equity", "exchange": "ARCA", "fractionable": True},
        ]
        bars = {"TQQQ": {"c": 60.0, "v": 80_000_000}, "SPY": {"c": 520.0, "v": 70_000_000}}
        quotes = {"TQQQ": {"bp": 59.99, "ap": 60.01}, "SPY": {"bp": 519.98, "ap": 520.02}}

        result = scan_market_universe(assets, bars, quotes, MarketScannerConfig(max_spread_bps=10, min_dollar_volume=20_000_000, top_n=10))

        candidates = result["market_scanner"]["candidates"]
        self.assertEqual([candidate["symbol"] for candidate in candidates], ["SPY"])
        self.assertEqual(candidates[0]["asset_type"], "etf")
        self.assertEqual(result["market_scanner"]["rejected_counts"]["etf_not_approved"], 1)
        self.assertEqual([asset["symbol"] for asset in choose_asset_universe(assets, 10)], ["SPY"])

    def test_non_fractionable_asset_above_cash_is_rejected(self):
        assets = [
            {"symbol": "CASHY", "status": "active", "tradable": True, "class": "us_equity", "exchange": "NYSE", "fractionable": False},
            {"symbol": "AAPL", "status": "active", "tradable": True, "class": "us_equity", "exchange": "NASDAQ", "fractionable": True},
        ]
        bars = {"CASHY": {"c": 200.0, "v": 2_000_000}, "AAPL": {"c": 200.0, "v": 50_000_000}}
        quotes = {"CASHY": {"bp": 199.99, "ap": 200.01}, "AAPL": {"bp": 199.99, "ap": 200.01}}

        result = scan_market_universe(assets, bars, quotes, MarketScannerConfig(max_spread_bps=5, min_dollar_volume=20_000_000), account_cash=100.0)

        self.assertEqual([candidate["symbol"] for candidate in result["market_scanner"]["candidates"]], ["AAPL"])
        self.assertEqual(result["market_scanner"]["rejected_counts"]["not_fractionable_for_cash"], 1)

    def test_analyze_technicals_outputs_complete_trade_plan_from_history(self):
        bars = []
        for i in range(80):
            close = 100 + i * 0.4
            bars.append({"o": close - 0.3, "h": close + 1.0, "l": close - 1.0, "c": close, "v": 2_000_000 + i * 10_000})
        bars[-1]["c"] = bars[-2]["c"] + 1.2
        bars[-1]["h"] = bars[-1]["c"] + 1.1
        bars[-1]["l"] = bars[-1]["c"] - 0.9
        bars[-1]["v"] = 3_500_000

        analysis = analyze_technicals("SPY", "etf", bars, {"bp": bars[-1]["c"] - 0.01, "ap": bars[-1]["c"] + 0.01})

        self.assertEqual(analysis["agent"], "Technical Analyst Agent")
        self.assertIn(analysis["setup"], {"momentum_continuation", "breakout_volume", "etf_trend_following"})
        self.assertGreater(analysis["entry"], 0)
        self.assertGreater(analysis["stop_loss"], 0)
        self.assertGreater(analysis["take_profit"], analysis["entry"])
        self.assertGreaterEqual(analysis["risk_reward"], 2.0)
        for key in ("ema9", "ema21", "ema50", "rsi14", "macd", "volume_relative", "atr14", "support", "resistance"):
            self.assertIn(key, analysis)


if __name__ == "__main__":
    unittest.main()
