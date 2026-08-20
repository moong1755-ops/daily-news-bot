import re
from datetime import datetime

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from ..config import RSS_SOURCE_METADATA, SIMILARITY_THRESHOLD

_model = None


_EVENT_PATTERNS = {
    "funding": (
        r"\braises?\b", r"\braised\b", r"\bfunding\b", r"\bfinancing\b",
        r"\bseries\s+[a-e]\b", "투자유치", "투자 유치", "펀딩", "시리즈",
    ),
    "acquisition": (
        r"\bacqui(?:re|res|red|ring|sition|sitions)\b", r"\bmerger\b",
        r"\bbuyout\b", r"\btakeover\b", "인수", "합병",
    ),
    "policy": (
        r"\bregulat", r"\bpolicy\b", r"\blegislat", r"\blawmakers?\b",
        "규제", "정책", "법안", "시행령",
    ),
    "contract": (
        r"\bcontracts?\b", r"\bprocurement\b", r"\bofftake\b",
        r"\bpartnership\b", "계약", "공공조달", "공급 협약",
    ),
    "risk": (
        r"\bfraud\b", r"\bbankrupt", r"\blawsuits?\b", r"\bstrike\b",
        "배임", "횡령", "파산", "소송", "파업",
    ),
    "product": (
        r"\blaunch(?:es|ed|ing)?\b", r"\brelease(?:s|d)?\b",
        r"\bdeployment\b", "출시", "상용화", "출범",
    ),
}

_FUNDING_STAGE_PATTERNS = (
    ("pre-seed", (r"\bpre[- ]seed\b", "프리시드", "프리 시드")),
    ("seed", (r"\bseed\b", "시드")),
    ("series-a", (r"\bseries\s+a\b", "시리즈a", "시리즈 a")),
    ("series-b", (r"\bseries\s+b\b", "시리즈b", "시리즈 b")),
    ("series-c", (r"\bseries\s+c\b", "시리즈c", "시리즈 c")),
    ("series-d", (r"\bseries\s+d\b", "시리즈d", "시리즈 d")),
    ("series-e", (r"\bseries\s+e\b", "시리즈e", "시리즈 e")),
    ("growth", (r"\bgrowth\s+(?:round|funding)\b", "그로스 투자", "성장 투자")),
)

_FACT_PATTERNS = (
    r"[$€£₩]\s?\d",
    r"\d[\d,.]*\s?(?:million|billion|mn|bn|억|조)(?:\s?(?:dollars?|달러|원))?\b",
    r"\bseries\s+[a-e]\b|시리즈\s?[a-e]",
    r"\bvaluation\b|기업가치",
    r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|인수|합병",
    r"\bcontracts?\b|\bprocurement\b|\bofftake\b|계약|조달",
    r"\bregulat|\bpolicy\b|규제|정책|법안",
)


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _first_text(value) -> str:
    values = _as_list(value)
    return str(values[0]).strip() if values else ""


def _article_text(article: dict) -> str:
    return f"{article.get('title', '')} {article.get('description', '')}".lower()


def _matches_any(text: str, patterns: tuple) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _source_priority(article: dict) -> int:
    feed = _first_text(article.get("feed"))
    source = _first_text(article.get("source"))
    metadata = RSS_SOURCE_METADATA.get(feed) or RSS_SOURCE_METADATA.get(source) or {}
    return int(metadata.get("priority", 0))


def _is_direct_article(article: dict) -> bool:
    link = _first_text(article.get("link")).lower()
    return not article.get("gnews_link") and "news.google.com" not in link


def _fact_detail_score(article: dict) -> int:
    text = _article_text(article)
    return sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in _FACT_PATTERNS)


def _published_rank(article: dict) -> int:
    raw_date = str(article.get("date") or "").strip()
    try:
        return datetime.strptime(raw_date[:10], "%Y-%m-%d").toordinal()
    except (TypeError, ValueError):
        return 0


def _representative_key(article: dict) -> tuple:
    description_length = len(str(article.get("description") or ""))
    return (
        _source_priority(article),
        int(_is_direct_article(article)),
        _fact_detail_score(article),
        description_length,
        _published_rank(article),
    )


def _event_types(article: dict) -> set:
    text = _article_text(article)
    return {
        event_type
        for event_type, patterns in _EVENT_PATTERNS.items()
        if _matches_any(text, patterns)
    }


def _funding_stage(article: dict) -> str:
    text = _article_text(article)
    for stage, patterns in _FUNDING_STAGE_PATTERNS:
        if _matches_any(text, patterns):
            return stage
    return ""


def _events_compatible(left: dict, right: dict) -> bool:
    left_types = _event_types(left)
    right_types = _event_types(right)
    if left_types and right_types and left_types.isdisjoint(right_types):
        return False

    left_stage = _funding_stage(left)
    right_stage = _funding_stage(right)
    if left_stage and right_stage and left_stage != right_stage:
        return False

    return True


def _unique_values(articles: list, key: str) -> list:
    result = []
    seen = set()
    for article in articles:
        for value in _as_list(article.get(key)):
            clean_value = str(value).strip()
            if clean_value and clean_value not in seen:
                result.append(clean_value)
                seen.add(clean_value)
    return result


def _merge_group(group: list) -> dict:
    representative = max(group, key=_representative_key)
    ordered = [representative] + [article for article in group if article is not representative]
    merged = representative.copy()

    merged["source"] = _unique_values(ordered, "source")
    merged["link"] = _unique_values(ordered, "link")
    merged["description"] = max(
        (str(article.get("description") or "") for article in group),
        key=len,
        default="",
    )
    merged["duplicate_count"] = len(group)
    merged["duplicate_titles"] = _unique_values(ordered, "title")
    merged["representative_reason"] = (
        f"source_priority={_source_priority(representative)},"
        f"direct={int(_is_direct_article(representative))},"
        f"facts={_fact_detail_score(representative)}"
    )
    return merged


def deduplicate_and_merge(articles: list) -> tuple:
    errors = []

    if not articles:
        return [], errors

    try:
        model = _get_model()
        titles = [article["title"] for article in articles]
        embeddings = model.encode(titles, convert_to_numpy=True)
        sim_matrix = cosine_similarity(embeddings)

        visited = set()
        merged = []

        for i in range(len(articles)):
            if i in visited:
                continue

            group = [i]
            for j in range(i + 1, len(articles)):
                if (
                    j not in visited
                    and sim_matrix[i][j] >= SIMILARITY_THRESHOLD
                    and _events_compatible(articles[i], articles[j])
                ):
                    group.append(j)
                    visited.add(j)
            visited.add(i)

            merged.append(_merge_group([articles[index] for index in group]))

        return merged, errors

    except Exception as error:
        errors.append(
            "Semantic deduplication failed, using title-based fallback: "
            f"{str(error)}"
        )
        return _title_based_dedup(articles), errors


def _title_based_dedup(articles: list) -> list:
    groups = {}
    group_order = []

    for article in articles:
        normalized_title = " ".join(str(article.get("title") or "").lower().split())
        if normalized_title not in groups:
            groups[normalized_title] = []
            group_order.append(normalized_title)
        groups[normalized_title].append(article)

    return [_merge_group(groups[title]) for title in group_order]


def filter_near_duplicates(articles: list, threshold: float) -> list:
    """앞서 고른 기사와 같은 사건을 다룬 기사를 제외한다(원래 순서 유지).

    수집 단계의 병합(SIMILARITY_THRESHOLD)보다 임계를 낮게 잡는다. 위험이
    비대칭이기 때문이다. 수집 단계에서 잘못 병합하면 기사가 아예 사라지지만,
    선정 단계에서 잘못 걸러도 비슷한 기사 하나를 덜 보여줄 뿐이고 그 자리는
    다른 뉴스로 채워진다.

    실제 실행에서 거시 카테고리 세 칸이 전부 같은 FOMC 의사록 기사였다.
    제목 임베딩 유사도는 0.62~0.76 이라 0.72 기준으로는 한 쌍만 걸렸다.

    모델을 쓸 수 없으면 입력을 그대로 돌려준다. 중복 제거 실패가 발송을
    막아서는 안 된다.
    """
    if len(articles) < 2:
        return list(articles)

    try:
        model = _get_model()
        embeddings = model.encode([a.get("title", "") for a in articles], convert_to_numpy=True)
        similarity = cosine_similarity(embeddings)
    except Exception as e:
        print(f"⚠️ 선정 단계 중복 검사 실패({e}) — 그대로 진행합니다.")
        return list(articles)

    kept_indexes = []
    for index in range(len(articles)):
        if any(similarity[index][kept] >= threshold for kept in kept_indexes):
            continue
        kept_indexes.append(index)

    dropped = len(articles) - len(kept_indexes)
    if dropped:
        print(f"   ↪ 같은 사건 {dropped}건을 선정에서 제외")
    return [articles[i] for i in kept_indexes]
