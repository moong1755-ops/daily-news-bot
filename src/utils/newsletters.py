"""Newsletter provenance and review policy, shared without mail/LLM dependencies."""

import re
from datetime import date

from ..config import NEWSLETTER_SOURCES


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def identify_newsletter(sender_name: str, address: str, subject: str) -> str:
    """Match sender domain/address before stream names; not email authentication.

    Shared sending services require an exact configured address. Display names
    alone never grant required-review status. Mail outside the registry still
    uses the ordinary newsletter pipeline.
    """
    address = address.strip().casefold()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    name, subject = _normalized(sender_name), _normalized(subject)
    for key, policy in NEWSLETTER_SOURCES.items():
        if address in policy.get("addresses", ()):
            return key
        if not any(domain == host or domain.endswith("." + host)
                   for host in policy.get("domains", ())):
            continue
        # A domain containing several newsletter streams needs an extra match.
        terms = policy.get("subject_terms", ())
        if terms and not (
            any(_normalized(alias) in name for alias in policy.get("aliases", ()))
            or any(re.search(r"\b" + re.escape(term) + r"\b", subject, re.I)
                   for term in terms)
        ):
            continue
        return key
    return ""


def newsletter_sources(article: dict) -> list[str]:
    """Keep required-source provenance even if RSS wins duplicate selection."""
    keys = article.get("newsletter_sources") or [article.get("newsletter_source")]
    return list(dict.fromkeys(key for key in keys if key in NEWSLETTER_SOURCES))


def newsletter_topics(article: dict) -> list[str]:
    return list(dict.fromkeys(
        topic for key in newsletter_sources(article)
        for topic in NEWSLETTER_SOURCES[key].get("topics", ())
    ))


_MATCH_STOPWORDS = {
    "about", "after", "among", "ahead", "book", "dealbook", "future",
    "investors", "investment", "market", "markets", "news", "report",
    "company", "companies", "global", "major", "impact", "week", "today",
    "climate", "finance", "policy", "energy", "capital", "private", "public",
    "technology", "industry", "investing", "venture", "startup", "startups",
    "the", "and", "for", "from", "with", "that", "this", "their",
}
_CAPITAL_EVENT = re.compile(r"\b(?:i\.?p\.?o\.?|public offering|listing|acquisition|merger|buyout|funding|raises?)\b", re.I)


def _terms(value: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z][a-z0-9]+|[가-힣]{2,}", (value or "").casefold())
        if len(word) >= 5 and word not in _MATCH_STOPWORDS
    }


def _same_story(note: dict, article: dict) -> bool:
    try:
        if note.get("date") and article.get("date") and abs((
            date.fromisoformat(str(note["date"])[:10])
            - date.fromisoformat(str(article["date"])[:10])
        ).days) > 3:
            return False
    except ValueError:
        return False
    title = str(article.get("title_orig") or article.get("title") or "")
    shared = _terms(note.get("title", "")) & _terms(title)
    if len(shared) >= 3 and any(len(word) >= 8 for word in shared):
        return True
    if len(shared) != 1 or max(len(word) for word in shared) < 7:
        return False
    # One named entity alone is not an event match: the newsletter's IPO must
    # not be attached to the same company's unrelated product or earnings news.
    return bool(_CAPITAL_EVENT.search(title) and _CAPITAL_EVENT.search(
        f"{note.get('title', '')} {note.get('description', '')}"
    ))


def attach_body_notes(articles: list[dict]) -> tuple[int, int]:
    """Add unlinked issue analysis only to an independently linked same event.

    Notes never become coverage records, representative URLs, or extra
    publishers; editor context states their source explicitly.
    """
    notes = [article for article in articles if article.get("newsletter_body_only")]
    linked = [article for article in articles if article.get("link") and not article.get("newsletter_body_only")]
    attached = 0
    for note in notes:
        matched = False
        for article in linked:
            if not _same_story(note, article):
                continue
            existing = article.setdefault("newsletter_body_notes", [])
            if len(existing) >= 2:
                continue
            record = {
                "source": note.get("source", ""),
                "source_key": note.get("newsletter_source", ""),
                "title": note.get("title", ""),
                "excerpt": note.get("description", "")[:300],
            }
            if record not in existing:
                existing.append(record)
                attached += 1
            matched = True
        note["filter_reason"] = (
            "newsletter_body_attached_to_linked_article" if matched
            else "newsletter_body_no_verified_public_link"
        )
    return len(notes), attached
