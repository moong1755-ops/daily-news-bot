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

    def test_authoritative_single_source_can_be_kept(self):
        articles = [article(
            "Government adopts binding national carbon market regulation",
            "Official Gazette",
            IMPACT,
            "https://government.example/carbon-rule",
            importance=2,
            signals=["policy_or_regulation"],
        )]
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
        })

        applied = agenda.apply_promotions([candidate])

        self.assertEqual(applied, 1)
        self.assertEqual(candidate["category"], IMPACT)
        self.assertEqual(candidate["agenda_original_category"], ALT)
        self.assertEqual(candidate["importance"], 2)
        self.assertEqual(candidate["editor_score"], 7.0)
        self.assertEqual(candidate["category_reason"], "agenda_flow")

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


if __name__ == "__main__":
    unittest.main()
