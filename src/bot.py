import os
import re
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import json
from pathlib import Path

from .fetchers import hackernews, rss_feeds
from .fetchers.rss_feeds import as_of_date
try:
    from .fetchers import newsletters
    HAS_NEWSLETTERS = True
except ImportError:
    # Try Gmail newsletters as fallback
    try:
        from .fetchers import gmail_newsletters as newsletters
        HAS_NEWSLETTERS = True
    except ImportError:
        HAS_NEWSLETTERS = False
        print("ℹ️ 뉴스레터/Gmail 모듈을 찾을 수 없어 수집 단계에서 제외합니다.")

from .processor import editor
from .processor.deduplicator import (
    collapse_editor_event_duplicates,
    deduplicate_and_merge,
    filter_near_duplicates,
)
from .processor.summarizer import summarize, keyword_hit
from .processor.reranker import rerank_by_category, is_enabled as llm_enabled
try:
    from .processor.translator import translate_titles
except Exception as _e:      # ImportError 뿐 아니라 하위 import 실패도 포착
    print(f"⚠️ 번역 모듈 로드 실패({_e}) — 번역 없이 진행합니다. "
          f"(processor/translator.py 존재 여부 확인)")
    def translate_titles(arts):
        return arts
from .config import (
    INTEREST_KEYWORDS,
    BLACKLIST_KEYWORDS,
    HN_KEYWORDS,
    CATEGORIES,
    MAX_PER_CATEGORY_DICT,
    MAX_PER_CATEGORY,
    IMPACT_MUST_READ_MAX,
    ALTERNATIVE_MAJOR_DEAL_MAX,
    OVERSEAS_PREFERRED_DOMAINS,
    REGION_WEIGHT,
    INSIGHTS_DOMESTIC_SCORE_TOLERANCE,
    SELECTION_SIMILARITY_THRESHOLD,
    HARD_EXCLUSION_KEYWORDS,
    SOFT_EDITORIAL_EXCLUSION_KEYWORDS,
    OPINION_FORMAT_KEYWORDS,
    OPINION_URL_PATTERNS,
    RESCUE_EVENT_SIGNALS,
    RSS_SOURCE_METADATA,
    FEED_CATEGORY_OVERRIDE,
    SIMILARITY_THRESHOLD,
)
try:
    from .config import LLM_SEND_MIN_SCORE
except ImportError:
    LLM_SEND_MIN_SCORE = 0
try:
    from .config import SLACK_HEADER          # 예: "📰 ISQ Daily News | {date}" / "" 이면 헤더 없음
except ImportError:
    SLACK_HEADER = ""

from .utils.file_handler import load_lines, save_lines, SEEN_FILE, SEEN_TITLES_FILE
CATEGORY_ORDER = list(CATEGORIES.keys())
IMPACT_CATEGORY = next(category for category in CATEGORY_ORDER if category.startswith("🌱"))
AI_CATEGORY = next(category for category in CATEGORY_ORDER if category.startswith("🤖"))
ALTERNATIVE_CATEGORY = next(category for category in CATEGORY_ORDER if category.startswith("📈"))
MACRO_CATEGORY = next(category for category in CATEGORY_ORDER if category.startswith("🌐"))
INSIGHTS_CATEGORY = next(category for category in CATEGORY_ORDER if category.startswith("👔"))
EDITOR_EVENT_CATEGORY_PRIORITY = {
    IMPACT_CATEGORY: 50,
    AI_CATEGORY: 40,
    ALTERNATIVE_CATEGORY: 30,
    MACRO_CATEGORY: 20,
    INSIGHTS_CATEGORY: 10,
}
REGION_SPLIT_CATEGORIES = {ALTERNATIVE_CATEGORY, MACRO_CATEGORY}
REGION_DISPLAY_ORDER = (("global", "해외"), ("korea", "국내"))
IMPACT_SOURCE_SOFT_CAP = max(1, IMPACT_MUST_READ_MAX // 2)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer"}
SLACK_ARCHIVE_PATH = Path(__file__).parent.parent / "data" / "slack_archive.jsonl"
KOREA_TIMEZONE = timezone(timedelta(hours=9))


def normalize_url(url: str) -> str:
    """Remove tracking parameters so one article has one persistent identity."""
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return url
    if not parts.scheme or not parts.netloc:
        return url

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _char_ngrams(text: str, n: int = 3) -> set:
    t = re.sub(r'[^\w가-힣]', '', text.lower())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _hangul_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if '가' <= c <= '힣') / len(letters)


def is_same_news_issue(title_a: str, title_b: str) -> bool:
    """같은 사건(다매체 중복) 판정. 한국어는 문자 3-gram(0.35), 영어는 단어 토큰(0.50).
    영어 임계를 높게 둬 'Fed vs ECB' 류 과병합을 막고, 영어 의미중복은 임베딩 dedup이 보완."""
    if _hangul_ratio(title_a) > 0.3 or _hangul_ratio(title_b) > 0.3:
        sa, sb = _char_ngrams(title_a), _char_ngrams(title_b)
        threshold = 0.35
    else:
        sa = {w for w in re.sub(r'[^\w\s]', ' ', title_a.lower()).split() if len(w) >= 3}
        sb = {w for w in re.sub(r'[^\w\s]', ' ', title_b.lower()).split() if len(w) >= 3}
        threshold = 0.50
    if not sa or not sb:
        return False
    return (len(sa & sb) / min(len(sa), len(sb))) >= threshold


def _text_value(value) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item)
    return str(value or "")


def _first_keyword_hit(keywords: list, text: str) -> str:
    for keyword in keywords:
        if keyword_hit(keyword, text):
            return keyword
    return ""


def _opinion_marker(article: dict) -> str:
    link = _text_value(article.get("link")).lower()
    for pattern in OPINION_URL_PATTERNS:
        if pattern.lower() in link:
            return pattern

    title_and_metadata = " ".join([
        _text_value(article.get("title")),
        _text_value(article.get("section")),
        _text_value(article.get("type")),
        _text_value(article.get("tags")),
    ]).lower()
    return _first_keyword_hit(OPINION_FORMAT_KEYWORDS, title_and_metadata)


def _is_curated_primary_source(article: dict) -> bool:
    source = _text_value(article.get("source")).strip()
    feed = _text_value(article.get("feed")).strip()
    source_meta = RSS_SOURCE_METADATA.get(source) or RSS_SOURCE_METADATA.get(feed)
    return bool(
        FEED_CATEGORY_OVERRIDE.get(feed)
        or (source_meta and source_meta.get("tier") == "primary")
    )


def is_relevant(article: dict, require_topic_match: bool = True) -> bool:
    """수집 기사를 통과시킬지 판정한다.

    require_topic_match=False 는 편집 게이트가 뒤에서 판단할 때 쓴다. 관심
    키워드에 걸리는지를 통과 조건으로 두면, 목록에 없는 표현을 쓴 기사가
    게이트에 닿지도 못하고 죽는다. 실제 실행에서 이 조건으로 탈락한 56건에
    규제 변화(일본 AI 학습데이터 공개 의무화)와 M&A(SpaceX의 Cognition 인수
    시도) 같은 최우선 기사가 들어 있었다.

    이 경우에도 블랙리스트·하드 제외·오피니언 URL 같은 값싸고 명확한 차단은
    그대로 적용해 게이트에 보낼 양을 줄인다.
    """
    article.pop("filter_reason", None)
    article.pop("rescue_signal", None)
    article.pop("relevance_signal", None)

    title = _text_value(article.get("title"))
    description = _text_value(article.get("description") or article.get("summary"))
    text = f"{title} {description}".lower()

    blacklist_hit = _first_keyword_hit(BLACKLIST_KEYWORDS, text)
    if blacklist_hit:
        article["filter_reason"] = f"blacklist:{blacklist_hit}"
        return False

    opinion_hit = _opinion_marker(article)
    if opinion_hit:
        article["filter_reason"] = f"opinion:{opinion_hit}"
        return False

    hard_exclusion_hit = _first_keyword_hit(HARD_EXCLUSION_KEYWORDS, text)
    if hard_exclusion_hit:
        article["filter_reason"] = f"hard_exclusion:{hard_exclusion_hit}"
        return False

    event_hit = _first_keyword_hit(RESCUE_EVENT_SIGNALS, text)
    soft_exclusion_hit = _first_keyword_hit(SOFT_EDITORIAL_EXCLUSION_KEYWORDS, text)
    if soft_exclusion_hit:
        if not event_hit:
            # 게이트가 뒤에 있으면 애매한 제외는 게이트가 문맥까지 보고 판단한다.
            if require_topic_match:
                article["filter_reason"] = f"soft_exclusion:{soft_exclusion_hit}"
                return False
        else:
            article["rescue_signal"] = f"{soft_exclusion_hit}:{event_hit}"

    if not require_topic_match:
        article["relevance_signal"] = "deferred_to_editor"
        return True

    if _is_curated_primary_source(article):
        article["relevance_signal"] = "curated_primary_source"
        return True

    interest_hit = _first_keyword_hit(INTEREST_KEYWORDS, text)
    if interest_hit:
        article["relevance_signal"] = f"keyword:{interest_hit}"
        return True

    if event_hit:
        article["relevance_signal"] = f"event:{event_hit}"
        return True

    article["filter_reason"] = "no_relevant_signal"
    return False


def _batch_filter_semantic_duplicates(
    articles: list,
    embedding_model,
    load_store,
    threshold: float,
) -> list:
    """Compare final candidates with the cross-day store in one model call."""
    if not articles or embedding_model is None:
        return articles

    indexed_texts = [
        (
            index,
            (
                (article.get("title_orig") or article.get("title", ""))
                + " \n "
                + (article.get("description", "") or "")
            ).strip(),
        )
        for index, article in enumerate(articles)
    ]
    indexed_texts = [item for item in indexed_texts if item[1]]
    if not indexed_texts:
        return articles

    try:
        import numpy as np

        stored_embeddings, _ = load_store()
        embeddings = np.asarray(
            embedding_model.encode(
                [text for _, text in indexed_texts],
                convert_to_numpy=True,
            ),
            dtype=float,
        )
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)

        duplicate_flags = np.zeros(len(indexed_texts), dtype=bool)
        stored_embeddings = np.asarray(stored_embeddings, dtype=float)
        if (
            stored_embeddings.ndim == 2
            and stored_embeddings.size
            and stored_embeddings.shape[1] == embeddings.shape[1]
        ):
            candidate_norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            stored_norms = np.linalg.norm(stored_embeddings, axis=1, keepdims=True)
            candidate_norms[candidate_norms == 0] = 1.0
            stored_norms[stored_norms == 0] = 1.0
            similarities = (
                embeddings / candidate_norms
            ) @ (
                stored_embeddings / stored_norms
            ).T
            duplicate_flags = np.any(similarities >= float(threshold), axis=1)

        embedding_by_index = {
            article_index: embedding
            for (article_index, _), embedding in zip(indexed_texts, embeddings)
        }
        duplicate_indices = {
            article_index
            for (article_index, _), is_duplicate in zip(
                indexed_texts,
                duplicate_flags,
            )
            if is_duplicate
        }

        filtered = []
        for index, article in enumerate(articles):
            if index in duplicate_indices:
                continue
            embedding = embedding_by_index.get(index)
            if embedding is not None:
                article["_embedding"] = embedding
            filtered.append(article)

        print(
            f"⚡ 임베딩 중복 검사: {len(indexed_texts)}건 일괄 처리, "
            f"과거 발송 중복 {len(duplicate_indices)}건 제외"
        )
        return filtered
    except Exception as exc:
        # 중복 검사 장애가 뉴스 발송 전체를 막지 않게 원래 후보로 계속 진행한다.
        print(f"⚠️ 임베딩 일괄 검사 실패: {exc}")
        return articles


def get_primary_link(article: dict) -> str:
    link = article.get("link", "")
    if isinstance(link, list):
        return link[0] if link else ""
    return link


def get_primary_source(article: dict) -> str:
    src = article.get("source", "")
    if isinstance(src, list):
        return src[0] if src else ""
    return src


def clean_source_name(source: str) -> str:
    mapping = {
        "ImpactOn (임팩트온)": "임팩트온",
        "ImpactOn": "임팩트온",
        "Platum (플랫텀)": "플랫텀",
        "VentureSquare (벤처스퀘어)": "벤처스퀘어",
        "VentureSquare": "벤처스퀘어",
        "한경 Geeks (벤처/VC)": "한경 Geeks",
        "전자신문 (벤처/스타트업)": "전자신문",
        "Trellis (구 GreenBiz)": "Trellis",
        "The Batch (deeplearning.ai)": "The Batch",
        "SemiAnalysis (칩/인프라)": "SemiAnalysis",
        "Sifted (EU 스타트업)": "Sifted",
    }
    if source in mapping:
        return mapping[source]
    cleaned = re.sub(r'\s*\(.*?\)', '', source).strip()
    # 슬랙이 도메인/URL 형태 출처를 자동 링크하지 않도록 스킴 제거
    cleaned = re.sub(r'^https?://', '', cleaned).strip().strip('/')
    # 공백 없는 도메인형(예: news.bbsi.co.kr, TODAY.com)은 슬랙이 자동 링크를 걺
    #  → dot 뒤에 zero-width space(U+200B, 비가시) 삽입해 링크화 차단. 제목만 링크 유지.
    if cleaned and " " not in cleaned and re.search(r'\.[a-zA-Z]{2,}', cleaned):
        cleaned = cleaned.replace(".", ".\u200b")
    return cleaned if cleaned else source


def fmt_date(date_str: str) -> str:
    for fmt in ("%Y-%m-%d",):
        try:
            return datetime.strptime(date_str, fmt).strftime("%y.%m.%d")
        except (ValueError, TypeError):
            pass
    return datetime.now().strftime("%y.%m.%d")


def _article_region(article: dict) -> str:
    return "korea" if article.get("region") == "korea" else "global"


def _article_source_key(article: dict) -> str:
    """Normalize a publisher name for final-page diversity checks."""
    return " ".join(str(get_primary_source(article) or "").casefold().split())


def _selection_score(article: dict, category: str) -> float:
    llm = article.get("llm_score")
    if llm is not None:
        score = float(llm)
    else:
        score = float(article.get("relevance", 0))
    if (
        llm is None
        and category in OVERSEAS_PREFERRED_DOMAINS
        and _article_region(article) == "global"
    ):
        score *= REGION_WEIGHT.get("global", 1.0)
    return score + float(article.get("selection_score_adjustment", 0.0))


_MACRO_RATE_ACTORS = (
    (
        "bank_of_korea",
        re.compile(
            r"\bbank of korea\b|\bbok\b|한국은행|한은|금통위|금통위원",
            re.IGNORECASE,
        ),
    ),
    (
        "federal_reserve",
        re.compile(
            r"\bfederal reserve\b|\bthe fed\b|\bfed\b|\bfomc\b|연준|warsh",
            re.IGNORECASE,
        ),
    ),
    (
        "ecb",
        re.compile(r"\beuropean central bank\b|\becb\b|유럽중앙은행", re.IGNORECASE),
    ),
    (
        "bank_of_japan",
        re.compile(r"\bbank of japan\b|\bboj\b|일본은행", re.IGNORECASE),
    ),
    (
        "pboc",
        re.compile(
            r"\bpeople'?s bank of china\b|\bpboc\b|중국인민은행",
            re.IGNORECASE,
        ),
    ),
)
_MACRO_RATE_TOPIC = re.compile(
    r"\b(?:interest|policy|benchmark) rates?\b|\brate (?:hike|cut|path|outlook)\b|"
    r"기준금리|정책금리|금리(?:인상|인하|전망)?|통화정책|금통위",
    re.IGNORECASE,
)
_MACRO_RATE_DECISION = re.compile(
    r"\b(?:rate hike|rate cut)\b|"
    r"\b(?:raises?|raised|hikes?|hiked|cuts?|cut|lowers?|lowered|holds?|held|"
    r"keeps?|kept|leaves?|left)\b.{0,40}\b(?:interest|policy|benchmark)?\s*rates?\b|"
    r"\b(?:interest|policy|benchmark)?\s*rates?\b.{0,40}"
    r"\b(?:raised|hiked|cut|lowered|held|unchanged)\b|"
    r"기준금리.{0,30}(?:인상|인하|동결|유지|결정)|"
    r"(?:인상|인하|동결|유지).{0,30}기준금리",
    re.IGNORECASE,
)
_MACRO_RATE_OUTLOOK = re.compile(
    r"\b(?:outlook|forecast|projection|guidance|dot plot|signals?|expects?|"
    r"may|might|could)\b|전망|향후|추가\s*(?:인상|인하)|시사|예상|가능성",
    re.IGNORECASE,
)


def _macro_rate_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in (
            "title",
            "title_orig",
            "description",
            "editor_event_key",
            "editor_reason",
        )
    )


def _macro_rate_family(article: dict) -> str:
    text = _macro_rate_text(article)
    if not _MACRO_RATE_TOPIC.search(text):
        return ""
    event_date = str(article.get("date") or "").strip()
    for actor, pattern in _MACRO_RATE_ACTORS:
        if pattern.search(text):
            return f"{actor}:rates:{event_date}"
    return ""


def _macro_story_priority(article: dict) -> int:
    text = _macro_rate_text(article)
    if _MACRO_RATE_DECISION.search(text):
        return 3
    if _MACRO_RATE_OUTLOOK.search(text):
        return 2
    return 1


def _collapse_macro_rate_stories(ranked: list) -> list:
    """Use one representative per central-bank rate event in the daily macro page."""
    groups = {}
    passthrough = []
    for article in ranked:
        family = _macro_rate_family(article)
        if not family:
            passthrough.append(article)
            continue
        groups.setdefault(family, []).append(article)

    representatives = []
    for group in groups.values():
        representative = max(
            group,
            key=lambda article: (
                _macro_story_priority(article),
                _selection_score(article, MACRO_CATEGORY),
            ),
        )
        representative["_macro_event_score"] = max(
            _selection_score(article, MACRO_CATEGORY) for article in group
        )
        representatives.append(representative)

    combined = passthrough + representatives
    return sorted(
        combined,
        key=lambda article: (
            float(
                article.get("_macro_event_score")
                if article.get("_macro_event_score") is not None
                else _selection_score(article, MACRO_CATEGORY)
            ),
            _macro_story_priority(article),
            _selection_score(article, MACRO_CATEGORY),
        ),
        reverse=True,
    )


def _is_sendable(article: dict) -> bool:
    if article.get("editorial_excluded", False):
        return False

    llm_score = article.get("llm_score")
    return llm_score is None or float(llm_score) >= LLM_SEND_MIN_SCORE


def _select_category_articles(ranked: list, category: str) -> list:
    """Keep normal caps while preserving tagged impact and major-deal overflow."""
    base_limit = MAX_PER_CATEGORY_DICT.get(category, MAX_PER_CATEGORY)

    # 거시는 같은 중앙은행 금리 이벤트의 본 결정/전망/코멘트가 서로
    # 슬롯을 잡아먹기 전에 대표기사 하나로 접는다. 대표는 본 결정이 우선한다.
    if category == MACRO_CATEGORY:
        ranked = _collapse_macro_rate_stories(ranked)

    # 같은 사건이 한 카테고리를 다 차지하지 않도록 발송 직전에 한 번 더 솎는다.
    ranked = filter_near_duplicates(ranked, SELECTION_SIMILARITY_THRESHOLD)

    if category in REGION_SPLIT_CATEGORIES:
        # 대체투자·거시는 해외와 국내를 각각 최대 3개까지 보존한다.
        # 대체투자의 확정 주요 딜만 한 지역의 3개 제한을 넘을 수 있다.
        region_counts = {"global": 0, "korea": 0}
        selected = []
        overflow = []
        final_limit = base_limit * 2

        for article in ranked:
            region = _article_region(article)
            if region_counts[region] < base_limit:
                selected.append(article)
                region_counts[region] += 1
            elif category == ALTERNATIVE_CATEGORY and article.get("major_deal", False):
                overflow.append(article)

        for article in overflow:
            if len(selected) >= min(final_limit, ALTERNATIVE_MAJOR_DEAL_MAX):
                break
            if article not in selected:
                selected.append(article)

        return [
            article
            for region, _label in REGION_DISPLAY_ORDER
            for article in selected
            if _article_region(article) == region
        ]

    if category == IMPACT_CATEGORY:
        # 다른 출처가 있다면 한 언론사가 임팩트 지면을 독점하지 않게 한다.
        # 대체 출처가 전혀 없을 때는 제한 때문에 2개에서 멈추지 않고, 이미
        # 편집 자격을 통과한 다음 순위 기사로 기본 3개를 채울 수 있게 한다.
        selected = []
        deferred = []
        source_counts = {}

        def can_add(article: dict) -> bool:
            source = _article_source_key(article)
            return not source or source_counts.get(source, 0) < IMPACT_SOURCE_SOFT_CAP

        def add(article: dict) -> None:
            selected.append(article)
            source = _article_source_key(article)
            if source:
                source_counts[source] = source_counts.get(source, 0) + 1

        for article in ranked:
            if len(selected) >= base_limit:
                break
            if can_add(article):
                add(article)
            else:
                deferred.append(article)

        for article in deferred:
            if len(selected) >= base_limit:
                break
            add(article)

        must_read = [
            article
            for article in ranked
            if article.get("impact_must_read", False) and article not in selected
        ]
        deferred_must_read = []
        for article in must_read:
            if len(selected) >= IMPACT_MUST_READ_MAX:
                break
            if can_add(article):
                add(article)
            else:
                deferred_must_read.append(article)

        for article in deferred_must_read:
            if len(selected) >= IMPACT_MUST_READ_MAX:
                break
            add(article)
        return selected

    if category == INSIGHTS_CATEGORY:
        # 해외 공식 보고서를 먼저 두되, 국내 공식자료가 마지막 해외기사와
        # 품질 차이가 작으면 1건을 포함한다. 국내 자료가 약한 날에는 억지로
        # 채우지 않는다.
        selected = list(ranked[:base_limit])
        if len(selected) < base_limit or any(
            _article_region(article) == "korea" for article in selected
        ):
            return selected

        domestic_candidates = [
            article for article in ranked
            if _article_region(article) == "korea" and article not in selected
        ]
        if not domestic_candidates:
            return selected

        best_domestic = max(
            domestic_candidates,
            key=lambda article: _selection_score(article, category),
        )
        weakest_selected = min(
            selected,
            key=lambda article: _selection_score(article, category),
        )
        if (
            _selection_score(best_domestic, category)
            + INSIGHTS_DOMESTIC_SCORE_TOLERANCE
            >= _selection_score(weakest_selected, category)
        ):
            selected.remove(weakest_selected)
            selected.append(best_domestic)

        return [
            article for article in selected
            if _article_region(article) == "global"
        ] + [
            article for article in selected
            if _article_region(article) == "korea"
        ]

    overflow_flag = ""
    final_limit = base_limit
    if category == IMPACT_CATEGORY:
        overflow_flag = "impact_must_read"
        final_limit = max(base_limit, IMPACT_MUST_READ_MAX)
    elif category == ALTERNATIVE_CATEGORY:
        overflow_flag = "major_deal"
        final_limit = max(base_limit, ALTERNATIVE_MAJOR_DEAL_MAX)

    selected = []
    nvidia = 0
    for article in ranked:
        if len(selected) >= base_limit and (
            not overflow_flag
            or not article.get(overflow_flag, False)
            or len(selected) >= final_limit
        ):
            continue

        title_lower = article.get("title", "").lower()
        if "nvidia" in title_lower or "엔비디아" in title_lower:
            if nvidia >= 2:
                continue
            nvidia += 1
        selected.append(article)

    return selected


def is_dry_run() -> bool:
    """DRY_RUN=1 이면 실제 발송·상태 저장 없이 결과만 출력한다(테스트용)."""
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True")


def editor_gate_enabled() -> bool:
    """EDITOR_GATE=1 이면 LLM 편집 게이트를 키워드 판정 위에 덧씌운다.

    기본값은 꺼짐이다. tools/run_eval.py 로 실제 정확도를 확인하기 전까지는
    운영 동작을 바꾸지 않는다.
    """
    return os.environ.get("EDITOR_GATE", "").strip() in ("1", "true", "True")


def filter_to_as_of_date(articles: list) -> list:
    """재현 모드에서 대상 날짜 기사만 남긴다.

    날짜 창은 RSS 피드에서만 적용된다. 해커뉴스와 Gmail 뉴스레터는 각자
    수집하므로, 실제로 월요일 재현에 목요일 기사 5건이 섞여 들어왔다.
    출처마다 고치는 대신 수집이 끝난 지점에서 한 번에 거른다.
    """
    target = as_of_date()
    if not target:
        return articles

    wanted = target.strftime("%Y-%m-%d")
    kept = [a for a in articles if a.get("date") == wanted]
    dropped = len(articles) - len(kept)
    if dropped:
        print(f"🕰 재현 모드: 대상일({wanted}) 외 기사 {dropped}건 제외")
    return kept


def select_for_briefing(classified: list) -> tuple:
    """편집 판정을 적용한 최종 후보와 (탈락 기사, 오류) 를 돌려준다.

    편집 게이트는 키워드 판정 위에 덧씌운다. summarize 가 매긴 impact_must_read·
    major_deal 플래그를 선정 단계가 쓰기 때문이다. 따라서 걸러내기만 할 뿐,
    키워드가 죽인 기사를 되살리지는 못한다.

    게이트가 돌았으면 리랭커는 건너뛴다. 둘 다 '읽을 가치가 있는가' 를 판정하는
    편집자인데 기준이 서로 달라, 겹쳐 돌리면 뒤의 것이 앞의 판단을 뒤집는다.
    실제 실행에서 게이트가 통과시킨 193건이 리랭커를 지나며 4건으로 줄어
    카테고리 셋이 비었다. 게이트가 카테고리와 0~10 점수를 이미 주므로 선정은
    점수순으로 충분하다.
    """
    rejected, errors = [], []
    gate_applied = False

    # summarize 단계의 확정 제외는 LLM이 되살릴 수 없다. 정례 공지·단독
    # 그래픽처럼 규칙으로 이미 판별된 노이즈를 모델에 보내지 않으면 비용과
    # 실행 시간도 줄고, 모델 응답이 editorial_excluded 값을 덮어쓰지 않는다.
    deterministic_rejected = [
        article
        for article in classified
        if article.get("editorial_excluded", False)
    ]
    if deterministic_rejected:
        rejected.extend(deterministic_rejected)
        classified = [
            article
            for article in classified
            if not article.get("editorial_excluded", False)
        ]
        print(
            f"🧹 확정 제외 규칙으로 {len(deterministic_rejected)}건을 "
            "편집 게이트 전에 제외했습니다."
        )

    if editor_gate_enabled():
        reviewed, gate_errors = editor.review(classified)
        errors.extend(gate_errors)
        if reviewed is None:
            print("⚠️ 편집 게이트 실패 — 키워드 판정 결과로 계속 진행합니다.")
        else:
            dropped = len(classified) - len(reviewed)
            print(f"🧑‍⚖️ 편집 게이트가 {dropped}건을 추가로 걸렀습니다.")
            rejected.extend(a for a in classified if a.get("editor_verdict") == "reject")
            classified = reviewed
            gate_applied = True

    classified.sort(key=lambda a: a.get("relevance", 0), reverse=True)

    print("\n===== CATEGORY DEBUG =====")
    for category in CATEGORY_ORDER:
        items = [x for x in classified if x.get("category") == category]
        print(f"\n{category}: {len(items)}개")
        for item in items[:3]:
            print("-", item.get("title"))

    if gate_applied:
        print("🧑‍⚖️ 편집 게이트 점수를 사용합니다 (리랭커 생략).")
    else:
        classified = rerank_by_category(classified, CATEGORY_ORDER)
        if llm_enabled():
            print("LLM 리랭크 적용됨 (Gemini)")

    return classified, rejected, errors


def _decision_record(article: dict, verdict: str) -> dict:
    """평가셋 구축과 사후 추적에 필요한 필드만 추린다."""
    return {
        "verdict": verdict,
        "title": article.get("title_orig") or article.get("title"),
        "source": get_primary_source(article),
        "feed": article.get("feed"),
        "url": get_primary_link(article),
        "category": article.get("category"),
        "category_reason": article.get("category_reason"),
        "region": _article_region(article),
        "region_reason": article.get("region_reason"),
        "relevance": article.get("relevance"),
        "filter_reason": article.get("filter_reason"),
        "editor_reason": article.get("editor_reason"),
        "editor_score": article.get("editor_score"),
        "editor_event_key": article.get("editor_event_key"),
        "relevance_signal": article.get("relevance_signal"),
        "editorial_signals": article.get("editorial_signals"),
        "deal_signals": article.get("deal_signals"),
        "selection_adjustments": article.get("selection_adjustments"),
        "selection_score_adjustment": article.get("selection_score_adjustment"),
    }


def save_run_decisions(rejected: list, considered: list, sent: list) -> None:
    """이번 실행의 기사별 판정을 통째로 남긴다(덮어쓰기라 파일이 자라지 않는다).

    슬랙 아카이브는 '나간 것'의 렌더링 결과만 담아 feed 같은 라우팅 정보가
    사라진다. 평가셋을 실제 파이프라인과 같은 입력으로 만들려면 구조화된
    기록이 필요하고, 무엇이 왜 탈락했는지는 여기에만 남는다.
    """
    sent_ids = {id(a) for a in sent}
    records = [_decision_record(a, "rejected") for a in rejected]
    records += [
        _decision_record(a, "sent" if id(a) in sent_ids else "not_selected")
        for a in considered
    ]
    path = Path(__file__).parent.parent / "data" / "last_run_decisions.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"ts": datetime.utcnow().isoformat(), "decisions": records},
                f, ensure_ascii=False, indent=2,
            )
        print(f"🧾 판정 로그 {len(records)}건 기록 → {path.name}")
    except OSError as e:
        print(f"⚠️ 판정 로그 저장 실패({e}) — 발송에는 영향 없음")


def bucket_by_category(articles) -> dict:
    """기사를 카테고리별로 묶는다. 모르는 카테고리는 마지막 카테고리로 보낸다."""
    buckets = {cat: [] for cat in CATEGORY_ORDER}
    for a in articles:
        cat = a.get("category", CATEGORY_ORDER[-1])
        if cat not in buckets:
            cat = CATEGORY_ORDER[-1]
        buckets[cat].append(a)
    return buckets


def _format_article_line(article: dict) -> str:
    title = article.get("title", "제목 없음").strip()
    url = get_primary_link(article) or "#"
    source = clean_source_name(get_primary_source(article) or "출처미상")
    date = fmt_date(article.get("date", ""))
    return f"• <{url}|{title}> ({source}, {date})"


def _slack_list_items(lines: list) -> list:
    """Convert articles into Slack-native list items without typed bullet glyphs."""
    list_items = []
    for item in lines:
        article = item.get("article")
        if article is None:
            item_elements = [{"type": "text", "text": "오늘 조건에 맞는 뉴스가 없습니다."}]
        else:
            title = article.get("title", "제목 없음").strip()
            url = get_primary_link(article)
            source = clean_source_name(get_primary_source(article) or "출처미상")
            date = fmt_date(article.get("date", ""))
            if url:
                item_elements = [{"type": "link", "url": url, "text": title}]
            else:
                item_elements = [{"type": "text", "text": title}]
            item_elements.append({"type": "text", "text": f" ({source}, {date})"})

        list_items.append({
            "type": "rich_text_section",
            "elements": item_elements,
        })
    return list_items


def _build_slack_blocks(category_lines: dict) -> list:
    """Build one Slack message with native, consistently indented bullet lists."""
    blocks = []
    if SLACK_HEADER:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d")),
            },
        })

    for index, category in enumerate(CATEGORY_ORDER):
        rich_elements = [{
            "type": "rich_text_section",
            "elements": [{
                "type": "text",
                "text": category,
                "style": {"bold": True},
            }],
        }]

        if category in REGION_SPLIT_CATEGORIES:
            for region, label in REGION_DISPLAY_ORDER:
                region_lines = [
                    item
                    for item in category_lines.get(category, [])
                    if item.get("region") == region
                ] or [{"article": None, "region": region}]
                rich_elements.append({
                    "type": "rich_text_section",
                    "elements": [{
                        "type": "text",
                        "text": label,
                        "style": {"bold": True},
                    }],
                })
                rich_elements.append({
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": 0,
                    "elements": _slack_list_items(region_lines),
                })
        else:
            items = category_lines.get(category, []) or [{"article": None}]
            rich_elements.append({
                "type": "rich_text_list",
                "style": "bullet",
                "indent": 0,
                "elements": _slack_list_items(items),
            })

        blocks.append({"type": "rich_text", "elements": rich_elements})
        if index < len(CATEGORY_ORDER) - 1:
            blocks.append({"type": "divider"})
    return blocks


def render_digest(articles_by_category: dict) -> str:
    """슬랙·텔레그램 공용 다이제스트 본문. 카테고리 헤더는 비어 있어도 항상 표시한다."""
    parts = []
    if SLACK_HEADER:
        parts.append(SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d")))
        parts.append("")
    for cat in CATEGORY_ORDER:
        parts.append(f"*{cat}*")
        selected = articles_by_category.get(cat) or []
        if cat in REGION_SPLIT_CATEGORIES:
            for region, label in REGION_DISPLAY_ORDER:
                parts.append(f"*{label}*")
                region_articles = [
                    article
                    for article in selected
                    if _article_region(article) == region
                ]
                if region_articles:
                    parts.extend(_format_article_line(a) for a in region_articles)
                else:
                    parts.append("• 오늘 조건에 맞는 뉴스가 없습니다.")
        elif selected:
            parts.extend(_format_article_line(a) for a in selected)
        else:
            parts.append("• 오늘 조건에 맞는 뉴스가 없습니다.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _append_slack_archive(
    selected_by_category: dict,
    message_text: str,
    archive_path=None,
) -> None:
    """실제로 발송된 다이제스트를 주간 브리핑용 구조화 기록으로 남긴다."""
    path = Path(archive_path) if archive_path is not None else SLACK_ARCHIVE_PATH
    articles = []
    for category in CATEGORY_ORDER:
        for article in selected_by_category.get(category, []):
            primary_url = get_primary_link(article) or ""
            articles.append({
                "category": category,
                "region": _article_region(article),
                "region_reason": article.get("region_reason") or "",
                "title": article.get("title", "제목 없음").strip(),
                "title_orig": (article.get("title_orig") or "").strip(),
                "url": primary_url,
                "normalized_url": normalize_url(primary_url),
                "source": clean_source_name(get_primary_source(article) or "출처미상"),
                "feed": article.get("feed") or "",
                "date": fmt_date(article.get("date", "")),
                "category_reason": article.get("category_reason") or "",
                "event_type": article.get("event_type") or "",
                "deal_status": article.get("deal_status") or "",
                "major_deal": bool(article.get("major_deal", False)),
                "impact_theme": article.get("impact_theme") or "",
                "editor_event_key": article.get("editor_event_key") or "",
                "editor_score": article.get("editor_score"),
                "editor_reason": article.get("editor_reason") or "",
                "selection_score": _selection_score(article, category),
                "selection_reason": article.get("selection_reason") or "",
                "selection_adjustments": list(article.get("selection_adjustments") or []),
                "selection_score_adjustment": article.get("selection_score_adjustment", 0),
                "editorial_signals": list(article.get("editorial_signals") or []),
                "deal_signals": list(article.get("deal_signals") or []),
            })

    sent_at = datetime.now(timezone.utc)
    sent_at_korea = sent_at.astimezone(KOREA_TIMEZONE)
    iso_year, iso_week, _ = sent_at_korea.isocalendar()
    record = {
        "version": 3,
        "ts": sent_at.isoformat(),
        "timezone": "Asia/Seoul",
        "edition_date": sent_at_korea.date().isoformat(),
        "edition_week": f"{iso_year}-W{iso_week:02d}",
        "github": {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
            "event_name": os.environ.get("GITHUB_EVENT_NAME", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "head_sha": os.environ.get("GITHUB_SHA", ""),
        },
        "article_count": len(articles),
        "articles": articles,
        # 기존 기록을 읽는 도구와 사람이 그대로 확인할 수 있도록 본문도 유지한다.
        "text": message_text,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as archive_file:
        archive_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def send_aggregated_slack_news(articles) -> tuple:
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook_url and not is_dry_run():
        print("SLACK_WEBHOOK_URL 이 설정되지 않았습니다.")
        return False, []

    sendable_articles = [article for article in articles if _is_sendable(article)]
    sendable_articles = collapse_editor_event_duplicates(
        sendable_articles,
        EDITOR_EVENT_CATEGORY_PRIORITY,
    )
    buckets = bucket_by_category(sendable_articles)

    sent_articles = []          # ✅ 실제 슬랙에 나간 기사만 수집(seen 처리용)
    selected_by_category = {}
    for cat_name in CATEGORY_ORDER:
        ranked = sorted(buckets[cat_name], key=lambda a: _selection_score(a, cat_name), reverse=True)
        selected = _select_category_articles(ranked, cat_name)
        selected_by_category[cat_name] = selected
        sent_articles.extend(selected)

    # 선정·중복 제거를 원문 제목으로 모두 끝낸 뒤 실제 발송 기사만 번역한다.
    # 수백 건의 후보를 미리 번역하면 API 호출이 느려지고, 번역된 표현 때문에
    # 같은 사건 판정이 흔들릴 수 있다. translate_titles 는 기사 객체를 제자리에서
    # 갱신하므로 selected_by_category 에도 번역 결과가 그대로 반영된다.
    print(
        f"🈯 최종 선정 후 번역: GEMINI_API_KEY="
        f"{'있음' if os.environ.get('GEMINI_API_KEY') else '없음'}, "
        f"대상 {len(sent_articles)}건"
    )
    translate_titles(sent_articles)

    message_text = render_digest(selected_by_category)
    print(f"ℹ️ Slack 메시지 {len(message_text):,}자, 선택 기사 {len(sent_articles)}건 전부 발송")

    if is_dry_run():
        print("\n===== DRY RUN — 실제 발송하지 않음 =====")
        print(message_text)
        print("===== DRY RUN 끝 =====\n")
        return True, sent_articles

    category_lines = {
        category: [
            {
                "article": article,
                "region": _article_region(article),
            }
            for article in selected_by_category.get(category, [])
        ]
        for category in CATEGORY_ORDER
    }
    slack_blocks = _build_slack_blocks(category_lines)
    notification_text = (
        SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d"))
        if SLACK_HEADER
        else f"VC 데일리 브리핑 · {datetime.now().strftime('%y.%m.%d')} · {len(sent_articles)}건"
    )

    # ✅ 링크 미리보기(unfurl) 끄기: 카드/썸네일이 딸려 나오지 않게 함
    resp = requests.post(
        slack_webhook_url,
        json={
            # text는 알림용이고, 전체 뉴스는 blocks 한 메시지에 자르지 않고 담는다.
            "text": notification_text,
            "blocks": slack_blocks,
            "unfurl_links": False,
            "unfurl_media": False,
        },
    )
    if resp.status_code == 200:
        # 주간 브리핑에는 실제 Slack 발송에 성공한 기사만 포함한다.
        try:
            _append_slack_archive(selected_by_category, message_text)
        except Exception as e:
            # 아카이브 실패는 이미 성공한 Slack 발송을 실패로 바꾸지 않는다.
            print(f"⚠️ 슬랙 아카이브 저장 실패: {e}")
        print(f"슬랙 메시지 1건 통합 전송 성공! (Block Kit {len(slack_blocks)}개)")
        return True, sent_articles
    print(f"슬랙 전송 실패: {resp.status_code}, {resp.text}")
    return False, sent_articles


def main():
    seen_links = {normalize_url(link) for link in load_lines(SEEN_FILE)}
    seen_titles = load_lines(SEEN_TITLES_FILE)
    all_errors, all_articles = [], []

    hn_articles, hn_errors = hackernews.fetch(HN_KEYWORDS)
    all_errors.extend(hn_errors)
    all_articles.extend(hn_articles)

    if HAS_NEWSLETTERS:
        try:
            print("📬 뉴스레터 수집 시도 중...")
            nl_articles, nl_errors = newsletters.fetch()
            all_errors.extend(nl_errors)
            all_articles.extend(nl_articles)
            print(f"📬 뉴스레터 {len(nl_articles)}건 수집 완료")
        except Exception as e:
            print(f"⚠️ 뉴스레터 수집 중 에러 발생 (스킵합니다): {e}")
            all_errors.append(f"뉴스레터 수집 실패: {str(e)}")
    else:
        print("⏩ 뉴스레터 수집 기능이 비활성화되어 넘어갑니다.")

    rss_articles, rss_errors = rss_feeds.fetch()
    all_errors.extend(rss_errors)
    all_articles.extend(rss_articles)

    all_articles = filter_to_as_of_date(all_articles)

    # --- Prepare embedding model + store for cross-day semantic dedupe ---
    try:
        from .processor.deduplicator import _get_model as _get_emb_model
        emb_model = _get_emb_model()
        from .utils.embedding_store import load_store
        EMBEDDING_AVAILABLE = True
    except Exception:
        emb_model = None
        load_store = lambda: ([], [])
        EMBEDDING_AVAILABLE = False

    filtered = []
    rejected = []          # 탈락 사유와 함께 판정 로그에 남긴다
    # 게이트가 뒤에서 판단하면 앞단은 값싼 차단만 하고 주제 판정은 넘긴다.
    gate_will_judge = editor_gate_enabled() and editor.is_enabled()
    if gate_will_judge:
        print("🧑‍⚖️ 주제 적합성 판정을 편집 게이트로 넘깁니다(키워드 사전 차단 최소화).")
    for art in all_articles:
        link = get_primary_link(art)
        normalized_link = normalize_url(link)
        title = art.get("title", "")
        if not link or not title:
            continue
        gnews_raw = normalize_url(art.get("gnews_link") or "")
        if normalized_link in seen_links or (gnews_raw and gnews_raw in seen_links):
            continue
        # 날짜 넘는 중복(어제까지 발송)
        if any(is_same_news_issue(title, old) for old in seen_titles[-800:]):
            continue
        if not is_relevant(art, require_topic_match=not gate_will_judge):
            rejected.append(art)      # is_relevant 가 filter_reason 을 붙여 둔다
            continue

        filtered.append(art)

    # 오늘 수집한 중복은 모두 전달해야 검증 출처와 원문을 대표 기사로 고를 수 있다.
    merged, dedup_errors = deduplicate_and_merge(filtered)
    all_errors.extend(dedup_errors)

    classified = []
    for art in merged:
        art, e = summarize(art)
        all_errors.extend(e)
        classified.append(art)

    classified, gate_rejected, gate_errors = select_for_briefing(classified)
    rejected.extend(gate_rejected)
    all_errors.extend(gate_errors)

    # 수백 개 수집 기사마다 모델을 부르지 않고, 최종 후보만 한 번에 과거 발송분과 비교한다.
    if EMBEDDING_AVAILABLE:
        classified = _batch_filter_semantic_duplicates(
            classified,
            emb_model,
            load_store,
            float(SIMILARITY_THRESHOLD),
        )

    sent_articles = []
    if classified:
        success, sent_articles = send_aggregated_slack_news(classified)
        if success and not is_dry_run():
            # ✅ 실제 발송된 기사만 seen 처리(미발송 기사가 유실되지 않게)
            for art in sent_articles:
                links = art.get("link", [])
                article_links = links if isinstance(links, list) else [links]
                seen_links.update(normalize_url(link) for link in article_links)
                # ✅ (P0-3) 디코딩 전 구글뉴스 원링크도 함께 저장
                #    → 디코더 성공/실패가 날마다 달라도 중복 재발송 방지
                gr = art.get("gnews_link")
                if gr:
                    seen_links.add(normalize_url(gr))
                seen_titles.append(art.get("title_orig") or art.get("title", ""))

            # persist embeddings only for actually sent articles
            try:
                from .utils.embedding_store import add_embeddings, meta_for_article
                new_embs = []
                new_meta = []
                for art in sent_articles:
                    emb = art.get("_embedding")
                    if emb is not None:
                        new_embs.append(emb)
                        new_meta.append(meta_for_article(art))
                if new_embs:
                    import numpy as np
                    add_embeddings(np.array(new_embs), new_meta)
            except Exception as _e:
                print(f"⚠️ 임베딩 저장 실패: {_e}")

            # ✅ Send to Telegram (optional, if configured)
            try:
                from .utils import telegram_sender
                if telegram_sender.is_configured():
                    message_text = render_digest(bucket_by_category(sent_articles))
                    tg_success, tg_msg = telegram_sender.send_aggregated_news(message_text)
                    if tg_success:
                        print(f"✅ 텔레그램 전송 성공: {tg_msg}")
                    else:
                        print(f"⚠️ 텔레그램 전송 실패: {tg_msg}")
            except Exception as _e:
                print(f"⚠️ 텔레그램 전송 중 에러(계속 진행): {_e}")
    else:
        print("전송할 새로운 기사가 없습니다.")

    # 한 건도 못 골랐을 때야말로 탈락 사유가 필요하므로 항상 남긴다.
    save_run_decisions(rejected, classified, sent_articles)

    if is_dry_run():
        print("ℹ️ DRY RUN — seen 상태를 저장하지 않았습니다(다음 실행에 영향 없음).")
    else:
        save_lines(SEEN_FILE, seen_links)
        save_lines(SEEN_TITLES_FILE, seen_titles, cap=2000)
    if all_errors:
        print(f"\n⚠️ 수집 오류 {len(all_errors)}건:")
        for e in all_errors:
            print(f"  • {e}")


if __name__ == "__main__":
    main()
