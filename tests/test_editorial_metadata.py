"""Regression tests for the minimal importance metadata rollout."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src import bot
from src.config import CATEGORIES
from src.editorial_review import (
    DAILY_REVIEW_FIELDS,
    WEEKLY_REVIEW_FIELDS,
    select_alt_with_soft_diversity,
    write_review_csv,
)
from src.processor import editor
from src.processor.deduplicator import collapse_editor_event_duplicates
from src.weekly.archive import KOREA_TIMEZONE, load_weekly_archive
from src.weekly.deduplicator import deduplicate_weekly_articles
from src.weekly import editor as weekly_editor
from src.weekly import renderer as weekly_renderer
from src.weekly.selector import WeeklySelection, _ranked


IMPACT = next(category for category in CATEGORIES if category.startswith("🌱"))
AI = next(category for category in CATEGORIES if category.startswith("🤖"))
ALTERNATIVE = next(category for category in CATEGORIES if category.startswith("📈"))


def article(
    title: str,
    *,
    category: str = ALTERNATIVE,
    region: str = "korea",
    importance: object = 0,
    score: float = 0,
    subtype: str = "",
    major_deal: bool = False,
) -> dict:
    slug = title.casefold().replace(" ", "-")
    return {
        "title": title,
        "title_orig": title,
        "url": f"https://example.com/{slug}",
        "link": f"https://example.com/{slug}",
        "source": "Test Source",
        "category": category,
        "region": region,
        "importance": importance,
        "relevance": score,
        "editor_score": score,
        "selection_score": score,
        "alt_subtype": subtype,
        "major_deal": major_deal,
    }


def daily_select(candidates: list[dict], category: str) -> list[dict]:
    ranked = sorted(
        candidates,
        key=lambda item: bot._selection_priority(item, category),
        reverse=True,
    )
    with patch.object(
        bot,
        "filter_near_duplicates",
        side_effect=lambda items, _threshold: items,
    ):
        return bot._select_category_articles(ranked, category)


class EditorMetadataParsingTests(unittest.TestCase):
    def apply(self, verdict: dict, *, category: str = AI) -> dict:
        candidate = article("candidate", category=category)
        editor._apply(candidate, verdict, set(CATEGORIES))
        return candidate

    def test_importance_one_two_three_are_saved(self):
        for value in (1, 2, 3, "1", "2", "3"):
            with self.subTest(value=value):
                candidate = self.apply({
                    "keep": True,
                    "category": AI,
                    "score": 7,
                    "importance": value,
                    "importance_reason": "industry_shift",
                })
                self.assertEqual(candidate["importance"], int(value))

    def test_invalid_importance_falls_back_without_rounding(self):
        for value in (0, 4, -1, 1.5, True, "2.0", "high", None):
            with self.subTest(value=value):
                candidate = self.apply({
                    "keep": True,
                    "category": AI,
                    "score": 7,
                    "importance": value,
                })
                self.assertEqual(candidate["importance"], 0)

    def test_missing_metadata_keeps_old_editor_fields(self):
        candidate = self.apply({
            "keep": True,
            "category": AI,
            "score": 8,
            "reason": "industry_update",
            "event_key": "example_event",
        })
        self.assertEqual(candidate["editor_score"], 8)
        self.assertEqual(candidate["editor_event_key"], "example_event")
        self.assertEqual(candidate["importance"], 0)
        self.assertEqual(candidate["importance_reason"], "")
        self.assertEqual(candidate["alt_subtype"], "")

    def test_alt_subtype_is_saved_only_for_alternative_articles(self):
        verdict = {
            "keep": True,
            "category": ALTERNATIVE,
            "score": 7,
            "importance": 2,
            "importance_reason": "major_deal",
            "alt_subtype": "venture_growth",
        }
        self.assertEqual(
            self.apply(verdict, category=ALTERNATIVE)["alt_subtype"],
            "venture_growth",
        )
        verdict["category"] = AI
        self.assertEqual(self.apply(verdict, category=AI)["alt_subtype"], "")

    def test_unknown_enums_are_cleared(self):
        candidate = self.apply({
            "keep": True,
            "category": ALTERNATIVE,
            "score": 7,
            "importance": 2,
            "importance_reason": "very_important",
            "alt_subtype": "other",
        }, category=ALTERNATIVE)
        self.assertEqual(candidate["importance_reason"], "")
        self.assertEqual(candidate["alt_subtype"], "")


class DailySelectionTests(unittest.TestCase):
    def test_importance_precedes_old_score(self):
        candidates = [
            article("must know", category=AI, importance=3, score=6),
            article("important", category=AI, importance=2, score=10),
            article("supporting", category=AI, importance=1, score=9),
            article("fourth", category=AI, importance=1, score=8),
        ]
        selected = daily_select(candidates, AI)
        self.assertEqual([item["title"] for item in selected], [
            "must know", "important", "supporting",
        ])

    def test_old_score_breaks_ties_inside_same_importance(self):
        candidates = [
            article("seven", category=AI, importance=2, score=7),
            article("ten", category=AI, importance=2, score=10),
            article("nine", category=AI, importance=2, score=9),
            article("eight", category=AI, importance=2, score=8),
        ]
        selected = daily_select(candidates, AI)
        self.assertEqual([item["title"] for item in selected], ["ten", "nine", "eight"])

    def test_alternative_diversity_is_soft_and_same_importance_only(self):
        candidates = [
            article("capital-a", importance=2, score=9, subtype="capital_formation"),
            article("capital-b", importance=2, score=8.9, subtype="capital_formation"),
            article("pe", importance=2, score=8.8, subtype="pe_ma"),
            article("exit", importance=2, score=8.7, subtype="exit_liquidity"),
        ]
        selected = select_alt_with_soft_diversity(
            candidates,
            limit=3,
            score_fn=lambda item: item["selection_score"],
        )
        self.assertEqual([item["title"] for item in selected], ["capital-a", "pe", "exit"])

    def test_diversity_never_replaces_higher_importance(self):
        candidates = [
            article("capital-a", importance=3, score=9, subtype="capital_formation"),
            article("capital-b", importance=3, score=8, subtype="capital_formation"),
            article("pe-low", importance=1, score=10, subtype="pe_ma"),
        ]
        selected = select_alt_with_soft_diversity(
            candidates,
            limit=2,
            score_fn=lambda item: item["selection_score"],
        )
        self.assertEqual([item["title"] for item in selected], ["capital-a", "capital-b"])

    def test_missing_metadata_preserves_old_score_order(self):
        candidates = [
            article("six", category=AI, score=6),
            article("nine", category=AI, score=9),
            article("seven", category=AI, score=7),
            article("eight", category=AI, score=8),
        ]
        selected = daily_select(candidates, AI)
        self.assertEqual([item["title"] for item in selected], ["nine", "eight", "seven"])

    def test_mother_fund_regression(self):
        candidates = [
            article("TPG Lotte Rental", importance=3, score=8, subtype="pe_ma"),
            article("Mother Fund 1.3tn", importance=3, score=7, subtype="capital_formation"),
            article("KEXIM commitment", importance=2, score=8, subtype="capital_formation"),
            article("SNT merger", importance=2, score=8, subtype="pe_ma"),
        ]
        selected = daily_select(candidates, ALTERNATIVE)
        self.assertEqual(
            {item["title"] for item in selected},
            {"TPG Lotte Rental", "Mother Fund 1.3tn", "KEXIM commitment"},
        )

    def test_new_major_deal_does_not_bypass_importance_limit(self):
        candidates = [
            article("one", region="global", importance=3, score=9),
            article("two", region="global", importance=2, score=9),
            article("three", region="global", importance=1, score=9),
            article("low deal", region="global", importance=1, score=8, major_deal=True),
        ]
        selected = daily_select(candidates, ALTERNATIVE)
        self.assertEqual(len(selected), 3)
        self.assertNotIn("low deal", {item["title"] for item in selected})

    def test_legacy_major_deal_overflow_is_preserved(self):
        candidates = [
            article("one", region="global", score=9),
            article("two", region="global", score=8),
            article("three", region="global", score=7),
            article("legacy deal", region="global", score=6, major_deal=True),
        ]
        selected = daily_select(candidates, ALTERNATIVE)
        self.assertEqual(len(selected), 4)
        self.assertIn("legacy deal", {item["title"] for item in selected})


class WeeklyMetadataTests(unittest.TestCase):
    def test_archive_loads_metadata_and_old_rows_remain_valid(self):
        records = [
            {
                "version": 3,
                "edition_date": "2026-08-25",
                "articles": [{
                    **article("new metadata", importance=3, subtype="capital_formation"),
                    "importance_reason": "systemic_capital",
                }],
            },
            {
                "version": 2,
                "edition_date": "2026-08-26",
                "articles": [article("old metadata") | {"importance": None}],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "archive.jsonl"
            path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )
            window = load_weekly_archive(
                path,
                now=datetime(2026, 8, 31, 8, 30, tzinfo=KOREA_TIMEZONE),
            )
        self.assertEqual(len(window.articles), 2)
        loaded = next(item for item in window.articles if item["title"] == "new metadata")
        self.assertEqual(loaded["importance"], 3)
        self.assertEqual(loaded["importance_reason"], "systemic_capital")

    def test_weekly_importance_precedes_weekly_score(self):
        high_importance = article("high importance", importance=3, score=8)
        lower_importance = article("higher score", importance=2, score=12)
        ranked = _ranked([lower_importance, high_importance])
        self.assertEqual(ranked[0]["title"], "high importance")

    def test_weekly_fallback_also_uses_importance_first(self):
        high_importance = article("high importance", importance=3, score=8)
        lower_importance = article("higher score", importance=2, score=12)
        high_importance["weekly_score"] = 8
        lower_importance["weekly_score"] = 12
        self.assertEqual(
            weekly_editor._fallback_lines([lower_importance, high_importance], 1),
            ("high importance",),
        )

    def test_weekly_prompt_contains_all_editorial_metadata(self):
        candidate = article("capital supply", importance=3, score=7, subtype="capital_formation")
        candidate.update({
            "description": "National VC allocation expands",
            "importance_reason": "systemic_capital",
            "weekly_score": 7,
        })
        prompt = weekly_editor._prompt([candidate], 3)
        for expected in (
            "capital supply",
            "Test Source",
            "National VC allocation expands",
            '"importance": 3',
            "systemic_capital",
            "capital_formation",
            '"weekly_score": 7',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, prompt)

    def test_weekly_headlines_make_only_one_existing_llm_call(self):
        candidate = article("capital supply", importance=3, score=7)
        candidate["weekly_score"] = 7
        with patch.object(
            weekly_editor,
            "generate_editor_json",
            return_value=('{"lines":["자본공급 확대"]}', "fake-model"),
        ) as generate:
            result = weekly_editor.build_weekly_headlines([candidate])
        self.assertEqual(result.lines, ("자본공급 확대",))
        generate.assert_called_once()

    def test_daily_event_merge_preserves_strongest_metadata(self):
        original = article("Original report", importance=1, score=9)
        original["editor_event_key"] = "funding_event"
        corroboration = article(
            "Corroborating report",
            importance=3,
            score=7,
            subtype="capital_formation",
        )
        corroboration["editor_event_key"] = "funding_event"
        corroboration["importance_reason"] = "systemic_capital"
        merged = collapse_editor_event_duplicates(
            [original, corroboration],
            {ALTERNATIVE: 30},
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["importance"], 3)
        self.assertEqual(merged[0]["importance_reason"], "systemic_capital")

    def test_weekly_event_merge_preserves_strongest_metadata(self):
        original = article("Original report", importance=1, score=9)
        original["editor_event_key"] = "funding_event"
        original["_archive_edition_date"] = "2026-08-25"
        corroboration = article(
            "Corroborating report",
            importance=3,
            score=7,
            subtype="capital_formation",
        )
        corroboration.update({
            "editor_event_key": "funding_event",
            "importance_reason": "systemic_capital",
            "_archive_edition_date": "2026-08-26",
        })
        merged = deduplicate_weekly_articles([original, corroboration])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["importance"], 3)
        self.assertEqual(merged[0]["alt_subtype"], "capital_formation")


class ReviewCsvTests(unittest.TestCase):
    def rows(self, path: Path) -> tuple[list[str], list[dict]]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or []), list(reader)

    def test_daily_records_selected_and_not_selected_but_skips_garbage(self):
        selected = article("selected", importance=3, score=8)
        not_selected = article("not selected", importance=2, score=7)
        garbage = article("job posting", importance=1, score=1)
        garbage["editorial_excluded"] = True
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "daily.csv"
            write_review_csv(
                path,
                edition_date="2026-09-02",
                candidates=[selected, not_selected, garbage],
                selected=[selected],
                retention_days=60,
            )
            fields, rows = self.rows(path)
        self.assertEqual(fields, list(DAILY_REVIEW_FIELDS))
        self.assertEqual([row["selected"] for row in rows], ["TRUE", "FALSE"])
        self.assertNotIn("job posting", {row["title"] for row in rows})

    def test_weekly_schema_and_rolling_window(self):
        old = article("old")
        current = article("current", importance=3)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "weekly.csv"
            write_review_csv(
                path,
                edition_date="2025-01-01",
                candidates=[old],
                selected=[],
                retention_days=370,
                review_type="weekly",
            )
            write_review_csv(
                path,
                edition_date="2026-09-02",
                candidates=[current],
                selected=[current],
                retention_days=370,
                review_type="weekly",
            )
            fields, rows = self.rows(path)
        self.assertEqual(fields, list(WEEKLY_REVIEW_FIELDS))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["week"], "2026-09-02")
        self.assertEqual(rows[0]["weekly_selected"], "TRUE")

    def test_daily_review_failure_is_non_fatal(self):
        with patch.object(bot, "write_review_csv", side_effect=OSError("disk full")):
            saved = bot._save_daily_review([article("candidate")], [])
        self.assertFalse(saved)


class ReviewSheetLinkTests(unittest.TestCase):
    def selection(self) -> WeeklySelection:
        return WeeklySelection(
            {category: () for category in CATEGORIES},
            (),
            0,
        )

    def test_weekly_link_is_added_only_when_configured(self):
        sheet_url = "https://docs.google.com/spreadsheets/d/test/edit"
        with patch.object(weekly_renderer, "EDITORIAL_REVIEW_SHEET_URL", sheet_url):
            message = weekly_renderer.render_weekly_briefing(
                date(2026, 8, 24),
                date(2026, 8, 30),
                weekly_editor.WeeklyHeadlines((), None, False),
                self.selection(),
                (),
            )
        self.assertIn(sheet_url, message.plain_text)
        self.assertIn(sheet_url, json.dumps(message.blocks, ensure_ascii=False))

        with patch.object(weekly_renderer, "EDITORIAL_REVIEW_SHEET_URL", ""):
            message_without_link = weekly_renderer.render_weekly_briefing(
                date(2026, 8, 24),
                date(2026, 8, 30),
                weekly_editor.WeeklyHeadlines((), None, False),
                self.selection(),
                (),
            )
        self.assertNotIn("선정·미선정 후보 보기", message_without_link.plain_text)
        self.assertNotIn(
            "선정·미선정 후보 보기",
            json.dumps(message_without_link.blocks, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
