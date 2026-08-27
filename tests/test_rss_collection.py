"""RSS 동시 수집과 비활성 웹소스 처리 회귀 테스트."""

import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
