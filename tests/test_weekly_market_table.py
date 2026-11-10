"""Weekly market table: all configured indicators, aligned values and linked sources."""
import json
import unittest
from datetime import date

from src.config import CATEGORIES, WEEKLY_MARKET_INDICATORS
from src.weekly.editor import WeeklyHeadlines
from src.weekly.market_data import MarketPoint, MarketSnapshot
from src.weekly.renderer import _market_block, render_weekly_briefing
from src.weekly.selector import WeeklySelection


class WeeklyMarketTableTests(unittest.TestCase):
    def snapshot(self, key="kospi", **extra):
        values = dict(key=key, label=key, provider="test", change_unit="percent",
                      points=(MarketPoint(date(2026, 11, 6), 1234.5),), change=1.5,
                      comparison=MarketPoint(date(2026, 10, 30), 1216.25),
                      source_url="https://example.org/indicator/" + key, sparkline="▁▃█")
        values.update(extra)
        return MarketSnapshot(**values)

    def test_all_seven_indicators_and_alignment(self):
        markets = tuple(self.snapshot(item["key"]) for item in WEEKLY_MARKET_INDICATORS)
        block = _market_block(markets, date(2026, 11, 8))
        self.assertEqual(block["type"], "table")
        self.assertEqual(len(block["rows"]), 8)
        self.assertTrue(all(len(row) == 4 for row in block["rows"]))
        self.assertEqual([c["align"] for c in block["column_settings"]], ["left", "right", "right", "left"])
        for snapshot, row in zip(markets, block["rows"][1:]):
            self.assertEqual(row[0]["elements"][0]["elements"][0]["url"], snapshot.source_url)
            self.assertEqual(row[1]["elements"][0]["elements"][0]["url"], snapshot.source_url)
            self.assertIn("10.30→11.06", row[3]["text"])

    def test_missing_is_not_zero_and_rate_change_stays_bp(self):
        missing = self.snapshot(points=(), comparison=None, change=None, error_code="missing_credentials")
        rate = self.snapshot("us_10y", change_unit="basis_points", change=8.0)
        block = _market_block((missing, rate), date(2026, 11, 8))
        self.assertEqual(block["rows"][1][1]["elements"][0]["elements"][0]["text"], "인증키 미설정")
        self.assertEqual(block["rows"][1][2]["text"], "—")
        self.assertEqual(block["rows"][2][2]["text"], "▲8bp")

    def test_one_table_no_graph_and_plain_text_preserved(self):
        snapshot = self.snapshot()
        selection = WeeklySelection({category: () for category in CATEGORIES}, (), 0)
        message = render_weekly_briefing(date(2026, 11, 2), date(2026, 11, 8),
                                        WeeklyHeadlines((), None, True), selection, (snapshot,))
        self.assertEqual(sum(block["type"] == "table" for block in message.blocks), 1)
        self.assertFalse(any(block["type"] == "image" for block in message.blocks))
        self.assertNotIn(snapshot.sparkline, json.dumps(message.blocks, ensure_ascii=False))
        self.assertIn(snapshot.source_url, message.plain_text)
        self.assertIn("10.30→11.06", message.plain_text)

    def test_empty_no_invalid_header_only_table(self):
        block = _market_block((), date(2026, 11, 8))
        self.assertEqual(block["type"], "section")
        self.assertIn("없음", block["text"]["text"])


if __name__ == "__main__":
    unittest.main()
