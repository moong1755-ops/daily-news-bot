"""Compatibility checks for reused title analysis and parallel market reads."""

import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import requests

from src import bot
from src.weekly import market_data
from src.weekly.renderer import _market_block


class TitleAnalysisReuseTests(unittest.TestCase):
    def setUp(self):
        for function in (bot._char_ngrams, bot._hangul_ratio, bot._word_tokens):
            function.cache_clear()
            self.addCleanup(function.cache_clear)

    def test_duplicate_decisions_keep_existing_boundaries(self):
        cases = [
            ("", "", False),
            ("AI", "AI", False),
            ("기후", "기후", True),
            ("기후", "돌봄", False),
            ("Fed raises interest rates", "FED raises interest rates!", True),
            ("alpha beta gamma delta", "alpha beta epsilon zeta", True),
            ("alpha beta gamma delta", "alpha epsilon zeta theta", False),
            ("OpenAI raises Series B funding", "OpenAI raises Series B funding", True),
        ]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(bot.is_same_news_issue(left, right), expected)
                self.assertEqual(bot.is_same_news_issue(right, left), expected)

    def test_repeated_comparisons_reuse_immutable_analysis(self):
        pair = ("OpenAI raises Series B funding", "Microsoft acquires gaming company")
        first = bot.is_same_news_issue(*pair)
        for _ in range(10):
            self.assertEqual(bot.is_same_news_issue(*pair), first)
        for function in (bot._hangul_ratio, bot._word_tokens):
            self.assertEqual(function.cache_info().misses, 2)
            self.assertEqual(function.cache_info().hits, 20)
        self.assertIsInstance(bot._word_tokens(pair[0]), frozenset)
        self.assertIsInstance(bot._char_ngrams("기후테크 투자"), frozenset)

    def test_cache_size_is_bounded(self):
        for index in range(4100):
            bot._word_tokens(f"Unique headline {index}")
        self.assertEqual(bot._word_tokens.cache_info().currsize, 4096)
        self.assertEqual(bot._word_tokens("Unique headline 0"),
                         frozenset(("unique", "headline")))


class ParallelMarketCollectionTests(unittest.TestCase):
    START = date(2026, 8, 24)
    END = date(2026, 8, 30)
    INDICATORS = tuple(
        indicator for indicator in market_data.WEEKLY_MARKET_INDICATORS
        if indicator.get("provider") == "fred"
    )[:3]

    @staticmethod
    def response(url, **kwargs):
        series = kwargs["params"]["id"]
        return SimpleNamespace(
            text=f"observation_date,{series}\n2026-08-21,100\n2026-08-28,102\n",
            raise_for_status=lambda: None,
        )

    def test_parallel_values_dates_links_and_output_equal_sequential(self):
        sequential_client = SimpleNamespace(get=self.response)
        expected = tuple(
            market_data._collect_one(item, self.START, self.END, sequential_client)
            for item in self.INDICATORS
        )
        barrier = threading.Barrier(3)
        completed = []
        finish_events = [threading.Event() for _ in self.INDICATORS]
        series_order = [item["series_id"] for item in self.INDICATORS]

        def get(url, **kwargs):
            index = series_order.index(kwargs["params"]["id"])
            barrier.wait(timeout=3)
            if index < 2 and not finish_events[index + 1].wait(timeout=3):
                raise requests.Timeout("test reverse completion timed out")
            completed.append(index)
            finish_events[index].set()
            return self.response(url, **kwargs)

        with patch.object(market_data, "WEEKLY_MARKET_INDICATORS", self.INDICATORS), \
             patch.object(market_data, "WEEKLY_MARKET_FETCH_WORKERS", 3), \
             patch.object(requests, "get", side_effect=get):
            actual = market_data.collect_market_snapshots(self.START, self.END)

        self.assertEqual(completed, [2, 1, 0])
        self.assertEqual(actual, expected)
        self.assertEqual(_market_block(actual, self.END), _market_block(expected, self.END))

    def test_one_failed_provider_does_not_drop_other_indicators(self):
        def get(url, **kwargs):
            if kwargs["params"]["id"] == self.INDICATORS[1]["series_id"]:
                raise requests.Timeout("unavailable")
            return self.response(url, **kwargs)

        with patch.object(market_data, "WEEKLY_MARKET_INDICATORS", self.INDICATORS), \
             patch.object(requests, "get", side_effect=get):
            snapshots = market_data.collect_market_snapshots(self.START, self.END)
        self.assertEqual([item.key for item in snapshots], [item["key"] for item in self.INDICATORS])
        self.assertEqual([item.available for item in snapshots], [True, False, True])

    def test_custom_clients_keep_sequential_access(self):
        thread_ids = []

        def get(url, **kwargs):
            thread_ids.append(threading.get_ident())
            return self.response(url, **kwargs)

        client = SimpleNamespace(get=get)
        with patch.object(market_data, "WEEKLY_MARKET_INDICATORS", self.INDICATORS):
            snapshots = market_data.collect_market_snapshots(self.START, self.END, session=client)
        self.assertTrue(all(item.available for item in snapshots))
        self.assertEqual(thread_ids, [threading.get_ident()] * len(self.INDICATORS))

    def test_empty_configuration_makes_no_request(self):
        with patch.object(market_data, "WEEKLY_MARKET_INDICATORS", ()), \
             patch.object(requests, "get") as get:
            self.assertEqual(market_data.collect_market_snapshots(self.START, self.END), ())
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
