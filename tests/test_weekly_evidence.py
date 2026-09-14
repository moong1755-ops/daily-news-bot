import json
import unittest
from unittest.mock import Mock

from src.weekly.evidence import (
    apply_evidence,
    apply_local_evidence,
    enrich_shortlist,
    guarded_title,
)
from src.weekly.editor import _grounded_lines, _fallback_lines
from src.weekly.runner import _select_with_evidence
from src.weekly.selector import weekly_score


def story(**values):
    return dict({"title": "Example Fund invests $13 million", "title_orig": "Example Fund invests $13 million",
                 "url": "https://www.esgtoday.com/example/", "category": "🌱 임팩트",
                 "importance": 2, "importance_reason": "major_deal", "editor_score": 9}, **values)


class WeeklyEvidenceTests(unittest.TestCase):
    def test_public_metadata_grant_correction_and_input_preservation(self):
        original = story()
        session = Mock()
        session.get.return_value = Mock(status_code=200, text=(
            '<meta property="og:title" content="Example Fund invests $13 million - ESG Today">'
            '<meta property="og:description" content="Example Fund provides $13 million in grants.">'))
        checked = enrich_shortlist([original], session=session)[0]
        self.assertEqual(checked["weekly_financing_type"], "grant")
        self.assertEqual(checked["importance_reason"], "")
        self.assertEqual(checked["importance"], 2)
        self.assertEqual(checked["weekly_previous_importance"], 2)
        self.assertEqual(checked["weekly_previous_importance_reason"], "major_deal")
        self.assertIn("weekly_evidence_checked_at", checked)
        self.assertNotIn("weekly_financing_type", original)
        self.assertLess(weekly_score(checked)[0], weekly_score(original)[0])
        self.assertEqual(guarded_title(checked, "농가에 1300만 달러 투자"), "[지원금] 농가에 1300만 달러 지원")

    def test_approval_and_mixed_financing_are_not_reclassified_as_grants(self):
        for description in ("Regulator grants approval for acquisition",
                            "$13 million in grants and equity financing"):
            candidate = story()
            apply_evidence(candidate, description)
            self.assertNotIn("weekly_financing_type", candidate)

    def test_source_attribution_preserves_reported_status(self):
        candidate = story()
        apply_evidence(candidate, "Set to announce a funding round, according to multiple sources")
        self.assertEqual(candidate["weekly_claim_status"], "reported")

    def test_blocked_funding_page_is_not_bypassed_or_assumed_confirmed(self):
        session = Mock()
        session.get.return_value = Mock(status_code=403)
        checked = enrich_shortlist([story(title_orig="Example $3bn fundraise")], session=session)[0]
        self.assertEqual(checked["weekly_claim_status"], "unverified")
        self.assertIn("HTTP 403", checked["weekly_evidence_status"])
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_blocked_raises_headline_is_also_marked_unverified(self):
        session = Mock()
        session.get.return_value = Mock(status_code=403)
        checked = enrich_shortlist([
            story(title="Example raises $13 million", title_orig="Example raises $13 million")
        ], session=session)[0]
        self.assertEqual(checked["weekly_claim_status"], "unverified")

    def test_two_daily_publishers_corroborate_a_blocked_representative(self):
        session = Mock()
        session.get.return_value = Mock(status_code=308)
        candidate = story(
            title="Example confirms $13 million Series B",
            title_orig="Example confirms $13 million Series B",
            weekly_related_links=[
                {"url": "https://www.bloomberg.com/example"},
                {"url": "https://news.crunchbase.com/example"},
                {"url": "https://sifted.eu/example"},
            ],
        )
        checked = enrich_shortlist([candidate], session=session)[0]
        self.assertNotIn("weekly_claim_status", checked)
        self.assertTrue(
            checked["weekly_evidence_status"].startswith("corroborated_daily_archive")
        )

    def test_unverified_title_never_claims_confirmation(self):
        candidate = story(weekly_claim_status="unverified")
        title = guarded_title(candidate, "Example, 1300만 달러 투자 확정")
        self.assertIn("공식발표 미확인", title)
        self.assertNotIn("확정", title)

    def test_local_descriptions_are_applied_to_every_candidate(self):
        candidates = [
            story(
                title=f"Company {index} invests $13 million",
                title_orig=f"Company {index} invests $13 million",
                url=f"https://www.esgtoday.com/company-{index}/",
                description="The company provides $13 million in grants.",
                importance=3,
            )
            for index in range(4)
        ]
        corrected = apply_local_evidence(candidates)
        self.assertTrue(all(item["weekly_financing_type"] == "grant" for item in corrected))
        self.assertTrue(all(item["importance"] == 2 for item in corrected))
        apply_evidence(corrected[0], corrected[0]["description"])
        self.assertEqual(corrected[0]["weekly_previous_importance"], 3)
        self.assertEqual(corrected[0]["weekly_previous_importance_reason"], "major_deal")

    def test_newly_promoted_candidate_is_checked_after_rerank(self):
        candidates = [
            story(
                title=f"Company {index} invests $13 million",
                title_orig=f"Company {index} invests $13 million",
                url=f"https://www.esgtoday.com/company-{index}/",
                importance=3,
                editor_score=10 - index,
            )
            for index in range(4)
        ]
        session = Mock()

        def response(url, **_kwargs):
            company = url.rstrip("/").rsplit("-", 1)[-1]
            return Mock(status_code=200, text=(
                f'<meta property="og:title" content="Company {company} invests $13 million">'
                '<meta property="og:description" content="The company provides $13 million in grants.">'
            ))

        session.get.side_effect = response
        checked, _selection = _select_with_evidence(candidates, session=session)
        self.assertEqual(session.get.call_count, 4)
        self.assertTrue(all(item["weekly_financing_type"] == "grant" for item in checked))

    def test_unrelated_metadata_is_ignored(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, text=(
            '<meta property="og:title" content="Subscribe now">'
            '<meta property="og:description" content="$13 million in grants">'))
        result = enrich_shortlist([story()], session=session)[0]
        self.assertNotIn("weekly_financing_type", result)
        self.assertTrue(result["weekly_evidence_status"].startswith("unavailable"))

    def test_other_domains_do_not_trigger_extra_fetches(self):
        session = Mock()
        enrich_shortlist([story(url="https://example.com/story")], session=session)
        session.get.assert_not_called()

    def test_impact_is_restored_first_if_model_omits_it(self):
        impact = story(importance=3)
        other = story(category="🤖 AI", title="AI model released", importance=3)
        raw = json.dumps({"lines": [{"article_id": 2, "text": "AI 모델 출시"}]})
        result = _grounded_lines(raw, [impact, other], 3)
        self.assertEqual(result, (impact["title"], "AI 모델 출시"))
        self.assertEqual(_fallback_lines([other, impact], 3)[0], impact["title"])

    def test_weak_impact_is_not_forced(self):
        raw = json.dumps({"lines": [{"article_id": 2, "text": "AI 모델 출시"}]})
        result = _grounded_lines(raw, [story(importance=1), story(category="🤖 AI")], 3)
        self.assertEqual(result, ("AI 모델 출시",))

    def test_unverified_headline_cannot_be_upgraded_by_model(self):
        candidate = story(title="Example 30억 유로 투자 유치", weekly_claim_status="unverified")
        raw = json.dumps({"lines": [{"article_id": 1, "text": "Example 투자 유치 확정"}]})
        result = _grounded_lines(raw, [candidate], 3)
        self.assertIn("공식발표 미확인", result[0])
        self.assertNotIn("확정", result[0])

    def test_unknown_ids_and_duplicate_ids_are_rejected(self):
        raw = json.dumps({"lines": [{"article_id": 99, "text": "없는 기사"},
                                     {"article_id": 1, "text": "확인된 내용"},
                                     {"article_id": 1, "text": "중복"}]})
        self.assertEqual(_grounded_lines(raw, [story()], 3), ("확인된 내용",))
        self.assertEqual(_grounded_lines('{"lines":["untraceable"]}', [story()], 3), ())


if __name__ == "__main__":
    unittest.main()
