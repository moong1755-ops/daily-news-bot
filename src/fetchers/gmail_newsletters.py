"""Read newsletter links from a personal Gmail account through read-only IMAP."""

import imaplib
import os
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from html import unescape
from typing import List, Tuple
from urllib.parse import urlsplit

from ..config import NEWSLETTER_COLLECTION, NEWSLETTER_SOURCES
from ..utils.newsletters import identify_newsletter
from .newsletter_html import article_contexts
from .newsletter_body import (
    primary_article_context, primary_public_story_url, public_story_url,
    unlinked_body_notes,
)


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
GMAIL_NEWSLETTER_QUERY = os.environ.get(
    "GMAIL_NEWSLETTER_QUERY", "label:newsletters"
).strip()
GMAIL_IMAP_FOLDER = os.environ.get("GMAIL_IMAP_FOLDER", "INBOX").strip() or "INBOX"
GMAIL_LOOKBACK_DAYS = _positive_env_int("GMAIL_LOOKBACK_DAYS", 3)
GMAIL_MAX_EMAILS = _positive_env_int("GMAIL_MAX_EMAILS", NEWSLETTER_COLLECTION["recent_emails"])
GMAIL_LINKS_PER_EMAIL = _positive_env_int("GMAIL_LINKS_PER_EMAIL", NEWSLETTER_COLLECTION["max_links_per_email"])

_SKIP_URL_MARKERS = (
    "unsubscribe", "subscription-preferences", "manage-preferences",
    "email-preferences", "newsletter/settings", "view-in-browser",
    "forward-to-a-friend", "mailto:",
)
_SKIP_LINK_TEXTS = {
    "read more", "learn more", "click here", "view online", "view in browser",
    "subscribe", "unsubscribe", "manage preferences", "privacy policy",
    "terms of use", "facebook", "instagram", "linkedin", "x", "twitter",
    "explore all bloomberg newsletters", "catch up on the conversation",
    "this week's must-reads on impactalpha",
}

# 정확히 일치하는 문구만 걸러내면 매체마다 표현이 달라 계속 새는데, 실제로
# Bloomberg 의 "Read in browser" 가 기사 제목으로 발송된 적이 있다. 문구 대신
# 뉴스레터 상용구에 공통으로 나타나는 조각으로 판단한다.
_SKIP_LINK_FRAGMENTS = (
    "in browser", "view online", "web version", "read online",
    "unsubscribe", "manage preference", "email preference", "privacy policy",
    "advertise with us", "become a sponsor", "download the app", "follow us", "contact us",
    "브라우저에서", "구독 취소", "수신 거부",
)


def _is_boilerplate_anchor(anchor_text: str) -> bool:
    """뉴스레터 상용구 링크인지 판단한다(기사 제목이 아닌 것)."""
    normalized = re.sub(r"\s+", " ", anchor_text or "").strip().lower()
    if not normalized or len(normalized) < 8:
        return True
    if normalized in _SKIP_LINK_TEXTS:
        return True
    if normalized.startswith(("subscribe to ", "get the ")) and (
        normalized.startswith("subscribe to ") or normalized.endswith(" app")
    ):
        return True
    return any(fragment in normalized for fragment in _SKIP_LINK_FRAGMENTS)


def _looks_like_headline(anchor_text: str) -> bool:
    """기사 제목처럼 보이는 링크만 통과시킨다.

    뉴스레터 본문은 문장 중간 단어에 링크를 건다. 그 앵커 텍스트가 그대로
    기사 제목이 되면 "swap soybeans for credits" 나 사람 이름 같은 조각이
    브리핑에 올라간다. 상용구 목록으로는 이런 걸 걸러낼 수 없다.

    두 가지 신호를 쓴다. 문장 중간에서 잘라온 링크는 소문자로 시작하고,
    제목이라기엔 너무 짧다. 편집 판단이 아니라 파싱 문제라 규칙으로 다룬다.
    """
    text = re.sub(r"\s+", " ", anchor_text or "").strip()
    if not text:
        return False

    first_letter = next((c for c in text if c.isalpha()), "")
    if first_letter and first_letter.isascii() and first_letter.islower():
        return False        # "rising ocean temperatures" — 문장 중간 조각

    if len(text.split()) >= 5:
        return True

    # 단어 수가 적으면 글자 수로 본다. 한국어·중국어·일본어는 같은 내용을 훨씬
    # 짧게 쓰므로("삼성전자, SK하이닉스 인수") 영문과 같은 기준을 대면 멀쩡한
    # 제목이 조각으로 몰린다.
    threshold = 12 if re.search(r"[가-힣぀-ヿ一-鿿]", text) else 30
    return len(text) >= threshold


def _anchor_headline_html(anchor_html: str) -> str:
    """앵커 안에서 제목에 해당하는 첫 블록만 텍스트로 뽑는다.

    뉴스레터는 제목과 본문을 한 링크 안에 서로 다른 블록으로 넣는다. 태그를
    전부 지우고 이어 붙이면 "...in Pivot LG Energy Solution is considering..."
    처럼 제목과 본문이 문장부호 없이 붙어, 글자 수로는 자를 지점을 알 수 없다.
    블록 경계를 먼저 살려서 첫 덩어리만 취한다.

    블록 구분이 없으면 전체 텍스트를 보존하며 글자 수로 자르지 않는다.
    """
    chunks = re.split(
        r"(?is)</(?:div|p|h[1-6]|td|tr|li|table|section)\s*>|<br\s*/?>",
        anchor_html or "",
    )
    for chunk in chunks:
        text = _clean_text(chunk)
        if len(text) >= 15:
            return text
    return _clean_text(anchor_html)


def _headline_from_anchor(anchor_text: str) -> str:
    """Normalize whitespace without truncating a real headline.

    Separate title/body HTML blocks are handled by _anchor_headline_html.
    Without a structural boundary we must not invent a title cut-off.
    """
    return re.sub(r"\s+", " ", anchor_text or "").strip()


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")


def is_configured() -> bool:
    """Return True only when both personal Gmail secrets are present."""
    return bool(GMAIL_USER and GMAIL_APP_PASSWORD)


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _clean_subject(subject: str) -> str:
    subject = re.sub(r"^\[.*?\]\s*", "", subject or "")
    subject = re.sub(r"^(FW|Re|Fwd):\s*", "", subject, flags=re.IGNORECASE)
    return _clean_text(subject) or "Newsletter"


def _is_usable_article_url(url: str) -> bool:
    cleaned = unescape(url or "").strip().strip("<>\"'")
    lowered = cleaned.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    if any(marker in lowered for marker in _SKIP_URL_MARKERS):
        return False

    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return False
    if not parts.netloc or parts.path.lower().endswith(_IMAGE_SUFFIXES):
        return False
    return parts.path not in ("", "/")


def _extract_link_candidates(html_text: str, plain_text: str) -> List[Tuple[str, str]]:
    """Extract ordered article URLs with meaningful anchor text when available."""
    candidates = []
    seen_urls = set()

    anchor_pattern = re.compile(
        r"<a\b[^>]*?href=[\"'](https?://[^\"'<>\s]+)[\"'][^>]*>(.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for url, anchor_html in anchor_pattern.findall(html_text or ""):
        url = unescape(url).strip()
        if url in seen_urls or not _is_usable_article_url(url):
            continue
        anchor_text = _anchor_headline_html(anchor_html)
        if _is_boilerplate_anchor(anchor_text) or not _looks_like_headline(anchor_text):
            continue
        candidates.append((url, anchor_text))
        seen_urls.add(url)

    # HTML/plain alternatives describe the same mail. Re-reading bare URLs
    # would resurrect rejected chrome links under the email subject.
    if html_text.strip():
        return candidates

    bare_url_pattern = re.compile(r"https?://[^\s<>\"'{}|\\^`\[\]]+")
    lines = (plain_text or "").splitlines()
    for index, line in enumerate(lines):
        match = bare_url_pattern.search(line)
        if not match:
            continue
        raw_url = match.group()
        url = unescape(raw_url).rstrip(".,);]")
        if url in seen_urls or not _is_usable_article_url(url):
            continue
        title = line[:match.start()].strip(" <>[]():-")
        if not title and index:
            title = lines[index - 1].strip()
        if _is_boilerplate_anchor(title) or not _looks_like_headline(title):
            continue
        candidates.append((url, title))
        seen_urls.add(url)

    return candidates


def _extract_message_bodies(message) -> Tuple[str, str]:
    html_parts = []
    plain_parts = []
    parts = message.walk() if message.is_multipart() else [message]

    for part in parts:
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/html":
            html_parts.append(str(content))
        else:
            plain_parts.append(str(content))

    return "\n".join(html_parts), "\n".join(plain_parts)


def _message_date(message) -> str:
    try:
        parsed = parsedate_to_datetime(message.get("Date", ""))
        if parsed is not None:
            return parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return datetime.now(timezone.utc).date().isoformat()


def _parse_email(raw_message: bytes, stats: dict | None = None) -> List[dict]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = _clean_subject(str(message.get("Subject", "Newsletter")))
    sender_name, sender_address = parseaddr(str(message.get("From", "")))
    sender = _clean_text(sender_name) or sender_address.split("@")[0] or "Newsletter"
    source_key = identify_newsletter(sender, sender_address, subject)
    source_policy = NEWSLETTER_SOURCES.get(source_key, {})
    sender = source_policy.get("name", sender)
    html_text, plain_text = _extract_message_bodies(message)
    candidates = _extract_link_candidates(html_text, plain_text)
    context_limit = NEWSLETTER_COLLECTION["context_chars"]
    notes = unlinked_body_notes(html_text, subject, source_policy, context_limit)
    if stats is not None:
        counts = stats.setdefault(source_key or "other", {"emails": 0, "links": 0, "overflow": 0})
        counts["emails"] += 1
        counts["links"] += min(len(candidates), GMAIL_LINKS_PER_EMAIL)
        counts["overflow"] += max(0, len(candidates) - GMAIL_LINKS_PER_EMAIL)
        if notes:
            counts["body_notes"] = counts.get("body_notes", 0) + len(notes)
    if len(candidates) > GMAIL_LINKS_PER_EMAIL:
        print(f"⚠️ 뉴스레터 {source_policy.get('name', '기타')}: 기사 링크 {len(candidates)}건 중 "
              f"안전 상한 {GMAIL_LINKS_PER_EMAIL}건만 수집 (config/환경변수 조정 필요)")
    candidates = candidates[:GMAIL_LINKS_PER_EMAIL]
    if not candidates and not notes:
        return []

    contexts = article_contexts(html_text, dict(candidates), context_limit)
    primary_context = primary_article_context(html_text, source_policy, context_limit)

    published_date = _message_date(message)
    articles = []
    for url, anchor_text in candidates:
        title = _headline_from_anchor(anchor_text) or subject
        public_url = public_story_url(url, source_policy)
        if not public_url and title.casefold() == subject.casefold() and primary_context:
            public_url = primary_public_story_url(html_text, title, source_policy)
        primary_story = bool(public_url and primary_context and title.casefold() == subject.casefold())
        articles.append({
            "title": title,
            "link": public_url or url,
            "source": sender,
            "feed": "Gmail Newsletters",
            "date": published_date,
            "description": primary_context if primary_story else contexts.get(url, ""),
            "region": source_policy.get("region", "global"),
            "newsletter_source": source_key,
            "newsletter_sources": [source_key] if source_key else [],
            "newsletter_context": "article",
            "date_basis": "newsletter_date",  # 메일 Date 헤더이며 원문 발행일은 아님
            "newsletter_primary_story": primary_story,
        })
    for title, body in notes:
        # No public article URL: this record is only evidence for matching a
        # separately linked article. bot.main never sends linkless records.
        articles.append({
            "title": title, "link": "", "source": sender,
            "feed": "Gmail Newsletters", "date": published_date,
            "description": body, "region": source_policy.get("region", "global"),
            "newsletter_source": source_key, "newsletter_sources": [source_key],
            "newsletter_body_only": True, "date_basis": "newsletter_date",
        })
    return articles


def _gmail_raw_query(source_query: str = "") -> str:
    # Required sources are explicitly configured by the user; their messages
    # may not carry the generic newsletter label. Keep the date window and
    # folder boundary, and never broaden the ordinary label-based search.
    scope = source_query or GMAIL_NEWSLETTER_QUERY
    parts = [
        part
        for part in (scope, f"newer_than:{GMAIL_LOOKBACK_DAYS}d")
        if part
    ]
    query = " ".join(parts)
    escaped = query.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def fetch() -> Tuple[List[dict], List[str]]:
    """Fetch recent newsletter emails without changing read/unread state."""
    if not is_configured():
        return [], ["Gmail 미설정: GMAIL_USER와 GMAIL_APP_PASSWORD가 필요합니다."]

    client = None
    articles = []
    errors = []
    stats = {}
    try:
        client = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
        client.login(GMAIL_USER, GMAIL_APP_PASSWORD)

        status, _ = client.select(GMAIL_IMAP_FOLDER, readonly=True)
        if status != "OK":
            return [], [f"Gmail 폴더를 열 수 없습니다: {GMAIL_IMAP_FOLDER}"]

        status, data = client.uid("search", None, "X-GM-RAW", _gmail_raw_query())
        if status != "OK":
            return [], ["Gmail 뉴스레터 검색에 실패했습니다."]

        raw_uids = data[0] if data else b""
        message_uids = (raw_uids or b"").split()
        message_uids = list(reversed(message_uids))[:GMAIL_MAX_EMAILS]
        # Recent busy sources must not crowd out the six required-review sources.
        # A required sender search is independent of the generic newsletter
        # label, while retaining the date window, folder, and read-only mode.
        for source_policy in NEWSLETTER_SOURCES.values():
            source_query = source_policy.get("gmail_query")
            if not source_query:
                continue
            try:
                status, data = client.uid("search", None, "X-GM-RAW", _gmail_raw_query(source_query))
            except (imaplib.IMAP4.error, OSError):
                errors.append(f"뉴스레터 출처 검색 실패: {source_policy['name']}")
                continue
            if status != "OK":
                errors.append(f"뉴스레터 출처 검색 실패: {source_policy['name']}")
                continue
            uids = ((data[0] if data else b"") or b"").split()
            message_uids.extend(list(reversed(uids))[:NEWSLETTER_COLLECTION["emails_per_required_source"]])
        message_uids = list(dict.fromkeys(message_uids))
        for uid in message_uids:
            try:
                status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    errors.append("뉴스레터 메일 한 건을 불러오지 못했습니다.")
                    continue
                raw_message = next(
                    (
                        item[1]
                        for item in payload
                        if isinstance(item, tuple)
                        and len(item) > 1
                        and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if raw_message:
                    articles.extend(_parse_email(raw_message, stats))
            except Exception as exc:
                errors.append(f"뉴스레터 메일 해석 실패: {type(exc).__name__}")

        print(
            f"✅ Gmail 뉴스레터: 최근 메일 {len(message_uids)}건에서 "
            f"기사 링크·본문 근거 {len(articles)}건 수집"
        )
        for key, source_policy in NEWSLETTER_SOURCES.items():
            counts = stats.get(key, {})
            print(f"📬 {source_policy['name']}: 조회 범위 내 읽은 메일 {counts.get('emails', 0)}건 / "
                  f"기사 링크 {counts.get('links', 0)}건 / 본문 참고 {counts.get('body_notes', 0)}건 / "
                  f"상한 초과 {counts.get('overflow', 0)}건")
        return articles, errors
    except imaplib.IMAP4.error:
        return [], ["Gmail 로그인 실패: 이메일 주소 또는 앱 비밀번호를 확인하세요."]
    except (OSError, TimeoutError) as exc:
        return [], [f"Gmail 연결 실패: {type(exc).__name__}"]
    finally:
        if client is not None:
            try:
                client.close()
            except (imaplib.IMAP4.error, OSError):
                pass
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
