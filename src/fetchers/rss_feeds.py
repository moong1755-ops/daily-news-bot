import re
import socket
import feedparser
import requests
from datetime import datetime, timedelta
from urllib.parse import urlsplit, quote

try:
    from ..config import ALL_FEEDS as FEEDS
except ImportError:
    from ..config import RSS_SOURCES as FEEDS

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

# 인사이트/리포트 계열은 발행 주기가 길어 1일 recency 예외
EVERGREEN_SOURCES = [
    "McKinsey Insights", "BCG Insights", "PwC strategy+business", "SSIR",
    "PitchBook News", "Impact Alpha", "Climate Home News", "The Batch",
]

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
    yesterday = datetime.utcnow() - timedelta(days=1)

    for source_name, url in FEEDS.items():
        if not url or url.startswith("<"):
            continue

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

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_date = datetime(*published[:6])
                if pub_date < yesterday and source_name not in EVERGREEN_SOURCES:
                    continue
                date_str = pub_date.strftime("%Y-%m-%d")
            else:
                date_str = "Unknown date"

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
