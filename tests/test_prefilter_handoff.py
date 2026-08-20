"""편집 게이트가 켜졌을 때 앞단 키워드 필터가 주제 판정을 넘기는지 검증한다.

실제 실행에서 키워드 단계가 56건을 no_relevant_signal 로 죽였고, 그 안에
규제 변화와 M&A 같은 최우선 기사가 있었다. 게이트는 이 기사들을 보지도
못했으므로, 게이트가 있을 때는 주제 판정을 게이트에 넘겨야 한다.
"""

import unittest

from src.bot import is_relevant


def _article(title, **extra):
    base = {"title": title, "description": "", "link": "https://example.com/a",
            "source": "Some Outlet", "feed": "Some Feed"}
    base.update(extra)
    return base


class PrefilterHandoffTestCase(unittest.TestCase):
    # 실제로 키워드에 죽었던 기사들
    KILLED_BUT_VALUABLE = [
        "Japan to require AI firms to disclose training data",
        "SpaceX Attempted to Acquire AI Coding Startup Cognition",
        "Exclusive: AI infrastructure startup Velatir raises €5m",
        "미 에너지부, 폐기물 수출 막고 2300억원 투입해 핵심광물 회수",
    ]

    def test_keyword_mode_still_drops_them(self):
        """현재 운영 동작을 기록해 둔다. 이게 문제의 출발점이다."""
        dropped = [
            title for title in self.KILLED_BUT_VALUABLE
            if not is_relevant(_article(title), require_topic_match=True)
        ]
        self.assertTrue(dropped, "키워드 경로가 이 기사들을 죽이는 것이 관측된 사실")

    def test_gate_mode_lets_them_through(self):
        for title in self.KILLED_BUT_VALUABLE:
            with self.subTest(title=title):
                article = _article(title)
                self.assertTrue(
                    is_relevant(article, require_topic_match=False),
                    "게이트가 판단하도록 통과시켜야 한다",
                )
                self.assertEqual(article["relevance_signal"], "deferred_to_editor")

    def test_gate_mode_still_blocks_blacklist(self):
        """값싸고 명확한 차단은 게이트 앞에서 계속 걸러 호출량을 줄인다."""
        article = _article("Best deals on gaming laptops, buy now")
        self.assertFalse(is_relevant(article, require_topic_match=False))
        self.assertTrue(article["filter_reason"].startswith("blacklist:"))

    def test_gate_mode_still_blocks_opinion_urls(self):
        article = _article("어떤 주장", link="https://example.com/opinion/why-x")
        self.assertFalse(is_relevant(article, require_topic_match=False))
        self.assertTrue(article["filter_reason"].startswith("opinion:"))

    def test_default_preserves_keyword_behaviour(self):
        """기본값은 기존 동작이어야 게이트가 꺼진 환경이 바뀌지 않는다."""
        article = _article("Japan to require AI firms to disclose training data")
        self.assertEqual(
            is_relevant(article),
            is_relevant(_article("Japan to require AI firms to disclose training data"),
                        require_topic_match=True),
        )


if __name__ == "__main__":
    unittest.main()
