"""Offline regressions using the actual November 9 briefing failure shapes."""
import json
import os
import unittest
from unittest.mock import patch

from test_editorial_logic import _install_optional_dependency_stubs
_install_optional_dependency_stubs()
from src.processor import translator
from src.processor.summarizer import _title_exclusion_reason
from src.weekly.selector import revalidate_weekly_articles
from src.weekly.deduplicator import deduplicate_weekly_articles
from src.config import CATEGORIES

MACRO = next(c for c in CATEGORIES if c.startswith('🌐'))
ALT = next(c for c in CATEGORIES if c.startswith('📈'))


class NovemberBriefingTests(unittest.TestCase):
    def test_kdi_foreign_reports_move_regions_without_category_change(self):
        for title in ('2026년 10월 FOMC 회의 결과 (한국은행)',
                      '라가르드 ECB 총재 사임 예정 발표 (한국은행)'):
            with self.subTest(title=title):
                original = dict(title=title, source='KDI', feed='Gmail Newsletters',
                                category=MACRO, region='korea', description='', url='https://eiec.kdi.re.kr/policy/domesticView.do?ac=1')
                result = revalidate_weekly_articles([original])[0]
                self.assertEqual(result['region'], 'global')
                self.assertEqual(result['category'], MACRO)
                self.assertEqual(original['region'], 'korea')

    def test_domestic_response_to_foreign_policy_remains_domestic(self):
        result = revalidate_weekly_articles([dict(
            title='미 FOMC 결과에 한국은행 대응 점검', source='KDI', category=MACRO,
            region='korea', description='한국 금융시장 영향 분석')])[0]
        self.assertEqual(result['region'], 'korea')

    def test_practical_notice_and_product_launch_are_not_insights(self):
        for title in ('Malta raises group consolidation thresholds',
                      '몰타, 그룹 연결재무제표 작성 기준 상향',
                      'EY launches enterprise-scale agentic AI to redefine the audit experience for the AI era | EY - Global',
                      'EY, AI 시대 감사 경험 재정의할 기업용 에이전트 AI 출시'):
            with self.subTest(title=title):
                self.assertTrue(_title_exclusion_reason(title, True))

    def test_real_research_reports_are_kept(self):
        for title in ('PwC launches 2027 Global Digital Trust Survey',
                      'EY launches global survey on agentic AI',
                      'How agentic AI will change the audit industry',
                      'McKinsey: The state of AI 2026'):
            with self.subTest(title=title):
                self.assertEqual(_title_exclusion_reason(title, True), '')

    def test_routine_ministry_personnel_is_not_macro_news(self):
        self.assertEqual(_title_exclusion_reason(
            '중기부, 소상공인정책실장에 조경원 창업정책관 임명', False), 'routine_personnel_announcement')
        self.assertEqual(_title_exclusion_reason('한국은행 신임 총재, 기준금리 전망 발표', False), '')

    def test_weekly_ipo_country_offers_share_one_slot_and_preserve_links(self):
        titles = ('Anthropic plans to sell $3.5 billion of shares to Japan investors in IPO, filing shows',
                  'Anthropic plans Australian share offer in IPO, CommSec says')
        keys = ('anthropic_ipo_process', 'anthropic_ipo_australia_share_offer')
        items = [dict(title=title, title_orig=title, editor_event_key=key, category=ALT,
                      source='Reuters', region='global', url=f'https://example.com/{i}',
                      _archive_edition_date=f'2026-11-0{i+2}', editor_score=9)
                 for i, (title, key) in enumerate(zip(titles, keys))]
        result = deduplicate_weekly_articles(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['weekly_related_links']), 2)
        other = dict(items[1], title='OpenAI plans Australian share offer in IPO',
                     title_orig='OpenAI plans Australian share offer in IPO',
                     editor_event_key='openai_ipo_australia_share_offer')
        self.assertEqual(len(deduplicate_weekly_articles([items[0], other])), 2)

    def test_bok_page_suffix_no_longer_prevents_translation(self):
        title = 'Real Gross Domestic Product: Third Quarter of 2026 (Advance Estimate) | (상세) | 한국은행 홈페이지'
        item = {'title': title, 'title_orig': ''}
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'fake'}), \
                patch.object(translator, 'TRANSLATE_TITLES', True), \
                patch.object(translator, 'generate_editor_json', return_value=(json.dumps({'0': '2026년 3분기 실질 국내총생산 속보치'}), 'mock')) as generate:
            translator.translate_titles([item])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(item['title_orig'], title)
        self.assertEqual(item['title'], '2026년 3분기 실질 국내총생산 속보치')


if __name__ == '__main__':
    unittest.main()
