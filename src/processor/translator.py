"""발송 확정 기사 제목 한글 번역 (선택 기능).
- GEMINI_API_KEY 없거나 TRANSLATE_TITLES=False 면 그대로 통과.
- 한글 제목은 로컬에서 스킵(토큰 절약). 영문만 id 매핑 JSON으로 일괄 번역.
- id 누락/파싱 실패/타임아웃 → 해당 건(또는 전체) 원문 유지. 번역 실패가 발송을 막지 않음.
- 모델 선택/폴백은 reranker 의 공개 편집 인터페이스를 재사용(한 곳에서 관리).
"""
import os
import re
import json

try:
    from ..config import TRANSLATE_TITLES
except ImportError:
    TRANSLATE_TITLES = True

from .reranker import generate_editor_json

_HANGUL = re.compile(r"[가-힣]")

# These checks are conservative: an uncertain original is safer than a fluent
# translation that turns a forecast or attributed claim into a confirmed fact.
_QUALIFIERS = (
    (r"\b(?:likely|unlikely|may|might|could|expects?|expected|forecast\w*|projected)\b",
     r"예상|예측|전망|가능|관측|추정|듯|수\s*있|것으로\s*보"),
    (r"\b(?:reportedly|reports?\s+say|sources?\s+say|said\s+to|according\s+to)\b",
     r"보도|전해|알려|소식통|관계자|따르면"),
    (r"\b(?:claims?|claimed|it\s+says)\b",
     r"주장|밝혔|밝혀|말했|설명|따르면"),
)


def _preserves_qualifiers(original: str, translated: str) -> bool:
    """Reject missing uncertainty/attribution; do not claim full fact checking."""
    return all(
        not re.search(source, original, re.I) or re.search(target, translated)
        for source, target in _QUALIFIERS
    )


def translate_titles(articles: list) -> list:
    for article in articles:
        title = str(article.get("title") or "").strip()
        cleaned = re.sub(r"\s*\|\s*\(상세\)\s*\|\s*한국은행\s*홈페이지\s*$", "", title)
        if cleaned and cleaned != title:
            article.setdefault("title_orig", title)
            if not article.get("title_orig"):
                article["title_orig"] = title
            article["title"] = cleaned
    if not TRANSLATE_TITLES or not os.environ.get("GEMINI_API_KEY"):
        return articles

    targets = {}
    for idx, a in enumerate(articles):
        t = (a.get("title") or "").strip()
        if t and not _HANGUL.search(t):        # 영문 제목만
            targets[str(idx)] = t
    if not targets:
        return articles

    prompt = (
        "다음 영문 뉴스 제목들을 자연스러운 한국어로 번역하라.\n"
        "규칙: (1) 기업명·제품명·인명·펀드명·티커는 영문 그대로 유지 (예: OpenAI, Nvidia, Series B). "
        "(2) 금액·수치와 비교 대상의 단위를 유지. 배출량 규모를 면적으로 바꾸지 마라. "
        "(3) 의역보다 뉴스 헤드라인체로 간결하게 쓰되 정보를 삭제하지 마라. "
        "(4) likely/may/expected/forecast의 예상·가능성, reportedly/said to의 보도·전언, "
        "claims/it says의 주장 주체를 명시하고 확정 사실로 바꾸지 마라. "
        "(5) 주어·목적어와 인수·투자·분사 방향을 보존하라. "
        "A, which spun out B는 A가 B를 분사시켰다는 뜻이지 A가 B에서 분사했다는 뜻이 아니다. "
        "(6) 반드시 입력과 같은 키의 JSON 객체만 반환하고 다른 텍스트는 쓰지 마라.\n\n"
        + json.dumps(targets, ensure_ascii=False)
    )

    try:
        raw, _model = generate_editor_json(prompt)
        if raw is None:
            return articles
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        mapping = json.loads(raw)
        if not isinstance(mapping, dict):
            return articles
        done = 0
        rejected = 0
        for key, translated in mapping.items():
            if key in targets and isinstance(translated, str) and translated.strip():
                if not _preserves_qualifiers(targets[key], translated):
                    rejected += 1
                    continue
                i = int(key)
                if not articles[i].get("title_orig"):
                    articles[i]["title_orig"] = articles[i]["title"]
                articles[i]["title"] = translated.strip()
                done += 1
        print(f"🈶 제목 번역 완료: {done}/{len(targets)}건 (미번역은 원문 유지)")
        if rejected:
            print(f"⚠️ 번역의 예상·전언·주장 누락 {rejected}건 — 원문 유지")
    except Exception as e:
        print(f"⚠️ 제목 번역 실패({type(e).__name__}) — 원문 제목으로 발송합니다.")
    return articles
