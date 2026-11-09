"""Regression coverage for IPO identity, changing outcomes and OECD geography."""

import unittest
from unittest.mock import Mock, patch

from test_editorial_logic import _install_optional_dependency_stubs
_install_optional_dependency_stubs()
from src import bot
from src.processor import deduplicator
from src.processor.ipo import ipo_stage
from src.processor.summarizer import _content_region


def article(title, key, *, date="2026-10-28", score=9):
    return {
        "title": title, "title_orig": title, "editor_event_key": key,
        "date": date, "source": "Reuters", "link": "https://example.com/" + key,
        "category": bot.ALTERNATIVE_CATEGORY, "region": "global",
        "importance": 3, "relevance": score,
    }


class IpoSafetyTests(unittest.TestCase):
    def test_current_relaunch_is_not_a_historical_withdrawal(self):
        for title in ('Acme relaunches IPO after last year withdrawal',
                      'Acme 상장 철회 후 재추진'):
            with self.subTest(title=title):
                self.assertEqual(ipo_stage(article(title, 'acme_ipo')), 'restarted')

    def test_tentative_withdrawal_does_not_replace_confirmed_filing(self):
        old = article('Acme files for IPO', 'acme_ipo', date='2026-10-26')
        for title in ('Acme may withdraw IPO', 'Acme denies IPO withdrawal', 'Acme 상장 철회 가능성'):
            with self.subTest(title=title):
                latest = article(title, 'acme_ipo')
                self.assertEqual(bot._collapse_daily_ipo_clusters([old, latest]), [old, latest])

    def test_cross_day_status_changes_are_not_repeated_news(self):
        old = article('Acme IPO withdrawn', 'acme_ipo', date='2026-10-26')
        latest = article('Acme relaunches IPO', 'acme_ipo')
        self.assertFalse(bot._same_cross_day_event(latest, old))

    def test_official_report_is_not_removed_by_cross_category_ipo_cluster(self):
        report = article('Acme IPO analysis', 'acme_ipo')
        report['category'] = bot.INSIGHTS_CATEGORY
        news = article('Acme files for IPO', 'acme_ipo')
        self.assertEqual(bot._collapse_cross_category_ipo_clusters([report, news]), [report, news])

    def test_sorted_keys_still_identify_the_company(self):
        for title, key, expected in (
            ("Anthropic IPO economics analysis", "analysis_anthropic_economics_filing_ipo", "anthropic"),
            ("OpenAI IPO economics analysis", "analysis_economics_filing_ipo_openai", "openai"),
            ("OpenAI files for IPO", "filing_ipo_openai", "openai"),
            ("Nvidia may invest in Mistral's IPO", "investment_ipo_mistral_nvidia", "mistral"),
        ):
            with self.subTest(key=key):
                self.assertEqual(bot._ipo_cluster_entities(article(title, key)), {expected})

    def test_different_companies_survive_editor_canonicalization(self):
        candidates = []
        for company in ("Anthropic", "OpenAI"):
            for publisher in ("Reuters", "TechCrunch"):
                item = article(
                    f"{company} IPO economics analysis",
                    f"{company.lower()}_ipo_filing_economics_analysis",
                )
                item["source"] = publisher
                candidates.append(item)
        merged = deduplicator.collapse_editor_event_duplicates(
            candidates, bot.EDITOR_EVENT_CATEGORY_PRIORITY,
        )
        result = bot._collapse_cross_category_ipo_clusters(merged)
        self.assertEqual(len(result), 2)
        self.assertEqual({item["title"].split()[0] for item in result}, {"Anthropic", "OpenAI"})

    def test_ambiguous_company_is_not_inferred_from_key_position(self):
        item = article("Investors weigh IPO economics", "analysis_economics_filing_ipo")
        self.assertEqual(bot._ipo_cluster_entities(item), set())

    def test_latest_withdrawal_survives_all_selection_stages(self):
        old = article("Acme files for IPO", "acme_ipo", date="2026-10-26", score=10)
        latest = article("Acme IPO withdrawn after market turmoil", "acme_ipo", score=9)
        self.assertFalse(deduplicator._should_merge(old, latest, 0.99))
        merged = deduplicator.collapse_editor_event_duplicates(
            [old, latest], bot.EDITOR_EVENT_CATEGORY_PRIORITY,
        )
        with patch.multiple(
            deduplicator, _get_model=Mock(return_value=Mock()),
            cosine_similarity=Mock(return_value=[[1.0, 0.99], [0.99, 1.0]]),
        ):
            selected = bot._select_category_articles(merged, bot.ALTERNATIVE_CATEGORY)
        self.assertEqual(selected, [latest])
        self.assertEqual(bot._collapse_cross_category_ipo_clusters(merged), [latest])

    def test_newer_relaunch_is_not_hidden_by_old_withdrawal(self):
        old = article("Acme IPO withdrawn", "acme_ipo", date="2026-10-26", score=10)
        latest = article("Acme relaunches IPO", "acme_ipo", score=8)
        self.assertFalse(deduplicator._same_editor_event(old, latest))
        self.assertEqual(bot._collapse_daily_ipo_clusters([old, latest]), [latest])

    def test_conflicting_outcomes_without_clear_chronology_are_preserved(self):
        for latest_date, date_basis in (("2026-10-26", ""), ("", ""), ("2026-10-28", "newsletter_date")):
            with self.subTest(date=latest_date, basis=date_basis):
                old = article("Acme files for IPO", "acme_ipo", date="2026-10-26", score=10)
                latest = article("Acme IPO withdrawn", "acme_ipo", date=latest_date, score=9)
                latest["date_basis"] = date_basis
                with patch.multiple(
                    deduplicator, _get_model=Mock(return_value=Mock()),
                    cosine_similarity=Mock(return_value=[[1.0, 0.99], [0.99, 1.0]]),
                ):
                    kept = deduplicator.filter_near_duplicates([old, latest], 0.6)
                self.assertEqual(kept, [old, latest])
                self.assertEqual(bot._collapse_daily_ipo_clusters(kept), [old, latest])


class OecdRegionSafetyTests(unittest.TestCase):
    def test_oecd_is_the_publisher_not_the_event_country(self):
        cases = (
            ("OECD, 올해 성장률 전망 2.6%로 상향", "한국 경제성장률 전망을 높였다. 국내 소비 회복이 배경이다.", "korea"),
            ("OECD raises South Korea growth forecast", "Stronger domestic demand.", "korea"),
            ("OECD fiscal policy report", "KDI 한국어 뉴스레터. 한국개발연구원 경제정보센터에서 소개한 보고서.", "global"),
            ("OECD raises US growth forecast", "미국 경제성장률 전망을 높였다.", "global"),
        )
        for title, description, expected in cases:
            for source_region in ("korea", "global"):
                with self.subTest(title=title, source_region=source_region):
                    region, _ = _content_region(
                        {"region": source_region}, bot.MACRO_CATEGORY,
                        f"{title} {description}".lower(), title,
                    )
                    self.assertEqual(region, expected)
