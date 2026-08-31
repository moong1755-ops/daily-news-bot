"""Relative weekly ranking for an impact-VC briefing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from ..config import (
    CATEGORIES,
    WEEKLY_BRIEFING_CONFIG,
    WEEKLY_CATEGORY_LIMITS,
    WEEKLY_REGION_LIMITS,
)


IMPACT_CATEGORY = next(category for category in CATEGORIES if category.startswith("🌱"))
SOURCE_PRIORITY = {
    "reuters": 1.5,
    "bloomberg": 1.3,
    "financial times": 1.3,
    "the economist": 1.2,
    "impactalpha": 1.2,
    "responsible investor": 1.1,
    "esg today": 1.0,
    "carbon brief": 1.0,
    "canary media": 1.0,
    "techcrunch": 0.9,
    "pe hub": 0.9,
    "crunchbase": 0.9,
    "연합뉴스": 0.9,
    "한국경제": 0.9,
    "임팩트온": 0.9,
    "딜사이트": 0.9,
    "thebell": 0.9,
    "mckinsey": 1.0,
    "bcg": 1.0,
    "bain": 1.0,
    "deloitte": 1.0,
    "pwc": 1.0,
    "kpmg": 1.0,
    "ey": 1.0,
}
EVENT_SIGNALS = {
    "investment_or_ma": re.compile(
        r"\b(?:funding|fundraise|raises?|raised|series [a-z]|investment|acquisition|"
        r"merger|buyout|ipo|takeover)\b|투자\s*유치|투자|출자|인수|합병|매각|상장|공개매수",
        re.IGNORECASE,
    ),
    "policy_or_regulation": re.compile(
        r"\b(?:policy|regulation|regulatory|legislation|law|tariff|sanction|ban|"
        r"subsidy|approval)\b|정책|규제|법안|관세|제재|보조금|승인|공공구매",
        re.IGNORECASE,
    ),
    "market_structure": re.compile(
        r"\b(?:infrastructure|supply chain|data center|semiconductor|energy transition|"
        r"power grid|platform|market structure)\b|인프라|공급망|데이터센터|반도체|"
        r"에너지\s*전환|전력망|시장\s*재편",
        re.IGNORECASE,
    ),
    "large_amount": re.compile(
        r"(?:[$€£]\s?\d[\d,.]*\s?(?:m|mn|million|b|bn|billion))|"
        r"(?:\d[\d,.]*\s?(?:억|조)\s*원)",
        re.IGNORECASE,
    ),
    "rumor_or_outlook": re.compile(
        r"\b(?:rumou?r|reportedly|talks?|mulls?|considering|could|may|outlook|forecast)\b|"
        r"추진|검토|협상|논의|가능성|전망|관측",
        re.IGNORECASE,
    ),
}
LOW_VALUE_FORMAT = re.compile(
    r"\b(?:weekly roundup|daily roundup|news briefing|the week in|podcast|webinar|"
    r"event recap|ai summary)\b|주간\s*(?:모음|정리)|오늘의\s*(?:뉴스|딜)|"
    r"뉴스\s*브리핑|ai\s*서머리|행사|세미나",
    re.IGNORECASE,
)
TECHNICAL_INSIGHT_FORMAT = re.compile(
    r"\b(?:fasb|iasb|gaap|ifrs)(?:\s+\d+)?\b|"
    r"\b(?:accounting|tax|regulatory) (?:updates?|alerts?|bulletins?)\b|"
    r"회계\s*기준.{0,40}(?:시행일|적용일)|세무.{0,30}(?:알림|업데이트)",
    re.IGNORECASE,
)
INSIGHT_REPORT_FORMAT = re.compile(
    r"\b(?:outlook|state of|industry (?:report|update|focus)|market (?:report|analysis)|"
    r"survey|barometer|trends?|future of|economic value|private equity update)\b|"
    r"전망|시장\s*(?:조사|분석|동향)|산업\s*(?:리포트|보고서|전망)|"
    r"사모펀드\s*업데이트|에너지\s*전환|경제적\s*가치",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WeeklySelection:
    by_category: dict[str, tuple[dict, ...]]
    articles: tuple[dict, ...]
    candidate_count: int


def _number(article: dict, field: str) -> float:
    try:
        return float(article.get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


def _article_text(article: dict) -> str:
    return " ".join(str(article.get(field) or "") for field in (
        "title_orig",
        "title",
        "event_type",
        "impact_theme",
        "editor_reason",
    ))


def _source_bonus(article: dict) -> float:
    source = str(article.get("source") or "").casefold()
    return max(
        (bonus for name, bonus in SOURCE_PRIORITY.items() if name in source),
        default=0.0,
    )


def _last_seen(article: dict) -> date:
    raw = str(article.get("weekly_last_seen") or article.get("_archive_edition_date") or "")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.min


def weekly_score(article: dict) -> tuple[float, tuple[str, ...]]:
    """Return an explainable relative score, never an eligibility cutoff."""
    text = _article_text(article)
    reasons: list[str] = []
    score = max(
        _number(article, "selection_score"),
        _number(article, "editor_score"),
    )

    source_bonus = _source_bonus(article)
    if source_bonus:
        score += source_bonus
        reasons.append("trusted_source")

    for signal, pattern in EVENT_SIGNALS.items():
        if not pattern.search(text):
            continue
        if signal in {"investment_or_ma", "policy_or_regulation"}:
            score += 2.0
        elif signal == "market_structure":
            score += 1.2
        elif signal == "large_amount":
            score += 1.0
        else:
            # 루머·전망은 제외하지 않되 확정 사건보다 작은 시장 신호로 취급한다.
            score += 0.2
        reasons.append(signal)

    if article.get("major_deal"):
        score += 2.0
        reasons.append("major_deal")
    if article.get("category") == IMPACT_CATEGORY:
        score += 1.5
        reasons.append("impact_priority")
    if article.get("impact_theme"):
        score += 0.7
        reasons.append("impact_theme")

    story_count = max(1, int(article.get("weekly_story_count") or 1))
    if story_count > 1:
        score += min(1.0, (story_count - 1) * 0.35)
        reasons.append("follow_up_coverage")

    status = str(article.get("deal_status") or "").casefold()
    if status in {"confirmed", "completed", "closed", "approved", "확정", "완료", "승인"}:
        score += 0.8
        reasons.append("confirmed")

    if LOW_VALUE_FORMAT.search(text):
        # 후보 자격을 없애지는 않되, 풍부한 주간 후보 중 재포장 모음이
        # 실제 사건·원문보다 앞서는 일은 막는다.
        score -= 4.0
        reasons.append("roundup_penalty")

    if str(article.get("category") or "").startswith("👔") and TECHNICAL_INSIGHT_FORMAT.search(text):
        score -= 4.0
        reasons.append("technical_bulletin_penalty")
    elif str(article.get("category") or "").startswith("👔") and INSIGHT_REPORT_FORMAT.search(text):
        score += 1.5
        reasons.append("insight_report")

    return score, tuple(reasons)


def _ranked(articles: list[dict]) -> list[dict]:
    ranked = []
    for source_article in articles:
        article = dict(source_article)
        score, reasons = weekly_score(article)
        article["weekly_score"] = score
        article["weekly_rank_reasons"] = list(reasons)
        ranked.append(article)
    return sorted(
        ranked,
        key=lambda article: (
            _number(article, "weekly_score"),
            _last_seen(article).toordinal(),
            _source_bonus(article),
        ),
        reverse=True,
    )


def _select_category(articles: list[dict], category: str) -> list[dict]:
    ranked = _ranked(articles)
    category_limit = int(WEEKLY_CATEGORY_LIMITS.get(category, 0))
    region_limits = WEEKLY_REGION_LIMITS.get(category)
    if not region_limits:
        return ranked[:category_limit]

    selected = []
    for region in ("global", "korea"):
        region_limit = int(region_limits.get(region, 0))
        selected.extend(
            article
            for article in ranked
            if article.get("region") == region
        )
        if region_limit:
            selected[-sum(1 for item in selected if item.get("region") == region):] = [
                item for item in selected if item.get("region") == region
            ][:region_limit]

    # 오래된 기록에 지역값이 없더라도 카테고리 한도 안에서는 후보로 남긴다.
    remaining_slots = category_limit - len(selected)
    if remaining_slots > 0:
        selected_ids = {id(article) for article in selected}
        selected.extend(
            article
            for article in ranked
            if id(article) not in selected_ids and not article.get("region")
        )
        selected = selected[:category_limit]
    return sorted(
        selected,
        key=lambda article: _number(article, "weekly_score"),
        reverse=True,
    )


def select_weekly_articles(articles: list[dict]) -> WeeklySelection:
    """Select category-relative leaders and enforce one global maximum."""
    selected_by_category: dict[str, list[dict]] = {}
    for category in CATEGORIES:
        candidates = [article for article in articles if article.get("category") == category]
        selected_by_category[category] = _select_category(candidates, category)

    impact_articles = list(selected_by_category.get(IMPACT_CATEGORY, []))
    other_articles = [
        article
        for category in CATEGORIES
        if category != IMPACT_CATEGORY
        for article in selected_by_category.get(category, [])
    ]
    total_limit = int(WEEKLY_BRIEFING_CONFIG["total_article_max"])
    protected = list(impact_articles)

    # 전체 12건 한도를 적용하기 전에 각 카테고리 대표 기사와 해외·국내 한 건씩을
    # 보호한다. 특정 카테고리의 고득점 기사들이 거시 국내/해외 한쪽을 밀어내는
    # 현상을 막고, 남는 칸만 순수 상대 점수로 경쟁시킨다.
    for category in CATEGORIES:
        if category == IMPACT_CATEGORY:
            continue
        category_articles = list(selected_by_category.get(category, []))
        if not category_articles:
            continue
        if category in WEEKLY_REGION_LIMITS:
            for region in ("global", "korea"):
                representative = next(
                    (article for article in category_articles if article.get("region") == region),
                    None,
                )
                if representative is not None:
                    protected.append(representative)
        else:
            protected.append(category_articles[0])

    protected_ids = {id(article) for article in protected[:total_limit]}
    remaining_slots = max(0, total_limit - len(protected_ids))
    remaining_ranked = sorted(
        (article for article in other_articles if id(article) not in protected_ids),
        key=lambda article: (
            _number(article, "weekly_score"),
            _last_seen(article).toordinal(),
        ),
        reverse=True,
    )
    kept_other_ids = protected_ids | {
        id(article) for article in remaining_ranked[:remaining_slots]
    }

    final_by_category = {
        category: tuple(
            article
            for article in selected_by_category.get(category, [])
            if category == IMPACT_CATEGORY or id(article) in kept_other_ids
        )
        for category in CATEGORIES
    }
    final_articles = tuple(
        article
        for category in CATEGORIES
        for article in final_by_category[category]
    )
    return WeeklySelection(
        by_category=final_by_category,
        articles=final_articles,
        candidate_count=len(articles),
    )
