import os
import re
import socket
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests

try:
    from ..config import ALL_FEEDS as FEEDS
except ImportError:
    from ..config import RSS_SOURCES as FEEDS

from ..config import (
    DIRECT_WEB_SOURCE_METADATA,
    FEED_CATEGORY_OVERRIDE,
    RSS_SOURCE_METADATA,
)

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

_DIRECT_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
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


class _ConfiguredArticleListParser(HTMLParser):
    """설정에 적힌 CSS class와 URL prefix만으로 공개 기사 목록을 읽는다."""

    def __init__(self, metadata: dict):
        super().__init__(convert_charrefs=True)
        self.title_class = metadata["title_container_class"]
        self.summary_class = metadata["summary_class"]
        self.article_url_prefix = metadata["article_url_prefix"]
        self.items = []
        self._seen_links = set()
        self._title_depth = 0
        self._summary_depth = 0
        self._title_parts = []
        self._summary_parts = []
        self._title_link = ""
        self._last_item = None

    @staticmethod
    def _attributes(attrs) -> dict:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag, attrs):
        attributes = self._attributes(attrs)
        classes = set(attributes.get("class", "").split())

        if self._title_depth:
            self._title_depth += 1
        elif self.title_class in classes:
            self._title_depth = 1
            self._title_parts = []
            self._title_link = ""

        if self._title_depth and tag == "a":
            href = attributes.get("href", "").strip()
            if href.startswith(self.article_url_prefix):
                self._title_link = href

        if self._summary_depth:
            self._summary_depth += 1
        elif self.summary_class in classes:
            self._summary_depth = 1
            self._summary_parts = []

    def handle_data(self, data):
        if self._title_depth:
            self._title_parts.append(data)
        if self._summary_depth:
            self._summary_parts.append(data)

    def handle_endtag(self, _tag):
        if self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                title = " ".join("".join(self._title_parts).split())
                if (
                    title
                    and self._title_link
                    and self._title_link not in self._seen_links
                ):
                    self._last_item = {
                        "title": title,
                        "link": self._title_link,
                        "description": "",
                    }
                    self.items.append(self._last_item)
                    self._seen_links.add(self._title_link)

        if self._summary_depth:
            self._summary_depth -= 1
            if self._summary_depth == 0 and self._last_item is not None:
                self._last_item["description"] = " ".join(
                    "".join(self._summary_parts).split()
                )


class _SemanticArticleListParser(HTMLParser):
    """제목 링크와 ``time`` 태그가 있는 공식 인사이트 목록을 읽는다.

    MBB 사이트처럼 CSS 클래스 이름이 수시로 바뀌는 페이지를 위한 파서다.
    허용 URL은 config의 도메인·경로 또는 정규식으로 제한하므로 메뉴 링크가
    기사로 들어오는 것을 막는다.
    """

    def __init__(self, metadata: dict):
        super().__init__(convert_charrefs=True)
        self.base_url = metadata["url"]
        self.allowed_domains = tuple(metadata.get("allowed_domains", ()))
        self.path_markers = tuple(metadata.get("article_path_markers", ()))
        self.url_patterns = tuple(metadata.get("article_url_patterns", ()))
        self.minimum_title_length = int(metadata.get("minimum_title_length", 12))
        self.items = []
        self._items_by_link = {}
        self._anchor_depth = 0
        self._anchor_link = ""
        self._anchor_parts = []
        self._time_depth = 0
        self._time_parts = []
        self._time_value = ""
        self._last_item = None

    @staticmethod
    def _attributes(attrs) -> dict:
        return {key: value or "" for key, value in attrs}

    def _article_link(self, href: str) -> str:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            return ""

        link = urljoin(self.base_url, href.strip())
        parsed = urlsplit(link)
        domain = parsed.netloc.lower().removeprefix("www.")
        if self.allowed_domains and not any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in self.allowed_domains
        ):
            return ""
        if self.path_markers and not any(
            marker in parsed.path for marker in self.path_markers
        ):
            return ""
        if self.url_patterns and not any(
            re.search(pattern, link, re.I) for pattern in self.url_patterns
        ):
            return ""
        return link

    def handle_starttag(self, tag, attrs):
        attributes = self._attributes(attrs)
        if self._anchor_depth:
            self._anchor_depth += 1
        elif tag == "a":
            link = self._article_link(attributes.get("href", ""))
            if link:
                self._anchor_depth = 1
                self._anchor_link = link
                self._anchor_parts = []

        if self._time_depth:
            self._time_depth += 1
        elif tag == "time":
            self._time_depth = 1
            self._time_parts = []
            self._time_value = attributes.get("datetime", "").strip()

    def handle_data(self, data):
        if self._anchor_depth:
            self._anchor_parts.append(data)
        if self._time_depth:
            self._time_parts.append(data)

    def handle_endtag(self, tag):
        if self._anchor_depth:
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                title = " ".join("".join(self._anchor_parts).split())
                if len(title) >= self.minimum_title_length:
                    item = self._items_by_link.get(self._anchor_link)
                    if item is None:
                        item = {
                            "title": title,
                            "link": self._anchor_link,
                            "description": "",
                            "published": "",
                        }
                        self.items.append(item)
                        self._items_by_link[self._anchor_link] = item
                    elif len(title) > len(item["title"]):
                        item["title"] = title
                    self._last_item = item

        if self._time_depth:
            self._time_depth -= 1
            if self._time_depth == 0 and tag == "time" and self._last_item:
                visible_date = " ".join("".join(self._time_parts).split())
                self._last_item["published"] = self._time_value or visible_date


def _parse_direct_web_date(raw_date: str):
    cleaned = " ".join((raw_date or "").strip().split())
    if not cleaned:
        return None

    # 21st처럼 영문 날짜에 붙는 서수 표현도 허용한다.
    cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", cleaned, flags=re.I)
    iso_candidate = cleaned.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass

    for date_format in ("%B %d, %Y", "%b %d, %Y", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return None


def _direct_web_date(
    link: str,
    metadata: dict,
    now_utc: datetime,
    raw_date: str = "",
) -> tuple:
    """화면 날짜 또는 기사 URL 날짜를 읽고 수집 기간을 적용한다."""
    article_date = _parse_direct_web_date(raw_date)
    date_pattern = metadata.get("date_from_url_pattern", "")
    match = re.search(date_pattern, link) if date_pattern else None
    if article_date is None and match:
        try:
            article_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
        except (TypeError, ValueError):
            article_date = None

    target_date = as_of_date()
    if article_date is None:
        return (
            ("", False)
            if target_date or metadata.get("require_date", False)
            else ("Unknown date", True)
        )
    if target_date:
        return (
            (article_date.isoformat(), True)
            if article_date == target_date
            else ("", False)
        )

    today_korea = now_utc.astimezone(KOREA_TIMEZONE).date()
    age_days = (today_korea - article_date).days
    default_lookback = (
        LONG_FORM_LOOKBACK.days
        if str(metadata.get("category", "")).startswith("👔")
        else STANDARD_LOOKBACK.days
    )
    lookback_days = int(metadata.get("lookback_days", default_lookback))
    if age_days > lookback_days or age_days < -MAX_FUTURE_SKEW.days:
        return "", False
    return article_date.isoformat(), True


def _fetch_direct_web_source(
    source_name: str,
    metadata: dict,
    now_utc: datetime,
) -> list:
    response = requests.get(
        metadata["url"],
        headers=_DIRECT_WEB_HEADERS,
        timeout=15,
    )
    response.raise_for_status()

    parser_type = metadata.get("parser", "configured_classes")
    if parser_type == "semantic_links":
        parser = _SemanticArticleListParser(metadata)
    else:
        parser = _ConfiguredArticleListParser(metadata)
    parser.feed(response.text)

    articles = []
    for item in parser.items:
        date_str, include_article = _direct_web_date(
            item["link"],
            metadata,
            now_utc,
            item.get("published", ""),
        )
        if not include_article:
            continue
        articles.append({
            "title": item["title"],
            "link": item["link"],
            "date": date_str,
            "source": source_name,
            "feed": source_name,
            "region": metadata.get("region", "global"),
            "gnews_link": "",
            "description": clean_html(item["description"]),
        })
    return articles


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

        # 발행량이 많은 매체는 3일 이내 기사도 20번째 뒤로 밀릴 수 있다.
        # feedparser가 이미 피드 전체를 읽은 상태이므로 여기서 먼저 자르지 않고,
        # 아래 _article_date()가 실제 수집 기간에 해당하는 기사만 남기게 한다.
        for entry in feed.entries:
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

    for source_name, metadata in DIRECT_WEB_SOURCE_METADATA.items():
        try:
            direct_articles = _fetch_direct_web_source(
                source_name,
                metadata,
                now_utc,
            )
            articles.extend(direct_articles)
            print(
                f"📰 {source_name}: 공개 기사 목록에서 "
                f"{len(direct_articles)}건 수집"
            )
        except Exception as exc:
            errors.append(f"{source_name} ({metadata.get('url', '')}): {exc}")

    return articles, errors
