"""Offline regressions for delivery persistence and final-editor consistency."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests
from test_editorial_logic import _install_optional_dependency_stubs
_install_optional_dependency_stubs()
from src import bot
from src.processor import impact, reranker
from src.weekly import runner
from src.weekly.archive import load_weekly_archive
from test_impact_final import article, decision
from test_weekly_briefing import MONDAY_RUN, FakeSession, IMPACT, article as weekly_article


class AuditSafetyTests(unittest.TestCase):
    def test_exception_summary_never_includes_url_or_body(self):
        response = requests.Response()
        response.status_code = 503
        error = requests.HTTPError('https://example.invalid/?key=FAKE_SECRET PRIVATE', response=response)
        self.assertEqual(reranker._safe_error_summary(error), 'HTTPError HTTP 503')
        self.assertEqual(reranker._safe_error_summary(ValueError('PRIVATE')), 'ValueError')

    def test_discovery_and_candidate_errors_are_safe_and_fallback_works(self):
        with patch.object(reranker, '_RESOLVED_MODEL', None), \
                patch.object(reranker, '_UNAVAILABLE_MODELS', set()), \
                patch.object(reranker, '_candidate_models', return_value=['first']), \
                patch.object(reranker, '_discover_models', return_value=['second']), \
                patch.object(reranker, '_post_generate', side_effect=[requests.Timeout('FAKE_SECRET'), '{}']):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(reranker._call_llm('test', 'key'), ('{}', 'second'))
            self.assertNotIn('FAKE_SECRET', output.getvalue())

    def payload(self, representative, reason='동일 사건의 대표 I1 선정'):
        cards = impact._cards([article(1), article(2), article(3)])
        payload = {'selected': [decision(cards[0])], 'omitted': [
            {'ids': ['I2'], 'duplicate_of': representative, 'reason': reason},
            {'ids': ['I3'], 'reason': '근거 부족'},
        ]}
        return cards, json.dumps(payload, ensure_ascii=False)

    def test_duplicate_requires_a_selected_representative(self):
        for representative in ('I3', 'I99', 'I2', [], None):
            with self.subTest(representative=representative):
                cards, payload = self.payload(representative)
                with self.assertRaises(ValueError):
                    impact._validate(payload, cards)

    def test_valid_duplicate_remains_omitted(self):
        cards, payload = self.payload('I1')
        selected, omitted = impact._validate(payload, cards)
        self.assertEqual(len(selected), 1)
        self.assertIn('I2', omitted)

    def test_conflicting_duplicate_explanation_is_rejected(self):
        cards, payload = self.payload('I1', 'I3와 동일 사건이므로 I3를 대표로 선정함')
        with self.assertRaises(ValueError):
            impact._validate(payload, cards)

    def test_claim_metric_conversions_require_source_support(self):
        examples = [
            ('Green debt fees rise for fifth year', '투자 총액 증가가 확인됨'),
            ('Impact assets under management increase', '신규 자금 유입 증가'),
            ('Government proposed a climate rule', '규제 시행 확정'),
        ]
        for title, reason in examples:
            with self.subTest(title=title):
                card = impact._cards([article(1, title=title, description='')])[0]
                with self.assertRaises(impact._ClaimEvidenceError):
                    impact._check_claim_basis(decision(card, reason=reason), card)
                impact._check_claim_basis(decision(card, reason=reason + '라고 단정할 수 없음'), card)

    def test_source_with_both_metrics_is_not_rejected(self):
        card = impact._cards([article(1, title='Assets under management and net inflows increase')])[0]
        impact._check_claim_basis(decision(card, reason='순유입 증가'), card)

    def test_claim_repair_keeps_article_and_uses_existing_retry_budget(self):
        item = article(1, title='Green debt fees rise for fifth year', description='')
        card = impact._cards([item])[0]
        def payload(reason):
            return json.dumps({'selected': [decision(card, reason=reason)], 'omitted': []})
        with patch.object(impact, 'generate_editor_json', side_effect=[
            (payload('투자 총액 증가'), 'test'), (payload('수수료 수익구조의 변화'), 'test')
        ]) as generate:
            self.assertEqual(impact.review([item]), [])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(item['impact_final_repair_trigger'], 'claim_basis_conflict')
        self.assertEqual(len(impact.selected([item])), 1)

    def test_daily_archive_failure_preserves_sent_articles_and_recovery(self):
        item = article(1)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bot, 'SLACK_ARCHIVE_PATH', Path(directory) / 'slack_archive.jsonl'), \
                patch.dict(os.environ, {'SLACK_WEBHOOK_URL': 'https://example.invalid/FAKE_SECRET', 'DRY_RUN': ''}), \
                patch.object(bot, 'translate_titles', side_effect=lambda items: items), \
                patch.object(bot.requests, 'post', return_value=Mock(status_code=200)) as post, \
                patch.object(bot, '_append_slack_archive', side_effect=[OSError('FAKE_SECRET'), None]) as save:
            output = io.StringIO()
            with redirect_stdout(output):
                success, sent = bot.send_aggregated_slack_news([item])
            self.assertTrue(success)
            self.assertTrue(sent[0]['delivery_archive_failed'])
            self.assertEqual(post.call_count, 1)
            self.assertEqual(save.call_count, 2)
            self.assertEqual(save.call_args.kwargs['archive_path'].name, 'slack_archive_recovery.jsonl')
            self.assertNotIn('FAKE_SECRET', output.getvalue())

    def test_weekly_recovery_blocks_second_send(self):
        record = {'version': 3, 'edition_date': '2026-08-30', 'articles': [
            weekly_article(IMPACT, 'impact', '기후 투자', source='ImpactAlpha')]}
        session = FakeSession()
        original_append = runner._append_delivery_archive
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'daily.jsonl'
            target = Path(directory) / 'weekly.jsonl'
            source.write_text(json.dumps(record), encoding='utf-8')
            def append(path, *args):
                if path == target:
                    raise OSError('FAKE_SECRET')
                return original_append(path, *args)
            with patch.dict(os.environ, {'WEEKLY_SLACK_WEBHOOK_URL': 'https://example.invalid/weekly'}, clear=True), \
                    patch.object(runner, '_append_delivery_archive', side_effect=append):
                first = runner.run_weekly_briefing(now=MONDAY_RUN, source_archive_path=source,
                                                   delivery_archive_path=target, session=session)
                second = runner.run_weekly_briefing(now=MONDAY_RUN, source_archive_path=source,
                                                    delivery_archive_path=target, session=session)
            self.assertFalse(first.success)
            self.assertTrue(first.delivered)
            self.assertEqual(first.reason, 'sent_archive_failed')
            self.assertTrue(second.success)
            self.assertFalse(second.delivered)
            self.assertEqual(len(session.post_calls), 1)

    def test_weekly_reads_daily_recovery_without_primary_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'daily.jsonl'
            recovery = Path(directory) / 'daily_recovery.jsonl'
            recovery.write_text(json.dumps({'version': 3, 'edition_date': '2026-08-30',
                'articles': [weekly_article(IMPACT, 'impact', '기후 투자')]}), encoding='utf-8')
            result = load_weekly_archive(source, now=MONDAY_RUN)
            self.assertEqual(len(result.articles), 1)


if __name__ == '__main__':
    unittest.main()
