import json
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from test_editorial_logic import _install_optional_dependency_stubs

_install_optional_dependency_stubs()
from src import bot
from src.config import IMPACT_DAILY_CONFIG
from src.processor import impact


def article(index, **changes):
    value = {
        "title": f"Impact report {index}: new evidence on social access",
        "description": f"Verified change {index} in access and investment flows.",
        "category": IMPACT_DAILY_CONFIG["category"],
        "editor_verdict": "keep",
        "importance": 3,
        "editor_score": 9,
        "source": "Reuters",
        "link": f"https://reuters.com/report-{index}",
        "editor_event_key": f"event_{index}",
    }
    value.update(changes)
    return value


def decision(card, **changes):
    row = {
        "id": card["id"], "type": "industry", "event_key": card["event_key"],
        "topic": "사회적 접근성 변화", "reason": "투자 환경을 바꾸는 새로운 변화",
        "evidence": card["title"], "extension_reason": "별도로 알아야 할 정책 변화",
    }
    row.update(changes)
    return row


class ImpactFinalTests(unittest.TestCase):
    def review(self, articles, build):
        def respond(prompt, **kwargs):
            cards = json.loads(prompt.split("[후보]\n", 1)[1])
            selected = build(cards)
            selected_ids = {row["id"] for row in selected}
            omitted = [{"ids": [c["id"] for c in cards if c["id"] not in selected_ids],
                        "reason": "선정된 구조적 정책 변화보다 시장 영향이 작음"}]
            return json.dumps({"selected": selected, "omitted": omitted}, ensure_ascii=False), "fixture"
        with patch.object(impact, "generate_editor_json", side_effect=respond):
            return impact.review(articles)

    def test_low_old_rank_can_be_first_and_all_candidates_are_seen(self):
        articles = [article(i) for i in range(40)]
        articles[-1].update(importance=1, editor_score=1)
        def build(cards):
            self.assertEqual(len(cards), 40)
            self.assertNotIn("importance", cards[0])
            self.assertNotIn("editor_score", cards[0])
            chosen = next(c for c in cards if c["event_key"] == "event_39")
            return [decision(chosen), decision(cards[0])]
        self.assertEqual(self.review(articles, build), [])
        with patch.object(bot, "filter_near_duplicates", side_effect=lambda a, _: a):
            selected = bot._select_category_articles(articles, IMPACT_DAILY_CONFIG["category"])
        self.assertEqual(len(selected), 2)
        self.assertIs(selected[0], articles[-1])
        self.assertEqual(articles[-1]["importance"], 1)

    def test_seven_allowed_with_reasons_and_no_extra_fill(self):
        articles = [article(i) for i in range(9)]
        self.assertEqual(self.review(articles, lambda cards: [decision(c) for c in cards[:7]]), [])
        self.assertEqual(len(impact.selected(articles)), 7)

    def test_empty_is_valid_not_fallback(self):
        articles = [article(1)]
        self.assertEqual(self.review(articles, lambda cards: []), [])
        self.assertEqual(bot._select_category_articles(articles, IMPACT_DAILY_CONFIG["category"]), [])

    def test_invalid_output_falls_back_atomically(self):
        cases = {
            "extra_without_reason": lambda c: [decision(x, extension_reason="") for x in c[:6]],
            "eight": lambda c: [decision(x) for x in c],
            "duplicate_id": lambda c: [decision(c[0]), decision(c[0])],
            "duplicate_event": lambda c: [decision(c[0]), decision(c[1], event_key=c[0]["event_key"])],
            "invented_quote": lambda c: [decision(c[0], evidence="this is not in the supplied article")],
            "unknown_id": lambda c: [decision(c[0], id="I999")],
            "wrong_type": lambda c: [decision(c[0], type="advertisement")],
        }
        for name, build in cases.items():
            with self.subTest(name=name):
                articles = [article(i) for i in range(8)]
                self.assertTrue(self.review(articles, build))
                self.assertIsNone(impact.selected(articles))
                self.assertTrue(all(a["impact_final_status"] == "fallback" for a in articles))
                with patch.object(bot, "filter_near_duplicates", side_effect=lambda a, _: a):
                    self.assertEqual(len(bot._select_category_articles(articles, IMPACT_DAILY_CONFIG["category"])), 5)

    def test_unavailable_and_budget_do_not_partially_select(self):
        articles = [article(1)]
        with patch.object(impact, "generate_editor_json", return_value=(None, None)):
            self.assertTrue(impact.review(articles))
        with patch.dict(IMPACT_DAILY_CONFIG, {"max_input_chars": 10}):
            with patch.object(impact, "generate_editor_json") as call:
                self.assertTrue(impact.review(articles))
                call.assert_not_called()

    def test_mbb_other_categories_and_excluded_stories_not_reviewed(self):
        articles = [article(1, category="👔 MBB·Big4 인사이트"),
                    article(2, editor_verdict="reject"), article(3, editorial_excluded=True)]
        with patch.object(impact, "generate_editor_json") as call:
            self.assertEqual(impact.review(articles), [])
            call.assert_not_called()

    def test_labels_both_outputs_preserve_full_title_and_native_bullets(self):
        for kind, display in IMPACT_DAILY_CONFIG["labels"].items():
            item = article(1, impact_type=kind, title="긴 제목" * 200)
            text = bot._format_article_line(item)
            self.assertIn(f"[{display}]", text)
            self.assertIn(item["title"], text)
            rich = bot._slack_list_items([{"article": item}])[0]
            self.assertEqual(rich["elements"][0]["text"], f"[{display}] ")
            self.assertEqual(rich["elements"][1]["text"], item["title"])
            self.assertEqual(rich["elements"][1]["type"], "link")
        self.assertEqual(impact.label(article(1, category="🤖 AI")), "")

    def test_audit_records_final_reason_and_rank(self):
        articles = [article(1)]
        self.review(articles, lambda cards: [decision(cards[0])])
        record = bot._decision_record(articles[0], "not_selected")
        self.assertEqual(record["impact_final_rank"], 1)
        self.assertEqual(record["impact_final_evidence"], articles[0]["title"])

    def test_missing_omission_reason_rejects_partial_review(self):
        articles = [article(1), article(2)]
        cards = impact._cards(articles)
        with patch.object(impact, "generate_editor_json", return_value=(
                json.dumps({"selected": [decision(cards[0])], "omitted": []}), "fixture")):
            self.assertTrue(impact.review(articles))
        self.assertIsNone(impact.selected(articles))

    def test_omission_reasons_are_saved(self):
        articles = [article(1), article(2)]
        self.review(articles, lambda cards: [decision(cards[0])])
        record = bot._decision_record(articles[1], "not_selected")
        self.assertIn("시장 영향", record["impact_final_reason"])

    def test_no_call_or_send_side_effects_in_integration(self):
        articles = [article(1), article(2, category="🤖 AI")]
        with patch.object(bot, "collapse_editor_event_duplicates", side_effect=lambda a, _: a), \
             patch.object(bot.agenda, "review", return_value=[]) as agenda_review, \
             patch.object(bot.agenda, "apply_promotions") as promote, \
             patch.object(impact, "review", return_value=[]) as final_review:
            bot.review_final_agenda(articles, articles)
        self.assertEqual(agenda_review.call_args.args[0], [articles[1]])
        self.assertEqual(promote.call_args.args[0], [articles[1]])
        self.assertEqual(final_review.call_args.args[0], articles)

    def test_seven_article_dry_preview_translates_only_final_selection(self):
        articles = [article(i) for i in range(9)]
        self.review(articles, lambda cards: [decision(c) for c in reversed(cards[:7])])
        output = io.StringIO()
        with patch.object(bot, "is_dry_run", return_value=True), \
             patch.object(bot, "filter_near_duplicates", side_effect=lambda a, _: a), \
             patch.object(bot, "translate_titles") as translate, \
             patch.object(bot.requests, "post") as post, redirect_stdout(output):
            ok, selected = bot.send_aggregated_slack_news(articles)
        self.assertTrue(ok)
        self.assertEqual(len(selected), 7)
        self.assertIs(selected[0], articles[6])
        translate.assert_called_once_with(selected)
        post.assert_not_called()
        self.assertEqual(output.getvalue().count("[산업]"), 7)
        for item in selected:
            self.assertIn(item["title"], output.getvalue())

    def test_original_event_duplicate_cannot_be_hidden_with_new_keys(self):
        articles = [article(1), article(2, editor_event_key="event_1")]
        self.assertTrue(self.review(articles, lambda cards: [
            decision(cards[0], event_key="new_key_a"),
            decision(cards[1], event_key="new_key_b"),
        ]))


if __name__ == "__main__":
    unittest.main()
