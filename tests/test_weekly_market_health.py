"""Report partial market failures without changing delivery or calling Slack."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.weekly import check_markets
from src.weekly.market_data import MarketPoint, MarketSnapshot


def snapshot(*, missing=False):
    if missing:
        return MarketSnapshot("usd_krw", "USD/KRW", "ecos", "percent",
                              error="ECOS_API_KEY 설정 필요", error_code="missing_credentials")
    return MarketSnapshot(
        "sp500", "S&P 500", "fred", "percent",
        points=(MarketPoint(date(2026, 10, 16), 101),),
        comparison=MarketPoint(date(2026, 10, 9), 100), change=1.0,
        source_url="https://fred.stlouisfed.org/series/SP500",
    )


class WeeklyMarketHealthTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_partial_failure_creates_visible_action_warning_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}):
                output = io.StringIO()
                with redirect_stdout(output):
                    issues = check_markets.report_market_health((snapshot(), snapshot(missing=True)))
            saved_summary = summary.read_text(encoding="utf-8")
        self.assertEqual(len(issues), 1)
        self.assertIn("1/2개 정상", saved_summary)
        self.assertIn("ECOS_API_KEY 설정 필요", saved_summary)
        self.assertIn("::warning title=주간 시장지표 미수집::USD/KRW:", output.getvalue())

    def test_warning_escapes_newlines_and_percent_signs(self):
        failed = MarketSnapshot("x", "x", "fred", "percent", error="50%\n::error::provider text")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(io.StringIO()) as output:
            check_markets.report_market_health((failed,))
        annotations = [line for line in output.getvalue().splitlines() if line.startswith("::warning")]
        self.assertEqual(len(annotations), 1)
        self.assertIn("50%25%0A::error::provider text", annotations[0])

    def test_market_only_check_does_not_call_slack_and_fails_if_incomplete(self):
        with patch.object(check_markets, "collect_market_snapshots", return_value=(snapshot(), snapshot(missing=True))) as collect, \
                patch("requests.post") as post, \
                redirect_stdout(io.StringIO()) as output:
            result = check_markets.main(["--end-date", "2026-10-18"])
        collect.assert_called_once_with(date(2026, 10, 12), date(2026, 10, 18))
        post.assert_not_called()
        self.assertEqual(result, 1)
        self.assertIn("2026-10-09→2026-10-16", output.getvalue())
        self.assertIn("https://fred.stlouisfed.org/series/SP500", output.getvalue())

    def test_market_only_check_returns_success_when_complete(self):
        with patch.object(check_markets, "collect_market_snapshots", return_value=(snapshot(),)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(check_markets.main(["--end-date", "2026-10-18"]), 0)

    def test_non_sunday_is_rejected_before_network(self):
        with patch.object(check_markets, "collect_market_snapshots") as collect, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                check_markets.main(["--end-date", "2026-10-19"])
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
