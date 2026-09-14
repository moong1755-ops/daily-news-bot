"""Public metadata checks for shortlisted daily deliveries, never new candidates."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from urllib.parse import urlparse

import requests

from ..config import WEEKLY_EVIDENCE_CONFIG


class ArticleMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        if attrs.get("property") == "og:title":
            self.title = attrs.get("content", "")
        elif attrs.get("property") == "og:description":
            self.description = attrs.get("content", "")


def _matches_title(expected, actual):
    tokens = lambda text: set(re.findall(r"[\w]+", text.casefold()))
    left, right = tokens(expected), tokens(actual)
    return bool(left and right and len(left & right) / len(left) >= 0.7)


def _has_funding_claim(text):
    """Find financing claims that need an explicit evidence status."""
    return bool(re.search(
        r"\b(?:fundrais\w*|funding|financing|series [a-z]|capital round)\b|"
        r"\b(?:raises?|raised|invests?|invested)\b.{0,45}(?:[$€£]\s?[\d,.]+|\d[\d,.]*\s?(?:m|mn|million|b|bn|billion)|fund|round|capital)|"
        r"(?:[$€£]\s?[\d,.]+|\d[\d,.]*\s?(?:m|mn|million|b|bn|billion)).{0,45}\b(?:raises?|raised|invests?|invested)\b|"
        r"투자\s*유치|자금\s*조달|시리즈\s*[a-zA-Z가-힣]",
        str(text or ""),
        re.IGNORECASE,
    ))


def _is_evidence_domain(url):
    parsed = urlparse(str(url or ""))
    hostname = parsed.hostname or ""
    return parsed.scheme == "https" and any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in WEEKLY_EVIDENCE_CONFIG["domains"]
    )


def needs_remote_evidence(article):
    """Limit network checks to financing claims on known ambiguous sources."""
    if not _is_evidence_domain(article.get("url")):
        return False
    text = " ".join(str(article.get(field) or "") for field in (
        "title_orig", "title", "description", "summary",
    ))
    return _has_funding_claim(text)


def _archive_corroborated(article):
    """Trust two independently delivered publisher links as archive corroboration."""
    hosts = {
        (urlparse(str(item.get("url") or "")).hostname or "").removeprefix("www.")
        for item in article.get("weekly_related_links") or []
        if isinstance(item, dict) and item.get("url")
    }
    hosts.discard("")
    return len(hosts) >= 2


def apply_evidence(article, description):
    """Require specific financing language, not ambiguous 'grants approval'."""
    text = description.casefold()
    grant = re.search(
        r"\b(?:grant funding|philanthropic capital|charitable grants?)\b|"
        r"(?:[$€£]\s?[\d,.]+(?:\s?(?:m|mn|million|b|bn|billion))?|"
        r"\d[\d,.]*\s?(?:million|billion))\s+(?:in|as)\s+grants?\b|"
        r"비상환\s*지원금|사회공헌\s*지원금|보조금",
        text,
    )
    mixed = re.search(r"\b(?:equity|series [a-z]|convertible|debt financing)\b|지분투자|전환사채", text)
    if grant and not mixed:
        if article.get("weekly_financing_type") != "grant":
            article["weekly_previous_importance"] = article.get("importance")
            article["weekly_previous_importance_reason"] = article.get("importance_reason")
        article["weekly_financing_type"] = "grant"
        article["major_deal"] = False
        if article.get("importance_reason") == "major_deal":
            try:
                article["importance"] = min(int(article.get("importance") or 0), 2)
            except (TypeError, ValueError):
                article["importance"] = 0
            article["importance_reason"] = ""
    if re.search(r"\b(?:according to .*sources|set to announce|reportedly|in talks)\b|관계자에\s*따르면|발표\s*예정|협상\s*중", text):
        article["weekly_claim_status"] = "reported"


def _check(article, session):
    result = dict(article)
    url = str(article.get("url") or "")
    try:
        response = session.get(url, timeout=WEEKLY_EVIDENCE_CONFIG["timeout_seconds"],
                               allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        metadata = ArticleMetadata()
        metadata.feed(response.text)
        if not metadata.description or not _matches_title(
            str(article.get("title_orig") or article.get("title") or ""), metadata.title
        ):
            raise ValueError("matching article metadata unavailable")
        result["weekly_evidence_description"] = metadata.description
        result["weekly_evidence_url"] = url
        result["weekly_evidence_status"] = "verified_metadata"
        result["weekly_evidence_checked_at"] = datetime.now(timezone.utc).isoformat()
        apply_evidence(result, metadata.description)
    except (requests.RequestException, ValueError) as exc:
        result["weekly_evidence_url"] = url
        result["weekly_evidence_checked_at"] = datetime.now(timezone.utc).isoformat()
        if _archive_corroborated(result):
            result["weekly_evidence_status"] = f"corroborated_daily_archive: {exc}"
            return result
        result["weekly_evidence_status"] = f"unavailable: {exc}"
        title = " ".join(str(article.get(field) or "") for field in (
            "title_orig", "title", "description", "summary",
        ))
        # A funding headline alone does not establish an official announcement.
        if _has_funding_claim(title):
            result["weekly_claim_status"] = "unverified"
    return result


def apply_local_evidence(articles):
    """Apply archive descriptions to every candidate before relative ranking."""
    results = [dict(article) for article in articles]
    for article in results:
        apply_evidence(
            article,
            str(article.get("description") or article.get("summary") or ""),
        )
    return results


def enrich_shortlist(articles, *, session=None):
    results = apply_local_evidence(articles)
    targets = [
        index for index, article in enumerate(results)
        if needs_remote_evidence(article)
    ]
    targets = targets[:WEEKLY_EVIDENCE_CONFIG["max_articles"]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = pool.map(lambda i: _check(results[i], session or requests), targets)
        for index, result in zip(targets, checked):
            results[index] = result
            print(f"🔎 기사 근거 확인: {result.get('source')} · {result.get('weekly_evidence_status')}")
    return results


def guarded_title(article, title):
    if article.get("weekly_financing_type") == "grant":
        title = re.sub(r"투자\s*유치|투자", "지원", title)
        if "지원금" not in title:
            title = "[지원금] " + title
    status = article.get("weekly_claim_status")
    if status == "unverified":
        title = re.sub(r"확정(?:했다|됐다|됨)?", "보도", title)
        title = re.sub(r"\bconfirms?|confirmed\b", "reports", title, flags=re.IGNORECASE)
    label = {"reported": "[보도]", "unverified": "[보도·공식발표 미확인]"}.get(status)
    if label and not title.startswith(label):
        title = label + " " + title
    return title
