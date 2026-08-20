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
    IMPACT_MUST_READ_MAX,
    ALTERNATIVE_MAJOR_DEAL_MAX,
    OVERSEAS_PREFERRED_DOMAINS,
    REGION_WEIGHT,
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
ALTERNATIVE_CATEGORY = next(category for category in CATEGORY_ORDER if category.startswith("💼"))
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


def _select_category_articles(ranked: list, category: str) -> list:
    """Keep normal caps while preserving tagged impact and major-deal overflow."""
    base_limit = MAX_PER_CATEGORY_DICT.get(category, MAX_PER_CATEGORY)
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
        "relevance": article.get("relevance"),
        "filter_reason": article.get("filter_reason"),
        "relevance_signal": article.get("relevance_signal"),
        "editorial_signals": article.get("editorial_signals"),
        "deal_signals": article.get("deal_signals"),
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


def render_digest(articles_by_category: dict) -> str:
    """슬랙·텔레그램 공용 다이제스트 본문. 카테고리 헤더는 비어 있어도 항상 표시한다."""
    parts = []
    if SLACK_HEADER:
        parts.append(SLACK_HEADER.replace("{date}", datetime.now().strftime("%y.%m.%d")))
        parts.append("")
    for cat in CATEGORY_ORDER:
        parts.append(f"*{cat}*")
        selected = articles_by_category.get(cat) or []
        if selected:
            parts.extend(_format_article_line(a) for a in selected)
        else:
            parts.append("• 오늘 조건에 맞는 뉴스가 없습니다.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def send_aggregated_slack_news(articles) -> tuple:
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook_url and not is_dry_run():
        print("SLACK_WEBHOOK_URL 이 설정되지 않았습니다.")
        return False, []

    buckets = bucket_by_category(a for a in articles if _is_sendable(a))

    sent_articles = []          # ✅ 실제 슬랙에 나간 기사만 수집(seen 처리용)
    selected_by_category = {}
    for cat_name in CATEGORY_ORDER:
        ranked = sorted(buckets[cat_name], key=lambda a: _selection_score(a, cat_name), reverse=True)
        selected = _select_category_articles(ranked, cat_name)
        selected_by_category[cat_name] = selected
        sent_articles.extend(selected)

    message_text = render_digest(selected_by_category)
    print(f"ℹ️ Slack 메시지 {len(message_text):,}자, 선택 기사 {len(sent_articles)}건 전부 발송")

    if is_dry_run():
        print("\n===== DRY RUN — 실제 발송하지 않음 =====")
        print(message_text)
        print("===== DRY RUN 끝 =====\n")
        return True, sent_articles

    # ✅ 메시지 아카이브: 발송 전 로컬 파일에 기록(append, JSONL)
    #    아카이브는 '실제로 나간 것'의 기록이므로 DRY RUN 은 남기지 않는다.
    try:
        archive_path = Path(__file__).parent.parent / "data" / "slack_archive.jsonl"
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
    rejected = []          # 탈락 사유와 함께 판정 로그에 남긴다
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
        if not is_relevant(art):
            rejected.append(art)      # is_relevant 가 filter_reason 을 붙여 둔다
            continue

        # Cross-day semantic dedupe using embedding store
        text_for_embed = (title + " \n " + art.get("description", "")).strip()
        is_duplicate_via_store = False
        if EMBEDDING_AVAILABLE and text_for_embed:
            try:
                emb = emb_model.encode([text_for_embed], convert_to_numpy=True)[0]
                if find_similar(emb, threshold=float(SIMILARITY_THRESHOLD)):
                    is_duplicate_via_store = True
                else:
                    # 발송이 확정된 뒤에만 저장하도록 기사에 임시로 붙여 둔다.
                    art["_embedding"] = emb
            except Exception as _e:
                # model failure should not block pipeline
                print(f"⚠️ 임베딩 검사 실패: {_e}")
        if is_duplicate_via_store:
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
