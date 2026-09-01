"""Read successful daily Slack deliveries for the weekly briefing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import CATEGORIES, WEEKLY_BRIEFING_CONFIG


KOREA_TIMEZONE = timezone(timedelta(hours=9))
DEFAULT_ARCHIVE_PATH = Path(__file__).resolve().parents[2] / "data" / "slack_archive.jsonl"
LEGACY_ARTICLE_PATTERN = re.compile(
    r"^•\s+<(?P<url>[^|>]+)\|(?P<title>.+)>\s+\((?P<meta>.+)\)\s*$"
)


@dataclass(frozen=True)
class WeeklyArchiveWindow:
    """A complete calendar-date window of successfully delivered articles."""

    start_date: date
    end_date: date
    articles: tuple[dict, ...]
    errors: tuple[str, ...]
    records_read: int
    records_in_window: int


def _as_korea_datetime(value: datetime | None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KOREA_TIMEZONE)


def weekly_date_window(
    now: datetime | None = None,
    lookback_days: int | None = None,
) -> tuple[date, date]:
    """Return the last N completed Korean calendar dates.

    A Monday 08:30 run therefore reads the previous Monday through Sunday and
    never mixes the current, still-in-progress Monday into the weekly edition.
    """
    days = (
        WEEKLY_BRIEFING_CONFIG["lookback_days"]
        if lookback_days is None
        else lookback_days
    )
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError("lookback_days must be a positive integer")

    korea_date = _as_korea_datetime(now).date()
    # 실행 요일과 무관하게 가장 최근에 완전히 끝난 일요일을 기준으로 한다.
    # 월요일 실행은 바로 전날, 수동 수요일 실행은 사흘 전 일요일이며,
    # 아직 진행 중인 일요일에는 직전 주 일요일을 사용한다.
    days_since_sunday = (korea_date.weekday() + 1) % 7
    if days_since_sunday == 0:
        days_since_sunday = 7
    end_date = korea_date - timedelta(days=days_since_sunday)
    start_date = end_date - timedelta(days=days - 1)
    return start_date, end_date


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _record_edition_date(record: dict) -> date | None:
    """Read the v3 Korean edition date, with a v2 UTC timestamp fallback."""
    raw_edition_date = str(record.get("edition_date") or "").strip()
    if raw_edition_date:
        try:
            return date.fromisoformat(raw_edition_date)
        except ValueError:
            pass

    sent_at = _parse_datetime(record.get("ts"))
    return sent_at.astimezone(KOREA_TIMEZONE).date() if sent_at else None


def _article_identity(article: dict) -> str:
    """Build an exact identity before semantic event deduplication."""
    for field in ("normalized_url", "url"):
        value = str(article.get(field) or "").strip()
        if value:
            return f"url:{value}"

    title = str(article.get("title_orig") or article.get("title") or "")
    source = str(article.get("source") or "")
    normalized_title = re.sub(r"\s+", " ", title).strip().casefold()
    normalized_source = re.sub(r"\s+", " ", source).strip().casefold()
    return f"title:{normalized_title}|source:{normalized_source}"


def _archive_article(article: dict, record: dict, edition_date: date) -> dict:
    archived = dict(article)
    archived["_archive_version"] = record.get("version", 1)
    archived["_archive_sent_at"] = str(record.get("ts") or "")
    archived["_archive_edition_date"] = edition_date.isoformat()
    github = record.get("github")
    archived["_archive_github"] = dict(github) if isinstance(github, dict) else {}
    return archived


def _legacy_articles_from_text(record: dict) -> list[dict]:
    """Recover article links from pre-v2 Slack-only archive records."""
    text = str(record.get("text") or "")
    current_category = ""
    current_region = ""
    articles: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("*") and line.endswith("*"):
            heading = line[1:-1].strip()
            if heading in CATEGORIES:
                current_category = heading
                current_region = ""
            elif heading == "해외":
                current_region = "global"
            elif heading == "국내":
                current_region = "korea"
            continue

        match = LEGACY_ARTICLE_PATTERN.match(line)
        if not match or not current_category:
            continue
        source_and_date = match.group("meta").rsplit(", ", maxsplit=1)
        source = source_and_date[0].strip()
        article_date = source_and_date[1].strip() if len(source_and_date) == 2 else ""
        articles.append({
            "category": current_category,
            "region": current_region,
            "title": match.group("title").strip(),
            "title_orig": "",
            "url": match.group("url").strip(),
            "source": source,
            "date": article_date,
        })

    return articles


def load_weekly_archive(
    archive_path: str | Path | None = None,
    *,
    now: datetime | None = None,
    lookback_days: int | None = None,
) -> WeeklyArchiveWindow:
    """Load successfully sent articles without failing on one malformed line.

    Version 2 and version 3 JSONL records are both accepted. Exact duplicate
    URLs are replaced by their latest archived representation; semantic
    duplicates with different URLs are intentionally left for the weekly event
    deduplicator.
    """
    start_date, end_date = weekly_date_window(now, lookback_days)
    path = Path(archive_path) if archive_path is not None else DEFAULT_ARCHIVE_PATH
    errors: list[str] = []
    records_read = 0
    records_in_window = 0
    articles_by_identity: dict[str, dict] = {}

    if not path.exists():
        errors.append(f"주간 아카이브 파일을 찾을 수 없습니다: {path}")
        return WeeklyArchiveWindow(
            start_date,
            end_date,
            (),
            tuple(errors),
            records_read,
            records_in_window,
        )

    with path.open("r", encoding="utf-8") as archive_file:
        for line_number, raw_line in enumerate(archive_file, start=1):
            if not raw_line.strip():
                continue
            records_read += 1
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                errors.append(f"{line_number}행 JSON 오류: {exc.msg}")
                continue
            if not isinstance(record, dict):
                errors.append(f"{line_number}행 형식 오류: JSON 객체가 아닙니다")
                continue

            edition_date = _record_edition_date(record)
            if edition_date is None:
                errors.append(f"{line_number}행 날짜 오류: edition_date/ts를 읽을 수 없습니다")
                continue
            if not start_date <= edition_date <= end_date:
                continue

            raw_articles = record.get("articles")
            if not isinstance(raw_articles, list):
                raw_articles = _legacy_articles_from_text(record)
                if not raw_articles:
                    errors.append(f"{line_number}행 형식 오류: 기사를 복구할 수 없습니다")
                    continue

            records_in_window += 1
            for article in raw_articles:
                if not isinstance(article, dict):
                    errors.append(f"{line_number}행 기사 형식 오류: JSON 객체가 아닙니다")
                    continue
                archived = _archive_article(article, record, edition_date)
                identity = _article_identity(archived)
                if identity == "title:|source:":
                    errors.append(f"{line_number}행 기사 식별 오류: URL과 제목이 없습니다")
                    continue
                articles_by_identity[identity] = archived

    return WeeklyArchiveWindow(
        start_date,
        end_date,
        tuple(articles_by_identity.values()),
        tuple(errors),
        records_read,
        records_in_window,
    )
