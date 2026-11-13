import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src import check_slack_connection as check
from src.slack_delivery import post_message, API_URL, delivery_configured
from src.quality.monitor import notify
from src.quality.checks import issue
from src.quality.runtime import KST
from datetime import datetime


def reply(**data):
    response = Mock(status_code=200)
    response.json.return_value = data
    return response


class SlackMigrationTests(unittest.TestCase):
    env = {"SLACK_BOT_TOKEN_NEXT": "test-secret-never-output", "SLACK_WORKSPACE_ID": "TWORK",
           "SLACK_CHANNEL_ID": "CDAILY", "WEEKLY_SLACK_CHANNEL_ID": "CWEEKLY"}

    def test_v2_configuration_does_not_require_old_webhook(self):
        with patch.dict(os.environ, {"SLACK_DELIVERY_VERSION": "v2"}):
            self.assertTrue(delivery_configured("", "new-token", "CDAILY"))
            self.assertFalse(delivery_configured("old-webhook", "", "CDAILY"))
            self.assertFalse(delivery_configured("old-webhook", "new-token", ""))

    def test_legacy_configuration_still_supported(self):
        with patch.dict(os.environ, {"SLACK_DELIVERY_VERSION": ""}):
            self.assertTrue(delivery_configured("old-webhook"))
            self.assertFalse(delivery_configured(""))

    def test_auth_only_never_posts_messages_or_exposes_key(self):
        session = Mock()
        session.post.return_value = reply(ok=True, team_id="TWORK", user_id="UBOT")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.env, clear=True), \
             patch.object(check, "Path", return_value=Path(directory) / "check.json"), redirect_stdout(io.StringIO()) as output:
            result = check.check(session=session)
            self.assertEqual(result["bot_user_id"], "UBOT")
            self.assertNotIn(self.env["SLACK_BOT_TOKEN_NEXT"], output.getvalue())
        session.post.assert_called_once()
        self.assertTrue(session.post.call_args.args[0].endswith("auth.test"))

    def test_test_messages_use_distinct_channels_and_one_daily_thread(self):
        session = Mock()
        session.post.side_effect = [reply(ok=True, team_id="TWORK", user_id="UBOT"),
            reply(ok=True, channel="CDAILY", ts="100.1"), reply(ok=True), reply(ok=True, channel="CWEEKLY", ts="200.1")]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.env, clear=True), \
             patch.object(check, "Path", return_value=Path(directory) / "check.json"), redirect_stdout(io.StringIO()):
            result = check.check(send_test=True, session=session)
        self.assertEqual(session.post.call_count, 4)
        self.assertTrue(result["thread_reply_verified"])
        calls = session.post.call_args_list
        self.assertEqual(calls[1].kwargs["json"]["channel"], "CDAILY")
        self.assertEqual(calls[2].kwargs["json"]["thread_ts"], "100.1")
        self.assertFalse(calls[2].kwargs["json"]["reply_broadcast"])
        self.assertEqual(calls[3].kwargs["json"]["channel"], "CWEEKLY")
        self.assertEqual(len(calls[3].kwargs["json"]["blocks"][1]["rows"]), 8)

    def test_wrong_workspace_stops_before_messages(self):
        session = Mock()
        session.post.return_value = reply(ok=True, team_id="TOTHER", user_id="UBOT")
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(ValueError):
                check.check(send_test=True, session=session)
        self.assertEqual(session.post.call_count, 1)

    def test_identical_channels_or_missing_token_stop_before_network(self):
        for values in ({"WEEKLY_SLACK_CHANNEL_ID": "CDAILY"}, {"SLACK_BOT_TOKEN_NEXT": ""}):
            session = Mock()
            with patch.dict(os.environ, {**self.env, **values}, clear=True):
                with self.assertRaises(ValueError):
                    check.check(send_test=True, session=session)
            session.post.assert_not_called()

    def test_ambiguous_send_never_retried(self):
        session = Mock()
        session.post.side_effect = [reply(ok=True, team_id="TWORK", user_id="UBOT"), requests.Timeout()]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.env, clear=True), \
             patch.object(check, "Path", return_value=Path(directory) / "check.json"):
            with self.assertRaises(requests.Timeout):
                check.check(send_test=True, session=session)
        self.assertEqual(session.post.call_count, 2)

    def test_v2_missing_credentials_never_uses_old_webhook(self):
        for token, channel in (("", "CDAILY"), ("secret", "")):
            session = Mock()
            with patch.dict(os.environ, {"SLACK_DELIVERY_VERSION": "v2"}):
                with self.assertRaises(requests.RequestException):
                    post_message("retired-webhook", {"text": "test"}, session=session, token=token, channel=channel)
            session.post.assert_not_called()

    def test_v2_critical_alert_uses_new_bot_not_retired_webhook(self):
        session = Mock()
        session.post.return_value = reply(ok=True)
        with patch.dict(os.environ, {"SLACK_DELIVERY_VERSION": "v2", "SLACK_BOT_TOKEN": "new-secret", "SLACK_CHANNEL_ID": "CDAILY"}):
            notify([issue("workflow_failed", "daily")], {}, datetime(2026, 11, 13, tzinfo=KST), "retired-webhook", session=session)
        self.assertEqual(session.post.call_args.args[0], API_URL)
        self.assertNotIn("thread_ts", session.post.call_args.kwargs["json"])

    def test_v2_missing_alert_token_is_fail_closed(self):
        session = Mock()
        with patch.dict(os.environ, {"SLACK_DELIVERY_VERSION": "v2", "SLACK_BOT_TOKEN": "", "SLACK_CHANNEL_ID": "CDAILY"}):
            with self.assertRaises(RuntimeError):
                notify([issue("workflow_failed")], {}, datetime(2026, 11, 13, tzinfo=KST), "retired-webhook", session=session)
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
