import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests

from src import slack_delivery as slack
from src.quality import monitor
from src.quality.checks import issue
from src.quality.runtime import KST


def response(status=200, text="ok", **data):
    result = Mock(status_code=status, text=text)
    result.json.return_value = data
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class SlackDeliverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"text": "브리핑", "blocks": [{"type": "header", "text": {"type": "plain_text", "text": "제목"}},
            {"type": "table", "rows": [[{"type": "raw_text", "text": "지표"}],
              [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
                  {"type": "link", "text": "USD/KRW 1,350 ▲1% 10.30→11.06", "url": "https://example.org/fx"}]}]}]]}]}

    def test_explicit_rejection_retries_once_with_all_cells_preserved(self):
        session = Mock()
        session.post.side_effect = [response(400, "invalid_blocks"), response()]
        _, reference, fallback = slack.post_message("webhook", self.payload, session=session)
        self.assertTrue(fallback)
        self.assertIsNone(reference)
        sent = session.post.call_args.kwargs["json"]
        self.assertNotIn("table", [block["type"] for block in sent["blocks"]])
        self.assertIn("https://example.org/fx", str(sent))
        self.assertIn("10.30→11.06", str(sent))
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(self.payload["blocks"][1]["type"], "table")

    def test_no_retry_timeout_or_other_http_failures(self):
        for error in (requests.Timeout(), response(500, "server_error"), response(400, "invalid_payload")):
            session = Mock()
            if isinstance(error, Exception):
                session.post.side_effect = error
            else:
                session.post.return_value = error
            with self.assertRaises(requests.RequestException):
                slack.post_message("webhook", self.payload, session=session)
            self.assertEqual(session.post.call_count, 1)

    def test_second_rejection_not_retried(self):
        session = Mock()
        session.post.side_effect = [response(400, "invalid_blocks"), response(400, "invalid_blocks")]
        with self.assertRaises(requests.RequestException):
            slack.post_message("webhook", self.payload, session=session)
        self.assertEqual(session.post.call_count, 2)

    def test_bot_returns_parent_reference(self):
        session = Mock()
        session.post.return_value = response(ok=True, channel="C123ABC", ts="12345.6789")
        _, reference, fallback = slack.post_message("webhook", self.payload, session=session, token="secret", channel="C123ABC")
        self.assertEqual(reference, {"channel": "C123ABC", "ts": "12345.6789"})
        self.assertFalse(fallback)
        self.assertEqual(session.post.call_args.args[0], slack.API_URL)
        self.assertEqual(session.post.call_args.kwargs["headers"], {"Authorization": "Bearer secret"})

    def test_bot_error_never_falls_back_to_webhook(self):
        session = Mock()
        session.post.return_value = response(ok=False, error="internal_error")
        with self.assertRaises(requests.RequestException):
            slack.post_message("webhook", self.payload, session=session, token="secret", channel="C123ABC")
        self.assertEqual(session.post.call_count, 1)

    def test_bot_explicit_block_error_falls_back_same_api_only(self):
        session = Mock()
        session.post.side_effect = [response(ok=False, error="invalid_blocks"), response(ok=True, channel="C123ABC", ts="123.4")]
        _, _, fallback = slack.post_message("webhook", self.payload, session=session, token="secret", channel="C123ABC")
        self.assertTrue(fallback)
        self.assertTrue(all(call.args[0] == slack.API_URL for call in session.post.call_args_list))


class AlertSafetyTests(unittest.TestCase):
    now = datetime(2026, 11, 10, 10, tzinfo=KST)
    reference = {"channel": "C123ABC", "ts": "123.456"}

    def test_new_failure_run_not_suppressed_but_same_run_is(self):
        ledger = {}
        session = Mock()
        first = [issue("delivery_failed", "daily", event="run1")]
        monitor.notify(first, ledger, self.now, "webhook", session=session)
        self.assertEqual(monitor.due_alerts(first, ledger, self.now + timedelta(days=1)), [])
        self.assertTrue(monitor.due_alerts([issue("delivery_failed", "daily", event="run2")], ledger, self.now))

    def test_without_event_new_day_is_not_suppressed(self):
        session, ledger = Mock(), {}
        findings = [issue("workflow_failed", "daily")]
        monitor.notify(findings, ledger, self.now, "webhook", session=session)
        self.assertTrue(monitor.due_alerts(findings, ledger, self.now + timedelta(days=1)))

    def test_quality_reply_uses_exact_parent_and_no_broadcast(self):
        session = Mock()
        session.post.return_value = response(ok=True)
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "secret", "SLACK_CHANNEL_ID": "C123ABC"}):
            monitor.notify([issue("llm_noise", advisory=True)], {}, self.now, "webhook", session=session, reference=self.reference)
        sent = session.post.call_args.kwargs["json"]
        self.assertEqual(sent["thread_ts"], "123.456")
        self.assertFalse(sent["reply_broadcast"])
        self.assertEqual(session.post.call_args.args[0], slack.API_URL)

    def test_critical_or_unknown_parent_remains_channel_alert(self):
        for findings, reference in (([issue("workflow_failed")], self.reference),
                                    ([issue("llm_noise")], {"channel": "CWRONG", "ts": "123.4"}),
                                    ([issue("llm_noise")], None)):
            session = Mock()
            with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "secret", "SLACK_CHANNEL_ID": "C123ABC"}):
                monitor.notify(findings, {}, self.now, "webhook", session=session, reference=reference)
            self.assertEqual(session.post.call_args.args[0], "webhook")
            self.assertNotIn("thread_ts", session.post.call_args.kwargs["json"])

    def test_thread_error_does_not_retry_or_advance_ledger(self):
        session, ledger = Mock(), {}
        session.post.return_value = response(ok=False, error="internal_error")
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "secret", "SLACK_CHANNEL_ID": "C123ABC"}):
            with self.assertRaises(RuntimeError):
                monitor.notify([issue("llm_noise")], ledger, self.now, "webhook", session=session, reference=self.reference)
        self.assertEqual(ledger, {})
        self.assertEqual(session.post.call_count, 1)

    def test_detail_comes_from_archived_article_not_model_url(self):
        report = {"run_id": "1", "kind": "daily", "delivery": "sent", "issues": [issue("llm_noise", article=0, advisory=True)]}
        archive = [{"github": {"run_id": "1"}, "articles": [{"title": "실제 발송 기사 <@here>",
                    "url": "https://www.impacton.net/news/articleView.html?idxno=1&utm_source=mail&token=secret"}]}]
        finding = monitor.delivery_findings(report, archive, "success")[0]
        self.assertNotIn("token", finding["article_url"])
        self.assertNotIn("utm_source", finding["article_url"])
        text = monitor.describe(finding)
        self.assertIn("idxno=1", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("utm_source", text)
        self.assertNotIn("<@here>", text)
        self.assertIn("재확인 필요", text)
        self.assertIn("실제 발송 기사", text)

    def test_distinct_article_warning_not_hidden_by_same_category(self):
        first = issue("llm_noise", "AI", article_url="https://example.org/1")
        second = issue("llm_noise", "AI", article_url="https://example.org/2")
        self.assertEqual(len(monitor.due_alerts([first, second], {}, self.now)), 2)


if __name__ == "__main__":
    unittest.main()
