"""LLM 편집 게이트 — 기사별로 '나갈 가치가 있는가'를 판정한다.

키워드로는 채용공고와 인사 뉴스를 구분할 수 없다. "Senior Investment Officer"
와 "OpenAI CIO 사임" 은 같은 단어를 쓰지만 하나는 구인글이고 하나는 기사다.
제목을 읽어야 아는 판단이므로 LLM 에 맡기고, 코드는 수집·중복 제거·개수 제한
처럼 기계적인 일만 맡는다.

호출은 배치로 묶어 후보 BATCH_SIZE 건당 1회. LLM_MODE=cache 면 같은 후보
집합에 대해 두 번째 실행부터는 호출이 일어나지 않는다(tools/run_eval.py 로
프롬프트를 반복 조정할 때 쿼터를 쓰지 않기 위한 것).

LLM 을 쓸 수 없으면 review() 가 None 을 돌려주고, 호출한 쪽은 기존 키워드
경로로 폴백한다. 이 모듈이 실패해도 브리핑은 계속 나가야 한다.
"""

import json
import os
import re

from ..config import CATEGORIES
from .reranker import _call_llm

BATCH_SIZE = 80

# 판정 이유 코드. LLM 에게 이 중에서 고르게 해 사후 집계가 가능하게 한다.
REJECT_REASONS = (
    "job_posting",       # 채용공고·구인
    "landing_page",      # 기사가 아닌 섹션/서비스/회사 소개 페이지
    "event_promo",       # 행사·웨비나·시상 안내
    "pr_promo",          # 홍보성 보도자료·광고·스폰서 콘텐츠
    "interview",         # 인물 인터뷰·프로필
    "local_government",  # 지자체 지원사업·업무협약
    "stock_tip",         # 종목 추천·주가 전망
    "off_topic",         # 투자와 무관
    "opinion",           # 칼럼·오피니언
)

_INSTRUCTIONS = """너는 임팩트 투자·벤처캐피탈 전문 뉴스 브리핑의 편집장이다.
아래 후보 각각에 대해 투자 심사역이 아침에 읽을 가치가 있는지 판정하라.

[반드시 제외]
- 채용공고·구인·직위 모집. 직함만 있는 제목이 대표적이다.
  예: "Senior Investment Officer", "Latin America Investment Intern"
- 기사가 아닌 페이지. 섹션 인덱스, 서비스·회사 소개, 커리어 페이지.
  예: "insights - Bain & Company", "Life Sciences | Sector Trends | EY"
- 행사·웨비나·시상 안내, 참가 신청
- 홍보성 보도자료, 광고, 스폰서 콘텐츠
- 인물 인터뷰·프로필 기사. 예: "Coffee with Suzano CEO"
- 지자체 지원사업·업무협약(MOU)
- 종목 추천·주가 전망
- 투자와 무관한 소비자·생활·게임 기사

[선정 우선순위]
1순위: 신규 투자·펀드 결성, M&A, IPO, 규제·정책 변화, 시장 구조 변화
2순위: 산업 경쟁구도 변화, 핵심 기업의 전략 변화, 기술 breakthrough,
       임팩트 성과 검증(실증 결과, 임팩트 측정, 공공조달)

[카테고리] 다음 중 정확히 하나를 그대로 쓴다:
{categories}

[점수] 0~10. 심사역이 오늘 꼭 읽어야 할수록 높게.

[출력] 후보 전부에 대해 아래 형식의 JSON 만 반환한다. 설명 문장을 쓰지 마라.
{{"verdicts": [
  {{"id": 1, "keep": true, "category": "🤖 AI", "score": 8, "reason": "funding_round"}},
  {{"id": 2, "keep": false, "reason": "job_posting"}}
]}}

keep 이 false 면 reason 은 반드시 다음 중 하나다: {reject_reasons}
keep 이 true 면 reason 은 선정 근거를 나타내는 짧은 스네이크케이스 코드다.
"""


def is_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _candidate_block(articles: list, start_id: int) -> str:
    lines = []
    for offset, article in enumerate(articles):
        source = article.get("source", "")
        if isinstance(source, list):
            source = source[0] if source else ""
        description = (article.get("description") or "")[:160]
        lines.append(
            f"ID [{start_id + offset}] | 출처: {source}\n"
            f"제목: {article.get('title', '')}\n"
            f"요약: {description}\n---"
        )
    return "\n".join(lines)


def _build_prompt(articles: list, start_id: int) -> str:
    return (
        "[후보 리스트]\n"
        + _candidate_block(articles, start_id)
        + "\n\n"
        + _INSTRUCTIONS.format(
            categories="\n".join(f"- {c}" for c in CATEGORIES),
            reject_reasons=", ".join(REJECT_REASONS),
        )
    )


def _parse_verdicts(raw: str) -> dict:
    """모델 응답에서 {id: verdict} 를 뽑는다. 형식이 흔들려도 최대한 건진다."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    data = json.loads(text)
    if isinstance(data, dict):
        items = data.get("verdicts") or data.get("articles") or []
    else:
        items = data

    verdicts = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        digits = re.sub(r"\D", "", str(item.get("id", "")))
        if not digits:
            continue
        verdicts[int(digits)] = item
    return verdicts


def _apply(article: dict, verdict: dict, valid_categories: set) -> None:
    keep = bool(verdict.get("keep"))
    article["editor_verdict"] = "keep" if keep else "reject"
    article["editor_reason"] = str(verdict.get("reason", "") or "")[:40]

    if not keep:
        article["editorial_excluded"] = True
        article["relevance"] = 0.0
        article["filter_reason"] = f"editor:{article['editor_reason']}"
        return

    article["editorial_excluded"] = False
    category = verdict.get("category")
    if category in valid_categories:
        article["category"] = category
        article["category_reason"] = "editor"
    try:
        score = float(verdict.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    article["editor_score"] = max(0.0, min(10.0, score))
    article["relevance"] = article["editor_score"]


def review(articles: list) -> tuple:
    """기사에 편집 판정을 붙인다.

    반환값은 (통과한 기사 목록, 오류 목록). LLM 을 쓸 수 없으면 첫 값이 None
    이며, 호출한 쪽은 기존 키워드 경로로 폴백해야 한다.
    """
    errors = []
    if not articles:
        return [], errors
    if not is_enabled():
        return None, ["GEMINI_API_KEY 없음 — 편집 게이트 건너뜀"]

    api_key = os.environ["GEMINI_API_KEY"]
    valid_categories = set(CATEGORIES)
    reviewed = 0

    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start:start + BATCH_SIZE]
        raw, used_model = _call_llm(_build_prompt(batch, start + 1), api_key)
        if raw is None:
            errors.append(f"편집 게이트 호출 실패(기사 {start + 1}~{start + len(batch)})")
            continue
        try:
            verdicts = _parse_verdicts(raw)
        except (ValueError, TypeError) as e:
            errors.append(f"편집 게이트 응답 파싱 실패: {e}")
            continue

        for offset, article in enumerate(batch):
            verdict = verdicts.get(start + offset + 1)
            if verdict is None:
                continue
            _apply(article, verdict, valid_categories)
            reviewed += 1
        print(f"🧑‍⚖️ 편집 게이트({used_model}): {len(batch)}건 중 {len(verdicts)}건 판정")

    if not reviewed:
        return None, errors + ["편집 게이트가 한 건도 판정하지 못함 — 폴백"]

    # 응답에서 누락된 기사는 버리지 않는다. 판정 실패로 좋은 기사를 조용히
    # 잃는 것보다, 점수를 낮게 줘 뒤로 밀리게 하는 편이 안전하다.
    unreviewed = 0
    for article in articles:
        if "editor_verdict" not in article:
            article["editor_verdict"] = "unreviewed"
            article["editor_score"] = 0.0
            unreviewed += 1
    if unreviewed:
        errors.append(f"편집 게이트 미판정 {unreviewed}건 — 낮은 점수로 통과시킴")

    kept = [a for a in articles if a.get("editor_verdict") != "reject"]
    print(f"🧑‍⚖️ 편집 게이트 결과: {len(articles)}건 중 {len(kept)}건 통과")
    return kept, errors
