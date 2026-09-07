"""Collect compact weekly market indicators without blocking the briefing."""

from __future__ import annotations

import ast
import csv
import io
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Iterable

import requests

from ..config import WEEKLY_MARKET_INDICATORS, WEEKLY_MARKET_SPARKLINE_POINTS


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
NAVER_INDEX_URL = "https://api.finance.naver.com/siseJson.naver"
NAVER_FX_URL = "https://finance.naver.com/marketindex/exchangeDailyQuote.naver"
KRX_API_URLS = {
    "kospi": "https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd",
    "kosdaq": "https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd",
}
SPARKLINE_BARS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class MarketPoint:
    observed_on: date
    value: float


@dataclass(frozen=True)
class MarketSnapshot:
    key: str
    label: str
    provider: str
    change_unit: str
    points: tuple[MarketPoint, ...] = ()
    change: float | None = None
    sparkline: str = ""
    error: str | None = None
    source_url: str = ""
    comparison: MarketPoint | None = None
    value_basis: str = ""

    @property
    def latest(self) -> MarketPoint | None:
        return self.points[-1] if self.points else None

    @property
    def available(self) -> bool:
        return self.latest is not None


def make_sparkline(values: Iterable[float]) -> str:
    """Render values as fixed-width Unicode bars without external chart services."""
    series = list(values)
    if not series:
        return ""
    low, high = min(series), max(series)
    if high == low:
        return SPARKLINE_BARS[len(SPARKLINE_BARS) // 2] * len(series)
    scale = len(SPARKLINE_BARS) - 1
    return "".join(
        SPARKLINE_BARS[round((value - low) / (high - low) * scale)]
        for value in series
    )


def calculate_change(points: tuple[MarketPoint, ...], unit: str) -> float | None:
    if len(points) < 2:
        return None
    first, last = points[0].value, points[-1].value
    if unit == "basis_points":
        return (last - first) * 100
    if first == 0:
        return None
    return (last / first - 1) * 100


def _float(value: object) -> float:
    result = float(str(value).replace(",", "").strip())
    if not math.isfinite(result):
        raise ValueError("시장지표 값이 유한한 숫자가 아님")
    return result


class _ExchangeTable(HTMLParser):
    """Read only the dated rows of Naver's exchange quote table."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_table = False
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = "tbl_exchange" in dict(attrs).get("class", "").split()
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag == "td":
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._in_table and self._row:
            self.rows.append(self._row)
        elif tag == "table":
            self._in_table = False


def _naver_fx_points(indicator, start_date, end_date, session) -> tuple[MarketPoint, ...]:
    """Compare both weeks using the same daily bank base rate, never live FX."""
    points: dict[date, MarketPoint] = {}
    collection_start = start_date - timedelta(days=10)
    for page in range(1, int(indicator.get("history_max_pages", 12)) + 1):
        response = session.get(
            NAVER_FX_URL,
            params={"marketindexCd": indicator["marketindex_code"], "page": page},
            headers={"User-Agent": "Mozilla/5.0 daily-news-bot/weekly-briefing"},
            timeout=20,
        )
        response.raise_for_status()
        parser = _ExchangeTable()
        parser.feed(response.text)
        page_dates = []
        for row in parser.rows:
            if len(row) < 2:
                continue
            try:
                observed_on = datetime.strptime(row[0], "%Y.%m.%d").date()
                value = _float(row[1])
            except (TypeError, ValueError):
                continue
            page_dates.append(observed_on)
            if collection_start <= observed_on <= end_date and value > 0:
                points[observed_on] = MarketPoint(observed_on, value)
        if not page_dates:
            raise RuntimeError("네이버 환율 일별시세 응답에 유효한 날짜 없음")
        if min(page_dates) <= collection_start:
            break
    return tuple(points[day] for day in sorted(points))


def _fred_points(
    indicator: dict,
    start_date: date,
    end_date: date,
    session,
) -> tuple[MarketPoint, ...]:
    collection_start = start_date - timedelta(days=10)
    response = session.get(
        FRED_CSV_URL,
        params={
            "id": indicator["series_id"],
            "cosd": collection_start.isoformat(),
            "coed": end_date.isoformat(),
        },
        headers={"User-Agent": "daily-news-bot/weekly-briefing"},
        timeout=20,
    )
    response.raise_for_status()

    rows = csv.DictReader(io.StringIO(response.text))
    points: list[MarketPoint] = []
    for row in rows:
        raw_date = row.get("observation_date") or row.get("DATE")
        raw_value = row.get(indicator["series_id"])
        if not raw_date or raw_value in (None, "", "."):
            continue
        try:
            points.append(MarketPoint(date.fromisoformat(raw_date), _float(raw_value)))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(points, key=lambda point: point.observed_on))


def _krx_point(
    indicator: dict,
    observed_on: date,
    auth_key: str,
    session,
) -> MarketPoint | None:
    endpoint = os.getenv(
        f"KRX_{indicator['key'].upper()}_API_URL",
        KRX_API_URLS[indicator["key"]],
    )
    response = session.get(
        endpoint,
        params={"basDd": observed_on.strftime("%Y%m%d")},
        headers={"AUTH_KEY": auth_key},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("KRX 응답이 JSON 객체가 아님")
    rows = payload.get("OutBlock_1") or []
    if not isinstance(rows, list):
        raise RuntimeError("KRX OutBlock_1 형식 오류")
    expected = str(indicator.get("index_name") or indicator["label"]).replace(" ", "").casefold()
    for row in rows:
        actual = str(row.get("IDX_NM") or "").replace(" ", "").casefold()
        if actual != expected:
            continue
        raw_date = str(row.get("BAS_DD") or observed_on.strftime("%Y%m%d"))
        try:
            point_date = date.fromisoformat(raw_date) if "-" in raw_date else date(
                int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8])
            )
            return MarketPoint(point_date, _float(row["CLSPRC_IDX"]))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _krx_points(
    indicator: dict,
    start_date: date,
    end_date: date,
    session,
    auth_key: str,
) -> tuple[MarketPoint, ...]:
    points: list[MarketPoint] = []
    lookback_days = (end_date - start_date).days + 11
    for days_ago in range(lookback_days):
        candidate = end_date - timedelta(days=days_ago)
        if candidate.weekday() >= 5:
            continue
        point = _krx_point(indicator, candidate, auth_key, session)
        if point is not None:
            points.append(point)
    return tuple(sorted(points, key=lambda point: point.observed_on))


def _naver_points(
    indicator: dict,
    start_date: date,
    end_date: date,
    session,
) -> tuple[MarketPoint, ...]:
    """Read keyless daily index closes from Naver Finance's index history."""
    symbol = str(indicator.get("fallback_symbol") or "").strip()
    if not symbol:
        raise RuntimeError("국내 지수 보조 심볼이 설정되지 않음")
    collection_start = start_date - timedelta(days=10)
    response = session.get(
        NAVER_INDEX_URL,
        params={
            "symbol": symbol,
            "requestType": "1",
            "startTime": collection_start.strftime("%Y%m%d"),
            "endTime": end_date.strftime("%Y%m%d"),
            "timeframe": "day",
        },
        headers={"User-Agent": "Mozilla/5.0 daily-news-bot/weekly-briefing"},
        timeout=20,
    )
    response.raise_for_status()
    try:
        rows = ast.literal_eval(response.text.strip())
    except (SyntaxError, ValueError) as exc:
        raise RuntimeError("네이버 금융 지수 응답 형식 오류") from exc
    if not isinstance(rows, list):
        raise RuntimeError("네이버 금융 지수 응답이 목록이 아님")

    points: list[MarketPoint] = []
    for row in rows[1:]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        raw_date, raw_close = str(row[0]), row[4]
        try:
            point_date = date(
                int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8])
            )
            points.append(MarketPoint(point_date, _float(raw_close)))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(points, key=lambda point: point.observed_on))


def _weekly_comparison_points(
    points: tuple[MarketPoint, ...],
    start_date: date,
    end_date: date,
) -> tuple[MarketPoint, MarketPoint]:
    previous_week = [
        point for point in points
        if start_date - timedelta(days=7) <= point.observed_on < start_date
    ]
    current_week = [
        point for point in points
        if start_date <= point.observed_on <= end_date
    ]
    if not previous_week:
        raise RuntimeError("전주 마지막 거래일 관측값 없음")
    if not current_week:
        raise RuntimeError("이번 주 마지막 거래일 관측값 없음")
    return previous_week[-1], current_week[-1]


def _collect_one(
    indicator: dict,
    start_date: date,
    end_date: date,
    session,
) -> MarketSnapshot:
    point_count = int(WEEKLY_MARKET_SPARKLINE_POINTS)
    try:
        provider = str(indicator["provider"])
        source_url = str(indicator.get("source_url") or "")
        if indicator["provider"] == "fred":
            points = _fred_points(indicator, start_date, end_date, session)
        elif indicator["provider"] == "naver_fx":
            points = _naver_fx_points(indicator, start_date, end_date, session)
        elif indicator["provider"] == "krx":
            auth_key = os.getenv("KRX_AUTH_KEY", "").strip()
            points = ()
            if auth_key:
                try:
                    points = _krx_points(
                        indicator, start_date, end_date, session, auth_key
                    )
                    _weekly_comparison_points(points, start_date, end_date)
                except (RuntimeError, ValueError, requests.RequestException):
                    points = ()
            if not points:
                if indicator.get("fallback_provider") != "naver":
                    raise RuntimeError("KRX 지수 수집 실패 및 보조 제공처 없음")
                points = _naver_points(indicator, start_date, end_date, session)
                provider = "naver"
                source_url = str(indicator.get("fallback_source_url") or "")
        else:
            raise ValueError(f"지원하지 않는 제공처: {indicator['provider']}")
        if not points:
            raise RuntimeError("최근 관측값 없음")
        comparison, latest = _weekly_comparison_points(points, start_date, end_date)
        display_points = tuple(
            point for point in points if point.observed_on <= latest.observed_on
        )[-point_count:]
        return MarketSnapshot(
            key=indicator["key"],
            label=indicator["label"],
            provider=provider,
            change_unit=indicator["change_unit"],
            points=display_points,
            change=calculate_change((comparison, latest), indicator["change_unit"]),
            sparkline=make_sparkline(point.value for point in display_points),
            source_url=source_url,
            comparison=comparison,
            value_basis=str(indicator.get("value_basis") or ""),
        )
    except (KeyError, RuntimeError, ValueError, requests.RequestException) as exc:
        return MarketSnapshot(
            key=str(indicator.get("key") or "unknown"),
            label=str(indicator.get("label") or indicator.get("key") or "알 수 없는 지표"),
            provider=str(indicator.get("provider") or "unknown"),
            change_unit=str(indicator.get("change_unit") or "percent"),
            error=str(exc),
            source_url=str(indicator.get("source_url") or ""),
        )


def collect_market_snapshots(
    start_date: date,
    end_date: date | None = None,
    session=requests,
) -> tuple[MarketSnapshot, ...]:
    """Compare the last close before the week with the last close in the week."""
    if end_date is None:
        end_date = start_date
        start_date = end_date - timedelta(days=6)
    return tuple(
        _collect_one(indicator, start_date, end_date, session)
        for indicator in WEEKLY_MARKET_INDICATORS
    )
