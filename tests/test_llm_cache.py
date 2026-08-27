"""LLM 캐시/재생 계층이 실제로 쿼터를 아끼는지 검증한다.

핵심 보장 두 가지:
  1. replay/off 모드에서는 네트워크를 절대 타지 않는다(쿼터 소모 0).
  2. cache 모드에서 같은 프롬프트는 두 번째부터 호출이 일어나지 않는다.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from src.processor import reranker
from src.utils import llm_cache


class LLMCacheTestCase(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.mkdtemp(prefix="llm-cache-test-")
        self.addCleanup(shutil.rmtree, self.cache_dir, True)
        patcher = mock.patch.dict(
            os.environ, {"LLM_CACHE_DIR": self.cache_dir}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set_mode(self, value):
        patcher = mock.patch.dict(os.environ, {"LLM_MODE": value}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cache_mode_calls_api_only_once_for_same_prompt(self):
        self._set_mode("cache")
        with mock.patch.object(reranker.requests, "post") as post:
            post.return_value = mock.Mock(
                status_code=200,
                json=lambda: {
                    "candidates": [{"content": {"parts": [{"text": '{"selected":["1"]}'}]}}]
                },
            )
            first = reranker._post_generate("test-model", "key", "동일 프롬프트")
            second = reranker._post_generate("test-model", "key", "동일 프롬프트")

        self.assertEqual(first, second)
        self.assertEqual(post.call_count, 1, "두 번째 호출은 캐시에서 와야 한다")

    def test_replay_mode_never_touches_network(self):
        self._set_mode("cache")
        with mock.patch.object(reranker.requests, "post") as post:
            post.return_value = mock.Mock(
                status_code=200,
                json=lambda: {
                    "candidates": [{"content": {"parts": [{"text": "recorded"}]}}]
                },
            )
            reranker._post_generate("test-model", "key", "녹화할 프롬프트")

        self._set_mode("replay")
        with mock.patch.object(reranker.requests, "post") as post:
            replayed = reranker._post_generate("test-model", "key", "녹화할 프롬프트")
            post.assert_not_called()
        self.assertEqual(replayed, "recorded")

    def test_replay_mode_raises_on_cache_miss(self):
        self._set_mode("replay")
        with mock.patch.object(reranker.requests, "post") as post:
            with self.assertRaises(llm_cache.CacheMiss):
                reranker._post_generate("test-model", "key", "녹화된 적 없는 프롬프트")
            post.assert_not_called()

    def test_off_mode_blocks_every_call(self):
        self._set_mode("off")
        with mock.patch.object(reranker.requests, "post") as post:
            with self.assertRaises(llm_cache.CallBlocked):
                reranker._post_generate("test-model", "key", "무엇이든")
            post.assert_not_called()

    def test_off_mode_falls_back_to_rule_based_selection(self):
        """LLM이 완전히 죽어도 파이프라인은 기사를 골라내야 한다."""
        self._set_mode("off")
        articles = [
            {"title": "A startup raises Series B", "description": "", "category": "🤖 AI", "relevance": 5},
            {"title": "OpenAI acquires a chip team", "description": "", "category": "🤖 AI", "relevance": 4},
        ]
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "dummy"}, clear=False):
            with mock.patch.object(reranker.requests, "post") as post:
                selected = reranker.rerank_by_category(articles, ["🤖 AI"])
                post.assert_not_called()
        self.assertTrue(selected, "폴백 경로가 기사를 반환해야 한다")

    def test_live_mode_ignores_cache(self):
        self._set_mode("cache")
        with mock.patch.object(reranker.requests, "post") as post:
            post.return_value = mock.Mock(
                status_code=200,
                json=lambda: {"candidates": [{"content": {"parts": [{"text": "v1"}]}}]},
            )
            reranker._post_generate("test-model", "key", "라이브 확인")

        self._set_mode("live")
        with mock.patch.object(reranker.requests, "post") as post:
            post.return_value = mock.Mock(
                status_code=200,
                json=lambda: {"candidates": [{"content": {"parts": [{"text": "v2"}]}}]},
            )
            fresh = reranker._post_generate("test-model", "key", "라이브 확인")
            self.assertEqual(post.call_count, 1, "live 는 항상 실제 호출이어야 한다")
        self.assertEqual(fresh, "v2")

    def test_model_discovery_excludes_non_text_variants(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "models": [
                {
                    "name": "models/gemini-3.1-flash-image",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.1-flash-tts-preview",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.5-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3-flash-preview",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }

        with mock.patch.object(llm_cache, "guard_network"):
            with mock.patch.object(reranker.requests, "get", return_value=response):
                models = reranker._discover_models("key")

        self.assertEqual(
            models,
            ["gemini-3.5-flash-lite", "gemini-3-flash-preview"],
        )

    def test_model_discovery_attempts_are_bounded(self):
        with mock.patch.object(reranker, "_RESOLVED_MODEL", None):
            with mock.patch.object(reranker, "_candidate_models", return_value=[]):
                with mock.patch.object(
                    reranker,
                    "_discover_models",
                    return_value=["text-one", "text-two", "text-three"],
                ):
                    with mock.patch.object(
                        reranker,
                        "_post_generate",
                        side_effect=RuntimeError("unavailable"),
                    ) as post:
                        result = reranker._call_llm("prompt", "key")

        self.assertEqual(result, (None, None))
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
