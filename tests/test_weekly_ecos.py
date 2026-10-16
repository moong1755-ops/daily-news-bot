"""Contract and safety tests for the weekly briefing's official domestic data."""

import json
import os
import traceback
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from src.config import WEEKLY_MARKET_INDICATORS
from src.weekly import market_data
from src.weekly.renderer import _market_block


START = date(2026, 8, 24)
END = date(2026, 8, 30)
PRIVATE_KEY = "private-ecos-token-never-publish"
DOMESTIC_CODES = {
    "kospi": ("802Y001", "0001000", "1980.01.04=100"),
    "kosdaq": ("802Y001", "0089000", "1996.07.01=1000"),
    "usd_krw": ("731Y003", "0000003", "원"),
}


def indicator(key="usd_krw"):
    return next(dict(item) for item in WEEKLY_MARKET_INDICATORS if item["key"] == key)


def row(key="usd_krw", day="20260828", value="1350.0", **changes):
    stat_code, item_code, unit_name = DOMESTIC_CODES[key]
    result = {
        "STAT_CODE": stat_code,
        "ITEM_CODE1": item_code,
        "UNIT_NAME": unit_name,
        "TIME": day,
        "DATA_VALUE": value,
    }
    result.update(changes)
    return result


def payload(rows, **changes):
    body = {"list_total_count": len(rows), "row": rows}
    body.update(changes)
    return {"StatisticSearch": body}


class Response:
    def __init__(self, body=None, *, status=200, json_error=None, text=""):
        self.body = body
        self.status_code = status
        self.json_error = json_error
        self.text = text

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP error for {PRIVATE_KEY}")


class Session:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class WeeklyEcosTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ECOS_API_KEY": PRIVATE_KEY})
        self.env.start()
        self.addCleanup(self.env.stop)

    def collect(self, rows, *, key="usd_krw", start=START, end=END):
        session = Session(Response(payload(rows)))
        snapshot = market_data._collect_one(indicator(key), start, end, session)
        return snapshot, session

    def test_domestic_codes_and_overseas_sources_are_explicit(self):
        for key, codes in DOMESTIC_CODES.items():
            with self.subTest(key=key):
                selected = indicator(key)
                self.assertEqual(selected["provider"], "ecos")
                self.assertEqual(
                    tuple(selected[field] for field in ("stat_code", "item_code", "unit_name")),
                    codes,
                )
        expected = {
            "sp500": "SP500", "nasdaq": "NASDAQCOM", "us_10y": "DGS10",
            "brent": "DCOILBRENTEU",
        }
        for key, series in expected.items():
            self.assertEqual(indicator(key)["provider"], "fred")
            self.assertEqual(indicator(key)["series_id"], series)
        self.assertIn("현물", indicator("brent")["label"])
        self.assertIn("15:30", indicator()["value_basis"])

    def test_weekly_closes_share_source_and_request_has_bounded_window(self):
        snapshot, session = self.collect([
            row(day="20260828", value="1350"),
            row(day="20260821", value="1400"),
            row(day="20260826", value="1360"),
        ])
        self.assertTrue(snapshot.available, snapshot.error)
        self.assertEqual(snapshot.provider, "ecos")
        self.assertEqual(snapshot.comparison.observed_on, date(2026, 8, 21))
        self.assertEqual(snapshot.latest.observed_on, date(2026, 8, 28))
        self.assertAlmostEqual(snapshot.change, (1350 / 1400 - 1) * 100)
        self.assertEqual(len(session.calls), 1)
        url, options = session.calls[0]
        self.assertEqual(urlsplit(url).hostname, "ecos.bok.or.kr")
        self.assertIn(f"/StatisticSearch/{PRIVATE_KEY}/json/kr/1/100/", url)
        self.assertTrue(url.endswith("/731Y003/D/20260814/20260830/0000003"))
        self.assertEqual(options["allow_redirects"], False)
        self.assertEqual(options["timeout"], 20)

    def test_all_domestic_labels_link_public_exact_latest_observation(self):
        for key, (stat_code, item_code, _) in DOMESTIC_CODES.items():
            with self.subTest(key=key):
                snapshot, _ = self.collect([
                    row(key, "20260821", "100"), row(key, "20260828", "101"),
                ], key=key)
                self.assertTrue(snapshot.available, snapshot.error)
                self.assertEqual(
                    snapshot.source_url,
                    "https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/1/10/"
                    f"{stat_code}/D/20260828/20260828/{item_code}",
                )
                output = _market_block((snapshot,), END)["text"]["text"]
                self.assertIn(f"<{snapshot.source_url}|{snapshot.label}", output)
                self.assertIn("08.21→08.28", output)
                self.assertIn(snapshot.value_basis, output)
                self.assertNotIn(PRIVATE_KEY, output)

    def test_holiday_uses_actual_observation_and_excludes_next_week(self):
        snapshot, _ = self.collect([
            row(day="20261002", value="1350.6"),
            row(day="20261008", value="1338.5"),
            row(day="20261012", value="9000"),
            row(day="20260920", value="8000"),
        ], start=date(2026, 10, 5), end=date(2026, 10, 11))
        self.assertTrue(snapshot.available, snapshot.error)
        self.assertEqual(snapshot.comparison.observed_on, date(2026, 10, 2))
        self.assertEqual(snapshot.latest.observed_on, date(2026, 10, 8))
        self.assertEqual(len(snapshot.points), 2)
        text = _market_block((snapshot,), date(2026, 10, 11))["text"]["text"]
        self.assertIn("10.02→10.08", text)
        self.assertIn("휴장·공표 지연 가능", text)

    def test_missing_comparison_does_not_use_older_week_or_invent_change(self):
        for days in (("20260814", "20260828"), ("20260821", "20260831")):
            with self.subTest(days=days):
                snapshot, _ = self.collect([row(day=day) for day in days])
                self.assertFalse(snapshot.available)
                self.assertIsNone(snapshot.change)
                self.assertIsNotNone(snapshot.error)

    def test_missing_or_sample_key_never_calls_network(self):
        for key in ("", "  ", "sample"):
            with self.subTest(key=key), patch.dict(os.environ, {"ECOS_API_KEY": key}):
                session = Session()
                snapshot = market_data._collect_one(indicator(), START, END, session)
                self.assertFalse(snapshot.available)
                self.assertIn("ECOS", snapshot.error)
                self.assertEqual(session.calls, [])

    def test_wrong_series_unit_or_date_is_not_silently_ranked_as_valid(self):
        invalid_fields = (
            {"STAT_CODE": "wrong"}, {"ITEM_CODE1": "0000013"},
            {"UNIT_NAME": "달러"}, {"TIME": "2026-08-28"},
            {"TIME": "202608"}, {"TIME": "20260230"}, {"TIME": None},
        )
        for changes in invalid_fields:
            with self.subTest(changes=changes):
                snapshot, _ = self.collect([row(day="20260821"), row(**changes)])
                self.assertFalse(snapshot.available)

    def test_bad_values_fail_instead_of_producing_extreme_changes(self):
        for value in ("NaN", "Infinity", "-Infinity", "oops", "0", "-1", {}, True):
            with self.subTest(value=value):
                snapshot, _ = self.collect([row(day="20260821"), row(value=value)])
                self.assertFalse(snapshot.available)
                self.assertIsNone(snapshot.change)

    def test_official_missing_value_markers_are_skipped_without_zero_fill(self):
        for value in (None, "", "-", "."):
            with self.subTest(value=value):
                snapshot, _ = self.collect([
                    row(day="20260821", value="1400"),
                    row(day="20260827", value="1350"),
                    row(value=value),
                ])
                self.assertTrue(snapshot.available, snapshot.error)
                self.assertEqual(snapshot.latest.observed_on, date(2026, 8, 27))

    def test_commas_and_whitespace_are_normalized(self):
        snapshot, _ = self.collect([
            row(day="20260821", value=" 1,400.0 ", UNIT_NAME=" 원 "),
            row(value="1,350.0"),
        ])
        self.assertTrue(snapshot.available, snapshot.error)
        self.assertEqual(snapshot.latest.value, 1350.0)

    def test_duplicate_dates_deduplicate_identical_but_reject_conflicting_values(self):
        snapshot, _ = self.collect([
            row(day="20260821", value="1400"), row(), row(),
        ])
        self.assertTrue(snapshot.available, snapshot.error)
        self.assertEqual(len(snapshot.points), 2)
        snapshot, _ = self.collect([
            row(day="20260821", value="1400"), row(), row(value="1351"),
        ])
        self.assertFalse(snapshot.available)

    def test_malformed_or_truncated_payload_cannot_be_used(self):
        cases = (
            None, [], {}, {"StatisticSearch": []},
            {"StatisticSearch": {"list_total_count": 2, "row": {}}},
            payload([row()], list_total_count=2),
            payload([row()], list_total_count=101),
            payload([row()], list_total_count="bad"),
            payload([row()], list_total_count=True),
            payload([None, row()]),
        )
        for body in cases:
            with self.subTest(body=body):
                session = Session(Response(body))
                snapshot = market_data._collect_one(indicator(), START, END, session)
                self.assertFalse(snapshot.available)
                self.assertIsNotNone(snapshot.error)

    def test_api_http_and_json_errors_are_sanitized_and_have_no_fallback(self):
        responses = [
            Response({"RESULT": {"CODE": "ERROR-100", "MESSAGE": PRIVATE_KEY}}),
            Response(payload([row(day="20260821"), row()]), status=302),
            Response(status=401), Response(status=500),
            Response(json_error=ValueError(f"bad JSON from {PRIVATE_KEY}")),
        ]
        for response in responses:
            with self.subTest(status=response.status_code, body=response.body):
                session = Session(response)
                snapshot = market_data._collect_one(indicator(), START, END, session)
                self.assertFalse(snapshot.available)
                self.assertNotIn(PRIVATE_KEY, repr(snapshot))
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(all("ecos.bok.or.kr" in url for url, _ in session.calls))

    def test_network_exception_and_traceback_cannot_leak_key(self):
        for exception in (
            requests.Timeout(f"request URL contained {PRIVATE_KEY}"),
            requests.ConnectionError(f"request URL contained {PRIVATE_KEY}"),
        ):
            with self.subTest(exception=type(exception).__name__):
                session = Session(error=exception)
                snapshot = market_data._collect_one(indicator(), START, END, session)
                self.assertFalse(snapshot.available)
                self.assertNotIn(PRIVATE_KEY, snapshot.error)
                with self.assertRaises(RuntimeError) as caught:
                    market_data._ecos_points(indicator(), START, END, Session(error=exception))
                formatted = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn(PRIVATE_KEY, formatted)

    def test_ecos_unavailability_does_not_block_any_overseas_indicator(self):
        class FredOnlySession(Session):
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "fredgraph.csv" not in url:
                    raise AssertionError("Missing ECOS key must not trigger another provider")
                series = kwargs["params"]["id"]
                return Response(text=f"observation_date,{series}\n2026-08-21,100\n2026-08-28,101\n")

        session = FredOnlySession()
        with patch.dict(os.environ, {"ECOS_API_KEY": ""}):
            snapshots = market_data.collect_market_snapshots(START, END, session)
        self.assertEqual(len(snapshots), 7)
        domestic = [item for item in snapshots if item.key in DOMESTIC_CODES]
        overseas = [item for item in snapshots if item.key not in DOMESTIC_CODES]
        self.assertTrue(all(not item.available for item in domestic))
        self.assertTrue(all(item.available and item.provider == "fred" for item in overseas))
        self.assertEqual(len(session.calls), 4)
        self.assertNotIn(PRIVATE_KEY, json.dumps([item.error for item in snapshots]))


if __name__ == "__main__":
    unittest.main()
