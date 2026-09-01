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
    except ValueError:
        try:
            parsed = date.fromisoformat(raw[:10])
        except ValueError:
            return raw
    return parsed.strftime("%m.%d")


def _article_date(article: dict) -> str:
    for field in ("weekly_last_seen", "published", "_archive_edition_date"):
        label = _date_label(article.get(field))
        if label:
            return label
    return ""


def _rich_article(article: dict) -> dict:
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
    if url:
        elements.append({"type": "link", "url": url, "text": title})
    else:
        elements.append({"type": "text", "text": title})
    meta = ", ".join(value for value in (source, published) if value)
    if meta:
        elements.append({"type": "text", "text": f" ({meta})"})
    return {"type": "rich_text_section", "elements": elements}


def _rich_list(articles: tuple[dict, ...] | list[dict]) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_list",
                "style": "bullet",
                "indent": 0,
                "elements": [_rich_article(article) for article in articles],
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
        return f"{latest.value:,.2f}"
    return f"${latest.value:,.2f}"


def _format_change(snapshot: MarketSnapshot) -> str:
    if snapshot.change is None:
        return ""
    arrow = "▲" if snapshot.change > 0 else "▼" if snapshot.change < 0 else "→"
    magnitude = abs(snapshot.change)
    suffix = "bp" if snapshot.change_unit == "basis_points" else "%"
    return f"{arrow}{magnitude:.1f}{suffix}"


def _market_block(markets: tuple[MarketSnapshot, ...]) -> dict:
    lines = []
    for snapshot in markets:
        value = _format_number(snapshot)
        change = _format_change(snapshot)
        suffix = "  ".join(part for part in (change, snapshot.sparkline) if part)
        lines.append(f"{snapshot.label}: {value}" + (f"  {suffix}" if suffix else ""))
    text = "\n".join(lines) if lines else "시장지표 수집 결과 없음"
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"```{text}```"}}


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

    if category.startswith(("📈", "🌐")):
        global_articles = tuple(article for article in articles if article.get("region") != "korea")
        korea_articles = tuple(article for article in articles if article.get("region") == "korea")
        if global_articles:
            blocks.extend((_heading("해외"), _rich_list(global_articles)))
        if korea_articles:
            blocks.extend((_heading("국내"), _rich_list(korea_articles)))
        return blocks

    blocks.append(_rich_list(articles))
    return blocks


def _plain_text(
    start_date: date,
    end_date: date,
    headlines: WeeklyHeadlines,
    selection: WeeklySelection,
    markets: tuple[MarketSnapshot, ...],
) -> str:
    lines = [f"VC 주간 브리핑 | {start_date:%Y.%m.%d}–{end_date:%m.%d}", "", "한 주 한눈에"]
    lines.extend(f"{index}. {line}" for index, line in enumerate(headlines.lines, 1))
    lines.extend(("", "시장지표"))
    for market in markets:
        lines.append(
            f"{market.label}: {_format_number(market)} "
            f"{_format_change(market)} {market.sparkline}".rstrip()
        )
    for category in CATEGORIES:
        lines.extend(("", category))
        articles = selection.by_category.get(category, ())
        if not articles:
            lines.append("(이번 주 해당 분야 주요 뉴스 없음)")
            continue
        for article in articles:
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
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"임팩트 VC 투자심사역용 · {selection.candidate_count}건 검토 → {len(selection.articles)}건 선정",
            }],
        },
        _heading("한 주 한눈에"),
        _headline_block(headlines),
        {"type": "divider"},
        _heading("시장지표 · 주간 변화 / 최근 5개 관측치"),
        _market_block(markets),
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
