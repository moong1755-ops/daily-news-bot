import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import requests

from src.config import CATEGORIES
from src.weekly.archive import KOREA_TIMEZONE, load_weekly_archive, weekly_date_window
from src.weekly.deduplicator import deduplicate_weekly_articles
from src.weekly.editor import WeeklyHeadlines, _parse_lines
from src.weekly.market_data import MarketPoint, MarketSnapshot, collect_market_snapshots
from src.weekly.renderer import render_weekly_briefing
from src.weekly.runner import run_weekly_briefing
from src.weekly.selector import WeeklySelection, select_weekly_articles


IMPACT = next(category for category in CATEGORIES if category.startswith("🌱"))
AI = next(category for category in CATEGORIES if category.startswith("🤖"))
ALTERNATIVE = next(category for category in CATEGORIES if category.startswith("📈"))
MACRO = next(category for category in CATEGORIES if category.startswith("🌐"))
INSIGHTS = next(category for category in CATEGORIES if category.startswith("👔"))
MONDAY_RUN = datetime(2026, 8, 31, 8, 30, tzinfo=KOREA_TIMEZONE)


def article(category, slug, title, *, region="global", score=1, source="Reuters"):
    return {
        "category": category,
        "region": region,
        "title": title,
        "title_orig": title,
        "url": f"https://example.com/{slug}",
        "source": source,
        "selection_score": score,
        "_archive_edition_date": "2026-08-30",
    }


class FakeResponse:
    def __init__(self, *, text="", payload=None, status_code=200):
        self.text = text
        self._payload = {} if payload is None else payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        params = kwargs.get("params") or {}
        if "fredgraph.csv" in url:
            series = params["id"]
            values = {
                "DEXKOUS": 1380,
                "SP500": 6500,
                "NASDAQCOM": 22000,
                "DGS10": 4.10,
                "DCOILBRENTEU": 80,
            }
            base = values[series]
            rows = [f"observation_date,{series}"]
            dates = ("2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28")
            for offset, observed_on in enumerate(dates):
                rows.append(f"{observed_on},{base + offset}")
            return FakeResponse(text="\n".join(rows))

        if "api.finance.naver.com" in url:
            base = 3200 if params["symbol"] == "KOSPI" else 900
            rows = [["날짜", "시가", "고가", "저가", "종가", "거래량"]]
            for offset, observed_on in enumerate(
                ("20260821", "20260824", "20260825", "20260826", "20260827", "20260828")
            ):
                rows.append([observed_on, 0, 0, 0, base + offset, 0])
            return FakeResponse(text=repr(rows))

        observed = str(params["basDd"])
        is_kospi = "kospi" in url
        name = "코스피" if is_kospi else "코스닥"
        base = 3200 if is_kospi else 900
        return FakeResponse(payload={
            "OutBlock_1": [{
                "BAS_DD": observed,
                "IDX_NM": name,
                "CLSPRC_IDX": str(base + int(observed[-2:])),
            }]
        })

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(text="ok")


class BadKrxSession(FakeSession):
    def get(self, url, **kwargs):
        if "fredgraph.csv" in url or "api.finance.naver.com" in url:
            return super().get(url, **kwargs)
        self.get_calls.append((url, kwargs))
        return FakeResponse(payload=[])


class WeeklyArchiveTests(unittest.TestCase):
    def test_manual_run_always_uses_last_completed_monday_to_sunday(self):
        wednesday = datetime(2026, 9, 2, 12, tzinfo=KOREA_TIMEZONE)
        in_progress_sunday = datetime(2026, 9, 6, 12, tzinfo=KOREA_TIMEZONE)
        expected = (date(2026, 8, 24), date(2026, 8, 30))

        self.assertEqual(weekly_date_window(wednesday), expected)
        self.assertEqual(weekly_date_window(in_progress_sunday), expected)

    def test_reads_v2_v3_and_legacy_without_aborting_on_bad_line(self):
        records = [
            "not-json",
            json.dumps({
                "version": 3,
                "edition_date": "2026-08-24",
                "articles": [article(IMPACT, "v3", "V3 impact")],
            }, ensure_ascii=False),
            json.dumps({
                "version": 2,
                "ts": "2026-08-25T00:00:00+00:00",
                "articles": [article(AI, "v2", "V2 AI")],
            }, ensure_ascii=False),
            json.dumps({
                "ts": "2026-08-26T00:00:00+00:00",
                "text": f"*{IMPACT}*\n• <https://example.com/legacy|Legacy impact> (ImpactAlpha, 26.08.26)",
            }, ensure_ascii=False),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "daily.jsonl"
            path.write_text("\n".join(records) + "\n", encoding="utf-8")
            window = load_weekly_archive(path, now=MONDAY_RUN)

        self.assertEqual((window.start_date, window.end_date), (date(2026, 8, 24), date(2026, 8, 30)))
        self.assertEqual(len(window.articles), 3)
        self.assertEqual(window.records_in_window, 3)
        self.assertTrue(any("JSON 오류" in error for error in window.errors))


class WeeklyDeduplicationTests(unittest.TestCase):
    def test_same_event_merges_but_different_counterparty_stays_separate(self):
        same_reuters = article(ALTERNATIVE, "r", "Acme raises $100m Series C", score=8)
        same_reuters["editor_event_key"] = "acme_funding_series_c"
        same_techcrunch = article(ALTERNATIVE, "t", "Acme lands $100m in Series C", score=7, source="TechCrunch")
        same_techcrunch["editor_event_key"] = "acme_funding_series_c"
        separate = article(ALTERNATIVE, "m", "Acme acquires Beta", score=9)
        separate["editor_event_key"] = "acme_acquires_beta"

        result = deduplicate_weekly_articles([same_reuters, same_techcrunch, separate])

        self.assertEqual(len(result), 2)
        merged = next(item for item in result if item["weekly_story_count"] == 2)
        self.assertEqual(set(merged["weekly_sources"]), {"Reuters", "TechCrunch"})


class WeeklySelectorTests(unittest.TestCase):
    def test_impact_and_region_balance_survive_global_limit(self):
        candidates = [
            *(article(
                IMPACT, f"impact-{index}", f"Climate investment {index}",
                score=9 - index, source="ImpactAlpha",
            ) for index in range(4)),
            *(article(
                AI, f"ai-{index}", f"AI infrastructure {index}",
                score=8 - index, source="TechCrunch",
            ) for index in range(4)),
            *(article(
                ALTERNATIVE, f"alt-g-{index}", f"Global acquisition {index}",
                score=10 - index,
            ) for index in range(4)),
            *(article(
                ALTERNATIVE, f"alt-k-{index}", f"국내 투자 유치 {index}",
                region="korea", score=10 - index, source="딜사이트",
            ) for index in range(4)),
            *(article(MACRO, f"macro-g-{index}", f"Fed policy shift {index}", score=7 - index) for index in range(4)),
            *(article(
                MACRO, f"macro-k-{index}", f"한국은행 금리 정책 {index}",
                region="korea", score=7 - index, source="연합뉴스",
            ) for index in range(4)),
            article(INSIGHTS, "insight-1", "The state of AI in 2026", score=5, source="McKinsey"),
            article(INSIGHTS, "insight-2", "Global private equity outlook", score=4, source="Bain"),
            article(INSIGHTS, "insight-3", "Energy transition survey", score=3, source="BCG"),
            article(INSIGHTS, "insight-4", "IFRS 20 accounting update", score=5, source="PwC"),
        ]

        selection = select_weekly_articles(candidates)

        self.assertEqual(len(selection.articles), 21)
        self.assertEqual(len(selection.by_category[IMPACT]), 3)
        self.assertEqual(len(selection.by_category[AI]), 3)
        self.assertEqual(len(selection.by_category[ALTERNATIVE]), 6)
        self.assertEqual(len(selection.by_category[MACRO]), 6)
        self.assertEqual(len(selection.by_category[INSIGHTS]), 3)
        self.assertEqual({item["region"] for item in selection.by_category[ALTERNATIVE]}, {"global", "korea"})
        self.assertEqual({item["region"] for item in selection.by_category[MACRO]}, {"global", "korea"})
        insight_titles = {item["title"] for item in selection.by_category[INSIGHTS]}
        self.assertIn("The state of AI in 2026", insight_titles)
        self.assertNotIn("IFRS 20 accounting update", insight_titles)


class WeeklyMarketTests(unittest.TestCase):
    def test_collects_all_configured_indicators_with_mocked_official_sources(self):
        session = FakeSession()
        with patch.dict(os.environ, {"KRX_AUTH_KEY": "test-key"}, clear=False):
            snapshots = collect_market_snapshots(date(2026, 8, 28), session=session)

        self.assertEqual(len(snapshots), 7)
        self.assertTrue(all(snapshot.available for snapshot in snapshots))
        self.assertTrue(all(len(snapshot.sparkline) == 5 for snapshot in snapshots))
        treasury = next(snapshot for snapshot in snapshots if snapshot.key == "us_10y")
        self.assertEqual(treasury.change_unit, "basis_points")

    def test_missing_krx_key_uses_naver_finance_fallback(self):
        session = FakeSession()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRX_AUTH_KEY", None)
            snapshots = collect_market_snapshots(
                date(2026, 8, 24), date(2026, 8, 30), session=session
            )

        kospi = next(item for item in snapshots if item.key == "kospi")
        self.assertTrue(kospi.available)
        self.assertEqual(kospi.provider, "naver")
        self.assertIn("finance.naver.com", kospi.source_url)
        self.assertTrue(next(item for item in snapshots if item.key == "sp500").available)

    def test_malformed_krx_response_uses_naver_fallback(self):
        session = BadKrxSession()
        with patch.dict(os.environ, {"KRX_AUTH_KEY": "test-key"}, clear=False):
            snapshots = collect_market_snapshots(
                date(2026, 8, 24), date(2026, 8, 30), session=session
            )

        kospi = next(item for item in snapshots if item.key == "kospi")
        self.assertTrue(kospi.available)
        self.assertEqual(kospi.provider, "naver")
        self.assertTrue(next(item for item in snapshots if item.key == "sp500").available)

    def test_compares_previous_and_current_week_last_trading_days(self):
        session = FakeSession()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRX_AUTH_KEY", None)
            snapshots = collect_market_snapshots(
                date(2026, 8, 24), date(2026, 8, 30), session=session
            )

        self.assertTrue(all(item.comparison for item in snapshots))
        self.assertEqual(
            {item.comparison.observed_on for item in snapshots},
            {date(2026, 8, 21)},
        )
        self.assertEqual(
            {item.latest.observed_on for item in snapshots},
            {date(2026, 8, 28)},
        )


class WeeklyRendererTests(unittest.TestCase):
    def test_uses_one_block_message_and_native_slack_lists(self):
        impact_article = article(IMPACT, "impact-link", "기후테크 투자")
        selection = WeeklySelection(
            {category: ((impact_article,) if category == IMPACT else ()) for category in CATEGORIES},
            (impact_article,),
            20,
        )
        markets = (MarketSnapshot(
            "sp500",
            "S&P 500",
            "fred",
            "percent",
            (MarketPoint(date(2026, 8, 24), 100), MarketPoint(date(2026, 8, 28), 105)),
            5.0,
            "▁█",
            source_url="https://fred.stlouisfed.org/series/SP500",
            comparison=MarketPoint(date(2026, 8, 21), 100),
        ),)

        message = render_weekly_briefing(
            date(2026, 8, 24),
            date(2026, 8, 30),
            WeeklyHeadlines(("임팩트 투자 확대",), None, True),
            selection,
            markets,
        )
        encoded = json.dumps(message.blocks, ensure_ascii=False)

        self.assertLessEqual(len(message.blocks), 50)
        self.assertIn('"style": "bullet"', encoded)
        self.assertIn('"type": "link"', encoded)
        self.assertNotIn("•", encoded)
        self.assertIn("https://example.com/impact-link", encoded)
        self.assertIn("https://fred.stlouisfed.org/series/SP500", encoded)
        self.assertIn("전주 마지막 거래일 08.21 대비", encoded)
        self.assertNotIn("임팩트 VC 투자심사역용", encoded)
        self.assertLess(encoded.index("시장지표"), encoded.index("한 주 한눈에"))


class WeeklyRunnerTests(unittest.TestCase):
    def test_success_archive_prevents_duplicate_slack_and_data_calls(self):
        source_record = {
            "version": 3,
            "edition_date": "2026-08-30",
            "articles": [
                article(IMPACT, "impact", "기후 투자", source="ImpactAlpha"),
                article(AI, "ai", "AI 인프라 투자", source="TechCrunch"),
                article(ALTERNATIVE, "alt", "Global acquisition", score=5),
                article(MACRO, "macro", "Fed policy shift", score=4),
                article(INSIGHTS, "insight", "The state of private equity", score=3, source="McKinsey"),
            ],
        }
        session = FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "daily.jsonl"
            delivery_path = Path(temp_dir) / "weekly.jsonl"
            source_path.write_text(json.dumps(source_record, ensure_ascii=False) + "\n", encoding="utf-8")
            env = {"WEEKLY_SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/weekly"}
            with patch.dict(os.environ, env, clear=True):
                first = run_weekly_briefing(
                    now=MONDAY_RUN,
                    source_archive_path=source_path,
                    delivery_archive_path=delivery_path,
                    session=session,
                )
                calls_after_first = (len(session.get_calls), len(session.post_calls))
                second = run_weekly_briefing(
                    now=MONDAY_RUN,
                    source_archive_path=source_path,
                    delivery_archive_path=delivery_path,
                    session=session,
                )

            archived = json.loads(delivery_path.read_text(encoding="utf-8"))

        self.assertTrue(first.success and first.delivered)
        self.assertTrue(second.success and not second.delivered)
        self.assertEqual((len(session.get_calls), len(session.post_calls)), calls_after_first)
        self.assertEqual(calls_after_first[1], 1)
        self.assertEqual(
            session.post_calls[0][0],
            "https://hooks.slack.test/services/weekly",
        )
        self.assertEqual(archived["articles"][0]["url"], "https://example.com/impact")

    def test_daily_webhook_is_never_used_for_weekly_delivery(self):
        source_record = {
            "version": 3,
            "edition_date": "2026-08-30",
            "articles": [article(IMPACT, "impact", "기후 투자", source="ImpactAlpha")],
        }
        session = FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "daily.jsonl"
            delivery_path = Path(temp_dir) / "weekly.jsonl"
            source_path.write_text(
                json.dumps(source_record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/daily"},
                clear=True,
            ):
                result = run_weekly_briefing(
                    now=MONDAY_RUN,
                    source_archive_path=source_path,
                    delivery_archive_path=delivery_path,
                    session=session,
                )

        self.assertFalse(result.success)
        self.assertIn("WEEKLY_SLACK_WEBHOOK_URL", result.reason)
        self.assertEqual(session.post_calls, [])

    def test_json_summary_parser_never_truncates_line_text(self):
        long_line = "중요한 시장 변화 " * 30
        parsed = _parse_lines(json.dumps({"lines": [long_line]}, ensure_ascii=False), 3)
        self.assertEqual(parsed, (long_line.strip(),))


if __name__ == "__main__":
    unittest.main()
