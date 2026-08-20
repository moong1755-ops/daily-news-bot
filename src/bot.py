import os
import re
import requests
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import json
from pathlib import Path

from .fetchers import hackernews, rss_feeds
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

from .processor.deduplicator import deduplicate_and_merge
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
    OVERSEAS_PREFERRED_DOMAINS,
    REGION_WEIGHT,
    HARD_EXCLUSION_KEYWORDS,
    SOFT_EDITORIAL_EXCLUSION_KEYWORDS,
    OPINION_FORMAT_KEYWORDS,
    OPINION_URL_PATTERNS,
    RESCUE_EVENT_SIGNALS,
    RSS_SOURCE_METADATA,
    FEED_CATEGORY_OVERRIDE,
)
try:
    from .config import LLM_SEND_MIN_SCORE
except ImportError:
    LLM_SEND_MIN_SCORE = 0
# ✅ 운영 노브(P1-7): config 에서 조정 가능, 없으면 기본값
try:
    from .config import SLACK_MAX_LENGTH
except ImportError:
    SLACK_MAX_LENGTH = 3900
try:
    from .config import SLACK_HEADER          # 예: "📰 ISQ Daily News | {date}" / "" 이면 헤더 없음
except ImportError:
    SLACK_HEADER = ""
try:
    from .config import MIN_CATEGORY_NEWS
except ImportError:
    MIN_CATEGORY_NEWS = 3

from .utils.file_handler import load_lines, save_lines, SEEN_FILE, SEEN_TITLES_FILE
CATEGORY_ORDER = list(CATEGORIES.keys())
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer"}


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


def is_relevant(article: dict) -> bool:
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
            article["filter_reason"] = f"soft_exclusion:{soft_exclusion_hit}"
            return False
        article["rescue_signal"] = f"{soft_exclusion_hit}:{event_hit}"

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
    return article.get("region", "global")


def _selection_score(article: dict, category: str) -> float:
    llm = article.get("llm_score")
    if llm is not None:
        return float(llm)
    score = float(article.get("relevance", 0))
    if category in OVERSEAS_PREFERRED_DOMAINS and _article_region(article) == "global":
        score *= REGION_WEIGHT.get("global", 1.0)
    return score


def _is_sendable(article: dict) -> bool:
    if article.get("editorial_excluded", False):
        return False

    llm_score = article.get("llm_score")
    return llm_score is None or float(llm_score) >= LLM_SEND_MIN_SCORE


def send_aggregated_slack_news(articles) -> bool:
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook_url:
        print("SLACK_WEBHOOK_URL 이 설정되지 않았습니다.")
        return False, []

    buckets = {cat: [] for cat in CATEGORY_ORDER}
    for a in articles:
        if not _is_sendable(a):
            continue
        cat = a.get("category", CATEGORY_ORDER[-1])
        if cat not in buckets:
            cat = CATEGORY_ORDER[-1]
        buckets[cat].append(a)

    sent_articles = []          # ✅ 실제 슬랙에 나간 기사만 수집(seen 처리용)
    category_lines = {}         # 카테고리별 라인(길이 가드용)

    for cat_name in CATEGORY_ORDER:
        max_limit = MAX_PER_CATEGORY_DICT.get(cat_name, MAX_PER_CATEGORY)
        ranked = sorted(buckets[cat_name], key=lambda a: _selection_score(a, cat_name), reverse=True)
        selected, nvidia = [], 0

        for a in ranked:
            tl = a.get("title", "").lower()
            if "nvidia" in tl or "엔비디아" in tl:
                if nvidia >= 2:
                    continue
                nvidia += 1
            selected.append(a)
            if len(selected) >= max_limit:
                break

        if len(selected) < MIN_CATEGORY_NEWS and cat_name != "👔 MBB·Big4 인사이트":
            for a in ranked:
                if a in selected:
                    continue
                tl = a.get("title", "").lower()
                if ("nvidia" in tl or "엔비디아" in tl) and nvidia >= 2:
                    continue
                selected.append(a)
                if len(selected) >= MIN_CATEGORY_NEWS:
                    break

        # ✅ 카테고리 헤더는 항상 표시. 비면 안내 문구.
        #    (P0-1) 문자열 매칭 대신 기사 객체를 함께 저장 → 트림 시 정확히 제거
        lines = []
        if selected:
            sent_articles.extend(selected)   # ✅ 발송분만 기록
            for a in selected:
                title = a.get("title", "제목 없음").strip()
                url = get_primary_link(a) or "#"
                raw_source = get_primary_source(a) or "출처미상"
                source = clean_source_name(raw_source)
                date = fmt_date(a.get("date", ""))
                lines.append({"text": f"• <{url}|{title}> ({source}, {date})", "article": a})
        else:
            lines.append({"text": "• 오늘 조건에 맞는 뉴스가 없습니다.", "article": None})
        category_lines[cat_name] = lines

    # ✅ 슬랙은 약 4,000자 초과 시 메시지를 분할함 → SLACK_MAX_LENGTH 안으로 가드.
    #    초과 시 기사 많은 카테고리 끝에서부터 한 건씩 덜어냄(안내 문구는 유지).
    def _build():
        parts = []
        if SLACK_HEADER:
            parts.append(SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d")))
            parts.append("")
        for cat in CATEGORY_ORDER:
            parts.append(f"*{cat}*")
            parts.extend(item["text"] for item in category_lines.get(cat, []))
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    message_text = _build()
    trimmed = 0
    while len(message_text) > SLACK_MAX_LENGTH:
        biggest = max(
            (c for c in CATEGORY_ORDER
             if len(category_lines.get(c, [])) > 1),
            key=lambda c: len(category_lines[c]),
            default=None,
        )
        if biggest is None:
            break
        dropped = category_lines[biggest].pop()
        # ✅ (P0-1) 객체 동일성으로 seen 제외 → 미발송 기사가 내일 다시 후보가 됨
        if dropped.get("article") is not None and dropped["article"] in sent_articles:
            sent_articles.remove(dropped["article"])
        trimmed += 1
        message_text = _build()
    if trimmed:
        print(f"ℹ️ 길이 제한으로 {trimmed}건 생략(내일 재후보)")

    # ✅ 메시지 아카이브: 발송 전 로컬 파일에 기록(append, JSONL)
    try:
        archive_path = Path(__file__).parent.parent.parent / "data" / "slack_archive.jsonl"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with open(archive_path, "a", encoding="utf-8") as af:
            af.write(json.dumps({"ts": datetime.utcnow().isoformat(), "text": message_text}, ensure_ascii=False) + "\n")
    except Exception as e:
        # 아카이브 실패는 발송 실패로 간주하지 않음
        print(f"⚠️ 슬랙 아카이브 저장 실패: {e}")

    # ✅ 링크 미리보기(unfurl) 끄기: 카드/썸네일이 딸려 나오지 않게 함
    resp = requests.post(
        slack_webhook_url,
        json={
            "text": message_text,
            "unfurl_links": False,
            "unfurl_media": False,
        },
    )
    if resp.status_code == 200:
        print("슬랙 메시지 통합 전송 성공!")
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

    # --- Prepare embedding model + store for cross-day semantic dedupe ---
    try:
        from .processor.deduplicator import _get_model as _get_emb_model
        emb_model = _get_emb_model()
        from .utils.embedding_store import find_similar, add_embeddings, meta_for_article
        EMBEDDING_AVAILABLE = True
    except Exception:
        emb_model = None
        find_similar = lambda *a, **k: False
        add_embeddings = lambda *a, **k: None
        meta_for_article = lambda a: {"ts": None}
        EMBEDDING_AVAILABLE = False

    filtered = []
    accepted_titles = []      # ✅ 런 내부 중복(경북펀드 등 다매체) 차단용
    # temporary container to keep embeddings for candidates to persist after send
    candidate_embeddings = []
    candidate_metas = []

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
        # ✅ 오늘 실행분 내 같은 이슈 중복 차단
        if any(is_same_news_issue(title, t) for t in accepted_titles):
            continue
        if not is_relevant(art):
            continue

        # Cross-day semantic dedupe using embedding store
        text_for_embed = (title + " \n " + art.get("description", "")).strip()
        is_duplicate_via_store = False
        if EMBEDDING_AVAILABLE and text_for_embed:
            try:
                emb = emb_model.encode([text_for_embed], convert_to_numpy=True)[0]
                if find_similar(emb, threshold=float(__import__('..config', fromlist=['SIMILARITY_THRESHOLD']).SIMILARITY_THRESHOLD)):
                    is_duplicate_via_store = True
                else:
                    # keep embedding to persist later if article is sent
                    art["_embedding"] = emb
                    candidate_embeddings.append(emb)
                    candidate_metas.append(meta_for_article(art))
            except Exception as _e:
                # model failure should not block pipeline
                print(f"⚠️ 임베딩 검사 실패: {_e}")
        if is_duplicate_via_store:
            continue

        filtered.append(art)
        accepted_titles.append(title)

    # persist candidate_embeddings? only persist after successful send to avoid noise
    merged, dedup_errors = deduplicate_and_merge(filtered)
    all_errors.extend(dedup_errors)

    classified = []
    for art in merged:
        art, e = summarize(art)
        all_errors.extend(e)
        classified.append(art)
    classified.sort(key=lambda a: a.get("relevance", 0), reverse=True)

    print("\n===== CATEGORY DEBUG =====")
    for c in CATEGORY_ORDER:
        items = [x for x in classified if x.get("category") == c]
        print(f"\n{c}: {len(items)}개")
        for item in items[:3]:
            print("-", item.get("title"))

    classified = rerank_by_category(classified, CATEGORY_ORDER)
    if llm_enabled():
        print("LLM 리랭크 적용됨 (Gemini)")

    # ✅ 발송 확정 후보만 제목 한글 번역(실패 시 원문 유지, 발송은 계속됨)
    print(f"🈯 번역 단계 진입: GEMINI_API_KEY={'있음' if os.environ.get('GEMINI_API_KEY') else '없음'}, "
          f"후보 {len(classified)}건")
    classified = translate_titles(classified)

    if classified:
        success, sent_articles = send_aggregated_slack_news(classified)
        if success:
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
                    add_embeddings(__import__('numpy').array(new_embs), new_meta)
            except Exception as _e:
                print(f"⚠️ 임베딩 저장 실패: {_e}")

            # ✅ Send to Telegram (optional, if configured)
            try:
                from .utils import telegram_sender
                if telegram_sender.is_configured():
                    # Get the message that was sent to Slack (reconstruct from sent_articles)
                    # Use the same formatting as Slack message
                    from .config import MAX_PER_CATEGORY_DICT, MAX_PER_CATEGORY, SLACK_HEADER
                    buckets = {cat: [] for cat in CATEGORY_ORDER}
                    for a in sent_articles:
                        cat = a.get("category", CATEGORY_ORDER[-1])
                        if cat not in buckets:
                            cat = CATEGORY_ORDER[-1]
                        buckets[cat].append(a)
                    
                    parts = []
                    if SLACK_HEADER:
                        parts.append(SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d")))
                        parts.append("")
                    for cat in CATEGORY_ORDER:
                        parts.append(f"*{cat}*")
                        items = buckets.get(cat, [])
                        if items:
                            for a in items:
                                title = a.get("title", "제목 없음").strip()
                                url = get_primary_link(a) or "#"
                                raw_source = get_primary_source(a) or "출처미상"
                                source = clean_source_name(raw_source)
                                date = fmt_date(a.get("date", ""))
                                parts.append(f"• <{url}|{title}> ({source}, {date})")
                        else:
                            parts.append("• 오늘 조건에 맞는 뉴스가 없습니다.")
                        parts.append("")
                    message_text = "\n".join(parts).rstrip() + "\n"
                    
                    tg_success, tg_msg = telegram_sender.send_aggregated_news(message_text)
                    if tg_success:
                        print(f"✅ 텔레그램 전송 성공: {tg_msg}")
                    else:
                        print(f"⚠️ 텔레그램 전송 실패: {tg_msg}")
            except Exception as _e:
                print(f"⚠️ 텔레그램 전송 중 에러(계속 진행): {_e}")
    else:
        print("전송할 새로운 기사가 없습니다.")

    save_lines(SEEN_FILE, seen_links)
    save_lines(SEEN_TITLES_FILE, seen_titles, cap=2000)
    # persist semantic text traces (recent N)
    try:
        from .utils.file_handler import SEEN_TEXTS_FILE
        # cap at 2000 lines to avoid unbounded growth
        save_lines(SEEN_TEXTS_FILE, seen_texts, cap=2000)
    except Exception as _e:
        print(f"⚠️ seen_texts 저장 실패: {_e}")

    if all_errors:
        print(f"\n⚠️ 수집 오류 {len(all_errors)}건:")
        for e in all_errors:
            print(f"  • {e}")


if __name__ == "__main__":
    main()
