"""Extract bounded newsletter analysis without inventing shareable URLs."""

import re
from html import unescape
from urllib.parse import urlsplit

from .newsletter_html import _MailHTML, _Node


def public_story_url(url: str, policy: dict) -> str:
    """Turn a verified publication's Substack post path into its public URL.

    Never include personalized query tokens or decode opaque email trackers.
    This is a path rewrite, not a redirect request or a claim that a page loads.
    """
    publication = policy.get("public_substack_publication")
    base = policy.get("public_substack_base", "")
    if not publication or not base:
        return ""
    try:
        parts = urlsplit(url)
        base_parts = urlsplit(base)
    except ValueError:
        return ""
    match = re.fullmatch(r"/pub/([a-z0-9_-]+)/p/([a-z0-9_-]+)", parts.path, re.I)
    if (parts.scheme != "https" or parts.hostname != "open.substack.com"
            or not match or match.group(1).casefold() != publication.casefold()
            or base_parts.scheme != "https" or not base_parts.hostname
            or base_parts.username or base_parts.password or base_parts.query or base_parts.fragment):
        return ""
    return f"{base.rstrip('/')}/p/{match.group(2)}"


def primary_public_story_url(html: str, title: str, policy: dict) -> str:
    """Find a verified public post link when the title anchor is an app link."""
    title_terms = set(re.findall(r"[a-z0-9]+", title.casefold()))
    for raw in re.findall(r"\bhref=[\"'](https://open\.substack\.com/[^\"']+)[\"']", html or "", re.I):
        public = public_story_url(unescape(raw), policy)
        if not public:
            continue
        slug_terms = set(urlsplit(public).path.rsplit("/", 1)[-1].split("-"))
        if len(title_terms & slug_terms) >= 2:
            return public
    return ""


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # This bound applies to editor context only, never the source headline.
    return text[:limit]


def primary_article_context(html: str, policy: dict, limit: int) -> str:
    """Read a known one-story body container, excluding mail chrome/footer."""
    wanted = set(policy.get("body_container_class", "").split())
    if not wanted or policy.get("body_mode") != "single_article":
        return ""
    parser = _MailHTML()
    parser.feed(html or "")
    for node in parser.nodes:
        classes = set(node.attrs.get("class", "").split())
        if wanted <= classes:
            body = node.text()
            if len(body) >= 300:
                return _short(body, limit)
    return ""


def unlinked_body_notes(html: str, subject: str, policy: dict, limit: int) -> list[tuple[str, str]]:
    """Return evidence only; caller must NEVER send these as article links."""
    mode = policy.get("body_mode")
    if mode not in {"issue_lead", "item_leads"}:
        return []
    parser = _MailHTML()
    parser.feed(html or "")
    if mode == "issue_lead":
        prefix = policy.get("lead_subject_prefix", "")
        marker = policy.get("lead_marker", "")
        if not prefix or not subject.startswith(prefix) or not marker:
            return []
        paragraphs = [node.text() for node in parser.nodes if node.tag == "p"]
        start = next((i for i, text in enumerate(paragraphs) if marker in text), -1)
        if start < 0:
            return []
        lead = " ".join(paragraphs[start:start + 4])
        lead = lead.split("Was this newsletter forwarded to you?", 1)[0]
        title = subject[len(prefix):].strip()
        return [(title, _short(lead, limit))] if len(lead) >= 250 and title else []

    notes = []
    for node in parser.nodes:
        if node.tag != "li":
            continue
        first = next((child for child in node.children if isinstance(child, _Node)), None)
        if first is None or first.tag not in {"strong", "b", "span"}:
            continue
        title_node = first
        if first.tag == "span":
            title_node = next(
                (child for child in first.children
                 if isinstance(child, _Node) and child.tag in {"strong", "b"}),
                None,
            )
        if title_node is None:
            continue
        heading = title_node.text().strip(" .")
        body = node.text()
        if len(heading) < 18 or len(body) < len(heading) + 180:
            continue
        if heading.casefold() in {value.casefold() for value in policy.get("body_skip_titles", ())}:
            continue
        if any(phrase in heading.casefold() for phrase in (
            "rsvp", "subscribe", "join us", "register now", "podcast", "webinar",
        )):
            continue
        notes.append((heading, _short(body[len(heading):].strip(" ."), limit)))
        if len(notes) >= policy.get("body_max_notes", 3):
            break
    return notes
