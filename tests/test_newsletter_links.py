"""뉴스레터에서 기사 링크만 뽑는지 검증한다.

Bloomberg 의 "Read in browser" 가 기사 제목으로 슬랙에 발송된 적이 있다.
스킵 목록이 "view in browser" 만 정확 일치로 걸러 매체별 표현 차이를 놓쳤다.
"""

import unittest

from src.fetchers.gmail_newsletters import (
    _anchor_headline_html,
    _headline_from_anchor,
    _is_boilerplate_anchor,
    _looks_like_headline,
)


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



class HeadlineShapeTestCase(unittest.TestCase):
    """본문 중간 링크가 기사 제목으로 둔갑하는 것을 막는다.

    실제 발송분에 Bloomberg Green 뉴스레터의 "US power sector emissions" 가
    거대한 트래킹 URL과 함께 올라갔고, 판정 로그에는 "swap soybeans for
    credits", "rising ocean temperatures", 기자 이름까지 기사로 잡혀 있었다.
    """

    FRAGMENTS = [
        "swap soybeans for credits",
        "rising ocean temperatures",
        "improved children’s lung health",
        "leave the forest standing",
        "overstated climate benefits",
        "unlimited access",
        "US power sector emissions",
        "Fabiano Maisonnave",
        "Keep reading",
    ]

    HEADLINES = [
        "Climate Fund Managers lands $182 million for green hydrogen fund",
        "Anthropic’s annualized revenue surges to $65B",
        "Ottawa Community Land Trust launches retail bond to preserve housing",
        "CATL, 2027년부터 저탄소 협력사 발주 우대…삼성SDI도 기준 위반 시 계약 종료",
        "미 에너지부, 폐기물 수출 막고 2300억원 투입해 핵심광물 회수",
    ]

    def test_rejects_mid_sentence_fragments(self):
        for text in self.FRAGMENTS:
            with self.subTest(text=text):
                self.assertFalse(_looks_like_headline(text), f"{text!r} 는 조각이다")

    def test_keeps_real_headlines(self):
        for text in self.HEADLINES:
            with self.subTest(text=text):
                self.assertTrue(_looks_like_headline(text), f"{text!r} 는 제목이다")

    def test_short_korean_headlines_are_not_mistaken_for_fragments(self):
        """한국어는 같은 내용을 짧게 쓴다. 영문 기준을 그대로 대면 안 된다."""
        for text in ["삼성전자, SK하이닉스 인수", "LG엔솔, 미국 공장 증설 결정"]:
            with self.subTest(text=text):
                self.assertLess(len(text), 30, "영문 기준이면 탈락했을 길이")
                self.assertTrue(_looks_like_headline(text))

    def test_very_short_korean_text_is_still_rejected(self):
        self.assertFalse(_looks_like_headline("계속 읽기"))


class HeadlineTruncationTestCase(unittest.TestCase):
    """제목과 본문 첫 문단이 한 링크로 묶여 오는 경우를 정리한다."""

    RUN_ON = (
        "Gates-Backed TerraPower to Announce Second Nuke Plant This Year "
        "TerraPower LLC, the only company building a utility-scale nuclear "
        "power plant in the US, expects to announce its next project this "
        "year — one intended for a data center."
    )

    def test_run_on_anchor_is_shortened(self):
        result = _headline_from_anchor(self.RUN_ON)
        self.assertLess(len(result), len(self.RUN_ON))
        self.assertLessEqual(len(result), 121)
        self.assertTrue(result.startswith("Gates-Backed TerraPower"))

    def test_normal_headline_is_untouched(self):
        text = "Climate Fund Managers lands $182 million for green hydrogen fund"
        self.assertEqual(_headline_from_anchor(text), text)

    def test_sentence_boundary_is_preferred_over_word_cut(self):
        text = "Fed holds rates steady. " + "Officials signalled patience " * 6
        self.assertEqual(_headline_from_anchor(text), "Fed holds rates steady.")

    def test_whitespace_is_normalized(self):
        self.assertEqual(_headline_from_anchor("  Fed   holds\nrates  "), "Fed holds rates")

    def test_empty_input_is_safe(self):
        self.assertEqual(_headline_from_anchor(""), "")
        self.assertEqual(_headline_from_anchor(None), "")



class AnchorBlockExtractionTestCase(unittest.TestCase):
    """제목과 본문이 한 링크 안의 다른 블록에 있을 때 제목만 취한다.

    Bloomberg 는 "LG Energy Mulls Supplying Batteries for US Drones in Pivot"
    뒤에 본문을 문장부호 없이 이어 붙여, 텍스트만 보면 자를 지점을 알 수 없다.
    """

    def test_takes_first_block_as_headline(self):
        html = (
            '<div>LG Energy Mulls Supplying Batteries for US Drones in Pivot</div>'
            '<div>LG Energy Solution is considering supplying batteries for '
            'drones made in the US, people familiar said.</div>'
        )
        self.assertEqual(
            _anchor_headline_html(html),
            "LG Energy Mulls Supplying Batteries for US Drones in Pivot",
        )

    def test_handles_paragraph_and_break_separators(self):
        self.assertEqual(
            _anchor_headline_html("<p>Fed holds rates steady again</p><p>본문입니다</p>"),
            "Fed holds rates steady again",
        )
        self.assertEqual(
            _anchor_headline_html("Fed holds rates steady again<br>본문입니다"),
            "Fed holds rates steady again",
        )

    def test_skips_short_leading_labels(self):
        html = "<div>NEWS</div><div>Climate Fund Managers lands $182 million</div>"
        self.assertEqual(
            _anchor_headline_html(html),
            "Climate Fund Managers lands $182 million",
        )

    def test_plain_anchor_is_unchanged(self):
        text = "Climate Fund Managers lands $182 million for green hydrogen fund"
        self.assertEqual(_anchor_headline_html(text), text)

    def test_inline_tags_do_not_split_the_headline(self):
        html = "Anthropic’s <b>annualized revenue</b> surges to $65B"
        self.assertEqual(
            _anchor_headline_html(html),
            "Anthropic’s annualized revenue surges to $65B",
        )

    def test_empty_input_is_safe(self):
        self.assertEqual(_anchor_headline_html(""), "")
        self.assertEqual(_anchor_headline_html(None), "")


if __name__ == "__main__":
    unittest.main()
