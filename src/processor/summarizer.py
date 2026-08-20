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
    IMPACT_THEME_KEYWORDS,
    OFFICIAL_INSIGHTS_SOURCE_ALIASES,
)


# 단독으로는 임팩트 투자 기사라는 근거가 부족한 넓은 산업 용어.
# 전문 임팩트 출처이거나 접근성·형평성·성과 같은 목적 신호가 함께 있어야 한다.
_BROAD_IMPACT_KEYWORDS = {
    "healthcare", "digital health", "healthtech", "mental health",
    "education", "edtech", "esg", "sustainability",
    "헬스케어", "디지털헬스", "헬스테크", "정신건강",
    "교육", "에듀테크", "지속가능",
}

_IMPACT_PURPOSE_SIGNALS = [
    "impact investing", "social impact", "measurable impact", "underserved",
    "low-income", "vulnerable communities", "public benefit", "patient outcomes",
    "learning outcomes", "emissions reduction", "reduces emissions", "health access",
    "임팩트투자", "사회적 가치",
    "취약계층", "저소득", "공공성",
    "의료접근성", "교육격차", "학습성과", "탄소 감축",
]

def keyword_hit(keyword: str, text: str) -> bool:
    kw_lower = keyword.lower()
    if re.search(r'[a-z]', kw_lower):
        pattern = r'\b' + re.escape(kw_lower) + r'\b'
        return bool(re.search(pattern, text))
    else:
        return kw_lower in text


def _has_any(keywords: list, text: str) -> bool:
    return any(keyword_hit(keyword, text) for keyword in keywords)


def _category_by_prefix(prefix: str) -> str:
    return next(category for category in CATEGORIES if category.startswith(prefix))


def _matched_groups(groups: dict, text: str) -> set:
    return {
        name
        for name, keywords in groups.items()
        if _has_any(keywords, text)
    }


def _matched_impact_themes(text: str) -> list:
    return [
        theme
        for theme, keywords in IMPACT_THEME_KEYWORDS.items()
        if _has_any(keywords, text)
    ]


def _has_specific_impact_theme(text: str) -> bool:
    """Return True when impact evidence is stronger than a broad sector word."""
    for keywords in IMPACT_THEME_KEYWORDS.values():
        specific_keywords = [
            keyword
            for keyword in keywords
            if keyword.lower() not in _BROAD_IMPACT_KEYWORDS
        ]
        if _has_any(specific_keywords, text):
            return True
    return False


def _official_aliases_for_feed(feed_name: str) -> tuple:
    """Find official publisher aliases for both direct and Google News feeds."""
    if feed_name in OFFICIAL_INSIGHTS_SOURCE_ALIASES:
        return OFFICIAL_INSIGHTS_SOURCE_ALIASES[feed_name]

    for official_feed, aliases in OFFICIAL_INSIGHTS_SOURCE_ALIASES.items():
        brand = official_feed.removesuffix(" Official Insights")
        if keyword_hit(brand, feed_name.lower()):
            return aliases
    return ()


def _is_verified_official_insight(
    feed_name: str,
    source_name: str,
    source_category: str,
    insights_category: str,
) -> bool:
    """Reject third-party stories that merely mention a consulting firm."""
    if source_category != insights_category or not source_name:
        return False

    aliases = _official_aliases_for_feed(feed_name)
    return bool(aliases) and any(
        keyword_hit(alias, source_name.lower())
        for alias in aliases
    )


def summarize(article: dict):
    errors = []
    title = article.get("title", "")
    desc = article.get("description", "") or article.get("summary", "")
    text = (title + " " + desc).lower()

    source = article.get("source", "")
    if isinstance(source, list):
        source = source[0] if source else ""
    source_clean = source.strip()

    base_score = 0.0
    feed_name = (article.get("feed") or "").strip()
    feed_override = FEED_CATEGORY_OVERRIDE.get(feed_name)
    source_meta = RSS_SOURCE_METADATA.get(feed_name) or RSS_SOURCE_METADATA.get(source_clean)
    source_category = feed_override or (source_meta or {}).get("category")
    source_priority = float((source_meta or {}).get("priority", 4.0 if feed_override else 0.0))
    base_score += source_priority

    # 출처 점수는 상대 순위의 보조 신호일 뿐이며 카테고리를 강제하지 않는다.
    category_scores = {cat: 0 for cat in CATEGORIES}
    for cat, kws in CATEGORIES.items():
        hits = sum(1 for kw in kws if keyword_hit(kw, text))
        category_scores[cat] = hits

    impact_category = _category_by_prefix("🌱")
    ai_category = _category_by_prefix("🤖")
    alternative_category = _category_by_prefix("📈")
    macro_category = _category_by_prefix("🌐")
    insights_category = _category_by_prefix("👔")

    impact_themes = _matched_impact_themes(text)
    editorial_groups = _matched_groups(EDITORIAL_PRIORITY_SIGNALS, text)
    deal_groups = _matched_groups(DEAL_PRIORITY_SIGNALS, text)
    early_stage = _has_any(DEAL_EARLY_STAGE_SIGNALS, text)
    deal_event = bool(deal_groups & {"transaction", "financing"}) or early_stage
    official_insights = _is_verified_official_insight(
        feed_name,
        source_clean,
        source_category,
        insights_category,
    )
    verified_impact_source = source_category == impact_category
    impact_content = bool(impact_themes) and (
        verified_impact_source
        or _has_specific_impact_theme(text)
        or _has_any(_IMPACT_PURPOSE_SIGNALS, text)
        or "impact_evidence" in editorial_groups
    )

    # MBB·Big4 공식 발행물만 출처 고정. 나머지는 기사 내용을 우선한다.
    if official_insights:
        assigned_category = insights_category
        category_reason = "official_insights_source"
    elif verified_impact_source:
        assigned_category = impact_category
        category_reason = "verified_impact_source"
    elif impact_content:
        assigned_category = impact_category
        category_reason = "impact_content"
    elif deal_event:
        assigned_category = alternative_category
        category_reason = "deal_event"
    elif (
        category_scores[alternative_category] > 0
        and category_scores[alternative_category] > category_scores[ai_category]
    ):
        assigned_category = alternative_category
        category_reason = "alternative_content"
    elif category_scores[ai_category] > 0:
        assigned_category = ai_category
        category_reason = "ai_content"
    elif category_scores[macro_category] > 0 or "policy_or_regulation" in editorial_groups:
        assigned_category = macro_category
        category_reason = "macro_content"
    elif category_scores[alternative_category] > 0:
        assigned_category = alternative_category
        category_reason = "alternative_content"
    elif "enterprise_risk" in editorial_groups:
        assigned_category = source_category if source_category in CATEGORIES else alternative_category
        category_reason = "enterprise_risk"
    elif source_category in CATEGORIES and source_category != insights_category:
        assigned_category = source_category
        category_reason = "source_fallback"
    else:
        assigned_category = macro_category
        category_reason = "general_fallback"

    if assigned_category in category_scores:
        base_score += float(category_scores[assigned_category])

    for w_kw in ALL_WATCHLISTS:
        if keyword_hit(w_kw, text):
            base_score += float(WATCHLIST_WEIGHT)

    for p_kw in SOFT_PENALTY_KEYWORDS:
        if keyword_hit(p_kw, text):
            base_score -= 1.0

    editorial_score = len(editorial_groups) * EDITORIAL_PRIORITY_WEIGHT
    deal_score = len(deal_groups) * EDITORIAL_PRIORITY_WEIGHT
    impact_exception = assigned_category == impact_category and _has_any(IMPACT_EARLY_STAGE_SIGNALS, text)
    if early_stage and not deal_groups and not impact_exception:
        if "investment_or_ma" in editorial_groups:
            editorial_score -= EDITORIAL_PRIORITY_WEIGHT
        deal_score -= EDITORIAL_PRIORITY_WEIGHT

    excluded = _has_any(DEAL_EXCLUSION_KEYWORDS + EDITORIAL_EXCLUSION_KEYWORDS, text)
    if article.get("rescue_signal"):
        excluded = False
    base_score += editorial_score + deal_score

    article["category"] = assigned_category
    article["category_reason"] = category_reason
    article["source_priority"] = source_priority
    article["impact_themes"] = impact_themes
    article["editorial_signals"] = sorted(editorial_groups)
    article["deal_signals"] = sorted(deal_groups)
    article["relevance"] = max(0.0, base_score)
    article["deal_score"] = deal_score
    article["major_deal"] = (
        assigned_category == alternative_category
        and bool(deal_groups & {"transaction", "financing"})
    )
    article["impact_must_read"] = (
        assigned_category == impact_category
        and bool(
            editorial_groups
            & {
                "investment_or_ma",
                "policy_or_regulation",
                "major_contract_or_technology",
                "enterprise_risk",
                "impact_evidence",
            }
            or deal_groups
        )
    )
    article["editorial_excluded"] = excluded
    if excluded:
        article["relevance"] = 0.0
    return article, errors
