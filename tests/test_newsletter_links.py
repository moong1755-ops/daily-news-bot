"""뉴스레터에서 기사 링크만 뽑는지 검증한다.

Bloomberg 의 "Read in browser" 가 기사 제목으로 슬랙에 발송된 적이 있다.
스킵 목록이 "view in browser" 만 정확 일치로 걸러 매체별 표현 차이를 놓쳤다.
"""

import unittest

from src.fetchers.gmail_newsletters import _is_boilerplate_anchor


class BoilerplateAnchorTestCase(unittest.TestCase):
    def test_rejects_browser_link_variants(self):
        for text in ["Read in browser", "View in browser", "read in browser",
                     "View online", "Web version", "브라우저에서 보기"]:
            with self.subTest(text=text):
                self.assertTrue(_is_boilerplate_anchor(text), f"{text!r} 는 상용구다")

    def test_rejects_subscription_and_legal_links(self):
        for text in ["Unsubscribe from this list", "Manage preferences",
                     "Privacy Policy", "Follow us on LinkedIn", "수신 거부하기"]:
            with self.subTest(text=text):
                self.assertTrue(_is_boilerplate_anchor(text))

    def test_rejects_empty_and_very_short_text(self):
        for text in ["", "   ", "More", "Go"]:
            with self.subTest(text=text):
                self.assertTrue(_is_boilerplate_anchor(text))

    def test_keeps_real_headlines(self):
        for text in [
            "Climate Fund Managers lands $182 million for green hydrogen fund",
            "US power sector emissions fall for a third year",
            "CATL, 2027년부터 저탄소 협력사 발주 우대",
            "Anthropic’s annualized revenue surges to $65B",
        ]:
            with self.subTest(text=text):
                self.assertFalse(_is_boilerplate_anchor(text), f"{text!r} 는 기사다")

    def test_normalizes_whitespace_before_matching(self):
        self.assertTrue(_is_boilerplate_anchor("  Read   in\n browser  "))


if __name__ == "__main__":
    unittest.main()
