"""Publisher provenance, independent of category feeds and portal hosting."""

from functools import lru_cache
import re
from urllib.parse import unquote, urlsplit

from ..config import (
    PUBLISHER_AGGREGATOR_DOMAINS, PUBLISHER_ALIASES, RSS_SOURCE_METADATA,
)


def _values(value):
    return value if isinstance(value, (list, tuple)) else [value] if value else []


def _name(value) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", str(value or "")).casefold()


def _domain(value) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _is_portal(domain: str) -> bool:
    return any(domain == host or domain.endswith("." + host)
               for host in PUBLISHER_AGGREGATOR_DOMAINS)


@lru_cache(maxsize=1)
def _registry() -> dict[str, str]:
    registry = {}
    for name, metadata in RSS_SOURCE_METADATA.items():
        url = str(metadata.get("url") or "")
        domain = _domain(url)
        if _is_portal(domain):
            match = re.search(r"site:([\w.-]+)", unquote(url))
            domain = match.group(1) if match else ""
        registry[_name(name)] = domain or "source:" + _name(name)
    registry.update({_name(name): domain for name, domain in PUBLISHER_ALIASES.items()})
    for domain in list(registry.values()):
        if not domain.startswith("source:"):
            registry[_name(domain)] = domain
    return registry


def publisher_id(source: str, url: str = "") -> str:
    """Known bylines win over syndication hosts; anonymous portals count zero."""
    name, domain = _name(source), _domain(url)
    registry = _registry()
    if name in registry:
        return registry[name]
    if name in {"googlenews", "구글뉴스", "네이버", "다음", "naver", "daum"}:
        name = ""
    if name and _is_portal(name):
        name = ""
    if domain and not _is_portal(domain):
        for known in registry.values():
            if domain == known or domain.endswith("." + known):
                return known
        return domain
    return "source:" + name if name else ""


def coverage_records(article: dict) -> list[dict]:
    """Retain each original title/source/URL together, even across repeat merges."""
    existing = article.get("coverage_sources")
    if isinstance(existing, list) and existing:
        return [dict(record) for record in existing if isinstance(record, dict)]
    sources = _values(article.get("source"))
    links = _values(article.get("link"))
    title = str(article.get("title_orig") or article.get("title") or "")
    # Legacy merged source/link lists have no positional relationship. Never zip
    # them: resolve known aliases but do not attach an invented publisher URL.
    if len(sources) > 1:
        pairs = [(source, "") for source in sources]
    elif sources:
        pairs = [(sources[0], links[0] if links else "")]
    else:
        pairs = [("", link) for link in links]
    return [
        {"publisher": identity, "source": str(source), "url": str(url), "title": title}
        for source, url in pairs
        if (identity := publisher_id(str(source), str(url)))
    ]


def merge_coverage(articles: list[dict]) -> list[dict]:
    records, seen = [], set()
    for article in articles:
        for record in coverage_records(article):
            key = tuple(str(record.get(field) or "") for field in ("publisher", "url", "title"))
            if key not in seen:
                seen.add(key)
                records.append(record)
    return records
