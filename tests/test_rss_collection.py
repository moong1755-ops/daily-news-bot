"""RSS 동시 수집과 비활성 웹소스 처리 회귀 테스트."""

import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from src.fetchers import rss_feeds


class RSSCollectionTestCase(unittest.TestCase):
    def test_direct_source_starts_before_all_feeds_finish(self):
        barrier = threading.Barrier(2)

        def fake_feed(*_args):
            barrier.wait(timeout=3)
            return [{"source": "RSS"}], [], ["RSS notice"]

        def fake_direct(*_args):
            barrier.wait(timeout=3)
            return [{"source": "Web"}]

        with mock.patch.object(rss_feeds, "FEEDS", {"RSS": "https://example.com/rss"}), \
             mock.patch.object(rss_feeds, "DIRECT_WEB_SOURCE_METADATA", {
                 "Web": {"url": "https://example.com/web"},
             }), \
             mock.patch.object(rss_feeds, "_RSS_FETCH_WORKERS", 2), \
             mock.patch.object(rss_feeds, "_fetch_feed_source", side_effect=fake_feed), \
             mock.patch.object(rss_feeds, "_fetch_direct_web_source", side_effect=fake_direct):
            articles, errors = rss_feeds.fetch()

        self.assertEqual(errors, [])
        self.assertEqual([article["source"] for article in articles], ["RSS", "Web"])

    def test_failed_feed_does_not_hide_a_direct_source(self):
        with mock.patch.object(rss_feeds, "FEEDS", {"Broken": "https://example.com/rss"}), \
             mock.patch.object(rss_feeds, "DIRECT_WEB_SOURCE_METADATA", {
                 "Web": {"url": "https://example.com/web"},
             }), \
             mock.patch.object(rss_feeds, "_fetch_feed_source", side_effect=ValueError("down")), \
             mock.patch.object(rss_feeds, "_fetch_direct_web_source", return_value=[{"source": "Web"}]):
            articles, errors = rss_feeds.fetch()

        self.assertEqual(articles, [{"source": "Web"}])
        self.assertEqual(len(errors), 1)
        self.assertIn("Broken", errors[0])

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


class RSSResponseValidationTests(unittest.TestCase):
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>'
        b'<link>https://publisher.example</link><description>News</description>'
        b'<item><title>New investment</title>'
        b'<link>https://publisher.example/article</link></item></channel></rss>'
    )
    atom = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        b'<title>Test</title><id>https://publisher.example</id>'
        b'<updated>2026-11-18T00:00:00Z</updated></feed>'
    )

    def response(self, body, status=200, content_type="application/rss+xml"):
        response = rss_feeds.requests.Response()
        response.status_code = status
        response._content = body
        response.url = "https://publisher.example/feed"
        response.headers["Content-Type"] = content_type
        return response

    def test_valid_rss_is_not_rejected_for_incorrect_html_mime(self):
        response = self.response(self.rss, content_type="text/html")
        with mock.patch.object(rss_feeds.requests, "get", return_value=response) as get:
            feed = rss_feeds._get_feed(response.url)
        self.assertEqual(feed.entries[0].title, "New investment")
        get.assert_called_once_with(response.url, headers=rss_feeds._HEADERS, timeout=15)

    def test_valid_empty_rss_and_atom_are_not_collection_errors(self):
        empty_rss = self.rss[:self.rss.index(b"<item>")] + b"</channel></rss>"
        for body in (empty_rss, self.atom):
            with self.subTest(body=body), mock.patch.object(
                rss_feeds.requests, "get", return_value=self.response(body)
            ):
                self.assertEqual(rss_feeds._get_feed("https://publisher.example/feed").entries, [])

    def test_login_html_is_rejected_before_xml_parsing(self):
        for body in (b"<!DOCTYPE html><html>Register</html>", b" \n<HTML>Sign in</HTML>"):
            with self.subTest(body=body), mock.patch.object(
                rss_feeds.requests, "get", return_value=self.response(body)
            ), mock.patch.object(rss_feeds.feedparser, "parse") as parse:
                with self.assertRaisesRegex(ValueError, "non_feed_response"):
                    rss_feeds._get_feed("https://publisher.example/feed")
                parse.assert_not_called()

    def test_http_error_is_not_misreported_as_xml_error(self):
        for status in (403, 404, 429, 503):
            with self.subTest(status=status), mock.patch.object(
                rss_feeds.requests, "get", return_value=self.response(b"Forbidden", status=status)
            ), mock.patch.object(rss_feeds.feedparser, "parse") as parse:
                with self.assertRaises(rss_feeds.requests.HTTPError) as error:
                    rss_feeds._get_feed("https://publisher.example/feed")
                self.assertEqual(error.exception.response.status_code, status)
                parse.assert_not_called()

    def test_empty_and_non_feed_responses_are_not_accepted(self):
        for body in (b"", b"{}", b"<document>Not a feed</document>"):
            with self.subTest(body=body), mock.patch.object(
                rss_feeds.requests, "get", return_value=self.response(body)
            ):
                with self.assertRaisesRegex(ValueError, "non_feed_response"):
                    rss_feeds._get_feed("https://publisher.example/feed")

    def test_illegal_xml_character_recovery_is_preserved(self):
        damaged = self.rss.replace(b"<title>New", b"<title>\x01New")
        with mock.patch.object(rss_feeds.requests, "get", return_value=self.response(damaged)):
            feed = rss_feeds._get_feed("https://publisher.example/feed")
        self.assertEqual(len(feed.entries), 1)
        self.assertIn("New investment", feed.entries[0].title)

    def test_html_response_still_uses_google_news_fallback(self):
        responses = [self.response(b"<html>Register</html>"), self.response(self.rss)]
        with mock.patch.object(rss_feeds.requests, "get", side_effect=responses) as get, \
             mock.patch.object(rss_feeds, "_article_date", return_value=("2026-11-18", True)), \
             mock.patch.object(rss_feeds, "resolve_gnews_url", side_effect=lambda url: url):
            articles, errors, notices = rss_feeds._fetch_feed_source(
                "Publisher", "https://publisher.example/feed",
                rss_feeds.datetime.now(rss_feeds.timezone.utc), None)
        self.assertEqual(errors, [])
        self.assertEqual(len(articles), 1)
        self.assertEqual(get.call_count, 2)
        self.assertIn("news.google.com/rss/search", get.call_args_list[1].args[0])
        self.assertEqual(len(notices), 1)

    def test_html_and_empty_fallback_remain_visible_as_collection_error(self):
        responses = [self.response(b"<html>Register</html>"), self.response(self.atom)]
        with mock.patch.object(rss_feeds.requests, "get", side_effect=responses) as get:
            articles, errors, notices = rss_feeds._fetch_feed_source(
                "Publisher", "https://publisher.example/feed",
                rss_feeds.datetime.now(rss_feeds.timezone.utc), None)
        self.assertEqual(articles, [])
        self.assertEqual(notices, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("non_feed_response", errors[0])
        self.assertIn("fallback empty", errors[0])
        self.assertEqual(get.call_count, 2)


class GoogleNewsRequestReuseTests(unittest.TestCase):
    def setUp(self):
        # Isolate the process caches without replacing the production lock.
        self.cache_patch = mock.patch.object(rss_feeds, "_GN_URL_CACHE", {})
        self.pending_patch = mock.patch.object(rss_feeds, "_GN_URL_PENDING", {})
        self.cache_patch.start()
        self.pending_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.pending_patch.stop)

    def _resolve_same_url_concurrently(self, *, fail=False):
        url = "https://news.google.com/rss/articles/shared"
        started, waiting, release = (threading.Event() for _ in range(3))

        class ObservedFuture(Future):
            def result(self, timeout=None):
                waiting.set()
                return super().result(timeout=timeout)

        def decode(_url):
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test release missing")
            if fail:
                raise ValueError("decoder unavailable")
            return {"status": True, "decoded_url": "https://publisher.example/story"}

        with mock.patch.object(rss_feeds, "Future", ObservedFuture), \
             mock.patch.object(rss_feeds, "gnewsdecoder", side_effect=decode) as decoder:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(rss_feeds.resolve_gnews_url, url)
                try:
                    self.assertTrue(started.wait(timeout=3))
                    second = pool.submit(rss_feeds.resolve_gnews_url, url)
                    self.assertTrue(waiting.wait(timeout=3))
                finally:
                    release.set()
                expected = url if fail else "https://publisher.example/story"
                self.assertEqual(first.result(timeout=3), expected)
                self.assertEqual(second.result(timeout=3), expected)
            self.assertEqual(rss_feeds.resolve_gnews_url(url), expected)
            decoder.assert_called_once_with(url)
            self.assertEqual(rss_feeds._GN_URL_PENDING, {})

    def test_same_url_has_one_in_flight_request(self):
        self._resolve_same_url_concurrently()

    def test_failed_decode_releases_all_waiters_and_preserves_original_url(self):
        self._resolve_same_url_concurrently(fail=True)

    def test_distinct_urls_are_not_serialized(self):
        barrier = threading.Barrier(2)

        def decode(url):
            barrier.wait(timeout=3)
            return {"status": True, "decoded_url": url.replace("news.google.com", "publisher.example")}

        urls = ["https://news.google.com/first", "https://news.google.com/second"]
        with mock.patch.object(rss_feeds, "gnewsdecoder", side_effect=decode) as decoder:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(rss_feeds.resolve_gnews_url, urls))
        self.assertEqual(results, [url.replace("news.google.com", "publisher.example") for url in urls])
        self.assertEqual(decoder.call_count, 2)

    def test_non_google_url_is_not_decoded(self):
        with mock.patch.object(rss_feeds, "gnewsdecoder") as decoder:
            self.assertEqual(rss_feeds.resolve_gnews_url("https://publisher.example/story"),
                             "https://publisher.example/story")
        decoder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
