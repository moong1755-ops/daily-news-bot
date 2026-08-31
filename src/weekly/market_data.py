"""Collect compact weekly market indicators without blocking the briefing."""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import requests

from ..config import WEEKLY_MARKET_INDICATORS, WEEKLY_MARKET_SPARKLINE_POINTS


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
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
    return float(str(value).replace(",", "").strip())


def _fred_points(
    indicator: dict,
    end_date: date,
    session,
    point_count: int,
) -> tuple[MarketPoint, ...]:
    start_date = end_date - timedelta(days=21)
    response = session.get(
        FRED_CSV_URL,
        params={
            "id": indicator["series_id"],
            "cosd": start_date.isoformat(),
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
    return tuple(sorted(points, key=lambda point: point.observed_on)[-point_count:])


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
    end_date: date,
    session,
    point_count: int,
) -> tuple[MarketPoint, ...]:
    auth_key = os.getenv("KRX_AUTH_KEY", "").strip()
    if not auth_key:
        raise RuntimeError("KRX_AUTH_KEY가 설정되지 않음")

    points: list[MarketPoint] = []
    for days_ago in range(point_count + 7):
        candidate = end_date - timedelta(days=days_ago)
        if candidate.weekday() >= 5:
            continue
        point = _krx_point(indicator, candidate, auth_key, session)
        if point is not None:
            points.append(point)
        if len(points) >= point_count:
            break
    return tuple(sorted(points, key=lambda point: point.observed_on))


def _collect_one(indicator: dict, end_date: date, session) -> MarketSnapshot:
    point_count = int(WEEKLY_MARKET_SPARKLINE_POINTS)
    try:
        if indicator["provider"] == "fred":
            points = _fred_points(indicator, end_date, session, point_count)
        elif indicator["provider"] == "krx":
            points = _krx_points(indicator, end_date, session, point_count)
        else:
            raise ValueError(f"지원하지 않는 제공처: {indicator['provider']}")
        if not points:
            raise RuntimeError("최근 관측값 없음")
        return MarketSnapshot(
            key=indicator["key"],
            label=indicator["label"],
            provider=indicator["provider"],
            change_unit=indicator["change_unit"],
            points=points,
            change=calculate_change(points, indicator["change_unit"]),
            sparkline=make_sparkline(point.value for point in points),
        )
    except (KeyError, RuntimeError, ValueError, requests.RequestException) as exc:
        return MarketSnapshot(
            key=str(indicator.get("key") or "unknown"),
            label=str(indicator.get("label") or indicator.get("key") or "알 수 없는 지표"),
            provider=str(indicator.get("provider") or "unknown"),
            change_unit=str(indicator.get("change_unit") or "percent"),
            error=str(exc),
        )


def collect_market_snapshots(
    end_date: date,
    session=requests,
) -> tuple[MarketSnapshot, ...]:
    """Collect every configured indicator; one failure never aborts the briefing."""
    return tuple(
        _collect_one(indicator, end_date, session)
        for indicator in WEEKLY_MARKET_INDICATORS
    )
