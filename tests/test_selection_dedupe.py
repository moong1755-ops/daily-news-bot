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
