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


def translate_titles(articles: list) -> list:
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
        "(2) 금액·수치 단위는 유지. (3) 의역보다 뉴스 헤드라인체로 간결하게. "
        "(4) 반드시 입력과 같은 키의 JSON 객체만 반환하고 다른 텍스트는 쓰지 마라.\n\n"
        + json.dumps(targets, ensure_ascii=False)
    )

    try:
        raw, _model = generate_editor_json(prompt)
        if raw is None:
            return articles
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        mapping = json.loads(raw)
        done = 0
        for key, translated in mapping.items():
            if key in targets and isinstance(translated, str) and translated.strip():
                i = int(key)
                articles[i]["title_orig"] = articles[i]["title"]   # 교차일 중복제거용 원문 보존
                articles[i]["title"] = translated.strip()
                done += 1
        print(f"🈶 제목 번역 완료: {done}/{len(targets)}건 (미번역은 원문 유지)")
    except Exception as e:
        print(f"⚠️ 제목 번역 실패({e}) — 원문 제목으로 발송합니다.")
    return articles
