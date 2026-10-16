"""RSS 동시 수집과 비활성 웹소스 처리 회귀 테스트."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from src.fetchers import rss_feeds


class RSSCollectionTestCase(unittest.TestCase):
    def test_feed_requests_run_concurrently_and_keep_config_order(self):
        feeds = {
            "Source A": "https://example.com/a.xml",
            "Source B": "https://example.com/b.xml",
            "Source C": "https://example.com/c.xml",
        }
        barrier = threading.Barrier(3)

        def fake_fetch(source_name, _url, _now, _target):
            barrier.wait(timeout=2)
            return [{"source": source_name}], [], []

        with mock.patch.object(rss_feeds, "FEEDS", feeds):
            with mock.patch.object(rss_feeds, "DIRECT_WEB_SOURCE_METADATA", {}):
                with mock.patch.object(rss_feeds, "_RSS_FETCH_WORKERS", 3):
                    with mock.patch.object(
                        rss_feeds,
                        "_fetch_feed_source",
                        side_effect=fake_fetch,
                    ):
                        articles, errors = rss_feeds.fetch()

        self.assertEqual(errors, [])
        self.assertEqual(
            [article["source"] for article in articles],
            list(feeds),
        )

    def test_disabled_direct_source_is_never_requested(self):
        sources = {
            "Active": {"enabled": True, "url": "https://active.example"},
            "Blocked": {"enabled": False, "url": "https://blocked.example"},
        }
        requested = []

        def fake_fetch(source_name, _metadata, _now):
            requested.append(source_name)
            return [{"source": source_name}]

        with mock.patch.object(rss_feeds, "FEEDS", {}):
            with mock.patch.object(rss_feeds, "DIRECT_WEB_SOURCE_METADATA", sources):
                with mock.patch.object(
                    rss_feeds,
                    "_fetch_direct_web_source",
                    side_effect=fake_fetch,
                ):
                    articles, errors = rss_feeds.fetch()

        self.assertEqual(errors, [])
        self.assertEqual(requested, ["Active"])
        self.assertEqual([article["source"] for article in articles], ["Active"])

    def test_out_of_window_google_article_is_not_decoded(self):
        feed = SimpleNamespace(entries=[
            {"title": "Old", "link": "https://news.google.com/old"},
            {"title": "Current", "link": "https://news.google.com/current"},
        ])
        with mock.patch.object(rss_feeds, "_get_feed", return_value=feed), \
             mock.patch.object(
                 rss_feeds,
                 "_article_date",
                 side_effect=[("", False), ("2026-10-16", True)],
             ), \
             mock.patch.object(
                 rss_feeds,
                 "resolve_gnews_url",
                 return_value="https://publisher.example/current",
             ) as decode:
            articles, errors, _notices = rss_feeds._fetch_feed_source(
                "Google Test",
                "https://news.google.com/rss/search?q=test",
                rss_feeds.datetime.now(rss_feeds.timezone.utc),
                None,
            )

        self.assertEqual(errors, [])
        self.assertEqual([article["title"] for article in articles], ["Current"])
        decode.assert_called_once_with("https://news.google.com/current")

    def test_failed_rss_goes_directly_to_google_news_fallback(self):
        fallback_feed = SimpleNamespace(entries=[
            {"title": "Recovered", "link": "https://news.google.com/recovered"},
        ])
        with mock.patch.object(
                 rss_feeds,
                 "_get_feed",
                 side_effect=[RuntimeError("rss down"), fallback_feed],
             ) as get_feed, \
             mock.patch.object(
                 rss_feeds,
                 "_article_date",
                 return_value=("2026-10-16", True),
             ), \
             mock.patch.object(
                 rss_feeds,
                 "resolve_gnews_url",
                 return_value="https://publisher.example/recovered",
             ):
            articles, errors, notices = rss_feeds._fetch_feed_source(
                "Publisher",
                "https://publisher.example/rss.xml",
                rss_feeds.datetime.now(rss_feeds.timezone.utc),
                None,
            )

        self.assertEqual(get_feed.call_count, 2)
        self.assertIn("news.google.com/rss/search", get_feed.call_args_list[1].args[0])
        self.assertEqual(errors, [])
        self.assertEqual([article["title"] for article in articles], ["Recovered"])
        self.assertEqual(len(notices), 1)


if __name__ == "__main__":
    unittest.main()
