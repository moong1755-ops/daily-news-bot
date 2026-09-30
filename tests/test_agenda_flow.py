import json
import unittest
from unittest.mock import patch

from src.processor import agenda


IMPACT = "🌱 임팩트"
AI = "🤖 AI"
ALT = "📈 대체투자"
MACRO = "🌐 거시·정책·지정학"
INSIGHTS = "👔 MBB·Big4 인사이트"


def article(
    title,
    source,
    category=IMPACT,
    link="",
    score=6,
    importance=1,
    signals=None,
):
    slug = re_slug(title)
    return {
        "title": title,
        "title_orig": title,
        "source": source,
        "feed": source,
        "link": link or f"https://{source.casefold().replace(' ', '')}.example/{slug}",
        "category": category,
        "relevance": score,
        "editor_score": score,
        "importance": importance,
        "editorial_signals": signals or ["market_or_industry_shift"],
        "editor_verdict": "keep",
    }


def re_slug(value):
    return "-".join(value.casefold().split())[:80]


class AgendaFlowTests(unittest.TestCase):
    def test_mbb_big4_is_not_sent_to_agenda_desk(self):
        articles = [
            article("Climate Week AI energy demand reshapes investment", "Reuters", IMPACT),
            article("Global private markets outlook 2027", "McKinsey", INSIGHTS),
        ]

        cards, lookup = agenda.build_topic_cards(articles)

        self.assertEqual(len(cards), 1)
        self.assertEqual({item["category"] for item in cards[0]["titles"]}, {IMPACT})
        self.assertEqual(len(lookup), 1)

    def test_cross_source_topic_marks_one_representative(self):
        articles = [
            article(
                "Climate Week AI energy demand reshapes climate investment",
                "Reuters",
                ALT,
                "https://reuters.com/climate-week-ai-energy",
                score=7,
            ),
            article(
                "Climate Week debate focuses on AI energy demand",
                "TechCrunch",
                IMPACT,
                "https://techcrunch.com/climate-week-ai-energy",
                score=6,
            ),
        ]
        articles[0]["impact_content_verified"] = True
        response = {
            "topics": [{
                "card_ids": ["T1"],
                "label": "AI 에너지 수요와 기후투자 재편",
                "strength": 3,
                "basis": "corroborated",
                "target_category": IMPACT,
                "representative_id": "A1",
                "reason": "cross_source_industry_shift",
            }]
        }

        with patch(
            "src.processor.agenda.generate_editor_json",
            return_value=(json.dumps(response, ensure_ascii=False), "test-model"),
        ):
            errors = agenda.review(articles)

        self.assertEqual(errors, [])
        marked = [item for item in articles if item.get("agenda_strength") == 3]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["agenda_target_category"], IMPACT)
        self.assertEqual(marked[0]["agenda_source_count"], 2)

    def test_same_source_repetition_is_not_corroboration(self):
        articles = [
            article(
                "Climate Week AI energy demand reshapes investment",
                "Newswire",
                link="https://wire.example/one",
            ),
            article(
                "Climate Week debate focuses on AI energy demand",
                "Newswire copy",
                link="https://wire.example/two",
            ),
        ]
        response = {
            "topics": [{
                "card_ids": ["T1"],
                "label": "AI 에너지 수요",
                "strength": 3,
                "basis": "corroborated",
                "target_category": IMPACT,
                "representative_id": "A1",
                "reason": "repeated_copy",
            }]
        }

        with patch(
            "src.processor.agenda.generate_editor_json",
            return_value=(json.dumps(response, ensure_ascii=False), "test-model"),
        ):
            agenda.review(articles)

        self.assertFalse(any(item.get("agenda_strength") for item in articles))

    def test_merged_publishers_are_counted_even_with_google_links(self):
        merged = article("Climate Week energy demand reshapes investment", "Reuters")
        merged.update({
            "source": ["Reuters", "Bloomberg", "TechCrunch"],
            "link": [
                "https://news.google.com/rss/articles/one",
                "https://news.google.com/rss/articles/two",
                "https://techcrunch.com/climate-week",
            ],
            "duplicate_titles": [
                "Climate Week energy demand reshapes investment",
                "Climate Week brings new energy investment focus",
            ],
        })
        cards, _ = agenda.build_topic_cards([merged])
        self.assertEqual(cards[0]["independent_source_count"], 3)

    def test_syndicated_reuters_story_is_one_publisher(self):
        original = article("Climate finance regulation changes", "Reuters",
                           link="https://reuters.com/climate-regulation")
        syndicated = article("Climate finance rules change", "Reuters",
                             link="https://finance.yahoo.com/climate-regulation")
        self.assertEqual(agenda._publisher_count([original, syndicated]), 1)

    def test_identical_syndicated_headlines_cannot_raise_agenda(self):
        first = article("Climate Week energy demand reshapes investment", "Reuters")
        second = article("Climate Week energy demand reshapes investment", "Bloomberg")
        response = {"topics": [{
            "card_ids": ["T1"], "label": "Energy demand", "strength": 3,
            "basis": "corroborated", "target_category": IMPACT,
            "representative_id": "A1", "reason": "same_headline",
        }]}
        with patch("src.processor.agenda.generate_editor_json",
                   return_value=(json.dumps(response), "test-model")):
            agenda.review([first, second])
        self.assertFalse(first.get("agenda_strength"))

    def test_authoritative_single_source_can_be_kept(self):
        articles = [article(
            "Government adopts binding national carbon market regulation",
            "Official Gazette",
            IMPACT,
            "https://government.example/carbon-rule",
            importance=2,
            signals=["policy_or_regulation"],
        )]
        articles[0].update({
            "event_status": "confirmed",
            "reporting_basis": "official_announcement",
        })
        response = {
            "topics": [{
                "card_ids": ["T1"],
                "label": "국가 탄소시장 규제 도입",
                "strength": 3,
                "basis": "authoritative_single",
                "target_category": IMPACT,
                "representative_id": "A1",
                "reason": "binding_policy_change",
            }]
        }

        with patch(
            "src.processor.agenda.generate_editor_json",
            return_value=(json.dumps(response, ensure_ascii=False), "test-model"),
        ):
            agenda.review(articles)

        self.assertEqual(articles[0]["agenda_strength"], 3)
        self.assertEqual(articles[0]["agenda_basis"], "authoritative_single")

    def test_unconfirmed_single_source_cannot_claim_authority(self):
        candidate = article("Company may revise climate plans", "PR Site")
        candidate.update({"event_status": "outlook", "reporting_basis": "analysis"})
        response = {"topics": [{
            "card_ids": ["T1"], "label": "Climate plans", "strength": 3,
            "basis": "authoritative_single", "target_category": IMPACT,
            "representative_id": "A1", "reason": "unsupported",
        }]}
        with patch("src.processor.agenda.generate_editor_json",
                   return_value=(json.dumps(response), "test-model")):
            agenda.review([candidate])
        self.assertFalse(candidate.get("agenda_strength"))

    def test_promotion_is_bounded_and_can_fix_category(self):
        candidate = article(
            "The AI boom reshapes Climate Week investment agenda",
            "TechCrunch",
            ALT,
            score=6,
            importance=1,
        )
        candidate.update({
            "agenda_strength": 3,
            "agenda_target_category": IMPACT,
            "impact_content_verified": True,
        })

        applied = agenda.apply_promotions([candidate])

        self.assertEqual(applied, 1)
        self.assertEqual(candidate["category"], IMPACT)
        self.assertEqual(candidate["agenda_original_category"], ALT)
        self.assertEqual(candidate["importance"], 2)
        self.assertEqual(candidate["editor_score"], 7.0)
        self.assertEqual(candidate["category_reason"], "agenda_flow")

    def test_moderate_agenda_never_creates_top_importance(self):
        candidate = article("Climate investment changes", "Reuters", importance=2)
        candidate.update({"agenda_strength": 2, "agenda_target_category": IMPACT})
        agenda.apply_promotions([candidate])
        self.assertEqual(candidate["importance"], 2)

    def test_unverified_impact_override_is_blocked(self):
        candidate = article("Generic healthcare software deal", "TechCrunch", ALT)
        candidate.update({"agenda_strength": 3, "agenda_target_category": IMPACT})
        agenda.apply_promotions([candidate])
        self.assertEqual(candidate["category"], ALT)

    def test_mbb_and_unreviewed_articles_cannot_be_promoted(self):
        mbb = article("Global climate outlook", "BCG", INSIGHTS)
        unreviewed = article("AI infrastructure investment expands", "Reuters", AI)
        for item in (mbb, unreviewed):
            item.update({"agenda_strength": 3, "agenda_target_category": IMPACT})
        unreviewed["editor_verdict"] = "unreviewed"

        applied = agenda.apply_promotions([mbb, unreviewed])

        self.assertEqual(applied, 0)
        self.assertEqual(mbb["category"], INSIGHTS)
        self.assertEqual(unreviewed["category"], AI)

    def test_model_failure_preserves_existing_articles(self):
        candidate = article("AI infrastructure investment expands", "Reuters", AI)
        before = dict(candidate)

        with patch(
            "src.processor.agenda.generate_editor_json",
            return_value=(None, None),
        ):
            errors = agenda.review([candidate])

        self.assertEqual(errors, [])
        self.assertEqual(candidate, before)

    def test_climate_agenda_reroute_survives_final_vc_pe_guard(self):
        from src import bot

        candidate = article(
            "The AI boom reshapes Climate Week investment agenda",
            "TechCrunch",
            ALT,
            score=6,
            importance=1,
        )
        candidate["impact_content_verified"] = True

        def mark_agenda(items):
            items[0].update({
                "agenda_strength": 3,
                "agenda_target_category": IMPACT,
                "agenda_topic": "AI 에너지 수요와 기후투자 재편",
            })
            return []

        with patch("src.bot.agenda.review", side_effect=mark_agenda), patch(
            "src.bot.editor_gate_enabled", return_value=True
        ), patch("src.bot.editor.review", return_value=([candidate], [])):
            selected, rejected, errors = bot.select_for_briefing([candidate])

        self.assertEqual(errors, [])
        self.assertEqual(rejected, [])
        self.assertEqual(selected, [candidate])
        self.assertEqual(candidate["category"], IMPACT)
        self.assertEqual(candidate["category_reason"], "agenda_flow")

    def test_agenda_sees_article_editors_final_event_key(self):
        from src import bot

        candidate = article("Climate investment policy changes", "Reuters")
        candidate.pop("editor_verdict")

        def edit(items):
            items[0]["editor_event_key"] = "climate_investment_policy"
            items[0]["editor_verdict"] = "keep"
            return items, []

        def inspect(items):
            self.assertEqual(items[0]["editor_event_key"], "climate_investment_policy")
            return []

        with patch("src.bot.editor_gate_enabled", return_value=True), patch(
            "src.bot.editor.review", side_effect=edit
        ), patch("src.bot.agenda.review", side_effect=inspect) as agenda_review:
            bot.select_for_briefing([candidate])
        agenda_review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
