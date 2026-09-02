"""Collapse repeated coverage into weekly event groups."""

from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher

from ..editorial_review import VALID_ALT_SUBTYPES, importance as _editorial_importance


GENERIC_TOKENS = {
    "about", "after", "amid", "and", "for", "from", "into", "over", "says",
    "the", "to", "with", "news", "report", "update", "이번", "관련", "대한",
    "통해", "위한", "발표", "전망", "시장", "기업", "글로벌", "국내", "해외",
}
ACTION_GROUPS = {
    "funding": {
        "funding", "fundraise", "fundraises", "raise", "raises", "raised", "round",
        "series", "investment", "invests", "투자", "투자유치", "유치", "출자",
    },
    "ma": {
        "acquire", "acquires", "acquired", "acquisition", "buyout", "merge", "merger",
        "인수", "인수합병", "합병", "매각", "공개매수",
    },
    "ipo": {"ipo", "listing", "listed", "상장", "기업공개"},
    "policy": {
        "policy", "regulation", "regulatory", "rule", "law", "ban", "sanction",
        "tariff", "정책", "규제", "법안", "법률", "제재", "관세", "승인",
    },
    "rates": {
        "rate", "rates", "inflation", "fed", "금리", "인플레이션", "연준", "환율",
    },
    "contract": {
        "contract", "deal", "agreement", "partnership", "order", "계약", "협약", "수주",
    },
    "launch": {"launch", "launches", "launched", "release", "출시", "공개"},
    "legal": {
        "court", "judge", "lawsuit", "sue", "sues", "ruling", "법원", "판결", "소송",
    },
}
SOURCE_PRIORITY = {
    "reuters": 10,
    "bloomberg": 9,
    "financial times": 9,
    "the economist": 8,
    "impactalpha": 8,
    "responsible investor": 8,
    "esg today": 7,
    "techcrunch": 7,
    "pe hub": 7,
    "crunchbase": 7,
    "연합뉴스": 7,
    "한국경제": 7,
    "임팩트온": 7,
    "딜사이트": 7,
    "thebell": 7,
}
CATEGORY_PRIORITY = {
    "🌱 임팩트": 50,
    "👔 MBB·Big4 인사이트": 45,
    "🤖 AI": 40,
    "📈 대체투자": 30,
    "🌐 거시·정책·지정학": 20,
}
CONFIRMED_STATUSES = {"confirmed", "completed", "closed", "approved", "확정", "완료", "승인"}
OFFICIAL_INSIGHT_SOURCES = {
    "mckinsey", "boston consulting group", "bcg", "bain", "deloitte", "pwc", "ey", "kpmg",
}
EVENT_TOKEN_ALIASES = {
    "blacklisting": "blacklist",
    "blacklisted": "blacklist",
    "ruling": "court",
    "judge": "court",
    "lawsuit": "court",
    "merger": "merge",
    "merged": "merge",
    "acquisition": "acquire",
    "acquired": "acquire",
    "fundraise": "funding",
    "fundraising": "funding",
    "raised": "funding",
    "raises": "funding",
}


def _normalized_text(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9가-힣]+", str(value or "").casefold()))


def _tokens(value: object) -> set[str]:
    return {
        EVENT_TOKEN_ALIASES.get(token, token)
        for token in _normalized_text(value).split()
        if len(token) >= 2 and token not in GENERIC_TOKENS
    }


def _event_key_tokens(article: dict) -> set[str]:
    raw = str(article.get("editor_event_key") or "").casefold()
    raw = re.sub(r"supply[_\s]+chain[_\s]+risk", "blacklist", raw)
    raw = re.sub(r"court[_\s]+win|judge[_\s]+blocks?", "court", raw)
    return _tokens(raw)


def _action_groups(article: dict) -> set[str]:
    tokens = _tokens(
        f"{article.get('title_orig') or ''} {article.get('title') or ''} "
        f"{article.get('event_type') or ''}"
    )
    return {
        group
        for group, keywords in ACTION_GROUPS.items()
        if tokens.intersection(keywords)
    }


def _archive_date(article: dict) -> date | None:
    raw = str(article.get("_archive_edition_date") or "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _same_event(left: dict, right: dict) -> bool:
    left_url = str(left.get("normalized_url") or left.get("url") or "").strip()
    right_url = str(right.get("normalized_url") or right.get("url") or "").strip()
    if left_url and left_url == right_url:
        return True

    left_key = _event_key_tokens(left)
    right_key = _event_key_tokens(right)
    if left_key and right_key:
        if left_key == right_key:
            return True
        key_union = left_key | right_key
        if key_union and len(left_key & right_key) / len(key_union) >= 0.75:
            return True
        # 편집장이 서로 다른 키를 부여한 경우 제목 유사도로 다시 합치지 않는다.
        # 같은 회사의 별도 투자·계약 상대방을 하나로 뭉개는 것을 막는다.
        return False

    left_date = _archive_date(left)
    right_date = _archive_date(right)
    if left_date and right_date and abs((left_date - right_date).days) > 7:
        return False

    left_title = _normalized_text(left.get("title_orig") or left.get("title"))
    right_title = _normalized_text(right.get("title_orig") or right.get("title"))
    if not left_title or not right_title:
        return False
    if left_title == right_title:
        return True

    similarity = SequenceMatcher(None, left_title, right_title).ratio()
    if similarity >= 0.88:
        return True

    left_tokens = _tokens(left_title)
    right_tokens = _tokens(right_title)
    shared_tokens = left_tokens & right_tokens
    token_union = left_tokens | right_tokens
    if not token_union or len(shared_tokens) < 2:
        return False

    left_actions = _action_groups(left)
    right_actions = _action_groups(right)
    if left_actions and right_actions and not left_actions.intersection(right_actions):
        return False

    entity_tokens = {
        token
        for token in shared_tokens
        if not any(token in keywords for keywords in ACTION_GROUPS.values())
        and not token.isdigit()
    }
    jaccard = len(shared_tokens) / len(token_union)
    smaller_title_coverage = len(shared_tokens) / min(len(left_tokens), len(right_tokens))
    # 같은 통계·정책 사건을 한 매체는 수치까지 길게, 다른 매체는 핵심만 짧게
    # 쓰면 Jaccard 분모만 커진다. 공통 핵심어가 충분하고 짧은 제목의 75% 이상을
    # 덮는 경우에는 같은 사건으로 본다.
    return len(entity_tokens) >= 1 and (
        jaccard >= 0.58
        or (len(shared_tokens) >= 4 and smaller_title_coverage >= 0.75)
    )


def _source_priority(article: dict) -> int:
    source = _normalized_text(article.get("source"))
    return max(
        (priority for name, priority in SOURCE_PRIORITY.items() if name in source),
        default=0,
    )


def _numeric_score(article: dict, field: str) -> float:
    try:
        return float(article.get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


def _representative_key(article: dict) -> tuple:
    status = _normalized_text(article.get("deal_status"))
    archive_date = _archive_date(article)
    return (
        status in CONFIRMED_STATUSES,
        archive_date.toordinal() if archive_date else 0,
        _source_priority(article),
        _numeric_score(article, "selection_score"),
        _numeric_score(article, "editor_score"),
        bool(article.get("title_orig")),
    )


def _official_insight(article: dict) -> bool:
    if article.get("category") != "👔 MBB·Big4 인사이트":
        return False
    source = _normalized_text(article.get("source"))
    return any(name in source for name in OFFICIAL_INSIGHT_SOURCES)


def _category_owner(group: list[dict]) -> dict:
    official = [article for article in group if _official_insight(article)]
    if official:
        return max(official, key=_representative_key)
    return max(
        group,
        key=lambda article: (
            CATEGORY_PRIORITY.get(article.get("category"), 0),
            _representative_key(article),
        ),
    )


def _group_representative(group: list[dict]) -> dict:
    representative = dict(max(group, key=_representative_key))
    owner = _category_owner(group)
    representative["category"] = owner.get("category", representative.get("category"))
    representative["region"] = owner.get("region", representative.get("region", ""))

    links = []
    seen_urls = set()
    for article in sorted(group, key=lambda item: _archive_date(item) or date.min):
        url = str(article.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        links.append({
            "title": article.get("title") or article.get("title_orig") or "제목 없음",
            "url": url,
            "source": article.get("source") or "출처미상",
            "date": article.get("date") or "",
        })

    dates = [value for value in (_archive_date(article) for article in group) if value]
    representative["weekly_story_count"] = len(group)
    representative["weekly_sources"] = sorted({
        str(article.get("source") or "출처미상") for article in group
    })
    representative["weekly_related_links"] = links
    representative["weekly_categories"] = sorted({
        str(article.get("category") or "") for article in group if article.get("category")
    })
    representative["weekly_first_seen"] = min(dates).isoformat() if dates else ""
    representative["weekly_last_seen"] = max(dates).isoformat() if dates else ""
    representative["editor_score"] = max(
        (_numeric_score(article, "editor_score") for article in group),
        default=0.0,
    )
    representative["selection_score"] = max(
        (_numeric_score(article, "selection_score") for article in group),
        default=0.0,
    )
    importance_owner = max(
        group,
        key=lambda article: (
            _editorial_importance(article),
            _numeric_score(article, "selection_score"),
            _numeric_score(article, "editor_score"),
        ),
    )
    representative["importance"] = _editorial_importance(importance_owner)
    representative["importance_reason"] = (
        importance_owner.get("importance_reason") or ""
    )
    if str(representative.get("category") or "").startswith("📈"):
        subtype_candidates = [
            article for article in group
            if article.get("alt_subtype") in VALID_ALT_SUBTYPES
        ]
        subtype_owner = max(
            subtype_candidates,
            key=lambda article: (
                _editorial_importance(article),
                _numeric_score(article, "selection_score"),
            ),
            default=None,
        )
        representative["alt_subtype"] = (
            subtype_owner.get("alt_subtype") if subtype_owner else ""
        )
    else:
        representative["alt_subtype"] = ""
    representative["major_deal"] = any(article.get("major_deal", False) for article in group)
    return representative


def deduplicate_weekly_articles(articles: list[dict]) -> list[dict]:
    """Return one representative per event while preserving related sources."""
    groups: list[list[dict]] = []
    for original in articles:
        article = dict(original)
        matching_group = next(
            (
                group
                for group in groups
                if any(_same_event(article, existing) for existing in group)
            ),
            None,
        )
        if matching_group is None:
            groups.append([article])
        else:
            matching_group.append(article)

    return [_group_representative(group) for group in groups]
