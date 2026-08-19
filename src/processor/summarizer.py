import re
from ..config import (
    CATEGORIES,
    SOFT_PENALTY_KEYWORDS,
    WATCHLIST_WEIGHT,
    ALL_WATCHLISTS,
    RSS_SOURCE_METADATA,      # 🚀 메타데이터 임포트
    FEED_CATEGORY_OVERRIDE,   # ✅ Google News 피드 → 카테고리 강제 매핑
    DEAL_PRIORITY_SIGNALS,
    DEAL_EARLY_STAGE_SIGNALS,
    DEAL_EXCLUSION_KEYWORDS,
    IMPACT_EARLY_STAGE_SIGNALS,
    EDITORIAL_PRIORITY_SIGNALS,
    EDITORIAL_PRIORITY_WEIGHT,
    EDITORIAL_EXCLUSION_KEYWORDS,
)

def keyword_hit(keyword: str, text: str) -> bool:
    kw_lower = keyword.lower()
    if re.search(r'[a-z]', kw_lower):
        pattern = r'\b' + re.escape(kw_lower) + r'\b'
        return bool(re.search(pattern, text))
    else:
        return kw_lower in text


def _has_any(keywords: list, text: str) -> bool:
    return any(keyword_hit(keyword, text) for keyword in keywords)

def summarize(article: dict):
    errors = []
    title = article.get("title", "")
    desc = article.get("description", "") or article.get("summary", "")
    text = (title + " " + desc).lower()

    source = article.get("source", "")
    if isinstance(source, list):
        source = source[0] if source else ""
    source_clean = source.strip()

    assigned_category = None
    base_score = 0.0

    # 🚀 [0단계] Google News 피드 기반 카테고리 강제 확정 (최우선)
    #    Google News 기사는 source 가 실제 언론사(Deloitte 등)라 RSS_SOURCE_METADATA 로는
    #    안 잡히므로, 원 피드명(article['feed'])으로 카테고리를 먼저 고정한다.
    feed_name = (article.get("feed") or "").strip()
    feed_override = FEED_CATEGORY_OVERRIDE.get(feed_name)
    if feed_override:
        assigned_category = feed_override
        base_score += 4.0   # 카테고리 규정 피드는 A급 준하는 우선순위 부여

    # 🚀 [1단계] 출처(RSS 전문매체) 기반 카테고리 강제 지정 및 Priority 적용
    #    (0단계에서 이미 확정됐으면 건너뜀)
    # ✅ site: 폴백 기사는 source 가 실제 언론사명이라 메타데이터 키와 불일치
    #    → 원 피드명(feed)으로도 조회해 카테고리·priority 를 유지한다.
    source_meta = RSS_SOURCE_METADATA.get(source_clean) or RSS_SOURCE_METADATA.get(feed_name)
    if source_meta:
        if not assigned_category:
            assigned_category = source_meta.get("category")
        # 메타데이터에 정의된 강력한 우선순위(Priority 4~5점)를 기본 점수로 부여
        base_score += float(source_meta.get("priority", 0))

    # 🚀 [2단계] 키워드 보정 (종합 매체 분류 & 전문 매체 가점)
    category_scores = {cat: 0 for cat in CATEGORIES}
    for cat, kws in CATEGORIES.items():
        hits = sum(1 for kw in kws if keyword_hit(kw, text))
        category_scores[cat] = hits

    # 아직 카테고리 미정(구글뉴스 일반 쿼리, 해커뉴스 등)이면 키워드로 결정
    if not assigned_category:
        if sum(category_scores.values()) > 0:
            assigned_category = max(category_scores, key=category_scores.get)
        else:
            assigned_category = list(CATEGORIES.keys())[-1]  # Fallback

    # 전문 매체 기사라도 우리 타겟 키워드가 많으면 추가 가점 (+보정)
    if assigned_category in category_scores:
        base_score += float(category_scores[assigned_category])

    # 🚀 [3단계] 관심 기업(Watchlist) 및 페널티 적용
    for w_kw in ALL_WATCHLISTS:
        if keyword_hit(w_kw, text):
            base_score += float(WATCHLIST_WEIGHT)

    for p_kw in SOFT_PENALTY_KEYWORDS:
        if keyword_hit(p_kw, text):
            base_score -= 1.0

    editorial_score = sum(EDITORIAL_PRIORITY_WEIGHT for keywords in EDITORIAL_PRIORITY_SIGNALS.values() if _has_any(keywords, text))
    deal_score = sum(EDITORIAL_PRIORITY_WEIGHT for keywords in DEAL_PRIORITY_SIGNALS.values() if _has_any(keywords, text))
    early_stage = _has_any(DEAL_EARLY_STAGE_SIGNALS, text)
    impact_exception = assigned_category.startswith("🌱") and _has_any(IMPACT_EARLY_STAGE_SIGNALS, text)
    if early_stage and not (deal_score or impact_exception):
        deal_score -= EDITORIAL_PRIORITY_WEIGHT
    excluded = _has_any(DEAL_EXCLUSION_KEYWORDS + EDITORIAL_EXCLUSION_KEYWORDS, text)
    base_score += editorial_score + deal_score

    # 최종 적용
    article["category"] = assigned_category
    article["relevance"] = max(0.0, base_score)  # 점수 마이너스 방지

    article["deal_score"] = deal_score
    article["editorial_excluded"] = excluded
    if excluded:
        article["relevance"] = 0.0
    return article, errors
