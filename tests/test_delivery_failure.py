import unittest
from unittest import mock

from src import bot


class DeliveryFailureTests(unittest.TestCase):
    def test_main_fails_after_auditing_when_slack_did_not_send(self):
        article = {
            "title": "Important AI launch",
            "link": "https://example.com/news",
            "category": "🤖 AI",
            "source": "Example",
            "relevance": 9,
        }
        with mock.patch.object(bot, "HAS_NEWSLETTERS", False), \
                mock.patch.object(bot, "load_lines", return_value=[]), \
                mock.patch.object(bot.hackernews, "fetch", return_value=([], [])), \
                mock.patch.object(bot.rss_feeds, "fetch", return_value=([article], [])), \
                mock.patch.object(bot, "filter_to_as_of_date", side_effect=lambda items: items), \
                mock.patch("src.processor.deduplicator._get_model", side_effect=RuntimeError("off")), \
                mock.patch.object(bot, "is_relevant", return_value=True), \
                mock.patch.object(bot, "deduplicate_and_merge", return_value=([article], [])), \
                mock.patch.object(bot, "summarize", return_value=(article, [])), \
                mock.patch.object(bot, "select_for_briefing", return_value=([article], [], [])), \
                mock.patch.object(bot, "send_aggregated_slack_news", return_value=(False, [])), \
                mock.patch.object(bot, "_save_daily_review"), \
                mock.patch.object(bot, "save_run_decisions") as decisions, \
                mock.patch.object(bot, "save_lines"):
            with self.assertRaisesRegex(RuntimeError, "Slack 발송에 실패"):
                bot.main()

        self.assertEqual(decisions.call_args.args[2], [])


if __name__ == "__main__":
    unittest.main()
