import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.quality import checks, monitor, runtime
from src.weekly.market_data import MarketPoint, MarketSnapshot


class QualityChecksTest(unittest.TestCase):
    def article(self, **extra):
        return {"title": "기후펀드 신규 투자 확대", "title_orig": "Climate fund raises new capital",
                "category": "🌱 임팩트", "url": "https://example.org/news/1", **extra}

    def test_exact_duplicate_not_company_similarity(self):
        a = self.article()
        findings, blocked = checks.inspect_articles([a, dict(a), self.article(url="https://example.org/news/2")])
        self.assertEqual(blocked, {1})
        self.assertEqual(sum(f["code"] == "exact_duplicate" for f in findings), 1)

    def test_healthy_semantic_one_call(self):
        generate = Mock(return_value='{"issues":[]}')
        self.assertEqual(checks.semantic_audit([self.article()], generate), [])
        generate.assert_called_once()
        self.assertNotIn("https://", generate.call_args.args[0])

    def test_empty_no_model_call(self):
        generate = Mock()
        self.assertEqual(checks.semantic_audit([], generate), [])
        generate.assert_not_called()

    def test_invalid_id_quote_code_and_duplicate_rejected(self):
        base = {"id": "0", "code": "llm_claim", "quote": "Climate fund", "other_id": None}
        for changed in ({"id": "99"}, {"quote": "invented evidence"}, {"code": "send_secret"},
                        {"code": "llm_duplicate", "other_id": "0"}):
            generate = Mock(return_value=json.dumps({"issues": [{**base, **changed}]}))
            self.assertEqual(checks.semantic_audit([self.article()], generate)[0]["code"], "audit_invalid")

    def test_grounded_finding_advisory_only(self):
        article = self.article()
        original = dict(article)
        generate = Mock(return_value=json.dumps({"issues": [{"id": "0", "code": "llm_claim", "quote": "Climate fund"}]}))
        findings = checks.semantic_audit([article], generate)
        self.assertTrue(findings[0]["advisory"])
        self.assertEqual(article, original)

    def test_provider_failure_is_safe(self):
        result = checks.semantic_audit([self.article()], Mock(side_effect=RuntimeError("private URL")))
        self.assertEqual(result, [checks.issue("audit_invalid", "provider")])

    def test_market_nan_and_missing_credentials(self):
        bad = MarketSnapshot("kospi", "KOSPI", "ecos", "percent",
                             points=(MarketPoint(date(2026, 11, 6), float("nan")),),
                             comparison=MarketPoint(date(2026, 10, 30), 3000))
        missing = MarketSnapshot("usdkrw", "USD/KRW", "ecos", "percent", error_code="missing_credentials")
        codes = {f["code"] for f in checks.inspect_markets([bad, missing], date(2026, 11, 8))}
        self.assertEqual(codes, {"market_invalid", "market_missing"})

    def test_valid_negative_rate(self):
        rate = MarketSnapshot("rate", "rate", "fred", "basis_points",
                              points=(MarketPoint(date(2026, 11, 6), -0.1),),
                              comparison=MarketPoint(date(2026, 10, 30), -0.2))
        self.assertEqual(checks.inspect_markets([rate], date(2026, 11, 8)), [])


class QualityMonitorTest(unittest.TestCase):
    now = datetime(2026, 11, 9, 11, tzinfo=runtime.KST)

    def test_no_alert_healthy(self):
        session = Mock()
        self.assertFalse(monitor.notify([], {}, self.now, "", session=session))
        session.post.assert_not_called()

    def test_cooldown_only_after_success(self):
        session = Mock()
        session.post.return_value.raise_for_status.return_value = None
        findings = [checks.issue("translation_missing", "AI")]
        ledger = {}
        self.assertTrue(monitor.notify(findings, ledger, self.now, "secret", session=session))
        self.assertFalse(monitor.notify(findings, ledger, self.now + timedelta(hours=1), "secret", session=session))
        self.assertEqual(session.post.call_count, 1)

    def test_failed_alert_does_not_suppress_retry_or_leak(self):
        session, ledger = Mock(), {}
        session.post.side_effect = requests.RequestException("https://private-webhook")
        with self.assertRaisesRegex(RuntimeError, "Quality alert delivery failed") as error:
            monitor.notify([checks.issue("workflow_failed")], ledger, self.now, "secret", session=session)
        self.assertEqual(ledger, {})
        self.assertNotIn("private-webhook", str(error.exception))

    def test_watchdog_deadlines(self):
        self.assertEqual(monitor.watchdog([], [], self.now.replace(hour=8)), [])
        self.assertEqual({i["code"] for i in monitor.watchdog([], [], self.now)}, {"daily_late", "weekly_late"})
        self.assertEqual(monitor.watchdog([{"edition_date": "2026-11-09"}], [{"end_date": "2026-11-08"}], self.now), [])

    def test_history_baseline_requires_different_days(self):
        report = {"kind": "daily", "date": "2026-11-09", "collection_counts": {}}
        history = [{"kind": "daily", "date": "2026-11-06", "collection_counts": {"newsletter:kdi": 10}}] * 4
        self.assertEqual(monitor.collection_changes(report, history), [])
        history += [{"kind": "daily", "date": day, "collection_counts": {"newsletter:kdi": 10}}
                    for day in ("2026-11-05", "2026-11-04")]
        self.assertEqual(monitor.collection_changes(report, history)[0]["code"], "collection_drop")

    def test_weekly_once_even_when_healthy(self):
        review = monitor.weekly_review([], [], self.now, set())
        self.assertEqual(review["issues"], [])
        self.assertIsNone(monitor.weekly_review([], [], self.now, {review["period"]}))

    def test_weekly_no_cross_day_false_duplicate(self):
        articles = [{"title": "기후 투자 증가", "url": "https://example.org/a"}]
        rows = [{"edition_date": day, "articles": articles} for day in ("2026-11-05", "2026-11-06")]
        review = monitor.weekly_review(rows, [], self.now, set())
        self.assertEqual(review["issues"], [])
        rows.append(dict(rows[-1]))
        self.assertTrue(any(f["scope"] == "duplicate_delivery" for f in monitor.weekly_review(rows, [], self.now, set())["issues"]))

    def test_report_delivery_matching(self):
        report = {"run_id": "123", "kind": "daily", "delivery": "sent"}
        self.assertEqual(monitor.delivery_findings(report, [{"github": {"run_id": "123"}}], "success"), [])
        self.assertEqual(monitor.delivery_findings(report, [], "success")[0]["code"], "delivery_unverified")
        report["delivery"] = "skipped"
        self.assertEqual(monitor.delivery_findings(report, [], "success"), [])

    def test_manual_preview_never_posts_or_writes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"QUALITY_SOURCE_RUN_ID": ""}), \
                patch("sys.argv", ["quality", "--data-dir", directory]), patch.object(monitor, "notify") as send:
            monitor.main()
            send.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_report_preview_never_posts_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"run_id": "42", "preview": True}), encoding="utf-8")
            with patch.dict(os.environ, {"QUALITY_SOURCE_RUN_ID": "42"}), \
                    patch("sys.argv", ["quality", "--data-dir", directory, "--report", str(path), "--notify"]), \
                    patch.object(monitor, "notify") as send:
                monitor.main()
                send.assert_not_called()
                self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_runtime_disabled_no_writes(self):
        with patch.dict(os.environ, {"QUALITY_MONITORING": ""}), patch.object(Path, "write_text") as write:
            runtime.start("daily")
            runtime.final_check([{}])
            write.assert_not_called()

    def test_telemetry_write_failure_does_not_block_news(self):
        with patch.dict(os.environ, {"QUALITY_MONITORING": "1"}), patch.object(Path, "write_text", side_effect=OSError):
            runtime.start("daily")

    def test_checker_exception_does_not_block_news(self):
        with patch.dict(os.environ, {"QUALITY_MONITORING": "1"}), \
                patch.object(runtime, "inspect_articles", side_effect=ValueError), patch.object(runtime, "update") as update:
            runtime.final_check([{}])
            self.assertEqual(update.call_args.kwargs["issues"][0]["code"], "audit_invalid")

    def test_quality_provider_never_retries(self):
        from src.processor import reranker
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                patch.object(reranker.llm_cache, "lookup", return_value=None), \
                patch.object(reranker.llm_cache, "guard_network"), \
                patch.object(reranker.requests, "post", side_effect=requests.Timeout) as post:
            self.assertIsNone(reranker.generate_quality_json("audit"))
            post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
