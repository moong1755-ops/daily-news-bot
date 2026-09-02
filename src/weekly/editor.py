"""Build the weekly briefing's three-line impact-VC overview."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..config import CATEGORIES, WEEKLY_BRIEFING_CONFIG
from ..editorial_review import importance as _editorial_importance
from ..processor.reranker import generate_editor_json


@dataclass(frozen=True)
class WeeklyHeadlines:
    lines: tuple[str, ...]
    model: str | None
    used_fallback: bool


def _score(article: dict) -> float:
    try:
        return float(article.get("weekly_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _title(article: dict) -> str:
    return str(article.get("title") or article.get("title_orig") or "").strip()


def _fallback_lines(articles: list[dict], limit: int) -> tuple[str, ...]:
    """Keep impact visible while covering more than one category when possible."""
    ranked = sorted(
        articles,
        key=lambda article: (_editorial_importance(article), _score(article)),
        reverse=True,
    )
    chosen: list[dict] = ranked[:1]

    represented = {article.get("category") for article in chosen}
    for article in ranked:
        if len(chosen) >= limit:
            break
        if article in chosen or article.get("category") in represented:
            continue
        chosen.append(article)
        represented.add(article.get("category"))
    for article in ranked:
        if len(chosen) >= limit:
            break
        if article not in chosen:
            chosen.append(article)

    return tuple(_title(article).rstrip(".。") for article in chosen if _title(article))


def _prompt(articles: list[dict], limit: int) -> str:
    candidates = [
        {
            "id": index + 1,
            "category": article.get("category"),
            "title": _title(article),
            "source": article.get("source"),
            "status": article.get("deal_status"),
            "signals": article.get("weekly_rank_reasons") or [],
            "weekly_score": article.get("weekly_score"),
            "importance": article.get("importance") or 0,
            "importance_reason": article.get("importance_reason") or "",
            "alt_subtype": article.get("alt_subtype") or "",
            "summary": str(article.get("description") or article.get("summary") or "")[:240],
        }
        for index, article in enumerate(articles)
    ]
    return f"""
당신은 임팩트 VC 투자심사역을 위한 주간 뉴스 편집장이다.
아래는 이미 중복 제거와 상대 순위 선별을 마친 기사다.

월요일 아침 임팩트 VC 투자심사역이 지난주를 이해하기 위해 기억해야 할 가장 중요한 변화를 정확히 {limit}줄 이내로 정리하라.
- 각 줄은 하나의 독립된 사건·변화만 다룬다. 서로 무관한 기사를 한 줄로 묶지 않는다.
- 같은 사건의 후속 보도가 여러 건이면 하나의 변화로만 요약한다.
- 다음 순서로 판단한다: ① 투자환경·정책·규제 변화 ② 시장 전체의 자본공급 변화 ③ 시장을 대표하는 투자·M&A·IPO ④ 산업구조 변화 ⑤ 투자판단을 바꾸는 새로운 증거.
- Daily importance는 3>2>1 순으로 보되 주간 전체 맥락에서 다시 비교한다. importance_reason은 위 다섯 판단축의 대표 이유다.
- 모태펀드·정책금융·연기금 같은 systemic_capital은 개별 기업의 평범한 M&A보다 주간 중요도가 높을 수 있다.
- major_deal은 단순 금액이 아니라 시장의 기준점을 보여주는 거래인지 본다.
- weekly_score는 같은 중요도 기사 사이의 참고치일 뿐 최종 판단 근거로 그대로 복사하지 않는다.
- 여러 기사가 같은 방향의 명확한 시장 변화를 독립적으로 뒷받침하면 한 줄의 해석으로 묶을 수 있다. 다만 제목·요약에 없는 인과관계는 만들지 않는다.
- 임팩트 투자 기회·리스크가 주간 핵심급이면 우선 포함하되 약한 사건을 억지로 넣지 않는다.
- 본 결정이 있는 사건에서는 전망·관계자 발언·시장 반응보다 본 결정을 먼저 쓴다.
- 기사에 없는 사실은 만들지 않는다.
- 루머·전망·검토 단계는 반드시 가능성 또는 전망임을 드러낸다.
- 행사·홍보 문구와 일반론은 쓰지 않는다.
- 뉴스 해설문이 아니라 15~32자 안팎의 보고서 제목형 문구로 쓴다.
- 핵심 주체와 변화만 남기고 짧고 단정한 명사형·단문형 어미를 사용한다.
- '~했습니다', '~로 나타났습니다', '~하고 있습니다' 같은 긴 서술형을 쓰지 않는다.
- 문장 끝 마침표를 붙이지 않는다.
- 좋은 예: '미 연준, 금리 인상 기조 재확인', 'AI 인프라 투자 경쟁 확대',
  '국내 Pre-IPO 대형 딜 증가'.
- JSON 이외의 문장은 출력하지 않는다.

출력 형식:
{{"lines": ["첫째 줄", "둘째 줄", "셋째 줄"]}}

기사:
{json.dumps(candidates, ensure_ascii=False)}
""".strip()


def _parse_lines(raw: str | None, limit: int) -> tuple[str, ...]:
    if not raw:
        return ()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return ()
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(lines, list):
        return ()
    cleaned = tuple(
        str(line).strip().lstrip("-• ")
        for line in lines[:limit]
        if isinstance(line, str) and line.strip()
    )
    return cleaned


def build_weekly_headlines(articles: tuple[dict, ...] | list[dict]) -> WeeklyHeadlines:
    limit = int(WEEKLY_BRIEFING_CONFIG["headline_summary_max"])
    article_list = list(articles)
    if not article_list or limit < 1:
        return WeeklyHeadlines((), None, True)

    raw, model = generate_editor_json(_prompt(article_list, limit), timeout=30)
    lines = _parse_lines(raw, limit)
    if lines:
        return WeeklyHeadlines(lines, model, False)
    return WeeklyHeadlines(_fallback_lines(article_list, limit), None, True)
