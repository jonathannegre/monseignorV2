import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from scripts.finnhub_catalyst_feed import (
    build_news_cache,
    normalize_company_news_item,
    normalize_earnings_calendar_item,
    write_jsonl_cache,
)
from scripts.catalyst_agent import CatalystAgent


class FinnhubCatalystFeedTests(unittest.TestCase):
    def test_normalizes_company_news_into_scored_catalyst_event(self):
        row = normalize_company_news_item(
            "AAPL",
            {
                "datetime": 1780495200,
                "headline": "Apple beats earnings and raises guidance after AI demand surge",
                "source": "Finnhub",
                "summary": "Revenue beat consensus and management raised FY guidance.",
                "url": "https://example.test/aapl",
                "related": "AAPL,MSFT",
            },
        )

        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["source"], "finnhub")
        self.assertEqual(row["source_tier"], 1)
        self.assertEqual(row["event_type"], "earnings_beat_guidance_raise")
        self.assertGreaterEqual(row["sentiment"], 0.7)
        self.assertGreaterEqual(row["materiality"], 0.8)
        self.assertTrue(row["published_at"].endswith("Z"))

    def test_normalizes_earnings_calendar_surprise_and_feeds_catalyst_agent(self):
        rows = build_news_cache(
            symbols=["NVDA"],
            company_news_by_symbol={"NVDA": []},
            earnings_rows=[
                {
                    "symbol": "NVDA",
                    "date": "2026-06-03",
                    "hour": "amc",
                    "epsActual": 1.25,
                    "epsEstimate": 1.0,
                    "revenueActual": 10_000_000_000,
                    "revenueEstimate": 8_500_000_000,
                }
            ],
            now=dt.datetime(2026, 6, 3, 12, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(rows[0]["event_type"], "earnings_beat")
        self.assertIn("EPS surprise", rows[0]["summary"])
        agent = CatalystAgent(rows, now=dt.datetime(2026, 6, 3, 13, tzinfo=dt.timezone.utc))
        score = agent.score_symbol("NVDA")
        self.assertEqual(score["catalyst_status"], "positive_catalyst")
        self.assertGreaterEqual(score["score"], 7.0)

    def test_write_jsonl_cache_sorts_deduplicates_and_keeps_valid_jsonl(self):
        rows = [
            {"symbol": "AAPL", "published_at": "2026-06-03T12:00:00Z", "headline": "same", "url": "https://x"},
            {"symbol": "AAPL", "published_at": "2026-06-03T12:00:00Z", "headline": "same", "url": "https://x"},
            {"symbol": "MSFT", "published_at": "2026-06-03T13:00:00Z", "headline": "new", "url": "https://y"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "news_cache.jsonl"
            written = write_jsonl_cache(rows, out)
            loaded = [json.loads(line) for line in out.read_text().splitlines()]

        self.assertEqual(written, 2)
        self.assertEqual([r["symbol"] for r in loaded], ["MSFT", "AAPL"])


if __name__ == "__main__":
    unittest.main()
