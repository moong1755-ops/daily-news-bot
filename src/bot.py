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
    CATEGORY_DISPLAY_NAMES,
    MAX_PER_CATEGORY_DICT,
    MAX_PER_CATEGORY,
    IMPACT_MUST_READ_MAX,
    ALTERNATIVE_MAJOR_DEAL_MAX,
    OVERSEAS_PREFERRED_DOMAINS,
    REGION_WEIGHT,
    INSIGHTS_DOMESTIC_SCORE_TOLERANCE,
    IMPACT_THEME_DIVERSITY_SCORE_TOLERANCE,
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
from .editorial_review import (
    EDITORIAL_REVIEW_SHEET_URL,
    importance as _editorial_importance,
    priority_key as _editorial_priority_key,
    select_alt_with_soft_diversity,
    write_review_csv,
)
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


def _category_display_name(category: str) -> str:
    """Return the reader-facing label without changing the internal category key."""
    return CATEGORY_DISPLAY_NAMES.get(category, category)


REGION_DISPLAY_ORDER = (("global", "해외"), ("korea", "국내"))
# 총 기사 상한과 출처 다양성 상한은 서로 다른 정책이다. 임팩트 총 3개 중
# 한 매체가 최대 2개까지는 차지할 수 있게 해, 다른 출처를 포함하면서도
# 가장 강한 기사 순서가 불필요하게 뒤집히지 않도록 한다.
IMPACT_SOURCE_SOFT_CAP = min(
    2,
    max(1, MAX_PER_CATEGORY_DICT.get(IMPACT_CATEGORY, MAX_PER_CATEGORY)),
)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer"}
SLACK_ARCHIVE_PATH = Path(__file__).parent.parent / "data" / "slack_archive.jsonl"
DAILY_REVIEW_PATH = Path(__file__).parent.parent / "data" / "daily_review.csv"
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
        "efn.co.kr": "기후에너지경제신문",
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


_IPO_CLUSTER_PATTERN = re.compile(
    r"\b(?:ipo|initial public offering|go public|public listing|nasdaq|nyse)\b|"
    r"기업공개|상장(?:지|시장|계획|추진|준비|투자)",
    re.IGNORECASE,
)
_IPO_EVENT_KEY_STOPWORDS = {
    "ipo", "initial", "public", "offering", "listing", "listed", "lists",
    "nasdaq", "nyse", "investment", "invest", "invests", "investing",
    "funding", "financing", "round", "series", "talks", "reported",
    "selection", "selects", "selected", "potential", "ahead", "mega",
    "stake", "deal", "files", "filed", "filing", "plans", "planned", "2026",
}


def _ipo_cluster_entities(article: dict) -> set[str]:
    """Return the IPO target company, not investors or underwriters."""
    event_key = str(article.get("editor_event_key") or "").casefold()
    title_text = " ".join(
        str(article.get(field) or "")
        for field in ("title", "title_orig")
    )
    if not event_key or not _IPO_CLUSTER_PATTERN.search(f"{event_key} {title_text}"):
        return set()
    tokens = re.findall(r"[a-z0-9]+", event_key)

    # 편집 사건키는 핵심 회사명 + ipo + 세부사건 순서를 쓰도록 지시한다.
    # ipo 바로 앞에서 거꾸로 찾으면 `nvidia_anthropic_ipo_investment`에서는
    # 투자자 Nvidia가 아니라 상장 대상 Anthropic을 안정적으로 고를 수 있다.
    marker_indexes = [
        index for index, token in enumerate(tokens)
        if token in {"ipo", "offering", "listing"}
    ]
    for marker_index in marker_indexes:
        for token in reversed(tokens[:marker_index]):
            if (
                len(token) >= 4
                and token not in _IPO_EVENT_KEY_STOPWORDS
                and not token.isdigit()
            ):
                return {token}
    return set()


def _collapse_daily_ipo_clusters(ranked: list[dict]) -> list[dict]:
    """Keep one representative when several stories cover the same company IPO."""
    kept: list[dict] = []
    kept_entities: list[set[str]] = []
    for article in ranked:
        entities = _ipo_cluster_entities(article)
        if entities and any(entities & previous for previous in kept_entities):
            article["selection_dedup_reason"] = "same_company_ipo_cluster"
            continue
        kept.append(article)
        kept_entities.append(entities)
    return kept


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
        score += REGION_WEIGHT.get("global", 0.0)
    return score + float(article.get("selection_score_adjustment", 0.0))


_CROSS_DAY_EVENT_LOOKBACK_DAYS = 7
_CROSS_DAY_EVENT_FAMILY_PATTERNS = (
    (
        "funding",
        re.compile(
            r"\b(?:funding|financing|raises?|raised|series\s+[a-e]|valuation)\b|"
            r"투자\s*유치|자금\s*조달|펀딩|시리즈\s*[a-e]",
            re.IGNORECASE,
        ),
    ),
    (
        "acquisition",
        re.compile(
            r"\b(?:acqui(?:re|res|red|ring|sition)|merger|buyout|takeover)\b|"
            r"인수|합병|매각",
            re.IGNORECASE,
        ),
    ),
    (
        "ipo",
        re.compile(r"\b(?:ipo|listing|go(?:ing)? public)\b|기업공개|상장", re.IGNORECASE),
    ),
    (
        "policy",
        re.compile(
            r"\b(?:policy|regulat(?:ion|ory)|amendment|parliament|commission)\b|"
            r"sfdr|dnsh|ets|규제|정책|법안|개편|시행령|채택",
            re.IGNORECASE,
        ),
    ),
)
_CROSS_DAY_US_10Y_YIELD = re.compile(
    r"\b(?:u\.?s\.?\s*)?(?:10[- ]?year\s+)?treasur(?:y|ies)\s+yield(?:s)?\b|"
    r"\bus[_\s-]*10y[_\s-]*yield\b|"
    r"(?:미국?\s*)?10년물\s*(?:국채)?\s*(?:금리|수익률)|"
    r"미\s*국채\s*(?:금리|수익률)",
    re.IGNORECASE,
)
_CROSS_DAY_US_CPI = re.compile(
    r"\b(?:u\.?s\.?|united states|american)\b.{0,50}"
    r"\b(?:cpi|consumer price index|inflation)\b|"
    r"\b(?:cpi|consumer price index)\b.{0,50}\b(?:u\.?s\.?|united states)\b|"
    r"미국?.{0,30}(?:소비자물가|소비자물가지수|CPI|인플레이션)",
    re.IGNORECASE,
)
_CROSS_DAY_RATE_TOPIC = re.compile(
    r"\b(?:interest|policy|benchmark) rates?\b|\brate (?:hike|cut|path|outlook)\b|"
    r"기준금리|정책금리|금리(?:인상|인하|전망)?|통화정책",
    re.IGNORECASE,
)
_CROSS_DAY_RATE_ACTORS = (
    (
        "fed",
        re.compile(
            r"\bfederal reserve\b|\bthe fed\b|\bfed\b|\bfomc\b|미\s*연준|연방준비제도",
            re.IGNORECASE,
        ),
    ),
    ("bok", re.compile(r"\bbank of korea\b|한국은행|한은|금통위", re.IGNORECASE)),
    ("ecb", re.compile(r"\beuropean central bank\b|\becb\b|유럽중앙은행", re.IGNORECASE)),
    ("boj", re.compile(r"\bbank of japan\b|\bboj\b|일본은행", re.IGNORECASE)),
    ("pboc", re.compile(r"\bpeople'?s bank of china\b|\bpboc\b|중국인민은행", re.IGNORECASE)),
)
_CROSS_DAY_EVENT_GENERIC_TOKENS = {
    "acquisition", "acquire", "acquires", "acquired", "agreement", "deal",
    "funding", "financing", "investment", "ipo", "listing", "merge", "merger",
    "policy", "regulation", "regulatory", "report", "reported", "round", "series",
    "talks", "update", "valuation", "ai", "global", "market", "company",
    "approval", "approved", "announcement", "africa", "europe", "asia",
    "인수", "합병", "매각", "투자", "유치", "조달", "펀딩", "상장", "규제", "정책",
}
_CROSS_DAY_POLICY_ANCHORS = {"sfdr", "dnsh", "cbam"}
_CROSS_DAY_FUNDING_STAGE = re.compile(
    r"\b(pre[- ]seed|seed|series\s+[a-e]|growth|bridge|debt|credit)\b|"
    r"프리\s*시드|시드|시리즈\s*[a-e]|그로스|브릿지|사모대출",
    re.IGNORECASE,
)
_CROSS_DAY_TENTATIVE_STATUS = re.compile(
    r"\b(?:talks?|reportedly|set to|plans?|seeks?|considering|mulls?|could|may)\b|"
    r"논의|협상|검토|추진|계획|예정|전망|가능성|방침",
    re.IGNORECASE,
)
_CROSS_DAY_CONFIRMED_STATUS = re.compile(
    r"\b(?:acquired|acquires|raised|raises|closed|completed|approved|confirmed|signed)\b|"
    r"확정|완료|체결|승인|채택|돌파|인수했|합병했|투자\s*유치",
    re.IGNORECASE,
)
_CROSS_DAY_ADVERSE_STATUS = re.compile(
    r"\b(?:cancelled|canceled|withdrawn|scrapped|collapsed|failed)\b|"
    r"무산|철회|결렬|취소|중단",
    re.IGNORECASE,
)


def _cross_day_event_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in ("editor_event_key", "title_orig", "title")
    ).replace("_", " ")


def _cross_day_event_family(article: dict) -> str:
    text = _cross_day_event_text(article)
    if _CROSS_DAY_US_10Y_YIELD.search(text):
        return "macro_us_10y_yield"
    if _CROSS_DAY_US_CPI.search(text):
        return "macro_us_cpi"
    if _CROSS_DAY_RATE_TOPIC.search(text):
        for actor, pattern in _CROSS_DAY_RATE_ACTORS:
            if pattern.search(text):
                return f"macro_{actor}_rates"
    for family, pattern in _CROSS_DAY_EVENT_FAMILY_PATTERNS:
        if pattern.search(text):
            return family
    return ""


def _cross_day_event_entities(article: dict) -> set[str]:
    raw_key = str(article.get("editor_event_key") or "").casefold()
    ordered = [
        token
        for token in re.findall(r"[a-z0-9가-힣]+", raw_key)
        if len(token) >= 2
        and not any(character.isdigit() for character in token)
        and token not in _CROSS_DAY_EVENT_GENERIC_TOKENS
    ]
    entities = set(ordered)
    # Preserve common target-company acronyms such as FCH / Flow Control
    # Holdings without treating one shared acquirer as proof of duplication.
    for window_size in range(3, min(4, len(ordered)) + 1):
        for start in range(len(ordered) - window_size + 1):
            acronym = "".join(token[0] for token in ordered[start:start + window_size])
            if len(acronym) >= 3:
                entities.add(acronym)
    return entities


def _cross_day_status_rank(article: dict) -> int:
    text = _cross_day_event_text(article)
    if _CROSS_DAY_ADVERSE_STATUS.search(text):
        return 3
    if _CROSS_DAY_CONFIRMED_STATUS.search(text):
        return 2
    if _CROSS_DAY_TENTATIVE_STATUS.search(text):
        return 1
    return 0


def _cross_day_funding_stage(article: dict) -> str:
    match = _CROSS_DAY_FUNDING_STAGE.search(_cross_day_event_text(article))
    if not match:
        return ""
    return re.sub(r"[\s-]+", "_", match.group(0).casefold())


def _same_cross_day_event(current: dict, previous: dict) -> bool:
    current_family = _cross_day_event_family(current)
    previous_family = _cross_day_event_family(previous)
    if not current_family or current_family != previous_family:
        return False
    if current_family.startswith("macro_"):
        return True
    if current_family == "funding":
        current_stage = _cross_day_funding_stage(current)
        previous_stage = _cross_day_funding_stage(previous)
        if current_stage and previous_stage and current_stage != previous_stage:
            return False

    current_entities = _cross_day_event_entities(current)
    previous_entities = _cross_day_event_entities(previous)
    if not current_entities or not previous_entities:
        return False
    shared = current_entities & previous_entities
    if current_family == "policy" and shared & _CROSS_DAY_POLICY_ANCHORS:
        return True
    if not shared:
        return False
    smaller = min(len(current_entities), len(previous_entities))
    return (
        current_entities == previous_entities
        or (smaller == 1 and len(shared) == 1)
        or (len(shared) >= 2 and len(shared) / smaller >= 0.5)
    )


def _load_recent_sent_articles(
    reference_date,
    *,
    archive_path: str | Path | None = None,
    lookback_days: int = _CROSS_DAY_EVENT_LOOKBACK_DAYS,
) -> list[dict]:
    path = Path(archive_path) if archive_path is not None else SLACK_ARCHIVE_PATH
    if not path.exists():
        return []
    cutoff = reference_date - timedelta(days=lookback_days)
    recent = []
    try:
        with path.open("r", encoding="utf-8") as archive_file:
            for raw_line in archive_file:
                try:
                    record = json.loads(raw_line)
                    edition_date = datetime.fromisoformat(
                        str(record.get("edition_date") or "")
                    ).date()
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not cutoff <= edition_date <= reference_date:
                    continue
                for article in record.get("articles") or []:
                    if isinstance(article, dict) and article.get("editor_event_key"):
                        recent.append(article)
    except OSError as exc:
        print(f"⚠️ 최근 발송 사건키 읽기 실패({exc}) — 기존 선정을 유지합니다.")
        return []
    return recent


def _filter_recent_editor_event_duplicates(
    articles: list[dict],
    *,
    reference_date=None,
    archive_path: str | Path | None = None,
) -> tuple[list[dict], list[dict]]:
    target_date = reference_date or as_of_date() or datetime.now(KOREA_TIMEZONE).date()
    recent = _load_recent_sent_articles(target_date, archive_path=archive_path)
    if not recent:
        return articles, []

    kept, dropped = [], []
    for article in articles:
        matches = [sent for sent in recent if _same_cross_day_event(article, sent)]
        if not matches:
            kept.append(article)
            continue
        previous = max(matches, key=_cross_day_status_rank)
        # A clearly confirmed/cancelled outcome may be sent after an earlier
        # rumor or discussion. Repeated coverage at the same status is noise.
        current_status = _cross_day_status_rank(article)
        previous_status = _cross_day_status_rank(previous)
        if current_status >= 2 and current_status > previous_status:
            kept.append(article)
            continue
        article["filter_reason"] = "recent_editor_event_duplicate"
        article["duplicate_of_title"] = previous.get("title") or previous.get("title_orig")
        dropped.append(article)

    if dropped:
        print(f"   ↪ 최근 {_CROSS_DAY_EVENT_LOOKBACK_DAYS}일 내 이미 발송한 사건 {len(dropped)}건 제외")
    return kept, dropped


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
_MACRO_US_TREASURY_YIELD = re.compile(
    r"\b(?:u\.?s\.?\s*)?(?:(?:10|ten)[- ]?year\s+)?treasur(?:y|ies)\s+yield(?:s)?\b|"
    r"\btreasury\s+(?:bond\s+)?yields?\b|"
    r"(?:미국?\s*)?(?:10년물\s*)?(?:미\s*)?국채(?:\s*(?:금리|수익률))",
    re.IGNORECASE,
)


def _selection_priority(article: dict, category: str) -> tuple[int, float]:
    return _editorial_priority_key(article, _selection_score(article, category))


_NON_CLIMATE_IMPACT_THEMES = {
    "circular_nature_food",
    "care_health",
    "education_access",
}
_NON_CLIMATE_SOCIAL_IMPACT = re.compile(
    r"\b(?:social economy|social enterprise|social venture|financial inclusion|"
    r"inclusive finance|affordable housing|workforce development|quality jobs)\b|"
    r"사회적경제|사회적기업|소셜벤처|금융포용|포용금융|주거복지|직업역량|좋은 일자리",
    re.IGNORECASE,
)


def _has_non_climate_impact_theme(article: dict) -> bool:
    raw_themes = article.get("impact_themes") or []
    if isinstance(raw_themes, str):
        raw_themes = [raw_themes]
    themes = {str(theme) for theme in raw_themes}
    if themes & _NON_CLIMATE_IMPACT_THEMES:
        return True
    text = " ".join(
        str(article.get(field) or "")
        for field in ("title_orig", "title", "description")
    )
    return bool(_NON_CLIMATE_SOCIAL_IMPACT.search(text))


def _ensure_impact_theme_diversity(
    selected: list[dict],
    ranked: list[dict],
    limit: int,
) -> list[dict]:
    """Use one non-climate impact story when it is comparable to the weakest pick."""
    if (
        len(selected) < limit
        or any(_has_non_climate_impact_theme(article) for article in selected)
    ):
        return selected

    selected_ids = {id(article) for article in selected}
    alternatives = [
        article
        for article in ranked
        if id(article) not in selected_ids and _has_non_climate_impact_theme(article)
    ]
    if not alternatives:
        return selected

    best_alternative = max(
        alternatives,
        key=lambda article: _selection_priority(article, IMPACT_CATEGORY),
    )
    weakest_selected = min(
        selected,
        key=lambda article: _selection_priority(article, IMPACT_CATEGORY),
    )
    alternative_importance, alternative_score = _selection_priority(
        best_alternative,
        IMPACT_CATEGORY,
    )
    weakest_importance, weakest_score = _selection_priority(
        weakest_selected,
        IMPACT_CATEGORY,
    )
    comparable = (
        alternative_importance > weakest_importance
        or (
            alternative_importance == weakest_importance
            and alternative_score + IMPACT_THEME_DIVERSITY_SCORE_TOLERANCE
            >= weakest_score
        )
    )
    if not comparable:
        return selected

    remaining_source_counts = {}
    for article in selected:
        if article is weakest_selected:
            continue
        source = _article_source_key(article)
        if source:
            remaining_source_counts[source] = remaining_source_counts.get(source, 0) + 1
    alternative_source = _article_source_key(best_alternative)
    if (
        alternative_source
        and remaining_source_counts.get(alternative_source, 0) >= IMPACT_SOURCE_SOFT_CAP
    ):
        return selected

    replacement_ids = {
        id(article)
        for article in selected
        if article is not weakest_selected
    }
    replacement_ids.add(id(best_alternative))
    return [article for article in ranked if id(article) in replacement_ids][:limit]



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
    event_date = str(article.get("date") or "").strip()
    if _MACRO_US_TREASURY_YIELD.search(text):
        return f"us_treasury:yield:{event_date}"
    if not _MACRO_RATE_TOPIC.search(text):
        return ""
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
    """Use one representative per rate or benchmark-yield event in daily macro."""
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
                _editorial_importance(article),
                _selection_score(article, MACRO_CATEGORY),
            ),
        )
        representative["_macro_event_score"] = max(
            _selection_score(article, MACRO_CATEGORY) for article in group
        )
        representative["_macro_event_importance"] = max(
            _editorial_importance(article) for article in group
        )
        representatives.append(representative)

    combined = passthrough + representatives
    return sorted(
        combined,
        key=lambda article: (
            int(article.get("_macro_event_importance") or _editorial_importance(article)),
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


_VC_PE_NON_CAPITAL_TITLE = re.compile(
    r"\b(?:data breach|cyberattack|ransomware|lawsuit|litigation|settlement|"
    r"earnings|revenue|supply contract|shipping contract)\b|"
    r"개인정보\s*유출|정보\s*유출|해킹|랜섬웨어|소송|합의금|벌금|과징금|"
    r"매출|실적|공급계약|운송계약|시설투자|설비투자|증설|팟캐스트|인터뷰|경고",
    re.IGNORECASE,
)
_VC_PE_CAPITAL_EVENT = re.compile(
    r"\b(?:raises?|raised|funding round|financing round|series\s+[a-e]|"
    r"seed round|growth round|fund close|closes? (?:its )?(?:first |new )?fund|"
    r"acqui(?:re|res|red|ring|sition)|merger|m&a|buyout|take[- ]private|"
    r"stake sale|secondary sale|continuation fund|private credit|"
    r"credit facility|project finance|investment mandate|fund mandate|"
    r"capital commitment|initial public offering|ipo|listing|exit)\b|"
    r"투자\s*유치|자금\s*조달|시리즈\s*[a-e]|펀드.{0,30}(?:결성|조성|클로징|"
    r"운용사\s*선정|낙점)|출자|인수|합병|바이아웃|지분.{0,20}(?:투자|매각)|"
    r"매각\s*추진|위탁\s*운용|운용사\s*선정|구주\s*거래|"
    r"\d[\d,.]*\s*(?:억|조)(?:원)?(?:\s*규모)?.{0,20}(?:유치|결성)|"
    r"세컨더리|사모대출|프로젝트\s*파이낸싱|기업공개|상장|엑시트|투자\s*회수",
    re.IGNORECASE,
)
_VC_PE_MARKET_CONTEXT = re.compile(
    r"\b(?:venture capital|private equity|private markets?|startup funding|"
    r"fundraising market|deal market|deal activity|ipo market|exit market|"
    r"secondaries market|dry powder|limited partners?|general partners?)\b|"
    r"벤처캐피탈|사모펀드|사모시장|벤처투자|스타트업\s*투자|펀드레이징|"
    r"딜\s*(?:시장|동향|환경)|회수시장|상장시장|드라이파우더|출자시장|"
    r"\b(?:lp|gp)\b",
    re.IGNORECASE,
)
_VC_PE_MARKET_CHANGE = re.compile(
    r"\b(?:outlook|trend|forecast|survey|report|record|surge|growth|decline|"
    r"slowdown|rebound|recovery|volume|activity|allocation|regulation|rule|policy)\b|"
    r"전망|동향|추세|조사|보고서|증가|감소|급증|둔화|회복|규모|건수|"
    r"자금흐름|자금\s*흐름|배분|규제|정책|제도\s*변화",
    re.IGNORECASE,
)


def _is_vc_pe_eligible(article: dict) -> bool:
    """Require a capital event or a decision-useful private-market change."""
    if article.get("category") != ALTERNATIVE_CATEGORY:
        return True

    title = " ".join(
        str(article.get(field) or "")
        for field in ("title_orig", "title")
    )
    text = " ".join(
        str(article.get(field) or "")
        for field in (
            "title_orig",
            "title",
            "description",
            "editor_reason",
            "editor_event_key",
        )
    )
    # A corrupted or over-broad description must never turn an operational
    # incident in the headline into a VC/PE capital event.
    if _VC_PE_NON_CAPITAL_TITLE.search(title):
        return False

    deal_signals = set(article.get("deal_signals") or [])
    if deal_signals & {"transaction", "financing"}:
        return True
    if _VC_PE_CAPITAL_EVENT.search(text):
        return True
    if _VC_PE_MARKET_CONTEXT.search(text) and _VC_PE_MARKET_CHANGE.search(text):
        return True
    return False


def _filter_final_category_qualification(
    articles: list[dict],
) -> tuple[list[dict], list[dict]]:
    kept, rejected = [], []
    for article in articles:
        if _is_vc_pe_eligible(article):
            kept.append(article)
            continue
        article["editorial_excluded"] = True
        article["filter_reason"] = "final_vc_pe_qualification"
        rejected.append(article)
    if rejected:
        print(f"🧹 VC·PE 최종 자격 검사로 비자본 사건 {len(rejected)}건 제외")
    return kept, rejected


def _is_sendable(article: dict) -> bool:
    if article.get("editorial_excluded", False):
        return False
    if article.get("editor_verdict") == "unreviewed":
        return False

    llm_score = article.get("llm_score")
    return llm_score is None or float(llm_score) >= LLM_SEND_MIN_SCORE


def _select_category_articles(ranked: list, category: str) -> list:
    """Apply category caps after importance-first ranking."""
    base_limit = MAX_PER_CATEGORY_DICT.get(category, MAX_PER_CATEGORY)

    # 거시는 같은 중앙은행 금리 이벤트의 본 결정/전망/코멘트가 서로
    # 슬롯을 잡아먹기 전에 대표기사 하나로 접는다. 대표는 본 결정이 우선한다.
    if category == MACRO_CATEGORY:
        ranked = _collapse_macro_rate_stories(ranked)

    # 같은 사건이 한 카테고리를 다 차지하지 않도록 발송 직전에 한 번 더 솎는다.
    ranked = filter_near_duplicates(ranked, SELECTION_SIMILARITY_THRESHOLD)
    if category == ALTERNATIVE_CATEGORY:
        ranked = _collapse_daily_ipo_clusters(ranked)

    if category in REGION_SPLIT_CATEGORIES:
        # 대체투자·거시는 해외와 국내를 각각 최대 3개까지 보존한다.
        # importance가 없는 기존 기사만 예전 major_deal overflow를 유지한다.
        # 신규 metadata가 있는 기사는 major_deal이 importance 판단을 우회하지 않는다.
        region_counts = {"global": 0, "korea": 0}
        selected = []
        overflow = []
        final_limit = base_limit * 2

        if category == ALTERNATIVE_CATEGORY:
            for region, _label in REGION_DISPLAY_ORDER:
                region_ranked = [article for article in ranked if _article_region(article) == region]
                region_selected = select_alt_with_soft_diversity(
                    region_ranked,
                    limit=base_limit,
                    score_fn=lambda article: _selection_score(article, category),
                )
                selected.extend(region_selected)
                region_counts[region] = len(region_selected)
                overflow.extend(
                    article
                    for article in region_ranked
                    if (
                        article not in region_selected
                        and article.get("major_deal", False)
                        and _editorial_importance(article) == 0
                    )
                )
        else:
            for article in ranked:
                region = _article_region(article)
                if region_counts[region] < base_limit:
                    selected.append(article)
                    region_counts[region] += 1

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
        return _ensure_impact_theme_diversity(selected, ranked, base_limit)

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
            if _article_region(article) == "korea"
            and article.get("category_reason") == "official_insights_source"
            and article not in selected
        ]
        if not domestic_candidates:
            return selected

        best_domestic = max(
            domestic_candidates,
            key=lambda article: _selection_priority(article, category),
        )
        weakest_selected = min(
            selected,
            key=lambda article: _selection_priority(article, category),
        )
        domestic_importance, domestic_score = _selection_priority(best_domestic, category)
        weakest_importance, weakest_score = _selection_priority(weakest_selected, category)
        if domestic_importance > weakest_importance or (
            domestic_importance == weakest_importance
            and domestic_score + INSIGHTS_DOMESTIC_SCORE_TOLERANCE >= weakest_score
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

    # 카테고리 이름만 맞는 운영·법률·보안 기사가 실제 투자 사건을 밀어내지
    # 않도록, LLM 판정 뒤에도 VC·PE 자격을 구조화 신호로 한 번 확인한다.
    classified, qualification_rejected = _filter_final_category_qualification(classified)
    rejected.extend(qualification_rejected)

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
        "importance": article.get("importance"),
        "importance_reason": article.get("importance_reason"),
        "alt_subtype": article.get("alt_subtype"),
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
                "text": _category_display_name(category),
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
        parts.append(f"*{_category_display_name(cat)}*")
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
                "importance": article.get("importance") or 0,
                "importance_reason": article.get("importance_reason") or "",
                "alt_subtype": article.get("alt_subtype") or "",
                "description": article.get("description") or article.get("summary") or "",
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
        for article in buckets[cat_name]:
            article["selection_score"] = _selection_score(article, cat_name)
        ranked = sorted(buckets[cat_name], key=lambda a: _selection_priority(a, cat_name), reverse=True)
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
    if EDITORIAL_REVIEW_SHEET_URL:
        slack_blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"<{EDITORIAL_REVIEW_SHEET_URL}|📎 선정·미선정 후보 보기>",
            }],
        })
    notification_text = (
        SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d"))
        if SLACK_HEADER
        else f"VC 데일리 브리핑 · {datetime.now().strftime('%y.%m.%d')} · {len(sent_articles)}건"
    )

    # ✅ 링크 미리보기(unfurl) 끄기: 카드/썸네일이 딸려 나오지 않게 함
    try:
        resp = requests.post(
            slack_webhook_url,
            json={
                # text는 알림용이고, 전체 뉴스는 blocks 한 메시지에 자르지 않고 담는다.
                "text": notification_text,
                "blocks": slack_blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"슬랙 전송 실패: {exc}")
        return False, []
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
    return False, []


def _save_daily_review(candidates: list[dict], selected: list[dict]) -> bool:
    """Persist the review audit without turning an audit failure into a send failure."""
    if not candidates:
        return True
    try:
        edition_date = (as_of_date() or datetime.now(KOREA_TIMEZONE).date()).isoformat()
        write_review_csv(
            DAILY_REVIEW_PATH,
            edition_date=edition_date,
            candidates=candidates,
            selected=selected,
            retention_days=60,
        )
    except Exception as exc:
        print(f"⚠️ Daily Review CSV 저장 실패: {exc}")
        return False
    return True


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

    # Gemini가 붙인 사건키를 최근 성공 발송 기록과 비교한다. 매체·금액·
    # 제목이 달라도 같은 자금조달·M&A·IPO·정책·미 10년물 사건은 한 번만
    # 보내되, 루머에서 확정·무산으로 바뀐 실질적 업데이트는 유지한다.
    classified, historical_event_duplicates = _filter_recent_editor_event_duplicates(
        classified,
    )
    rejected.extend(historical_event_duplicates)

    # 수백 개 수집 기사마다 모델을 부르지 않고, 최종 후보만 한 번에 과거 발송분과 비교한다.
    if EMBEDDING_AVAILABLE:
        classified = _batch_filter_semantic_duplicates(
            classified,
            emb_model,
            load_store,
            float(SIMILARITY_THRESHOLD),
        )

    sent_articles = []
    delivery_failed = False
    if classified:
        success, sent_articles = send_aggregated_slack_news(classified)
        delivery_failed = not success
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

    # 최종 편집 후보와 실제 발송 결과를 비교할 수 있게 최근 60일만 저장한다.
    _save_daily_review(classified, sent_articles)

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
    if delivery_failed:
        # GitHub Actions가 실제 미발송을 성공으로 표시하지 않게 한다.
        # 검토 CSV와 의사결정 로그를 먼저 저장한 뒤 비정상 종료한다.
        raise RuntimeError("Slack 발송에 실패했습니다. 위 로그를 확인해 주세요.")


if __name__ == "__main__":
    main()
