"""한 카테고리가 같은 사건으로 채워지지 않는지 검증한다.

실제 실행에서 거시 카테고리 세 칸이 전부 같은 FOMC 의사록 기사였다.
제목 임베딩 유사도는 0.62~0.76 이라 수집 단계 임계값 0.72 로는 한 쌍만 걸렸다.
"""

import unittest
from unittest import mock

from src.processor import deduplicator


class FilterNearDuplicatesTestCase(unittest.TestCase):
    FED = [
        {"title": "Several Fed officials wanted to raise rates last month"},
        {"title": "Fed Minutes Show ‘Many’ Officials Prefer Hiking Rates"},
        {"title": "Fed Minutes Reveal Broader Support for Rate Increases"},
    ]

    def _with_similarity(self, matrix):
        """임베딩 계산을 고정 유사도 행렬로 대체한다(모델 다운로드 없이)."""
        return mock.patch.multiple(
            deduplicator,
            _get_model=mock.Mock(return_value=mock.Mock(encode=mock.Mock(return_value=[[0.0]] * len(matrix)))),
            cosine_similarity=mock.Mock(return_value=matrix),
        )

    def test_same_event_collapses_to_one(self):
        # 실측값: 1-2 = 0.76, 1-3 = 0.62, 2-3 = 0.59
        matrix = [[1.00, 0.76, 0.62],
                  [0.76, 1.00, 0.59],
                  [0.62, 0.59, 1.00]]
        with self._with_similarity(matrix):
            kept = deduplicator.filter_near_duplicates(self.FED, 0.60)
        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], self.FED[0], "가장 앞선(점수 높은) 기사가 남아야 한다")

    def test_unrelated_articles_survive(self):
        articles = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        # 실측 무관 기사 유사도는 0.34 이하
        matrix = [[1.00, 0.34, 0.12],
                  [0.34, 1.00, 0.07],
                  [0.12, 0.07, 1.00]]
        with self._with_similarity(matrix):
            kept = deduplicator.filter_near_duplicates(articles, 0.60)
        self.assertEqual(len(kept), 3)

    def test_order_is_preserved(self):
        articles = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        matrix = [[1.0, 0.1, 0.1], [0.1, 1.0, 0.1], [0.1, 0.1, 1.0]]
        with self._with_similarity(matrix):
            kept = deduplicator.filter_near_duplicates(articles, 0.60)
        self.assertEqual([a["title"] for a in kept], ["A", "B", "C"])

    def test_editor_event_key_collapses_cross_language_titles(self):
        articles = [
            {
                "title": "Stability AI raises $76 million in fresh funding",
                "editor_event_key": "stability_ai_funding_76m",
            },
            {
                "title": "스태빌리티AI, 7,600만 달러 자금 조달했다",
                "editor_event_key": "stability_ai_funding_76m",
            },
        ]
        with self._with_similarity([[1.0, 0.1], [0.1, 1.0]]):
            kept = deduplicator.filter_near_duplicates(articles, 0.60)
        self.assertEqual(kept, [articles[0]])

    def test_different_editor_event_keys_keep_distinct_company_events(self):
        articles = [
            {
                "title": "Stability AI raises $76 million",
                "editor_event_key": "stability_ai_funding_76m",
            },
            {
                "title": "Stability AI launches a new image model",
                "editor_event_key": "stability_ai_model_launch_2026_08",
            },
        ]
        with self._with_similarity([[1.0, 0.1], [0.1, 1.0]]):
            kept = deduplicator.filter_near_duplicates(articles, 0.60)
        self.assertEqual(kept, articles)

    def test_event_key_prefers_verified_original_over_domestic_repost(self):
        domestic_repost = {
            "title": "스태빌리티AI, 7,600만 달러 자금 조달했다",
            "editor_event_key": "stability_ai_funding_76m",
            "feed": "국내 스타트업레시피 VC",
            "source": "스타트업레시피",
            "link": "https://startuprecipe.co.kr/example",
        }
        overseas_original = {
            "title": "Stability AI raises $76 million in fresh funding",
            "editor_event_key": "stability_ai_funding_76m",
            "feed": "TechCrunch AI",
            "source": "TechCrunch AI",
            "link": "https://techcrunch.com/example",
        }
        articles = [domestic_repost, overseas_original]

        with self._with_similarity([[1.0, 0.1], [0.1, 1.0]]):
            kept = deduplicator.filter_near_duplicates(articles, 0.60)

        self.assertEqual(kept, [overseas_original])

    def test_cross_category_event_keeps_original_in_impact(self):
        domestic_impact = {
            "title": "CIX와 Carbonplace 합병",
            "editor_event_key": "cix_carbonplace_merger",
            "category": "🌱 임팩트",
            "category_reason": "editor",
            "relevance": 9,
            "feed": "ImpactOn (임팩트온)",
            "source": "임팩트온",
            "link": "https://impacton.net/example",
            "region": "global",
        }
        overseas_original = {
            "title": "Climate Impact X and Carbonplace to merge",
            "editor_event_key": "climate_impact_x_carbonplace_merge",
            "category": "📈 대체투자",
            "category_reason": "editor",
            "relevance": 8,
            "feed": "글로벌 임팩트 주요 사건",
            "source": "ESG Today",
            "link": "https://www.esgtoday.com/example",
            "region": "global",
        }

        collapsed = deduplicator.collapse_editor_event_duplicates(
            [domestic_impact, overseas_original],
            {"🌱 임팩트": 50, "📈 대체투자": 30},
        )

        self.assertEqual(collapsed, [overseas_original])
        self.assertEqual(collapsed[0]["category"], "🌱 임팩트")
        self.assertEqual(collapsed[0]["relevance"], 9)
        self.assertEqual(collapsed[0]["source"], "ESG Today")

    def test_anthropic_pentagon_ruling_aliases_collapse_to_one_event(self):
        reuters = {
            "title": "US court blocks Pentagon blacklisting of Anthropic",
            "description": "The court ruled on the Pentagon supply-chain risk designation.",
            "editor_event_key": "anthropic_pentagon_blacklist_ruling",
            "category": "🤖 AI",
            "category_reason": "editor",
            "relevance": 9,
            "feed": "Reuters 거시/정책",
            "source": "Reuters",
            "link": "https://www.reuters.com/example",
            "date": "2026-08-30",
        }
        techcrunch = {
            "title": "Anthropic wins first ruling over Pentagon supply-chain risk label",
            "description": "A judge blocked the same Pentagon designation.",
            "editor_event_key": "anthropic_pentagon_supply_chain_risk_ruling",
            "category": "🤖 AI",
            "category_reason": "editor",
            "relevance": 8,
            "feed": "TechCrunch AI",
            "source": "TechCrunch AI",
            "link": "https://techcrunch.com/example",
            "date": "2026-08-30",
        }

        collapsed = deduplicator.collapse_editor_event_duplicates(
            [reuters, techcrunch],
            {"🤖 AI": 40},
        )

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["source"], "Reuters")
        self.assertEqual(collapsed[0]["relevance"], 9)

    def test_same_company_different_counterparty_events_stay_separate(self):
        cursor = {
            "title": "OpenAI ends model supply to Cursor",
            "editor_event_key": "openai_cursor_model_supply_stop",
            "category": "🤖 AI",
            "date": "2026-08-30",
        }
        microsoft = {
            "title": "OpenAI changes model supply agreement with Microsoft",
            "editor_event_key": "openai_microsoft_model_supply_stop",
            "category": "🤖 AI",
            "date": "2026-08-30",
        }

        collapsed = deduplicator.collapse_editor_event_duplicates(
            [cursor, microsoft],
            {"🤖 AI": 40},
        )

        self.assertEqual(collapsed, [cursor, microsoft])

    def test_model_failure_does_not_drop_articles(self):
        """중복 검사 실패가 발송을 막아서는 안 된다."""
        with mock.patch.object(deduplicator, "_get_model", side_effect=OSError("모델 없음")):
            kept = deduplicator.filter_near_duplicates(self.FED, 0.60)
        self.assertEqual(len(kept), 3)

    def test_short_input_skips_the_model(self):
        with mock.patch.object(deduplicator, "_get_model") as model:
            self.assertEqual(deduplicator.filter_near_duplicates([], 0.6), [])
            one = [{"title": "혼자"}]
            self.assertEqual(deduplicator.filter_near_duplicates(one, 0.6), one)
            model.assert_not_called()


class ThresholdSeparationTestCase(unittest.TestCase):
    def test_selection_threshold_is_looser_than_ingest(self):
        """수집 병합은 기사를 없애므로 보수적, 선정 제외는 자리만 비우므로 공격적."""
        from src.config import SELECTION_SIMILARITY_THRESHOLD, SIMILARITY_THRESHOLD

        self.assertLess(SELECTION_SIMILARITY_THRESHOLD, SIMILARITY_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
