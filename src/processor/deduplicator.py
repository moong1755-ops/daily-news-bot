import re
from datetime import datetime
from urllib.parse import urlsplit

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..config import DISCOVERY_TOPICS, RSS_SOURCE_METADATA, SIMILARITY_THRESHOLD

_model = None

# 일반 기사에는 기존 임계값을 그대로 사용한다. 같은 주제 검색에서 나온
# 연속 기사만 출처·날짜·산업 주체를 추가 확인한 뒤 보조 임계값을 허용한다.
_TOPIC_SERIES_SIMILARITY_FLOOR = max(0.0, SIMILARITY_THRESHOLD - 0.30)
_TOPIC_SERIES_MAX_DATE_GAP_DAYS = 2
_POLICY_REACTION_SIMILARITY_FLOOR = max(0.0, SIMILARITY_THRESHOLD - 0.15)
_POLICY_REACTION_MAX_DATE_GAP_DAYS = 1
_FULL_SEMANTIC_SCAN_LIMIT = 200
_LEXICAL_CANDIDATE_FLOOR = 0.30
_RARE_TOKEN_MAX_DOCUMENTS = 6
_CANDIDATE_TOKEN_STOPWORDS = {
    "about", "after", "against", "company", "global", "insights", "market",
    "news", "report", "startup", "startups", "funding", "investment",
    "investing", "raises", "raised", "launches", "policy", "business",
    "with", "from", "into", "over", "through", "amid", "says", "could",
    "기사", "기업", "국내", "글로벌", "금융", "뉴스", "리포트", "발표", "사업", "산업",
    "시장", "스타트업", "전망", "정책", "정부", "조사", "추진", "출시", "투자", "투자유치",
    "펀드", "확대", "회사",
}

_HEADLINE_EVENT_TOKEN_STOPWORDS = _CANDIDATE_TOKEN_STOPWORDS | {
    "관련", "대해", "따라", "위해", "통해", "영향", "오늘", "올해",
    "latest", "new", "update",
}
_KOREAN_PARTICLE_SUFFIXES = ("으로", "에서", "에게", "까지", "부터", "처럼", "보다", "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "로")

_ENTITY_FAMILY_PATTERNS = {
    "securities": (
        r"증권(?:가|사|업계)", r"금융투자(?:업계|협회)", r"금투협",
        r"\bsecurit(?:y|ies)\s+(?:firm|industry)\b",
    ),
    "banking": (
        r"은행(?:권|업계)?", r"금융지주", r"\bbank(?:ing|s)?\b",
    ),
    "insurance": (r"보험(?:사|업계)?", r"\binsur(?:er|ers|ance)\b"),
    "venture_capital": (
        r"벤처캐피탈", r"vc협회", r"모태펀드", r"\bventure capital\b",
    ),
    "government": (
        r"정부", r"금융위원회", r"금융감독원", r"국회", r"당국",
        r"\bgovernment\b", r"\bregulator\b",
    ),
}


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

_POLICY_ACTOR_PATTERNS = {
    "federal_reserve": (r"\bfederal reserve\b", r"\bthe fed\b", r"\b연준\b"),
    "ecb": (r"\beuropean central bank\b", r"\becb\b", r"유럽중앙은행"),
    "bank_of_korea": (r"\bbank of korea\b", r"한국은행", r"\b한은\b"),
    "bank_of_england": (r"\bbank of england\b", r"\bboe\b", r"영란은행"),
    "bank_of_japan": (r"\bbank of japan\b", r"\bboj\b", r"일본은행"),
    "pboc": (r"\bpeople'?s bank of china\b", r"\bpboc\b", r"중국인민은행"),
    "turkey_central_bank": (
        r"\bturkey(?:'s)?\s+central bank\b",
        r"\bturkish\b.{0,80}\bcentral bank\b",
        r"\bcbrt\b",
    ),
}

_POLICY_ACTION_PATTERNS = {
    "funding": (
        r"\bfunding\b", r"\bliquidity\b", r"\brefinancing\b",
        "유동성", "자금 공급", "자금공급",
    ),
    "interest_rate": (
        r"\b(?:interest|policy|benchmark|deposit|lending)\s+rates?\b",
        r"\brate\s+(?:cut|hike|increase|decrease)\b",
        "기준금리", "정책금리", "금리 인상", "금리 인하",
    ),
    "asset_purchase": (
        r"\basset purchases?\b", r"\bquantitative (?:easing|tightening)\b",
        "자산 매입", "양적완화", "양적긴축",
    ),
    "reserve_requirement": (
        r"\breserve requirements?\b", r"\brequired reserve\b",
        "지급준비율", "지준율",
    ),
    "currency_intervention": (
        r"\bcurrency intervention\b", r"\bforeign exchange intervention\b",
        "외환시장 개입", "환율 개입",
    ),
}

_MARKET_REACTION_PATTERNS = (
    r"\brall(?:y|ies|ied)\b", r"\bsurg(?:e|es|ed)\b", r"\bjump(?:s|ed)?\b",
    r"\bclimb(?:s|ed)?\b", r"\bgain(?:s|ed)?\b", r"\bsoar(?:s|ed)?\b",
    r"\bfall(?:s|en)?\b", r"\bfell\b", r"\bdrop(?:s|ped)?\b",
    r"\bslid(?:e|es)\b", r"\bslump(?:s|ed)?\b", r"\bsell[- ]?off\b",
    "급등", "상승", "반등", "강세", "급락", "하락", "약세",
)

_POLICY_RATE_VALUE_PATTERNS = (
    r"(?:policy|interest|benchmark|deposit|lending)\s+rates?"
    r"(?:\s+\w+){0,4}\s+(?:to|at|of)?\s*(\d+(?:\.\d+)?\s?%)",
    r"(\d+(?:\.\d+)?\s?%)\s+"
    r"(?:policy|interest|benchmark|deposit|lending)\s+rates?",
    r"(?:기준|정책)금리(?:를|가|는|도)?\s*(\d+(?:\.\d+)?\s?%)",
    r"(\d+(?:\.\d+)?\s?%)\s*(?:의\s*)?(?:기준|정책)금리",
)

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

_COMPREHENSIVE_ARTICLE_PATTERNS = (
    r"[\[(（]\s*종합\s*[\])）]", r"\b종합(?:판|기사|분석)?\b",
    r"\bupdated?\b", r"\bfull report\b", r"\bexplainer\b",
    r"최종(?:안|결과|집계)", r"심층(?:분석|취재)",
)

_AGGREGATOR_DOMAINS = (
    "news.google.com",
    "v.daum.net",
    "news.nate.com",
    "news.yahoo.com",
    "msn.com",
)

_EDITOR_EVENT_TOKEN_ALIASES = {
    "merger": "merge",
    "merges": "merge",
    "merged": "merge",
    "merging": "merge",
    "acquire": "acquisition",
    "acquires": "acquisition",
    "acquired": "acquisition",
    "raise": "funding",
    "raises": "funding",
    "raised": "funding",
    "financing": "funding",
}
_EDITOR_EVENT_TOKEN_EXPANSIONS = {
    "cix": ("climate", "impact", "x"),
}


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


def _link_quality(article: dict) -> int:
    """Prefer publisher URLs over relay and unresolved Google News links."""
    link = _first_text(article.get("link")).lower()
    if not link:
        return 0

    domain = urlsplit(link).netloc.removeprefix("www.")
    if domain == "news.google.com":
        return 0
    if any(domain == item or domain.endswith(f".{item}") for item in _AGGREGATOR_DOMAINS):
        return 1
    if article.get("gnews_link"):
        return 2
    return 3


def _is_direct_article(article: dict) -> bool:
    return _link_quality(article) == 3


def _comprehensive_article_score(article: dict) -> int:
    text = _article_text(article)
    return sum(
        bool(re.search(pattern, text, re.IGNORECASE))
        for pattern in _COMPREHENSIVE_ARTICLE_PATTERNS
    )


def _fact_detail_score(article: dict) -> int:
    text = _article_text(article)
    return sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in _FACT_PATTERNS)


def _published_rank(article: dict) -> int:
    raw_date = str(article.get("date") or "").strip()
    try:
        return datetime.strptime(raw_date[:10], "%Y-%m-%d").toordinal()
    except (TypeError, ValueError):
        return 0


def _publisher_origin_priority(article: dict) -> int:
    """Prefer overseas originals when source quality is otherwise tied."""
    feed = _first_text(article.get("feed")).strip()
    source = _first_text(article.get("source")).strip()
    link = _first_text(article.get("link")).strip()
    domain = urlsplit(link).netloc.casefold().removeprefix("www.")
    publisher_text = f"{feed} {source}".casefold()
    is_domestic_publisher = (
        feed.startswith("국내")
        or domain.endswith(".kr")
        or domain == "impacton.net"
        or domain.endswith(".impacton.net")
        or "임팩트온" in publisher_text
        or "impacton" in publisher_text
    )
    return 0 if is_domestic_publisher else 1


def _publication_gap_days(left: dict, right: dict) -> int | None:
    left_rank = _published_rank(left)
    right_rank = _published_rank(right)
    if not left_rank or not right_rank:
        return None
    return abs(left_rank - right_rank)


def _entity_families(article: dict) -> set:
    text = _article_text(article)
    return {
        family
        for family, patterns in _ENTITY_FAMILY_PATTERNS.items()
        if _matches_any(text, patterns)
    }


def _same_discovery_topic(left: dict, right: dict) -> bool:
    left_feed = _first_text(left.get("feed"))
    right_feed = _first_text(right.get("feed"))
    return bool(left_feed) and left_feed == right_feed and left_feed in DISCOVERY_TOPICS


def _same_source(left: dict, right: dict) -> bool:
    left_source = _first_text(left.get("source")).casefold()
    right_source = _first_text(right.get("source")).casefold()
    return bool(left_source) and left_source == right_source


def _topic_series_compatible(left: dict, right: dict) -> bool:
    """Identify adjacent installments of one issue without merging a whole topic."""
    if not _same_discovery_topic(left, right) or not _same_source(left, right):
        return False

    date_gap = _publication_gap_days(left, right)
    if date_gap is None or date_gap > _TOPIC_SERIES_MAX_DATE_GAP_DAYS:
        return False

    left_families = _entity_families(left)
    right_families = _entity_families(right)
    return bool(left_families & right_families)


def _representative_key(article: dict) -> tuple:
    description_length = len(str(article.get("description") or ""))
    return (
        _publisher_origin_priority(article),
        _source_priority(article),
        _link_quality(article),
        _comprehensive_article_score(article),
        _fact_detail_score(article),
        _published_rank(article),
        description_length,
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


def _matching_keys(article: dict, pattern_groups: dict) -> set:
    text = _article_text(article)
    return {
        key
        for key, patterns in pattern_groups.items()
        if _matches_any(text, patterns)
    }


def _policy_rate_values(article: dict) -> set:
    text = _article_text(article)
    values = set()
    for pattern in _POLICY_RATE_VALUE_PATTERNS:
        values.update(
            match.replace(" ", "")
            for match in re.findall(pattern, text, re.IGNORECASE)
        )
    return values


def _same_policy_event_with_reaction(left: dict, right: dict) -> bool:
    """Merge one policy decision with its immediate market-reaction headline."""
    date_gap = _publication_gap_days(left, right)
    if date_gap is None or date_gap > _POLICY_REACTION_MAX_DATE_GAP_DAYS:
        return False

    left_actors = _matching_keys(left, _POLICY_ACTOR_PATTERNS)
    right_actors = _matching_keys(right, _POLICY_ACTOR_PATTERNS)
    if not left_actors.intersection(right_actors):
        return False

    left_actions = _matching_keys(left, _POLICY_ACTION_PATTERNS)
    right_actions = _matching_keys(right, _POLICY_ACTION_PATTERNS)
    if not left_actions.intersection(right_actions):
        return False

    if not (
        _matches_any(_article_text(left), _MARKET_REACTION_PATTERNS)
        or _matches_any(_article_text(right), _MARKET_REACTION_PATTERNS)
    ):
        return False

    # 같은 날의 서로 다른 금리 결정을 잘못 합치지 않는다.
    left_rates = _policy_rate_values(left)
    right_rates = _policy_rate_values(right)
    if left_rates and right_rates and left_rates.isdisjoint(right_rates):
        return False

    return True


def _normalized_title(article: dict) -> str:
    return " ".join(str(article.get("title") or "").casefold().split())


def _same_editor_event(left: dict, right: dict) -> bool:
    """Match the language-neutral event key produced by the editorial gate."""
    left_key = _canonical_editor_event_key(left)
    right_key = _canonical_editor_event_key(right)
    return bool(left_key) and left_key == right_key


def _canonical_editor_event_key(article: dict) -> str:
    """Normalize common aliases in model-generated event identifiers."""
    raw_key = str(article.get("editor_event_key") or "").casefold()
    tokens = re.findall(r"[a-z0-9가-힣]+", raw_key)
    normalized = []
    for token in tokens:
        expansion = _EDITOR_EVENT_TOKEN_EXPANSIONS.get(token)
        if expansion:
            normalized.extend(expansion)
            continue
        normalized.append(_EDITOR_EVENT_TOKEN_ALIASES.get(token, token))
    return "_".join(sorted(set(normalized)))


def collapse_editor_event_duplicates(
    articles: list,
    category_priority: dict,
) -> list:
    """Keep one trusted representative for an event split across categories."""
    grouped = {}
    order = []
    for index, article in enumerate(articles):
        event_key = _canonical_editor_event_key(article)
        group_key = ("event", event_key) if event_key else ("article", index)
        if group_key not in grouped:
            grouped[group_key] = []
            order.append(group_key)
        grouped[group_key].append(article)

    collapsed = []
    for group_key in order:
        group = grouped[group_key]
        if group_key[0] != "event" or len(group) == 1:
            collapsed.append(group[0])
            continue

        representative = max(group, key=_representative_key)
        category_owner = max(
            group,
            key=lambda article: (
                article.get("category_reason") == "official_insights_source",
                int(category_priority.get(article.get("category"), 0)),
                float(article.get("relevance") or 0),
            ),
        )
        representative["category"] = category_owner.get("category")
        representative["category_reason"] = "editor_event_key_consensus"
        representative["editor_event_key"] = group_key[1]
        representative["region"] = category_owner.get("region", representative.get("region"))
        representative["region_reason"] = category_owner.get(
            "region_reason",
            representative.get("region_reason"),
        )
        representative["relevance"] = max(
            float(article.get("relevance") or 0)
            for article in group
        )
        editor_scores = [
            float(article.get("editor_score") or 0)
            for article in group
            if article.get("editor_score") is not None
        ]
        if editor_scores:
            representative["editor_score"] = max(editor_scores)
        representative["major_deal"] = any(
            article.get("major_deal", False) for article in group
        )
        representative["impact_must_read"] = any(
            article.get("impact_must_read", False) for article in group
        )
        collapsed.append(representative)

    dropped = len(articles) - len(collapsed)
    if dropped:
        print(f"   ↪ 카테고리 간 같은 사건 {dropped}건을 통합")
    return collapsed


def _candidate_tokens(title: str) -> set:
    """Return brand, organization, number, and Korean phrase tokens."""
    tokens = {
        token.strip(".-")
        for token in re.findall(
            r"[a-z0-9][a-z0-9+.-]{2,}|[가-힣]{2,}",
            title.casefold(),
        )
        if len(token.strip(".-")) >= 3
    }
    return {
        token
        for token in tokens
        if token not in _CANDIDATE_TOKEN_STOPWORDS
    }


def _headline_event_tokens(article: dict) -> set:
    """Extract short Korean/English anchors used to detect one reported event."""
    tokens = set()
    for raw_token in re.findall(
        r"[a-z0-9가-힣]+",
        _normalized_title(article),
        re.IGNORECASE,
    ):
        token = raw_token.casefold()
        for suffix in _KOREAN_PARTICLE_SUFFIXES:
            if len(token) >= len(suffix) + 2 and token.endswith(suffix):
                token = token[:-len(suffix)]
                break
        if len(token) >= 2 and token not in _HEADLINE_EVENT_TOKEN_STOPWORDS:
            tokens.add(token)
    return tokens


def _same_headline_event(left: dict, right: dict) -> bool:
    """Catch close-date paraphrases that Korean sentence embeddings can miss."""
    date_gap = _publication_gap_days(left, right)
    if date_gap is not None and date_gap > 1:
        return False
    if not _events_compatible(left, right):
        return False

    left_rates = _policy_rate_values(left)
    right_rates = _policy_rate_values(right)
    if left_rates and right_rates and left_rates.isdisjoint(right_rates):
        return False

    left_tokens = _headline_event_tokens(left)
    right_tokens = _headline_event_tokens(right)
    shared = left_tokens & right_tokens
    smaller_size = min(len(left_tokens), len(right_tokens))
    return (
        len(shared) >= 4
        and smaller_size >= 4
        and len(shared) / smaller_size >= 0.65
    )


def _large_batch_candidate_pairs(articles: list) -> set:
    """Find plausible duplicate pairs cheaply before loading the AI model."""
    titles = [_normalized_title(article) for article in articles]
    candidate_pairs = set()

    # Exact titles must always be compared and merged.
    exact_groups = {}
    for index, title in enumerate(titles):
        exact_groups.setdefault(title, []).append(index)
    for group in exact_groups.values():
        for offset, left in enumerate(group):
            for right in group[offset + 1:]:
                candidate_pairs.add((left, right))

    # Character n-grams handle Korean particles and slightly different headlines.
    try:
        lexical_vectors = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            min_df=2,
            max_features=30000,
        ).fit_transform(titles)
        lexical_similarity = cosine_similarity(
            lexical_vectors,
            dense_output=False,
        ).tocoo()
        for left, right, score in zip(
            lexical_similarity.row,
            lexical_similarity.col,
            lexical_similarity.data,
        ):
            if left < right and float(score) >= _LEXICAL_CANDIDATE_FLOOR:
                candidate_pairs.add((int(left), int(right)))
    except ValueError:
        # All titles may be empty or have no repeated n-grams.
        pass

    # A shared rare brand/entity token catches paraphrases with low word order overlap.
    token_documents = {}
    for index, title in enumerate(titles):
        for token in _candidate_tokens(title):
            token_documents.setdefault(token, []).append(index)
    for document_indices in token_documents.values():
        if not 2 <= len(document_indices) <= _RARE_TOKEN_MAX_DOCUMENTS:
            continue
        for offset, left in enumerate(document_indices):
            for right in document_indices[offset + 1:]:
                candidate_pairs.add((left, right))

    # Discovery-topic installments intentionally use a lower semantic threshold.
    topic_groups = {}
    for index, article in enumerate(articles):
        feed = _first_text(article.get("feed"))
        source = _first_text(article.get("source")).casefold()
        if feed in DISCOVERY_TOPICS and source:
            topic_groups.setdefault((feed, source), []).append(index)
    for group in topic_groups.values():
        for offset, left in enumerate(group):
            for right in group[offset + 1:]:
                gap = _publication_gap_days(articles[left], articles[right])
                if gap is not None and gap <= _TOPIC_SERIES_MAX_DATE_GAP_DAYS:
                    candidate_pairs.add((left, right))

    return {
        (left, right)
        for left, right in candidate_pairs
        if _events_compatible(articles[left], articles[right])
    }


def _should_merge(left: dict, right: dict, similarity: float) -> bool:
    if not _events_compatible(left, right):
        return False

    if _same_headline_event(left, right):
        return True

    left_families = _entity_families(left)
    right_families = _entity_families(right)
    if (
        _same_discovery_topic(left, right)
        and left_families
        and right_families
        and left_families.isdisjoint(right_families)
    ):
        return False

    if similarity >= SIMILARITY_THRESHOLD:
        return True

    if (
        similarity >= _POLICY_REACTION_SIMILARITY_FLOOR
        and _same_policy_event_with_reaction(left, right)
    ):
        return True

    return (
        similarity >= _TOPIC_SERIES_SIMILARITY_FLOOR
        and _topic_series_compatible(left, right)
    )


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
        f"link_quality={_link_quality(representative)},"
        f"comprehensive={_comprehensive_article_score(representative)},"
        f"facts={_fact_detail_score(representative)},"
        f"published={_published_rank(representative)}"
    )
    return merged


def deduplicate_and_merge(articles: list) -> tuple:
    errors = []

    if not articles:
        return [], errors

    try:
        model = _get_model()
        if len(articles) <= _FULL_SEMANTIC_SCAN_LIMIT:
            candidate_pairs = {
                (left, right)
                for left in range(len(articles))
                for right in range(left + 1, len(articles))
            }
        else:
            candidate_pairs = _large_batch_candidate_pairs(articles)

        candidate_indices = sorted({
            index
            for pair in candidate_pairs
            for index in pair
        })
        if not candidate_indices:
            return [_merge_group([article]) for article in articles], errors

        local_index = {
            article_index: embedding_index
            for embedding_index, article_index in enumerate(candidate_indices)
        }
        titles = [articles[index]["title"] for index in candidate_indices]
        embeddings = model.encode(titles, convert_to_numpy=True)
        sim_matrix = cosine_similarity(embeddings)

        neighbors = {}
        for left, right in candidate_pairs:
            neighbors.setdefault(left, []).append(right)

        if len(articles) > _FULL_SEMANTIC_SCAN_LIMIT:
            print(
                f"⚡ 당일 중복 후보 축소: {len(articles)}건 중 "
                f"{len(candidate_indices)}건만 AI 비교 "
                f"(후보 쌍 {len(candidate_pairs)}개)"
            )

        visited = set()
        merged = []

        for i in range(len(articles)):
            if i in visited:
                continue

            group = [i]
            for j in sorted(neighbors.get(i, [])):
                if (
                    j not in visited
                    and _should_merge(
                        articles[i],
                        articles[j],
                        float(
                            sim_matrix[
                                local_index[i]
                            ][
                                local_index[j]
                            ]
                        ),
                    )
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
    event_key_positions = {}
    for index in range(len(articles)):
        event_key = str(articles[index].get("editor_event_key") or "").strip()
        if event_key and event_key in event_key_positions:
            kept_position = event_key_positions[event_key]
            representative_index = kept_indexes[kept_position]
            if _representative_key(articles[index]) > _representative_key(
                articles[representative_index]
            ):
                kept_indexes[kept_position] = index
            continue

        if any(
            _same_editor_event(articles[index], articles[kept])
            or _same_headline_event(articles[index], articles[kept])
            or similarity[index][kept] >= threshold
            for kept in kept_indexes
        ):
            continue
        kept_indexes.append(index)
        if event_key:
            event_key_positions[event_key] = len(kept_indexes) - 1

    dropped = len(articles) - len(kept_indexes)
    if dropped:
        print(f"   ↪ 같은 사건 {dropped}건을 선정에서 제외")
    return [articles[i] for i in kept_indexes]
