"""End-to-end weekly briefing runner."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

from ..config import WEEKLY_BRIEFING_CONFIG
from ..editorial_review import write_review_csv
from ..processor.translator import translate_titles
from .archive import KOREA_TIMEZONE, WeeklyArchiveWindow, load_weekly_archive
from .deduplicator import deduplicate_weekly_articles
from .editor import build_weekly_headlines
from .evidence import (
    apply_local_evidence,
    enrich_shortlist,
    guarded_title,
    needs_remote_evidence,
)
from .market_data import collect_market_snapshots
from .renderer import WeeklySlackMessage, render_weekly_briefing
from .selector import WeeklySelection, revalidate_weekly_articles, select_weekly_articles, weekly_score


DEFAULT_DELIVERY_ARCHIVE = Path(__file__).resolve().parents[2] / "data" / "weekly_archive.jsonl"
WEEKLY_REVIEW_PATH = Path(__file__).resolve().parents[2] / "data" / "weekly_review.csv"


@dataclass(frozen=True)
class WeeklyRunResult:
    success: bool
    delivered: bool
    reason: str
    source_window: WeeklyArchiveWindow
    selection: WeeklySelection | None = None
    message: WeeklySlackMessage | None = None


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _effective_now() -> datetime:
    """Allow a completed week to be reproduced without changing production time."""
    raw_end_date = os.environ.get("WEEKLY_END_DATE", "").strip()
    if not raw_end_date:
        return datetime.now(timezone.utc)
    try:
        end_date = date.fromisoformat(raw_end_date)
    except ValueError as exc:
        raise ValueError("WEEKLY_END_DATE는 YYYY-MM-DD 형식이어야 합니다") from exc
    return datetime.combine(end_date + timedelta(days=1), time(8, 30), KOREA_TIMEZONE)


def _already_delivered(path: Path, end_date: date) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as archive_file:
        for raw_line in archive_file:
            try:
                record = json.loads(raw_line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("end_date") == end_date.isoformat():
                return True
    return False


def _append_delivery_archive(
    path: Path,
    window: WeeklyArchiveWindow,
    selection: WeeklySelection,
    message: WeeklySlackMessage,
) -> None:
    record = {
        "version": 1,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "start_date": window.start_date.isoformat(),
        "end_date": window.end_date.isoformat(),
        "candidate_count": selection.candidate_count,
        "article_count": len(selection.articles),
        "github": {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "head_sha": os.environ.get("GITHUB_SHA", ""),
        },
        "articles": [
            {
                "category": article.get("category"),
                "region": article.get("region"),
                "title": article.get("title") or article.get("title_orig"),
                "url": (
                    article.get("link")
                    or article.get("normalized_url")
                    or article.get("url")
                ),
                "source": article.get("source"),
                "weekly_score": article.get("weekly_score"),
                "weekly_rank_reasons": article.get("weekly_rank_reasons") or [],
                "weekly_financing_type": article.get("weekly_financing_type"),
                "weekly_claim_status": article.get("weekly_claim_status"),
                "weekly_evidence_status": article.get("weekly_evidence_status"),
                "weekly_evidence_url": article.get("weekly_evidence_url"),
                "weekly_evidence_checked_at": article.get("weekly_evidence_checked_at"),
                "weekly_previous_importance": article.get("weekly_previous_importance"),
                "weekly_previous_importance_reason": article.get(
                    "weekly_previous_importance_reason"
                ),
            }
            for article in selection.articles
        ],
        "text": message.plain_text,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as archive_file:
        archive_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _empty_result(window: WeeklyArchiveWindow, reason: str) -> WeeklyRunResult:
    print(f"❌ 주간 브리핑 중단: {reason}")
    return WeeklyRunResult(False, False, reason, window)


def _select_with_evidence(articles: list[dict], *, session=requests):
    """Rerank until every promoted evidence-sensitive article is checked."""
    articles = apply_local_evidence(articles)
    selection = select_weekly_articles(articles)
    checked_urls = set()
    for _ in range(3):
        pending = [
            article for article in selection.articles
            if needs_remote_evidence(article)
            and article.get("url") not in checked_urls
        ]
        if not pending:
            break
        checked_articles = enrich_shortlist(pending, session=session)
        checked = {article.get("url"): article for article in checked_articles}
        checked_urls.update(checked)
        articles = [checked.get(article.get("url"), article) for article in articles]
        selection = select_weekly_articles(articles)

    selected_urls = {article.get("url") for article in selection.articles}
    for article in articles:
        if (
            article.get("url") in selected_urls
            and needs_remote_evidence(article)
            and article.get("url") not in checked_urls
        ):
            article["weekly_claim_status"] = "unverified"
            article["weekly_evidence_status"] = "not_checked_after_rerank"
    return articles, select_weekly_articles(articles)


def run_weekly_briefing(
    *,
    now: datetime | None = None,
    source_archive_path: str | Path | None = None,
    delivery_archive_path: str | Path | None = None,
    session=requests,
) -> WeeklyRunResult:
    run_now = now or _effective_now()
    source_path = source_archive_path or os.environ.get("SLACK_ARCHIVE_PATH") or None
    configured_delivery_path = delivery_archive_path or os.environ.get("WEEKLY_ARCHIVE_PATH")
    delivery_path = Path(configured_delivery_path or DEFAULT_DELIVERY_ARCHIVE)
    review_path = Path(
        os.environ.get("WEEKLY_REVIEW_PATH")
        or (
            Path(configured_delivery_path).with_name("weekly_review.csv")
            if configured_delivery_path
            else WEEKLY_REVIEW_PATH
        )
    )
    window = load_weekly_archive(source_path, now=run_now)
    for error in window.errors:
        print(f"⚠️ 주간 아카이브: {error}")
    if not window.articles:
        return _empty_result(
            window,
            f"{window.start_date.isoformat()}~{window.end_date.isoformat()} 발송 기사 없음",
        )

    dry_run = _enabled("DRY_RUN")
    if not dry_run and _already_delivered(delivery_path, window.end_date) and not _enabled("WEEKLY_FORCE_SEND"):
        reason = f"{window.end_date.isoformat()} 종료 주차가 이미 발송됨"
        print(f"ℹ️ {reason} — 데이터·Gemini를 다시 호출하지 않습니다.")
        return WeeklyRunResult(True, False, reason, window)

    weekly_webhook_url = os.environ.get("WEEKLY_SLACK_WEBHOOK_URL", "").strip()
    if not dry_run and not weekly_webhook_url:
        return _empty_result(window, "WEEKLY_SLACK_WEBHOOK_URL이 설정되지 않음")

    reviewed = revalidate_weekly_articles(list(window.articles))
    rejected = [article for article in reviewed if article.get("weekly_exclusion_reason")]
    corrected = [article for article in reviewed if article.get("weekly_previous_category")]
    print(f"🔎 주간 기록 재검사: 분류 수정 {len(corrected)}건, 제외 {len(rejected)}건")
    for article in corrected + rejected:
        print(
            f"  {article.get('title_orig') or article.get('title')}: "
            f"{article.get('weekly_previous_category') or article.get('category')} → "
            f"{article.get('weekly_exclusion_reason') or article.get('category')}"
        )
    deduplicated = deduplicate_weekly_articles([
        article for article in reviewed if not article.get("weekly_exclusion_reason")
    ])
    # 아카이브에 저장된 설명으로 전체 후보를 먼저 바로잡는다. 초기 상위권만
    # 고친 뒤 재정렬하면 새로 올라온 후보가 근거 확인을 건너뛸 수 있다.
    deduplicated, selection = _select_with_evidence(deduplicated, session=session)
    try:
        review_candidates = []
        for source_article in deduplicated + rejected:
            article = dict(source_article)
            score, _reasons = weekly_score(article)
            article["weekly_score"] = score
            review_candidates.append(article)
        write_review_csv(
            review_path,
            edition_date=window.end_date.isoformat(),
            candidates=review_candidates,
            selected=selection.articles,
            retention_days=370,
            review_type="weekly",
        )
    except Exception as exc:
        print(f"⚠️ Weekly Review CSV 저장 실패: {exc}")
    if not selection.articles:
        return _empty_result(window, "주간 상대 선별 결과가 0건")

    # 사건 통합·카테고리 분류·상대 순위가 모두 확정된 뒤 최종 발송 제목만
    # 번역한다. 원문 기반 판단을 보존하고 불필요한 LLM 호출도 막는다.
    print(f"🈯 주간 최종 선정 후 번역: 대상 {len(selection.articles)}건")
    translate_titles(list(selection.articles))
    for article in selection.articles:
        article["title"] = guarded_title(article, str(article.get("title") or ""))

    markets = collect_market_snapshots(
        window.start_date,
        window.end_date,
        session=session,
    )
    for market in markets:
        if market.error:
            print(f"⚠️ 시장지표 {market.label}: {market.error}")
    headlines = build_weekly_headlines(selection.articles)
    message = render_weekly_briefing(
        window.start_date,
        window.end_date,
        headlines,
        selection,
        markets,
    )
    print(
        f"🗞️ 주간 후보 {len(window.articles)}건 → 사건 {len(deduplicated)}건 → "
        f"최종 {len(selection.articles)}건, Slack 블록 {len(message.blocks)}개"
    )

    if dry_run:
        print("\n===== WEEKLY DRY RUN — 실제 발송하지 않음 =====")
        print(message.plain_text)
        print("===== WEEKLY DRY RUN 끝 =====\n")
        return WeeklyRunResult(True, False, "dry_run", window, selection, message)

    try:
        response = session.post(
            weekly_webhook_url,
            json={
                "text": message.notification_text,
                "blocks": list(message.blocks),
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return _empty_result(window, f"Slack 발송 실패: {exc}")

    try:
        _append_delivery_archive(delivery_path, window, selection, message)
    except OSError as exc:
        print(f"⚠️ Slack 발송은 성공했지만 주간 아카이브 저장 실패: {exc}")
    print("✅ 주간 브리핑 Slack 한 메시지 발송 성공")
    return WeeklyRunResult(True, True, "sent", window, selection, message)


def main() -> int:
    if not WEEKLY_BRIEFING_CONFIG.get("enabled", False):
        print("ℹ️ 주간 브리핑이 config에서 비활성화되어 있습니다.")
        return 0
    try:
        result = run_weekly_briefing()
    except ValueError as exc:
        print(f"❌ 주간 브리핑 설정 오류: {exc}")
        return 1
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
