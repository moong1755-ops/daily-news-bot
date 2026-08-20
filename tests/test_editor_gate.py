"""LLM 편집 게이트의 응답 처리와 폴백 동작을 검증한다.

실제 모델을 부르지 않고 응답만 흉내 낸다. 검증 대상은 모델의 판단 품질이
아니라, 응답이 깨지거나 비어 있을 때 파이프라인이 안전하게 버티는지다.
품질 측정은 tools/run_eval.py 가 맡는다.
"""

import json
import os
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

    def test_unknown_category_does_not_overwrite_existing(self):
        articles = [_article("어떤 기사", category="🤖 AI")]
        with _llm({"verdicts": [{"id": 1, "keep": True, "category": "존재하지 않는 분야", "score": 5}]}):
            kept, _ = editor.review(articles)

        self.assertEqual(kept[0]["category"], "🤖 AI")

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

        def respond(prompt, _key):
            ids = [int(m) for m in __import__("re").findall(r"ID \[(\d+)\]", prompt)]
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


if __name__ == "__main__":
    unittest.main()
