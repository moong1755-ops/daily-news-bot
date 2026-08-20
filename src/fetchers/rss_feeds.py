import os
import re
import socket
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import feedparser
import requests

try:
    from ..config import ALL_FEEDS as FEEDS
except ImportError:
    from ..config import RSS_SOURCES as FEEDS

from ..config import FEED_CATEGORY_OVERRIDE, RSS_SOURCE_METADATA

try:
    from ..config import source_region
except ImportError:
    def source_region(_name):
        return "global"

# ✅ Google News 리다이렉트 링크 → 원문 URL 디코딩(선택 의존성, 실패 시 원링크 유지)
try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

feedparser.USER_AGENT = "daily-news-bot/1.0"
socket.setdefaulttimeout(15)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# 일반 뉴스는 실행 지연을 고려해 3일, 정기 간행물은 주간 발행 주기를 고려해 8일 수집한다.
STANDARD_LOOKBACK = timedelta(days=3)
LONG_FORM_LOOKBACK = timedelta(days=8)
MAX_FUTURE_SKEW = timedelta(days=1)
KOREA_TIMEZONE = timezone(timedelta(hours=9))

LONG_FORM_SOURCES = frozenset({"SSIR", "The Batch"})

_GN_URL_CACHE = {}


def resolve_gnews_url(url: str) -> str:
    if not url or "news.google.com" not in url or gnewsdecoder is None:
        return url
    if url in _GN_URL_CACHE:
        return _GN_URL_CACHE[url]
    out = url
    try:
        d = gnewsdecoder(url)
        if isinstance(d, dict) and d.get("status") and d.get("decoded_url"):
            out = d["decoded_url"]
    except Exception:
        out = url
    _GN_URL_CACHE[url] = out
    return out


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return text.strip()[:500]


def extract_gnews(entry, raw_title: str, feed_name: str):
    """Google News: 실제 언론사명(entry.source.title) 우선 + 제목 끝 '- 언론사' 제거."""
    outlet = ""
    src = entry.get("source")
    if isinstance(src, dict):
        outlet = (src.get("title") or "").strip()

    title = raw_title.strip()
    if outlet and title.endswith(" - " + outlet):
        title = title[: -len(" - " + outlet)].strip()
    elif " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if head.strip() and 1 <= len(tail) <= 40 and "\n" not in tail:
            title = head.strip()
            outlet = outlet or tail.strip()
    return title, (outlet or feed_name)


def _published_utc(entry):
    """feedparser 날짜를 UTC datetime으로 정규화한다."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    raw_date = entry.get("published") or entry.get("updated")
    if not raw_date:
        return None
    try:
        published = parsedate_to_datetime(raw_date)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return published.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _uses_long_form_window(source_name: str) -> bool:
    category = FEED_CATEGORY_OVERRIDE.get(source_name) or RSS_SOURCE_METADATA.get(
        source_name, {}
    ).get("category", "")
    return str(category).startswith("👔") or source_name in LONG_FORM_SOURCES


def _article_date(entry, source_name: str, now_utc: datetime) -> tuple:
    """표시 날짜와 수집 여부를 반환한다. 날짜가 없으면 버리지 않고 표시만 보류한다."""
    published = _published_utc(entry)

    target_date = as_of_date()
    if target_date:
        # 재현 모드에서는 그날 기사만 남긴다. 날짜를 모르는 기사는 그날 것인지
        # 확인할 수 없으므로 제외한다(평소에는 살려 두는 것과 반대).
        if published is None:
            return "", False
        local_date = published.astimezone(KOREA_TIMEZONE).date()
        if local_date != target_date:
            return "", False
        return local_date.strftime("%Y-%m-%d"), True

    if published is None:
        return "Unknown date", True

    lookback = (
        LONG_FORM_LOOKBACK
        if _uses_long_form_window(source_name)
        else STANDARD_LOOKBACK
    )
    if published < now_utc - lookback or published > now_utc + MAX_FUTURE_SKEW:
        return "", False

    return published.astimezone(KOREA_TIMEZONE).strftime("%Y-%m-%d"), True


# ── 과거 날짜 재현(테스트 전용) ────────────────────────────────────────────
# AS_OF_DATE=YYYY-MM-DD 를 주면 그날 발행된 기사만 모은다. 편집 로직을 다른
# 날짜의 뉴스로 검증하기 위한 것이다.
#
# 완전한 재현은 불가능하다. RSS 는 과거 시점을 돌려주지 않고 최신 항목만
# 싣기 때문에, TechCrunch·PE Hub 처럼 발행량이 많은 피드는 며칠 지나면 그날
# 기사가 이미 빠져 있다. 반면 Google News 는 after:/before: 검색을 지원해
# 지난 날짜를 그대로 가져올 수 있다. 따라서 결과는 'Google News 쪽은 충실,
# 직접 RSS 쪽은 남아 있는 만큼' 이라는 부분 재현이다.


def as_of_date():
    """AS_OF_DATE 환경변수를 date 로 돌려준다. 없거나 형식이 틀리면 None."""
    raw = os.environ.get("AS_OF_DATE", "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        print(f"⚠️ AS_OF_DATE 형식이 잘못됨({raw}) — 무시하고 현재 날짜로 수집합니다.")
        return None


def rewrite_for_as_of(url: str, target) -> str:
    """Google News 쿼리의 when:Nd 를 해당 날짜 하루 구간으로 바꾼다."""
    if not target or "news.google.com" not in url:
        return url
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    window = (
        f"after:{target - timedelta(days=1)} before:{target + timedelta(days=1)}"
    )
    changed = []
    for key, value in params:
        if key == "q":
            value = re.sub(r"when:\d+[dhm]", window, value)
            if "after:" not in value:
                value = f"{value} {window}"
        changed.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(changed), ""))


def _get_feed(url: str):
    """요청 + 파싱. bozo+기사0 이면 불법 XML 문자 정화 후 1회 재시도. 실패 시 예외."""
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        cleaned = resp.content.decode("utf-8", errors="ignore")
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
        feed = feedparser.parse(cleaned)
    if feed.bozo and not feed.entries:
        raise ValueError(f"parse fail: {feed.bozo_exception}")
    if feed.bozo:
        print(f"⚠️ RSS 파싱 경고(일부 수집): {feed.bozo_exception}")
    return feed


def _discover_feed_url(original_url: str) -> str:
    """Try heuristics to find a working feed URL for a site.
    Return working URL or None."""
    try:
        resp = requests.get(original_url, headers=_HEADERS, timeout=10)
    except Exception:
        resp = None
    # If got content and it already looks like RSS/Atom
    if resp is not None:
        ctype = resp.headers.get("content-type", "") if resp.headers else ""
        text_head = resp.text[:2000] if resp is not None else ""
        if "xml" in ctype.lower() or re.search(r"<\?xml|<rss|<feed", text_head, re.I):
            return original_url
        # try to find <link rel="alternate" type="application/rss+xml"
        m = re.search(r'<link[^>]+type=["\']application/(rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']', resp.text, re.I)
        if m:
            href = m.group(2)
            # absolute?
            if href.startswith("http"):
                return href
            from urllib.parse import urljoin
            return urljoin(original_url, href)

    # try common suffixes
    suffixes = ["/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/feeds/posts/default?alt=rss"]
    for s in suffixes:
        candidate = original_url.rstrip("/") + s if not original_url.endswith(s) else original_url
        try:
            r = requests.get(candidate, headers=_HEADERS, timeout=10)
            if r.status_code == 200 and ("xml" in r.headers.get("content-type","") or re.search(r"<rss|<feed", r.text, re.I)):
                return candidate
        except Exception:
            pass
    return None


def _gnews_site_fallback_url(original_url: str, source_name: str) -> str:
    """✅ 3단계 안전망: 원본 RSS 사망 시 해당 '도메인 한정' 구글뉴스 검색으로 우회.
    (매체명 검색은 '그 매체에 관한 기사'가 섞이므로 site: 을 사용)"""
    domain = urlsplit(original_url).netloc.replace("www.", "")
    if source_region(source_name) == "korea":
        lang = "hl=ko&gl=KR&ceid=KR:ko"
    else:
        lang = "hl=en-US&gl=US&ceid=US:en"
    q = quote(f"site:{domain} when:2d")
    return f"https://news.google.com/rss/search?q={q}&{lang}"


def fetch() -> tuple:
    articles = []
    errors = []
    now_utc = datetime.now(timezone.utc)
    target_date = as_of_date()
    if target_date:
        print(f"🕰 재현 모드: {target_date} 발행 기사만 수집합니다 "
              "(Google News 는 해당 날짜로 재검색, 직접 RSS 는 남아 있는 만큼).")

    for source_name, url in FEEDS.items():
        if not url or url.startswith("<"):
            continue

        url = rewrite_for_as_of(url, target_date)
        is_gnews = "news.google.com" in url
        via_fallback = False

        try:
            feed = _get_feed(url)
        except Exception as first_err:
            if is_gnews:
                errors.append(f"{source_name} ({url}): {first_err}")
                continue
            # Try to discover an alternate feed URL heuristically
            discovered = _discover_feed_url(url)
            if discovered:
                try:
                    feed = _get_feed(discovered)
                    print(f"🔍 {source_name}: 발견된 대체 피드로 수집({discovered})")
                except Exception as d_err:
                    discovered = None
            if not discovered:
                # ✅ 원본 RSS 실패 → site: 구글뉴스 폴백
                try:
                    fb_url = _gnews_site_fallback_url(url, source_name)
                    feed = _get_feed(fb_url)
                    if not feed.entries:
                        raise ValueError("fallback empty")
                    via_fallback = True
                    print(f"🔁 {source_name}: 원본 RSS 실패 → Google News site: 폴백으로 수집")
                except Exception as fb_err:
                    errors.append(f"{source_name} ({url}): {first_err} | 폴백 실패: {fb_err}")
                    continue

        parse_as_gnews = is_gnews or via_fallback

        for entry in feed.entries[:20]:
            raw_title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not raw_title or not link:
                continue

            gnews_link = ""
            if parse_as_gnews:
                title, display_source = extract_gnews(entry, raw_title, source_name)
                gnews_link = link                  # 디코딩 전 원링크 보존(seen 이중키)
                link = resolve_gnews_url(link)
            else:
                title = raw_title
                display_source = source_name

            date_str, include_article = _article_date(entry, source_name, now_utc)
            if not include_article:
                continue

            description = clean_html(entry.get("summary") or entry.get("description") or "")

            articles.append({
                "title": title,
                "link": link,
                "date": date_str,
                "source": display_source,      # 표시용(실제 언론사)
                "feed": source_name,           # 라우팅/메타데이터 매칭용 원 피드명
                "region": source_region(source_name),
                "gnews_link": gnews_link,
                "description": description,
            })

    return articles, errors
