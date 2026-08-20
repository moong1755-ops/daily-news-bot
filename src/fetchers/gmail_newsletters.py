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
GMAIL_MAX_EMAILS = _positive_env_int("GMAIL_MAX_EMAILS", 20)
GMAIL_LINKS_PER_EMAIL = _positive_env_int("GMAIL_LINKS_PER_EMAIL", 8)

_SKIP_URL_MARKERS = (
    "unsubscribe", "subscription-preferences", "manage-preferences",
    "email-preferences", "newsletter/settings", "view-in-browser",
    "forward-to-a-friend", "mailto:",
)
_SKIP_LINK_TEXTS = {
    "read more", "learn more", "click here", "view online", "view in browser",
    "subscribe", "unsubscribe", "manage preferences", "privacy policy",
    "terms of use", "facebook", "instagram", "linkedin", "x", "twitter",
}

# 정확히 일치하는 문구만 걸러내면 매체마다 표현이 달라 계속 새는데, 실제로
# Bloomberg 의 "Read in browser" 가 기사 제목으로 발송된 적이 있다. 문구 대신
# 뉴스레터 상용구에 공통으로 나타나는 조각으로 판단한다.
_SKIP_LINK_FRAGMENTS = (
    "in browser", "view online", "web version", "read online",
    "unsubscribe", "manage preference", "email preference", "privacy",
    "advertise", "sponsor", "download the app", "follow us", "contact us",
    "브라우저에서", "구독 취소", "수신 거부",
)


def _is_boilerplate_anchor(anchor_text: str) -> bool:
    """뉴스레터 상용구 링크인지 판단한다(기사 제목이 아닌 것)."""
    normalized = re.sub(r"\s+", " ", anchor_text or "").strip().lower()
    if not normalized or len(normalized) < 8:
        return True
    if normalized in _SKIP_LINK_TEXTS:
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


_MAX_HEADLINE_CHARS = 120


def _headline_from_anchor(anchor_text: str) -> str:
    """앵커 텍스트에서 제목 부분만 남긴다.

    뉴스레터는 제목과 본문 첫 문단을 한 링크로 묶는 일이 많아, 그대로 쓰면
    한 줄이 200자를 넘는다("Gates-Backed TerraPower to Announce Second Nuke
    Plant This Year TerraPower LLC, the only company ..."). 본문은 대개
    문장으로 이어지므로 첫 문장 경계에서 끊고, 그래도 길면 단어 단위로 자른다.
    """
    text = re.sub(r"\s+", " ", anchor_text or "").strip()
    if len(text) <= _MAX_HEADLINE_CHARS:
        return text

    sentence_end = re.search(r"(?<=[.!?])\s+[A-Z가-힣]", text)
    if sentence_end and sentence_end.start() + 1 <= _MAX_HEADLINE_CHARS:
        return text[: sentence_end.start() + 1].strip()

    clipped = text[:_MAX_HEADLINE_CHARS].rsplit(" ", 1)[0].strip()
    return (clipped or text[:_MAX_HEADLINE_CHARS].strip()) + "…"


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
        anchor_text = _clean_text(anchor_html)
        if _is_boilerplate_anchor(anchor_text) or not _looks_like_headline(anchor_text):
            continue
        candidates.append((url, anchor_text))
        seen_urls.add(url)

    bare_url_pattern = re.compile(r"https?://[^\s<>\"'{}|\\^`\[\]]+")
    for raw_url in bare_url_pattern.findall(plain_text or ""):
        url = unescape(raw_url).rstrip(".,);]")
        if url in seen_urls or not _is_usable_article_url(url):
            continue
        candidates.append((url, ""))
        seen_urls.add(url)

    return candidates[:GMAIL_LINKS_PER_EMAIL]


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


def _parse_email(raw_message: bytes) -> List[dict]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = _clean_subject(str(message.get("Subject", "Newsletter")))
    sender_name, sender_address = parseaddr(str(message.get("From", "")))
    sender = _clean_text(sender_name) or sender_address.split("@")[0] or "Newsletter"
    html_text, plain_text = _extract_message_bodies(message)
    candidates = _extract_link_candidates(html_text, plain_text)
    if not candidates:
        return []

    plain_summary = _clean_text(plain_text)[:350]
    description = f"Newsletter: {subject}"
    if plain_summary:
        description = f"{description}. {plain_summary}"

    published_date = _message_date(message)
    articles = []
    for url, anchor_text in candidates:
        articles.append({
            "title": _headline_from_anchor(anchor_text) or subject,
            "link": url,
            "source": sender,
            "feed": "Gmail Newsletters",
            "date": published_date,
            "description": description,
            "region": "global",
        })
    return articles


def _gmail_raw_query() -> str:
    parts = [
        part
        for part in (GMAIL_NEWSLETTER_QUERY, f"newer_than:{GMAIL_LOOKBACK_DAYS}d")
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
                    articles.extend(_parse_email(raw_message))
            except Exception as exc:
                errors.append(f"뉴스레터 메일 해석 실패: {type(exc).__name__}")

        print(
            f"✅ Gmail 뉴스레터: 최근 메일 {len(message_uids)}건에서 "
            f"기사 링크 {len(articles)}건 수집"
        )
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
