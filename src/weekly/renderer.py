"""Render one readable Slack Block Kit message for the weekly briefing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..config import CATEGORIES
from .editor import WeeklyHeadlines
from .market_data import MarketSnapshot
from .selector import WeeklySelection


@dataclass(frozen=True)
class WeeklySlackMessage:
    notification_text: str
    blocks: tuple[dict, ...]
    plain_text: str


def _date_label(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        return parsed.strftime("%m.%d")
    except ValueError:
        pass
    for fmt in ("%y.%m.%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m.%d")
        except ValueError:
            continue
    try:
        return date.fromisoformat(raw[:10]).strftime("%m.%d")
    except ValueError:
        return raw


def _article_date(article: dict) -> str:
    for field in ("date", "published", "weekly_last_seen", "_archive_edition_date"):
        label = _date_label(article.get(field))
        if label:
            return label
    return ""


def _rich_article(article: dict, *, bold: bool = False) -> dict:
    title = str(article.get("title") or article.get("title_orig") or "제목 없음")
    url = str(
        article.get("link")
        or article.get("normalized_url")
        or article.get("url")
        or ""
    )
    source = str(article.get("source") or "출처 미상")
    published = _article_date(article)
    elements = []
    title_element = {"type": "link", "url": url, "text": title} if url else {
        "type": "text",
        "text": title,
    }
    if bold:
        title_element["style"] = {"bold": True}
    elements.append(title_element)
    meta = ", ".join(value for value in (source, published) if value)
    if meta:
        elements.append({"type": "text", "text": f" ({meta})"})
    return {"type": "rich_text_section", "elements": elements}


def _rich_list(
    articles: tuple[dict, ...] | list[dict],
    *,
    highlight_article: dict | None = None,
) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_list",
                "style": "bullet",
                "indent": 0,
                "elements": [
                    _rich_article(article, bold=article is highlight_article)
                    for article in articles
                ],
            }
        ],
    }


def _headline_block(headlines: WeeklyHeadlines) -> dict:
    if not headlines.lines:
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_(이번 주 핵심 요약 없음)_"},
        }
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_list",
                "style": "ordered",
                "indent": 0,
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": line}],
                    }
                    for line in headlines.lines
                ],
            }
        ],
    }


def _format_number(snapshot: MarketSnapshot) -> str:
    latest = snapshot.latest
    if latest is None:
        return "수집 지연"
    if snapshot.key == "us_10y":
        return f"{latest.value:.2f}%"
    if snapshot.key in {"kospi", "kosdaq", "sp500", "nasdaq", "usd_krw"}:
        return f"{latest.value:,.1f}"
    return f"${latest.value:,.2f}"


def _format_change(snapshot: MarketSnapshot) -> str:
    if snapshot.change is None:
        return ""
    arrow = "▲" if snapshot.change > 0 else "▼" if snapshot.change < 0 else "→"
    magnitude = abs(snapshot.change)
    if snapshot.change_unit == "basis_points":
        formatted = f"{magnitude:.0f}" if magnitude.is_integer() else f"{magnitude:.1f}"
        return f"{arrow}{formatted}bp"
    return f"{arrow}{magnitude:.1f}%"


def _comparison_label(
    snapshot: MarketSnapshot,
    freshest_observation: date | None = None,
) -> str:
    latest = snapshot.latest
    comparison = snapshot.comparison
    if latest is None or comparison is None:
        return ""
    label = f"· {comparison.observed_on:%m.%d}→{latest.observed_on:%m.%d}"
    if (
        freshest_observation is not None
        and (freshest_observation - latest.observed_on).days >= 2
    ):
        label += " · 데이터 갱신 지연"
    return label


def _market_block(markets: tuple[MarketSnapshot, ...]) -> dict:
    lines = []
    freshest_observation = max(
        (snapshot.latest.observed_on for snapshot in markets if snapshot.latest),
        default=None,
    )
    for snapshot in markets:
        value = _format_number(snapshot)
        change = _format_change(snapshot)
        linked_text = " ".join(
            part for part in (snapshot.label, value, change) if part
        )
        if snapshot.source_url:
            linked_text = f"<{snapshot.source_url}|{linked_text}>"
        comparison = _comparison_label(snapshot, freshest_observation)
        lines.append(" ".join(part for part in (linked_text, comparison) if part))
    text = "\n".join(lines) if lines else "시장지표 수집 결과 없음"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _heading(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}}


def _category_blocks(category: str, articles: tuple[dict, ...]) -> list[dict]:
    blocks = [_heading(category)]
    if not articles:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_(이번 주 해당 분야 주요 뉴스 없음)_"},
        })
        return blocks

    highlight_article = articles[0]
    if category.startswith(("📈", "🌐")):
        global_articles = tuple(article for article in articles if article.get("region") != "korea")
        korea_articles = tuple(article for article in articles if article.get("region") == "korea")
        if global_articles:
            blocks.extend((
                _heading("해외"),
                _rich_list(global_articles, highlight_article=highlight_article),
            ))
        if korea_articles:
            blocks.extend((
                _heading("국내"),
                _rich_list(korea_articles, highlight_article=highlight_article),
            ))
        return blocks

    blocks.append(_rich_list(articles, highlight_article=highlight_article))
    return blocks


def _plain_text(
    start_date: date,
    end_date: date,
    headlines: WeeklyHeadlines,
    selection: WeeklySelection,
    markets: tuple[MarketSnapshot, ...],
) -> str:
    lines = [
        f"VC 주간 브리핑 | {start_date:%Y.%m.%d}–{end_date:%m.%d}",
        "",
        "시장지표 · 전주 마지막 거래일 대비",
    ]
    freshest_observation = max(
        (market.latest.observed_on for market in markets if market.latest),
        default=None,
    )
    for market in markets:
        source = f" {market.source_url}" if market.source_url else ""
        lines.append(
            f"{market.label} {_format_number(market)} {_format_change(market)} "
            f"{_comparison_label(market, freshest_observation)}{source}".rstrip()
        )
    lines.extend(("", "한 주 한눈에"))
    lines.extend(f"{index}. {line}" for index, line in enumerate(headlines.lines, 1))
    for category in CATEGORIES:
        lines.extend(("", category))
        articles = selection.by_category.get(category, ())
        if not articles:
            lines.append("(이번 주 해당 분야 주요 뉴스 없음)")
            continue
        regions = ("global", "korea") if category.startswith(("📈", "🌐")) else (None,)
        for region in regions:
            region_articles = tuple(
                article for article in articles
                if region is None or article.get("region") == region
            )
            if not region_articles:
                continue
            if region is not None:
                lines.append("해외" if region == "global" else "국내")
            for article in region_articles:
                title = str(article.get("title") or article.get("title_orig") or "제목 없음")
                url = str(
                    article.get("link")
                    or article.get("normalized_url")
                    or article.get("url")
                    or ""
                )
                lines.append(f"- {title} {url}".rstrip())
    return "\n".join(lines)


def render_weekly_briefing(
    start_date: date,
    end_date: date,
    headlines: WeeklyHeadlines,
    selection: WeeklySelection,
    markets: tuple[MarketSnapshot, ...],
) -> WeeklySlackMessage:
    title = f"VC 주간 브리핑 | {start_date:%Y.%m.%d}–{end_date:%m.%d}"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        _heading("시장지표 · 전주 마지막 거래일 대비"),
        _market_block(markets),
        {"type": "divider"},
        _heading("한 주 한눈에"),
        _headline_block(headlines),
        {"type": "divider"},
    ]
    for index, category in enumerate(CATEGORIES):
        blocks.extend(_category_blocks(category, selection.by_category.get(category, ())))
        if index < len(CATEGORIES) - 1:
            blocks.append({"type": "divider"})

    if len(blocks) > 50:
        raise ValueError(f"Slack Block Kit 한도 초과: {len(blocks)}개")
    return WeeklySlackMessage(
        notification_text=f"{title} · 주요 기사 {len(selection.articles)}건",
        blocks=tuple(blocks),
        plain_text=_plain_text(start_date, end_date, headlines, selection, markets),
    )
