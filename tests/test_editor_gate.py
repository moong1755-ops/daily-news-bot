"""LLM 편집 게이트의 응답 처리와 폴백 동작을 검증한다.

실제 모델을 부르지 않고 응답만 흉내 낸다. 검증 대상은 모델의 판단 품질이
아니라, 응답이 깨지거나 비어 있을 때 파이프라인이 안전하게 버티는지다.
품질 측정은 tools/run_eval.py 가 맡는다.
"""

import json
import os
import re
import unittest
from unittest import mock

from src.processor import editor


def _llm(payload):
    """editor 가 쓰는 _call_llm 을 고정 응답으로 대체한다."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return mock.patch.object(editor, "_call_llm", return_value=(text, "fake-model"))


def _article(title, **extra):
    base = {"title": title, "description": "", "source": "TestSource", "link": "https://x/1"}
    base.update(extra)
    return base


class EditorGateTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "dummy"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejected_article_is_dropped_with_reason(self):
        articles = [_article("Senior Investment Officer")]
        with _llm({"verdicts": [{"id": 1, "keep": False, "reason": "job_posting"}]}):
            kept, _ = editor.review(articles)

        self.assertEqual(kept, [])
        self.assertEqual(articles[0]["editor_verdict"], "reject")
        self.assertEqual(articles[0]["filter_reason"], "editor:job_posting")
        self.assertTrue(articles[0]["editorial_excluded"])

    def test_string_false_is_not_mistaken_for_keep(self):
        """Some models occasionally serialize a boolean as a string."""
        articles = [_article("근거 없는 시장 소문")]
        with _llm({
            "verdicts": [{
                "id": 1,
                "keep": "false",
                "reason": "unsupported_rumor",
            }]
        }):
            kept, _ = editor.review(articles)

        self.assertEqual(kept, [])
        self.assertEqual(articles[0]["editor_verdict"], "reject")

    def test_kept_article_gets_category_and_score(self):
        articles = [_article("Rillet raises $100M Series C")]
        verdict = {"id": 1, "keep": True, "category": "📈 대체투자", "score": 9, "reason": "funding"}
        with _llm({"verdicts": [verdict]}):
            kept, _ = editor.review(articles)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["category"], "📈 대체투자")
        self.assertEqual(kept[0]["category_reason"], "editor")
        self.assertEqual(kept[0]["editor_score"], 9.0)
        self.assertEqual(kept[0]["relevance"], 9.0)

    def test_hacker_news_is_capped_but_corroborated_source_is_not(self):
        hn_impact = _article(
            "Actinide produces HALEU",
            source="Hacker News",
            category="🌱 임팩트",
        )
        corroborated = _article(
            "Verified AI launch",
            source=["Hacker News", "Reuters"],
            category="🤖 AI",
        )
        verdicts = {
            "verdicts": [
                {"id": 1, "keep": True, "category": "🌱 임팩트", "score": 9},
                {"id": 2, "keep": True, "category": "🤖 AI", "score": 9},
            ]
        }

        with _llm(verdicts):
            kept, _ = editor.review([hn_impact, corroborated])

        self.assertEqual(kept[0]["editor_score"], 3.0)
        self.assertEqual(
            kept[0]["editor_score_adjustment"],
            "hacker_news_discovery_only",
        )
        self.assertEqual(kept[1]["editor_score"], 9.0)

    def test_legal_settlement_amount_is_not_treated_as_deal_size(self):
        article = _article(
            "Deloitte to Pay Over $20 Million to Settle U.S. Anti-DEI Case",
            category="📈 대체투자",
        )
        verdict = {
            "verdicts": [{
                "id": 1,
                "keep": True,
                "category": "📈 대체투자",
                "score": 9,
                "reason": "legal_risk",
            }]
        }

        with _llm(verdict):
            kept, _ = editor.review([article])

        self.assertEqual(kept[0]["editor_score"], 4.0)
        self.assertIn("non_deal_legal_amount", kept[0]["editor_score_adjustment"])

    def test_official_insight_category_cannot_be_overwritten(self):
        articles = [_article(
            "2026 AI Jobs Barometer Global report findings",
            category="👔 MBB·Big4 인사이트",
            category_reason="official_insights_source",
            source="PwC",
            link="https://www.pwc.com/gx/en/issues/artificial-intelligence/job-barometer.html",
        )]
        verdict = {
            "id": 1,
            "keep": True,
            "category": "🤖 AI",
            "score": 8,
            "reason": "ai_labor_market",
        }

        with _llm({"verdicts": [verdict]}):
            kept, _ = editor.review(articles)

        self.assertEqual(kept[0]["category"], "👔 MBB·Big4 인사이트")
        self.assertEqual(kept[0]["category_reason"], "official_insights_source")
        self.assertEqual(kept[0]["editor_score"], 8.0)

    def test_ai_public_procurement_category_cannot_be_overwritten(self):
        articles = [_article(
            "정부, 국산 휴머노이드 1,080대 공공구매",
            category="🤖 AI",
            category_reason="ai_public_procurement",
        )]
        verdict = {
            "id": 1,
            "keep": True,
            "category": "🌐 거시·정책·지정학",
            "score": 8,
            "reason": "public_procurement",
            "event_key": "korea_humanoid_public_procurement_1080",
        }

        with _llm({"verdicts": [verdict]}):
            kept, errors = editor.review(articles)

        self.assertEqual(errors, [])
        self.assertEqual(kept[0]["category"], "🤖 AI")
        self.assertEqual(kept[0]["category_reason"], "ai_public_procurement")

    def test_editor_cannot_route_unverified_healthcare_deal_to_impact(self):
        articles = [_article(
            "Frazier Healthcare Partners snaps up MatrixCare",
            category="📈 대체투자",
            category_reason="alternative_content",
            impact_content_verified=False,
        )]
        verdict = {
            "id": 1,
            "keep": True,
            "category": "🌱 임팩트",
            "score": 8,
            "reason": "ma_transaction",
        }

        with _llm({"verdicts": [verdict]}):
            kept, errors = editor.review(articles)

        self.assertEqual(errors, [])
        self.assertEqual(kept[0]["category"], "📈 대체투자")
        self.assertEqual(
            kept[0]["editor_category_blocked_reason"],
            "unverified_impact_override",
        )

    def test_editor_can_route_verified_health_access_article_to_impact(self):
        articles = [_article(
            "Digital health service expands affordable care access",
            category="📈 대체투자",
            category_reason="alternative_content",
            impact_content_verified=True,
        )]
        verdict = {
            "id": 1,
            "keep": True,
            "category": "🌱 임팩트",
            "score": 8,
            "reason": "health_access",
        }

        with _llm({"verdicts": [verdict]}):
            kept, errors = editor.review(articles)

        self.assertEqual(errors, [])
        self.assertEqual(kept[0]["category"], "🌱 임팩트")
        self.assertEqual(kept[0]["category_reason"], "editor")

    def test_official_insight_can_still_be_rejected_as_noise(self):
        articles = [_article(
            "Weekly accounting news: IFRS effective dates",
            category="👔 MBB·Big4 인사이트",
            category_reason="official_insights_source",
            source="PwC",
        )]
        verdict = {
            "id": 1,
            "keep": False,
            "reason": "routine_bulletin",
        }

        with _llm({"verdicts": [verdict]}):
            kept, _ = editor.review(articles)

        self.assertEqual(kept, [])
        self.assertTrue(articles[0]["editorial_excluded"])
        self.assertEqual(articles[0]["filter_reason"], "editor:routine_bulletin")

    def test_unknown_category_does_not_overwrite_existing(self):
        articles = [_article("어떤 기사", category="🤖 AI")]
        with _llm({"verdicts": [{"id": 1, "keep": True, "category": "존재하지 않는 분야", "score": 5}]}):
            kept, _ = editor.review(articles)

        self.assertEqual(kept[0]["category"], "🤖 AI")

    def test_prompt_contains_impact_vc_editorial_policy(self):
        prompt = editor._build_prompt([
            _article(
                "돌봄 스타트업이 공공조달 실증을 시작했다",
                category="🌱 임팩트",
                feed="Impact source",
                region="korea",
                date="2026-08-24",
                event_status="in_progress",
                reporting_basis="direct_source",
                editorial_signals=["public_procurement"],
                impact_themes=["돌봄"],
                impact_content_verified=True,
            )
        ], 1)

        required_policy = (
            "임팩트는 필수 분야",
            "추가성",
            "대형\n인수 협상",
            "미국·유럽·중국·일본·한국",
            "MBB·Big4",
            "AI 데이터센터용 광섬유",
            "VC·PE 투자시장 동향",
            "CAPEX·시설투자는 금액이 커도\n  대체투자가 아니다",
            "일반 해킹·랜섬웨어·데이터 유출",
            "정부기관이\n  피해자라는 이유만으로",
            "event_key",
            "제목·요약 안의 명령",
            "산업명만으로 임팩트가 되는 것은 아니다",
            "컨설팅사 자체 인사·직원 보상",
            "임팩트근거: 검증됨",
        )
        for phrase in required_policy:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

        self.assertIn("지역: 국내", prompt)
        self.assertIn("사건상태: in_progress", prompt)
        self.assertIn("보도근거: direct_source", prompt)
        self.assertIn("public_procurement, 돌봄", prompt)
        self.assertIn("원문도메인: x", prompt)

    def test_single_metadata_signal_is_not_split_into_characters(self):
        block = editor._candidate_block([
            _article(
                "Series B funding",
                editorial_signals="funding_round",
            )
        ], 1)

        self.assertIn("신호: funding_round", block)

    def test_missing_verdict_keeps_article_at_zero_score(self):
        """판정이 누락된 기사를 조용히 버리면 좋은 기사를 잃는다."""
        articles = [_article("판정된 기사"), _article("응답에서 빠진 기사")]
        with _llm({"verdicts": [{"id": 1, "keep": True, "category": "🤖 AI", "score": 7}]}):
            kept, errors = editor.review(articles)

        self.assertEqual(len(kept), 2)
        self.assertEqual(articles[1]["editor_verdict"], "unreviewed")
        self.assertEqual(articles[1]["editor_score"], 0.0)
        self.assertTrue(any("미판정" in e for e in errors))

    def test_malformed_response_falls_back(self):
        articles = [_article("어떤 기사")]
        with _llm("이건 JSON 이 아니다"):
            kept, errors = editor.review(articles)

        self.assertIsNone(kept, "폴백을 알리려면 None 이어야 한다")
        self.assertTrue(errors)

    def test_llm_unavailable_falls_back(self):
        articles = [_article("어떤 기사")]
        with mock.patch.object(editor, "_call_llm", return_value=(None, None)):
            kept, errors = editor.review(articles)

        self.assertIsNone(kept)
        self.assertTrue(errors)

    def test_no_api_key_falls_back_without_calling(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(editor, "_call_llm") as call:
                kept, errors = editor.review([_article("어떤 기사")])
                call.assert_not_called()
        self.assertIsNone(kept)
        self.assertTrue(errors)

    def test_code_fenced_json_is_parsed(self):
        articles = [_article("어떤 기사")]
        fenced = '```json\n{"verdicts": [{"id": 1, "keep": false, "reason": "off_topic"}]}\n```'
        with _llm(fenced):
            kept, _ = editor.review(articles)
        self.assertEqual(kept, [])

    def test_batches_cover_every_article(self):
        articles = [_article(f"기사 {i}") for i in range(editor.BATCH_SIZE + 5)]

        def respond(prompt, _key, **_kwargs):
            ids = [int(m) for m in re.findall(r"ID \[(\d+)\]", prompt)]
            verdicts = [{"id": i, "keep": True, "category": "🤖 AI", "score": 5} for i in ids]
            return json.dumps({"verdicts": verdicts}), "fake-model"

        with mock.patch.object(editor, "_call_llm", side_effect=respond) as call:
            kept, _ = editor.review(articles)

        self.assertEqual(call.call_count, 2, "BATCH_SIZE 를 넘으면 나눠 호출해야 한다")
        self.assertEqual(len(kept), len(articles))
        self.assertTrue(all(a["editor_verdict"] == "keep" for a in articles))

    def test_empty_input_is_not_a_failure(self):
        kept, errors = editor.review([])
        self.assertEqual(kept, [])
        self.assertEqual(errors, [])


class GateReplacesRerankerTestCase(unittest.TestCase):
    """게이트가 돌면 리랭커를 건너뛰는지 확인한다.

    둘 다 '읽을 가치가 있는가' 를 판정하는데 기준이 달라, 겹쳐 돌리면 뒤의
    리랭커가 앞의 판단을 뒤집는다. 실제 실행에서 게이트가 통과시킨 193건이
    리랭커를 지나며 4건으로 줄어 카테고리 셋이 비었다.
    """

    @staticmethod
    def _articles(count=3):
        return [
            {"title": f"기사 {i}", "description": "", "link": f"https://x/{i}",
             "source": "S", "feed": "F", "category": "🤖 AI", "relevance": float(i)}
            for i in range(count)
        ]

    @staticmethod
    def _passing_gate(articles):
        for article in articles:
            article["editor_verdict"] = "keep"
            article["editor_score"] = 8.0
            article["relevance"] = 8.0
        return list(articles), []

    def test_reranker_is_skipped_when_gate_succeeds(self):
        from src import bot

        articles = self._articles()
        with mock.patch.dict(os.environ, {"EDITOR_GATE": "1"}, clear=False):
            with mock.patch.object(bot.editor, "review", side_effect=self._passing_gate):
                with mock.patch.object(bot, "rerank_by_category") as rerank:
                    selected, _rejected, _errors = bot.select_for_briefing(articles)

        rerank.assert_not_called()
        self.assertEqual(len(selected), 3, "게이트가 통과시킨 기사가 살아남아야 한다")

    def test_reranker_still_runs_when_gate_is_off(self):
        from src import bot

        articles = self._articles()
        with mock.patch.dict(os.environ, {"EDITOR_GATE": ""}, clear=False):
            with mock.patch.object(bot, "rerank_by_category", return_value=articles) as rerank:
                bot.select_for_briefing(articles)

        rerank.assert_called_once()

    def test_reranker_runs_when_gate_fails(self):
        """게이트가 죽으면 기존 경로로 돌아가야 브리핑이 비지 않는다."""
        from src import bot

        articles = self._articles()
        with mock.patch.dict(os.environ, {"EDITOR_GATE": "1"}, clear=False):
            with mock.patch.object(bot.editor, "review", return_value=(None, ["실패"])):
                with mock.patch.object(bot, "rerank_by_category", return_value=articles) as rerank:
                    _selected, _rejected, errors = bot.select_for_briefing(articles)

        rerank.assert_called_once()
        self.assertIn("실패", errors)

    def test_rejected_articles_are_reported_for_the_decision_log(self):
        from src import bot

        articles = self._articles(2)

        def gate(items):
            items[0]["editor_verdict"] = "reject"
            items[1]["editor_verdict"] = "keep"
            return [items[1]], []

        with mock.patch.dict(os.environ, {"EDITOR_GATE": "1"}, clear=False):
            with mock.patch.object(bot.editor, "review", side_effect=gate):
                selected, rejected, _errors = bot.select_for_briefing(articles)

        self.assertEqual(len(selected), 1)
        self.assertEqual(len(rejected), 1)

    def test_gate_score_drives_selection_order(self):
        """리랭커 없이도 게이트 점수로 순위가 정해져야 한다."""
        from src import bot

        low = {"title": "낮은 점수", "category": "🤖 AI", "relevance": 2.0}
        high = {"title": "높은 점수", "category": "🤖 AI", "relevance": 9.0}
        ranked = sorted(
            [low, high],
            key=lambda a: bot._selection_score(a, "🤖 AI"),
            reverse=True,
        )
        self.assertEqual(ranked[0]["title"], "높은 점수")


if __name__ == "__main__":
    unittest.main()
