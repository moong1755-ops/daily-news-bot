"""Conservative IPO identity and outcome rules shared by deduplication stages."""

import re
from datetime import date


_IPO = re.compile(
    r"\b(?:ipo|initial public offering|go(?:ing)? public|public listing)\b|기업공개|상장(?!사)",
    re.IGNORECASE,
)
_GENERIC_TOKENS = set("""
    ipo initial public offering listing listed lists nasdaq nyse investment invest
    invests investing funding financing round series talks reported selection selects
    selected potential ahead mega stake deal files filed filing plans planned plan
    analysis economics economic financial financials revenue revenues sales losses
    profit profits profitability valuation market investors shares stock ai company
    growth surge strategy enterprise customer customers capital outlook report
    withdrawal withdrawn withdraw withdraws scrapped cancelled canceled cancellation
    postponed delayed delay relaunch relaunches relaunched restart restarted resume
    resumes resumed refiles refiled completed completion debut priced pricing
    approval approved approves regulatory regulator regulators africa asia europe
    global international domestic
    process offer offers share
    기업공개 상장 신청 추진 계획 철회 취소 연기 재추진 분석 투자
""".split())
_STAGES = (
    ("withdrawn", r"\b(?:withdraw(?:s|n|al)?|scrapp?ed|cancel(?:s|led|ed|lation)?|abandon(?:s|ed)?|called off)\b|철회|취소|무산"),
    ("delayed", r"\b(?:delay(?:s|ed)?|postpon(?:e|es|ed|ement)|shelv(?:e|es|ed))\b|연기|보류"),
    ("restarted", r"\b(?:relaunch(?:es|ed)?|restart(?:s|ed)?|resum(?:e|es|ed)|refil(?:e|es|ed))\b|재추진|재개|재신청"),
    ("completed", r"\b(?:completes?|completed|debut(?:s|ed)?|priced)\b|상장\s*완료|공모가\s*확정"),
)


def _title(article: dict) -> str:
    # Translation runs after selection. Prefer the preserved original if present.
    return str(article.get("title_orig") or article.get("title") or "")


def is_ipo_story(article: dict) -> bool:
    return bool(_IPO.search(_title(article)))


def ipo_target(article: dict) -> str:
    """Require a company token in both the event key and headline, regardless of order."""
    if not is_ipo_story(article):
        return ""
    title = _title(article).casefold()
    key_tokens = set(re.findall(r"[a-z0-9가-힣]+", str(article.get("editor_event_key") or "").casefold()))
    title_tokens = set(re.findall(r"[a-z0-9가-힣]+", title))
    candidates = {
        token for token in key_tokens & title_tokens
        if len(token) >= 2 and not token.isdigit() and token not in _GENERIC_TOKENS
    }
    if len(candidates) == 1:
        return next(iter(candidates))
    # With several companies, distinguish the IPO target from its investors:
    # "Nvidia invests in Anthropic's mega IPO" must not be grouped under Nvidia.
    explicit = {
        token for token in candidates
        if re.search(
            rf"\b{re.escape(token)}(?:['’]s)?\s+"
            r"(?:(?:planned|proposed|upcoming|potential|mega)\s+)*"
            r"(?:ipo|initial public offering|public listing)\b",
            title,
        )
    }
    return next(iter(explicit)) if len(explicit) == 1 else ""


def ipo_stage(article: dict) -> str:
    if not is_ipo_story(article):
        return ""
    title = _title(article)
    # A prior withdrawal in a background clause must not override a relaunch.
    lead = re.split(r"\b(?:after|following|previously)\b", title, maxsplit=1, flags=re.I)[0]
    korean_tail = re.split(r"(?:이후|\s후)\s*", title)[-1]
    contexts = [korean_tail, lead, title] if korean_tail != title else [lead, title]
    for context in contexts:
        stages = {stage for stage, pattern in _STAGES if re.search(pattern, context, re.I)}
        if not stages:
            continue
        stage = next(iter(stages)) if len(stages) == 1 else "ambiguous"
        uncertain = re.search(
            r"\b(?:may|might|could|consider\w*|rumou?r\w*|not|denies?|denied)\b|가능성|검토|설\b|부인|아니",
            context, re.I,
        )
        return f"unconfirmed_{stage}" if uncertain else stage
    return "active"


def ipo_events_conflict(left: dict, right: dict) -> bool:
    """Do not erase different companies or changing outcomes in an earlier stage."""
    left_stage, right_stage = ipo_stage(left), ipo_stage(right)
    if not left_stage or not right_stage:
        return False
    left_target, right_target = ipo_target(left), ipo_target(right)
    return bool(
        (left_target and right_target and left_target != right_target)
        or left_stage != right_stage
    )


def _published_date(article: dict):
    # Mail receipt dates cannot establish the chronology of the underlying event.
    if article.get("date_basis") == "newsletter_date":
        return None
    if ipo_stage(article).startswith("unconfirmed_") or ipo_stage(article) == "ambiguous":
        return None
    try:
        return date.fromisoformat(str(article.get("date") or "")[:10])
    except ValueError:
        return None


def collapse_ipo_stories(articles: list[dict], priority_key) -> list[dict]:
    """Choose one per company/outcome; only supersede older outcomes with clear dates."""
    groups: dict[str, list[dict]] = {}
    for article in articles:
        target = ipo_target(article)
        if target:
            groups.setdefault(target, []).append(article)

    dropped = set()
    for group in groups.values():
        stages: dict[str, list[dict]] = {}
        for article in group:
            stages.setdefault(ipo_stage(article), []).append(article)
        retained_stages = set(stages)
        dates = [_published_date(article) for article in group]
        if len(stages) > 1 and all(value is not None for value in dates):
            newest = max(dates)
            latest_stages = {
                ipo_stage(article) for article, published in zip(group, dates)
                if published == newest
            }
            if len(latest_stages) == 1:
                retained_stages = latest_stages
        for stage, candidates in stages.items():
            representative = max(candidates, key=priority_key) if stage in retained_stages else None
            for article in candidates:
                if article is not representative:
                    article["selection_dedup_reason"] = (
                        "same_company_ipo_cluster" if representative else "superseded_ipo_status"
                    )
                    dropped.add(id(article))
    return [article for article in articles if id(article) not in dropped]
