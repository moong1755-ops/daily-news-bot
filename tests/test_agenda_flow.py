import json
import os
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
        for item in articles:
            item["editor_agenda_key"] = "ai_energy_climate_investment"
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
        self.assertEqual(marked[0]["agenda_target_category"], ALT)
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
        for item in (first, second):
            item["editor_event_key"] = "energy_demand_climate_week"
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

    def test_promotion_is_bounded_and_never_overrides_editor_category(self):
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
        self.assertEqual(candidate["category"], ALT)
        self.assertNotIn("agenda_original_category", candidate)
        self.assertEqual(candidate["importance"], 2)
        self.assertEqual(candidate["editor_score"], 7.0)
        self.assertEqual(agenda.apply_promotions([candidate]), 0)
        self.assertEqual(candidate["editor_score"], 7.0)

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

    def test_climate_article_content_classification_survives_vc_pe_guard(self):
        from src import bot
        from src.processor.summarizer import summarize

        candidate = article(
            "The AI boom took over Climate Week and not everyone is happy about it",
            "TechCrunch",
            ALT,
            score=6,
            importance=1,
        )
        candidate["feed"] = "글로벌 VC/PE"
        candidate["link"] = "https://news.google.com/rss/articles/climate-test"
        candidate, _ = summarize(candidate)
        self.assertFalse(candidate.get("impact_content_verified"))
        verdict = {"verdicts": [{
            "id": 1, "keep": True, "category": IMPACT, "score": 6,
            "reason": "climate_industry_shift", "importance": 1,
            "importance_reason": "industry_shift",
            "event_key": "climate_week_ai_boom_impact",
            "agenda_key": "ai_energy_climate_investment",
            "impact_basis": "climate_transition",
            "impact_evidence": "AI boom took over Climate Week",
        }]}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), patch(
            "src.bot.editor_gate_enabled", return_value=True
        ), patch("src.processor.editor._call_llm", return_value=(json.dumps(verdict), "test")):
            selected, rejected, errors = bot.select_for_briefing([candidate])

        self.assertEqual(errors, [])
        self.assertEqual(rejected, [])
        self.assertEqual(selected, [candidate])
        self.assertEqual(candidate["category"], IMPACT)
        self.assertEqual(candidate["category_reason"], "editor")
        self.assertEqual(candidate["editor_impact_evidence"], "AI boom took over Climate Week")

    def test_agenda_runs_only_after_article_selection_and_dedup(self):
        from src import bot

        candidate = article("Climate investment policy changes", "Reuters")
        candidate.pop("editor_verdict")

        def edit(items):
            items[0]["editor_event_key"] = "climate_investment_policy"
            items[0]["editor_verdict"] = "keep"
            return items, []

        def inspect(items, *, coverage_articles):
            self.assertEqual(items[0]["editor_event_key"], "climate_investment_policy")
            self.assertEqual(coverage_articles, [candidate])
            return []

        with patch("src.bot.editor_gate_enabled", return_value=True), patch(
            "src.bot.editor.review", side_effect=edit
        ), patch("src.bot.agenda.review", side_effect=inspect) as agenda_review:
            selected, _, _ = bot.select_for_briefing([candidate])
            agenda_review.assert_not_called()
            bot.review_final_agenda(selected, list(selected))
        agenda_review.assert_called_once()

    def test_unrelated_series_b_deals_do_not_form_a_topic(self):
        left = article("Alpha raises $100 million Series B for cancer screening", "Reuters")
        right = article("Beta raises $80 million Series B for satellite manufacturing", "Bloomberg")
        self.assertFalse(agenda._same_topic(left, right))
        self.assertEqual(len(agenda.build_topic_cards([left, right])[0]), 2)

    def test_cross_language_topic_uses_editor_meaning_not_words(self):
        left = article("AI power demand threatens grid capacity", "Reuters")
        right = article("데이터센터 전력 수요에 송전망 투자 확대", "한국경제")
        for item in (left, right):
            item["editor_agenda_key"] = "ai_datacenter_grid_constraints"
        cards, _ = agenda.build_topic_cards([left, right])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["independent_source_count"], 2)

    def test_feed_aliases_and_portal_publishers(self):
        from src.processor.deduplicator import _merge_group
        tc1 = article("AI infrastructure financing", "TechCrunch AI", link="https://techcrunch.com/a")
        tc2 = article("AI funding and power demand", "TechCrunch Venture", link="https://techcrunch.com/b")
        merged = _merge_group([tc1, tc2])
        self.assertEqual(agenda._publisher_count([merged]), 1)
        self.assertEqual(len(merged["coverage_sources"]), 2)
        self.assertEqual(merged["coverage_sources"][0]["source"], merged["source"][0])
        yna = article("국내 송전망 변화", "연합뉴스", link="https://n.news.naver.com/a")
        hk = article("데이터센터 전력투자", "한국경제", link="https://n.news.naver.com/b")
        nested = _merge_group([_merge_group([yna, hk]), merged])
        self.assertEqual(agenda._publisher_count([nested]), 3)
        self.assertEqual(len(nested["coverage_sources"]), 4)
        self.assertEqual({r["title"] for r in nested["coverage_sources"]},
                         {a["title"] for a in (tc1, tc2, yna, hk)})

    def test_legacy_merged_feed_aliases_count_once(self):
        merged = article("An AI story", ["TechCrunch AI", "TechCrunch Venture"],
                         link="https://techcrunch.com/a")
        self.assertEqual(agenda._publisher_count([merged]), 1)

    def test_previous_sent_article_is_evidence_not_representative(self):
        from src import bot
        old = article("Helios raises $100 million for AI power grid capacity", "Reuters", score=9, importance=3)
        fresh = article("Grid operators expand investment as AI demand grows", "Bloomberg")
        old["editor_event_key"] = "helios_funding_100m"
        fresh["editor_event_key"] = "grid_operators_capex_increase"
        for item in (old, fresh):
            item["editor_agenda_key"] = "ai_datacenter_grid_constraints"
        with patch("src.bot._load_recent_sent_articles", return_value=[dict(old)]):
            survivors, dropped = bot._filter_recent_editor_event_duplicates([old, fresh])
        self.assertEqual(dropped, [old])
        self.assertEqual(survivors, [fresh])
        response = {"topics": [{"card_ids": ["T1"], "label": "Grid investment",
                                "strength": 3, "basis": "corroborated",
                                "representative_id": "A1", "reason": "capital_shift"}]}
        def desk(prompt, **kwargs):
            self.assertIn(fresh["title"], prompt)
            return json.dumps(response), "test"
        with patch("src.processor.agenda.generate_editor_json", side_effect=desk):
            final, errors = bot.review_final_agenda(survivors, [old, fresh])
        self.assertEqual(final, [fresh])
        self.assertEqual(errors, [])
        self.assertTrue(fresh["agenda_promoted"])
        self.assertEqual(fresh["agenda_source_count"], 2)
        self.assertFalse(old.get("agenda_promoted"))

    def test_removed_semantic_duplicate_cannot_be_a_representative(self):
        removed = article("Original climate report", "Reuters", score=9)
        survivor = article("Follow-up on energy transition", "Bloomberg")
        for item in (removed, survivor):
            item["editor_agenda_key"] = "clean_energy_capital_shift"
        cards, lookup = agenda.build_topic_cards([survivor], coverage_articles=[removed, survivor])
        self.assertEqual(list(lookup.values()), [survivor])
        self.assertEqual(cards[0]["independent_source_count"], 2)
        self.assertIn(removed["title"], cards[0]["coverage_titles"])

    def test_empty_survivors_and_failed_editor_do_not_call_model(self):
        candidate = article("Climate investment shifts", "Reuters")
        with patch("src.processor.agenda.generate_editor_json") as model:
            self.assertEqual(agenda.review([], coverage_articles=[candidate]), [])
            candidate.pop("editor_verdict")
            self.assertEqual(agenda.review([candidate]), [])
        model.assert_not_called()

    def test_model_exception_does_not_stop_briefing(self):
        candidate = article("Climate investment shifts", "Reuters")
        with patch("src.processor.agenda.generate_editor_json", side_effect=TimeoutError):
            errors = agenda.review([candidate])
        self.assertEqual(len(errors), 1)
        self.assertFalse(candidate.get("agenda_promoted"))


if __name__ == "__main__":
    unittest.main()
