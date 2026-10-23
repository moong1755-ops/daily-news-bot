import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import requests

from src.processor import jev
from tools import run_jev_shadow

_UNSET = object()


class _Response:
    def __init__(self, status_code=200, payload=_UNSET):
        self.status_code = status_code
        self._payload = {} if payload is _UNSET else payload
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _success_payload():
    result = {
        "model": "jev-test",
        "answers": {
            "is_editorially_qualified": {"type": "noul", "noul": 0.91},
            "article_form": {
                "type": "choice", "choice": "reported_event", "confidence": 0.88,
            },
            "primary_desk": {
                "type": "choice", "choice": "impact", "confidence": 0.82,
            },
            "impact_angle": {
                "type": "choice", "choice": "investment", "confidence": 0.79,
            },
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    for key, answer in result["answers"].items():
        if answer["type"] == "choice":
            answer["probabilities"] = {
                option: float(option == answer["choice"])
                for option in jev.article_questions()[key]["criteria"]
            }
    return result


def _noise_payload(job=0.02, promotion=0.03):
    return {"model": "jev-test", "answers": {
        "job_posting": {"type": "noul", "noul": job},
        "promotion": {"type": "noul", "noul": promotion},
    }, "usage": {"input_tokens": 110, "output_tokens": 40}}


class JevTests(unittest.TestCase):
    def test_disabled_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(jev.is_enabled())

    def test_judge_article_returns_compact_typed_result(self):
        session = mock.Mock()
        session.post.return_value = _Response(payload=_success_payload())

        result = jev.judge_article(
            {
                "title": "Climate fund closes $200 million vehicle",
                "description": "The fund will invest in transition technology.",
                "source": "Example News",
            },
            api_key="test-key",
            session=session,
        )

        self.assertEqual(result["primary_desk"], "impact")
        self.assertEqual(result["impact_angle"], "investment")
        self.assertEqual(result["qualified_probability"], 0.91)
        request = session.post.call_args
        self.assertEqual(request.kwargs["json"]["model"], "jev-latest")
        self.assertNotIn("test-key", str(request.kwargs["json"]))
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_retryable_status_is_retried(self):
        for status in (429, 503, 529):
            with self.subTest(status=status):
                session = mock.Mock()
                session.post.side_effect = [
                    _Response(status_code=status),
                    _Response(payload=_success_payload()),
                ]
                with mock.patch.object(jev.time, "sleep"):
                    result = jev.judge_article(
                        {"title": "Material policy change"},
                        api_key="test-key", session=session,
                    )
                self.assertEqual(result["article_form"], "reported_event")
                self.assertEqual(session.post.call_count, 2)

    def test_persistent_service_failure_stops_at_retry_limit(self):
        session = mock.Mock()
        session.post.return_value = _Response(status_code=503)
        with mock.patch.object(jev.time, "sleep"), self.assertRaisesRegex(jev.JevError, "HTTP 503"):
            jev.flag_noise({"title": "test"}, api_key="dummy", session=session)
        self.assertEqual(session.post.call_count, jev.MAX_ATTEMPTS)

    def test_invalid_key_gets_safe_error(self):
        session = mock.Mock()
        session.post.return_value = _Response(status_code=401)

        with self.assertRaisesRegex(jev.JevError, "유효하지 않습니다"):
            jev.judge_article(
                {"title": "Material policy change"},
                api_key="bad-key",
                session=session,
            )

    def test_network_failure_gets_bounded_retries(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("offline")

        with mock.patch.object(jev.time, "sleep"):
            with self.assertRaisesRegex(jev.JevError, "연결 실패"):
                jev.judge_article(
                    {"title": "Material policy change"},
                    api_key="test-key",
                    session=session,
                )

        self.assertEqual(session.post.call_count, jev.MAX_ATTEMPTS)

    def test_non_object_json_is_a_controlled_error_not_attribute_error(self):
        for value in (None, [], "bad", 42, True):
            with self.subTest(value=value):
                session = mock.Mock()
                session.post.return_value = _Response(payload=value)
                with self.assertRaises(jev.JevError):
                    jev.judge_article({"title": "test"}, api_key="dummy", session=session)
                session.post.assert_called_once()

    def test_non_finite_and_out_of_range_probabilities_are_rejected(self):
        for value in (-0.01, 1.01, float("nan"), float("inf"), True, "0.9", None):
            with self.subTest(value=value):
                session = mock.Mock()
                session.post.return_value = _Response(payload=_noise_payload(job=value))
                with self.assertRaises(jev.JevError):
                    jev.flag_noise({"title": "test"}, api_key="dummy", session=session)

    def test_missing_answer_or_wrong_answer_type_is_not_a_clear_article(self):
        missing = _noise_payload()
        missing["answers"].pop("promotion")
        wrong_type = _noise_payload()
        wrong_type["answers"]["job_posting"]["type"] = "choice"
        for payload in (missing, wrong_type):
            session = mock.Mock()
            session.post.return_value = _Response(payload=payload)
            with self.assertRaises(jev.JevError):
                jev.flag_noise({"title": "test"}, api_key="dummy", session=session)

    def test_unknown_choice_and_invalid_confidence_are_rejected(self):
        for field, value in (("choice", "unknown"), ("choice", []), ("confidence", -2),
                             ("confidence", True), ("confidence", float("nan"))):
            with self.subTest(field=field, value=value):
                payload = _success_payload()
                payload["answers"]["primary_desk"][field] = value
                session = mock.Mock()
                session.post.return_value = _Response(payload=payload)
                with self.assertRaises(jev.JevError):
                    jev.judge_article({"title": "test"}, api_key="dummy", session=session)

    def test_invalid_distribution_is_rejected(self):
        for distribution in (None, {}, {"impact": 1.0}, {"unknown": 1.0},
                             {k: 0.0 for k in jev.article_questions()["primary_desk"]["criteria"]}):
            session = mock.Mock()
            payload = _success_payload()
            payload["answers"]["primary_desk"]["probabilities"] = distribution
            session.post.return_value = _Response(payload=payload)
            with self.assertRaises(jev.JevError):
                jev.judge_article({"title": "test"}, api_key="dummy", session=session)

    def test_usage_must_not_crash_report_aggregation(self):
        for usage in (None, [], {"input_tokens": -1, "output_tokens": 3},
                      {"input_tokens": True, "output_tokens": 3},
                      {"input_tokens": "secret", "output_tokens": 3}):
            session = mock.Mock()
            payload = _noise_payload()
            payload["usage"] = usage
            session.post.return_value = _Response(payload=payload)
            with self.assertRaises(jev.JevError):
                jev.flag_noise({"title": "test"}, api_key="dummy", session=session)

    def test_request_error_does_not_echo_secret_and_redirects_are_disabled(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("Bearer private-test-token")
        with mock.patch.object(jev.time, "sleep"), self.assertRaises(jev.JevError) as caught:
            jev.flag_noise({"title": "test"}, api_key="private-test-token", session=session)
        self.assertNotIn("private-test-token", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_http_redirect_is_an_error_not_a_json_response(self):
        session = mock.Mock()
        response = mock.Mock(status_code=302)
        session.post.return_value = response
        with self.assertRaisesRegex(jev.JevError, "HTTP 302"):
            jev.flag_noise({"title": "test"}, api_key="dummy", session=session)
        response.json.assert_not_called()

    def test_noise_flags_are_independent_and_only_annotations(self):
        cases = [(0.7999, 0.1, [], "uncertain"), (0.8, 0.1, ["job_posting"], "suspected_noise"),
                 (0.9, 0.9, ["job_posting", "promotion"], "suspected_noise"),
                 (0.0, 0.0, [], "no_flag"), (0.2, 0.2, [], "no_flag"),
                 (0.0, 1.0, ["promotion"], "suspected_noise")]
        for job, promo, expected_flags, status in cases:
            with self.subTest(job=job, promo=promo):
                session = mock.Mock()
                session.post.return_value = _Response(payload=_noise_payload(job, promo))
                result = jev.flag_noise({"title": "test"}, api_key="dummy", session=session)
                self.assertEqual(result["suspected_noise"], expected_flags)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["automatic_action"], "none")
                self.assertNotIn("qualified_probability", result)
                self.assertNotIn("category", result)

    def test_original_input_and_old_judgments_never_change_or_leak_to_model(self):
        article = {"title": "번역 제목", "title_orig": "Original headline", "summary": "Full evidence",
                   "label": "reject", "category": "impact", "editor_score": 2,
                   "verdict": "sent", "editor_reason": "private_existing_judgment"}
        original = copy.deepcopy(article)
        session = mock.Mock()
        session.post.return_value = _Response(payload=_noise_payload())
        result = jev.flag_noise(article, api_key="dummy", session=session)
        self.assertEqual(article, original)
        sent = session.post.call_args.kwargs["json"]
        self.assertEqual(set(sent["questions"]), {"job_posting", "promotion"})
        self.assertEqual(sent["state"]["title"], "Original headline")
        self.assertEqual(sent["state"]["description"], "Full evidence")
        self.assertNotIn("private_existing_judgment", json.dumps(sent))
        self.assertNotIn("editor_score", sent["state"])
        self.assertEqual(result["evidence_level"], "title_and_summary")

    def test_empty_or_oversized_input_is_skipped_not_silently_truncated(self):
        session = mock.Mock()
        for article in ({}, {"title": "x" * (jev.MAX_STATE_CHARS + 1)}):
            with self.assertRaises(jev.JevError):
                jev.flag_noise(article, api_key="dummy", session=session)
        session.post.assert_not_called()

    def test_invalid_threshold_is_rejected_before_network(self):
        session = mock.Mock()
        for value in (True, float("nan"), 0.5, 1.1, "0.8"):
            with self.assertRaises(jev.JevError):
                jev.flag_noise({"title": "test"}, threshold=value, api_key="dummy", session=session)
        session.post.assert_not_called()


class JevShadowCliTests(unittest.TestCase):
    def test_default_limits_to_three_preserves_full_titles_and_does_not_send(self):
        title = "긴 제목" * 100
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "input.json"
            output = Path(temp) / "output.json"
            original = json.dumps({"decisions": [{"title": title, "verdict": "sent"}] * 5})
            source.write_text(original, encoding="utf-8")
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "dummy"}), \
                 mock.patch.object(jev.requests, "post", return_value=_Response(payload=_noise_payload())) as post, \
                 redirect_stdout(io.StringIO()) as captured:
                code = run_jev_shadow.main(["--file", str(source), "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(post.call_count, 3)
            self.assertTrue(all(c.args[0] == jev.API_URL for c in post.call_args_list))
            self.assertEqual(len(report["records"]), 3)
            self.assertEqual(report["records"][0]["existing_label"], "sent")
            self.assertIn(title, captured.getvalue())
            self.assertEqual(report["records"][0]["title"], title)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual({p.name for p in Path(temp).iterdir()}, {"input.json", "output.json"})

    def test_malformed_response_is_unavailable_and_later_article_still_processed(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"
            with mock.patch.object(run_jev_shadow, "load_articles", return_value=[{"title": "a"}, {"title": "b"}]), \
                 mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "dummy"}), \
                 mock.patch.object(jev.requests, "post", side_effect=[_Response(payload=None), _Response(payload=_noise_payload())]), \
                 redirect_stdout(io.StringIO()):
                code = run_jev_shadow.main(["--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(report["records"][0]["result"]["status"], "unavailable")
            self.assertNotIn("probabilities", report["records"][0]["result"])
            self.assertEqual(report["records"][1]["result"]["status"], "no_flag")

    def test_invalid_inputs_do_not_call_api(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(jev.requests, "post") as post:
            source = Path(temp) / "invalid.json"
            for text in ("not json", '{"articles": [1]}', '{"articles": null}',
                         '{"articles":[{"title":"test","label":NaN}]}',
                         '{"articles":[{"title":"test","score":Infinity}]}'):
                source.write_text(text, encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run_jev_shadow.main(["--file", str(source)]), 2)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_jev_shadow.main(["--file", str(Path(temp) / "missing.json")]), 2)
            post.assert_not_called()

    def test_report_serialization_failure_does_not_leave_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"
            with mock.patch.object(run_jev_shadow, "load_articles", return_value=[{"title": "a", "label": float("nan")}]), \
                 mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "dummy"}), \
                 mock.patch.object(jev.requests, "post", return_value=_Response(payload=_noise_payload())), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run_jev_shadow.main(["--output", str(output)]), 2)
            self.assertFalse(output.exists())

    def test_existing_report_is_not_overwritten_or_rebilled(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "keep.json"
            output.write_text("keep me", encoding="utf-8")
            with mock.patch.object(jev.requests, "post") as post, redirect_stdout(io.StringIO()):
                self.assertEqual(run_jev_shadow.main(["--output", str(output)]), 2)
            post.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_bad_limits_and_thresholds_cannot_trigger_calls(self):
        for args in (["--limit", "0"], ["--limit", "51"], ["--offset", "-1"],
                     ["--threshold", "nan"], ["--threshold", "0.5"]):
            with mock.patch.object(jev.requests, "post") as post, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run_jev_shadow.main(args)
                post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
