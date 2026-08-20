"""과거 날짜 재현 모드(AS_OF_DATE)를 검증한다.

RSS 는 과거 시점을 돌려주지 않으므로 완전한 재현은 불가능하다. Google News
검색만 after:/before: 로 지난 날짜를 가져올 수 있어, 부분 재현이라는 점을
전제로 동작만 확인한다.
"""

import os
import unittest
from datetime import date
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from src.fetchers import rss_feeds


class AsOfDateTestCase(unittest.TestCase):
    def test_absent_env_means_normal_mode(self):
        with mock.patch.dict(os.environ, {"AS_OF_DATE": ""}, clear=False):
            self.assertIsNone(rss_feeds.as_of_date())

    def test_valid_date_is_parsed(self):
        with mock.patch.dict(os.environ, {"AS_OF_DATE": "2026-08-17"}, clear=False):
            self.assertEqual(rss_feeds.as_of_date(), date(2026, 8, 17))

    def test_malformed_date_falls_back_to_normal_mode(self):
        with mock.patch.dict(os.environ, {"AS_OF_DATE": "8/17/2026"}, clear=False):
            self.assertIsNone(rss_feeds.as_of_date())


class RewriteForAsOfTestCase(unittest.TestCase):
    GNEWS = (
        "https://news.google.com/rss/search?q=%28venture+capital%29+when%3A3d"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    def _query(self, url):
        return parse_qs(urlsplit(url).query)["q"][0]

    def test_when_operator_becomes_a_date_window(self):
        out = rss_feeds.rewrite_for_as_of(self.GNEWS, date(2026, 8, 17))
        query = self._query(out)
        self.assertNotIn("when:", query)
        self.assertIn("after:2026-08-16", query)
        self.assertIn("before:2026-08-18", query)
        self.assertIn("venture capital", query)

    def test_query_without_when_gets_the_window_appended(self):
        url = "https://news.google.com/rss/search?q=climate+tech&hl=en-US"
        query = self._query(rss_feeds.rewrite_for_as_of(url, date(2026, 8, 17)))
        self.assertIn("climate tech", query)
        self.assertIn("after:2026-08-16", query)

    def test_other_query_params_are_preserved(self):
        out = rss_feeds.rewrite_for_as_of(self.GNEWS, date(2026, 8, 17))
        params = parse_qs(urlsplit(out).query)
        self.assertEqual(params["hl"], ["en-US"])
        self.assertEqual(params["ceid"], ["US:en"])

    def test_direct_rss_urls_are_untouched(self):
        url = "https://techcrunch.com/feed/"
        self.assertEqual(rss_feeds.rewrite_for_as_of(url, date(2026, 8, 17)), url)

    def test_no_target_means_no_change(self):
        self.assertEqual(rss_feeds.rewrite_for_as_of(self.GNEWS, None), self.GNEWS)


class ArticleDateFilterTestCase(unittest.TestCase):
    @staticmethod
    def _entry(iso):
        # feedparser 의 published_parsed 형태(UTC struct_time 앞 6개)
        y, m, d = (int(x) for x in iso.split("-"))
        return {"published_parsed": (y, m, d, 12, 0, 0)}

    def test_only_the_target_day_is_collected(self):
        with mock.patch.dict(os.environ, {"AS_OF_DATE": "2026-08-17"}, clear=False):
            _, keep_same = rss_feeds._article_date(self._entry("2026-08-17"), "X", None)
            _, keep_other = rss_feeds._article_date(self._entry("2026-08-19"), "X", None)
        self.assertTrue(keep_same)
        self.assertFalse(keep_other)

    def test_undated_entries_are_dropped_in_replay(self):
        """평소에는 살려 두지만, 그날 기사인지 확인할 수 없으므로 제외한다."""
        with mock.patch.dict(os.environ, {"AS_OF_DATE": "2026-08-17"}, clear=False):
            _, keep = rss_feeds._article_date({}, "X", None)
        self.assertFalse(keep)

    def test_undated_entries_survive_in_normal_mode(self):
        with mock.patch.dict(os.environ, {"AS_OF_DATE": ""}, clear=False):
            label, keep = rss_feeds._article_date({}, "X", rss_feeds.datetime.now(rss_feeds.timezone.utc))
        self.assertTrue(keep)
        self.assertEqual(label, "Unknown date")



class CentralDateFilterTestCase(unittest.TestCase):
    """수집이 끝난 뒤 한 곳에서 거른다.

    날짜 창은 RSS 피드에만 걸려 있어, 월요일 재현에 해커뉴스·Bloomberg
    뉴스레터 기사 5건이 섞여 나왔다.
    """

    ARTICLES = [
        {"title": "월요일 기사", "date": "2026-08-17", "source": "RSS"},
        {"title": "해커뉴스 수요일", "date": "2026-08-19", "source": "Hacker News"},
        {"title": "뉴스레터 목요일", "date": "2026-08-20", "source": "Bloomberg Green"},
        {"title": "날짜 없음", "date": "Unknown date", "source": "X"},
    ]

    def test_only_target_day_survives(self):
        from src.bot import filter_to_as_of_date

        with mock.patch.dict(os.environ, {"AS_OF_DATE": "2026-08-17"}, clear=False):
            kept = filter_to_as_of_date(self.ARTICLES)

        self.assertEqual([a["title"] for a in kept], ["월요일 기사"])

    def test_all_sources_are_covered_not_just_rss(self):
        from src.bot import filter_to_as_of_date

        with mock.patch.dict(os.environ, {"AS_OF_DATE": "2026-08-17"}, clear=False):
            kept = filter_to_as_of_date(self.ARTICLES)

        sources = {a["source"] for a in kept}
        self.assertNotIn("Hacker News", sources)
        self.assertNotIn("Bloomberg Green", sources)

    def test_normal_mode_keeps_everything(self):
        from src.bot import filter_to_as_of_date

        with mock.patch.dict(os.environ, {"AS_OF_DATE": ""}, clear=False):
            kept = filter_to_as_of_date(self.ARTICLES)

        self.assertEqual(len(kept), len(self.ARTICLES))


if __name__ == "__main__":
    unittest.main()
