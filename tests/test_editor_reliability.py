"""Offline regressions: macro release routing and bounded impact review repair."""
import copy
import json
import unittest
from unittest.mock import patch

from test_editorial_logic import _install_optional_dependency_stubs
_install_optional_dependency_stubs()
from src.processor import impact
from src.processor.summarizer import summarize


class EditorReliabilityTests(unittest.TestCase):
    def test_official_release_titles_route_to_macro(self):
        titles = [
            ('Personal Income and Outlays, October 2026', 'global'),
            ('Advance Monthly Sales for Retail and Food Services', 'global'),
            ('Industrial Production and Capacity Utilization', 'global'),
            ('2026년 10월 산업활동동향', 'korea'),
            ('2026년 10월 국제수지(잠정)', 'korea'),
            ('2026년 10월 소비자물가동향', 'korea'),
        ]
        for title, region in titles:
            with self.subTest(title=title):
                result, _ = summarize({'title': title, 'region': region})
                self.assertTrue(result['category'].startswith('🌐'))
                self.assertEqual(result['region'], region)

    def test_company_sales_are_not_a_macro_release(self):
        result, _ = summarize({'title': 'Acme retail sales rise after new store opening'})
        self.assertFalse(result['category'].startswith('🌐'))

    def candidate(self):
        return {'title': 'Impact investing assets grow to $1.8 trillion',
                'description': '', 'category': impact.IMPACT_DAILY_CONFIG['category'],
                'editor_verdict': 'keep', 'newsletter_source': 'impactalpha',
                'newsletter_body_notes': [{'source': 'Impact Alpha', 'source_key': 'impactalpha',
                    'title': 'Impact investing assets grow to $1.8 trillion',
                    'excerpt': 'A $200 billion gain since 2024, according to the market study.'}]}

    def response(self, reason):
        return json.dumps({'selected': [], 'omitted': [{'ids': ['I1'], 'reason': reason}]})

    def test_attributed_notes_reach_final_cards_without_replacing_summary(self):
        item = self.candidate()
        card = impact._cards([item])[0]
        self.assertEqual(card['summary'], '')
        self.assertIn('$200 billion', card['newsletter_evidence'][0]['excerpt'])
        item['title'] = 'Unrelated project finance transaction'
        self.assertEqual(impact._cards([item])[0]['newsletter_evidence'], [])

    def test_policy_conflict_rechecked_without_forced_selection(self):
        bad = self.response('시장 규모 통계이나, 최신 딜과 정책 변화를 우선하여 제외함.')
        good = self.response('기준일과 비교 기간의 근거가 부족하여 최신성을 확인할 수 없음.')
        items = [self.candidate()]
        with patch.object(impact, 'generate_editor_json', side_effect=[(bad, 'test'), (good, 'test')]) as call:
            self.assertEqual(impact.review(items), [])
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args_list[0].args[0].split('[후보]\n')[1],
                             call.call_args_list[1].args[0].split('[후보]\n')[1])
        self.assertEqual(impact.selected(items), [])
        self.assertEqual(items[0]['impact_final_repair_trigger'], 'format_only_omission')

    def test_schema_and_policy_share_one_repair_budget(self):
        bad = self.response('통계로, 새로운 사건이 아님.')
        items = [self.candidate()]
        with patch.object(impact, 'generate_editor_json', side_effect=[('{', 'test'), (bad, 'test')]) as call:
            self.assertTrue(impact.review(items))
            self.assertEqual(call.call_count, 2)
        self.assertIsNone(impact.selected(items))

    def test_valid_omission_has_no_extra_call(self):
        raw = self.response('지난달과 수치가 같아 추가 정보 없음.')
        with patch.object(impact, 'generate_editor_json', return_value=(raw, 'test')) as call:
            self.assertEqual(impact.review([self.candidate()]), [])
            self.assertEqual(call.call_count, 1)

    def test_missing_service_does_not_trigger_policy_retry(self):
        with patch.object(impact, 'generate_editor_json', return_value=(None, None)) as call:
            self.assertTrue(impact.review([self.candidate()]))
            self.assertEqual(call.call_count, 1)


if __name__ == '__main__':
    unittest.main()
