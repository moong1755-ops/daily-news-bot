"""Validate direct attribution links offline. Never contact email trackers."""

from urllib.parse import urlsplit, urlunsplit


def _host(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in (None, 443)):
            return ""
        return (parts.hostname or "").casefold()
    except ValueError:
        return ""


def _matches(host: str, domains: tuple) -> bool:
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in domains)


def resolve_attributed_url(url: str, domains: tuple) -> str:
    """Accept only a direct publisher article path, with zero network requests."""
    if not _matches(_host(url), domains):
        return ""
    parts = urlsplit(url)
    if parts.path in ("", "/") or any(
        token in parts.path.lower() for token in ("/subscribe", "/login", "/account", "/newsletters")
    ):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
