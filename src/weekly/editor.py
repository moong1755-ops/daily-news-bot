"""Build the weekly briefing's three-line impact-VC overview."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..config import CATEGORIES, WEEKLY_BRIEFING_CONFIG
from ..processor.reranker import generate_editor_json


@dataclass(frozen=True)
class WeeklyHeadlines:
    lines: tuple[str, ...]
    model: str | None
    used_fallback: bool


def _score(article: dict) -> float:
    try:
        return float(article.get("weekly_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _title(article: dict) -> str:
    return str(article.get("title") or article.get("title_orig") or "").strip()


def _fallback_lines(articles: list[dict], limit: int) -> tuple[str, ...]:
    """Keep impact visible while covering more than one category when possible."""
    ranked = sorted(articles, key=_score, reverse=True)
    chosen: list[dict] = []
    impact_category = next(category for category in CATEGORIES if category.startswith("🌱"))
    impact = next((article for article in ranked if article.get("category") == impact_category), None)
    if impact is not None:
        chosen.append(impact)

    represented = {article.get("category") for article in chosen}
    for article in ranked:
        if len(chosen) >= limit:
            break
        if article in chosen or article.get("category") in represented:
            continue
        chosen.append(article)
        represented.add(article.get("category"))
    for article in ranked:
        if len(chosen) >= limit:
            break
        if article not in chosen:
            chosen.append(article)

    return tuple(_title(article).rstrip(".。") for article in chosen if _title(article))


def _prompt(articles: list[dict], limit: int) -> str:
    candidates = [
        {
            "id": index + 1,
            "category": article.get("category"),
            "title": _title(article),
            "source": article.get("source"),
            "status": article.get("deal_status"),
            "signals": article.get("weekly_rank_reasons") or [],
        }
        for index, article in enumerate(articles)
    ]
    return f"""
당신은 임팩트 VC 투자심사역을 위한 주간 뉴스 편집장이다.
아래는 이미 중복 제거와 상대 순위 선별을 마친 기사다.

이번 주 투자 판단에 가장 중요한 변화를 정확히 {limit}줄 이내로 정리하라.
- 임팩트 투자 기회·리스크를 최소 1줄 포함한다.
- 시장 변화, 정책·규제, 투자·M&A, 산업 구조 변화 순으로 우선한다.
- 서로 다른 기사를 한 흐름으로 묶어도 되지만 기사에 없는 사실은 만들지 않는다.
- 루머·전망·검토 단계는 반드시 가능성 또는 전망임을 드러낸다.
- 행사·홍보 문구와 일반론은 쓰지 않는다.
- 뉴스 해설문이 아니라 15~32자 안팎의 보고서 제목형 문구로 쓴다.
- 핵심 주체와 변화만 남기고 짧고 단정한 명사형·단문형 어미를 사용한다.
- '~했습니다', '~로 나타났습니다', '~하고 있습니다' 같은 긴 서술형을 쓰지 않는다.
- 문장 끝 마침표를 붙이지 않는다.
- 좋은 예: '미 연준, 금리 인상 기조 재확인', 'AI 인프라 투자 경쟁 확대',
  '국내 Pre-IPO 대형 딜 증가'.
- JSON 이외의 문장은 출력하지 않는다.

출력 형식:
{{"lines": ["첫째 줄", "둘째 줄", "셋째 줄"]}}

기사:
{json.dumps(candidates, ensure_ascii=False)}
""".strip()


def _parse_lines(raw: str | None, limit: int) -> tuple[str, ...]:
    if not raw:
        return ()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return ()
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(lines, list):
        return ()
    cleaned = tuple(
        str(line).strip().lstrip("-• ")
        for line in lines[:limit]
        if isinstance(line, str) and line.strip()
    )
    return cleaned


def build_weekly_headlines(articles: tuple[dict, ...] | list[dict]) -> WeeklyHeadlines:
    limit = int(WEEKLY_BRIEFING_CONFIG["headline_summary_max"])
    article_list = list(articles)
    if not article_list or limit < 1:
        return WeeklyHeadlines((), None, True)

    raw, model = generate_editor_json(_prompt(article_list, limit), timeout=30)
    lines = _parse_lines(raw, limit)
    if lines:
        return WeeklyHeadlines(lines, model, False)
    return WeeklyHeadlines(_fallback_lines(article_list, limit), None, True)
