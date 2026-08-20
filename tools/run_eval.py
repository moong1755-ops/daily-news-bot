"""편집 판정 평가셋으로 파이프라인의 정확도를 측정한다.

    python -m tools.run_eval                    # 키워드 경로만
    python -m tools.run_eval --verbose          # 틀린 항목 전부 출력
    python -m tools.run_eval --llm              # LLM 편집 게이트도 함께 채점
    LLM_MODE=cache python -m tools.run_eval --llm   # 첫 1회만 호출, 이후 공짜

data/eval_set.json 의 사람 라벨과 실제 판정을 비교한다. --llm 없이는 API 를
호출하지 않으므로 몇 번을 돌려도 쿼터를 쓰지 않는다.

프롬프트나 필터를 고친 뒤 수치가 어느 방향으로 움직였는지 보는 것이 목적이다.
"""

import argparse
import json
import sys
from pathlib import Path

from src.bot import is_relevant
from src.processor import editor
from src.processor.summarizer import summarize

EVAL_PATH = Path(__file__).parent.parent / "data" / "eval_set.json"


def load_eval_set(path=EVAL_PATH) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["articles"]


def _as_article(entry: dict) -> dict:
    """평가셋 항목을 파이프라인이 받는 기사 형태로 되돌린다.

    feed 는 라우팅에 쓰이는데 슬랙 아카이브에는 남지 않는다. 판정 로그가
    생긴 뒤 추출한 항목은 feed 를 갖고 있으므로 그것을 우선 쓰고, 없으면
    source 로 대신한다(이 경우 출처 기반 분기가 실제와 달라질 수 있다).
    """
    return {
        "title": entry["title"],
        "description": entry.get("description", ""),
        "link": entry["url"],
        "source": entry["source"],
        "feed": entry.get("feed") or entry["source"],
        "date": "2026-08-20",
    }


def evaluate(entries: list) -> dict:
    """각 항목에 파이프라인 판정을 붙이고 집계를 돌려준다."""
    results = []
    for entry in entries:
        article = _as_article(entry)
        passed = is_relevant(article)
        predicted_category = None
        if passed:
            article, _ = summarize(article)
            if article.get("editorial_excluded"):
                passed = False
            else:
                predicted_category = article.get("category")

        expected_keep = entry["label"] == "keep"
        results.append({
            "entry": entry,
            "predicted_keep": passed,
            "predicted_category": predicted_category,
            "decision_correct": passed == expected_keep,
            "category_correct": (
                predicted_category == entry.get("expected_category")
                if passed and expected_keep else None
            ),
            "filter_reason": article.get("filter_reason"),
        })

    keeps = [r for r in results if r["entry"]["label"] == "keep"]
    rejects = [r for r in results if r["entry"]["label"] == "reject"]
    category_judged = [r for r in results if r["category_correct"] is not None]

    return {
        "results": results,
        "total": len(results),
        "caught_rejects": sum(1 for r in rejects if not r["predicted_keep"]),
        "total_rejects": len(rejects),
        "kept_keeps": sum(1 for r in keeps if r["predicted_keep"]),
        "total_keeps": len(keeps),
        "category_correct": sum(1 for r in category_judged if r["category_correct"]),
        "category_judged": len(category_judged),
    }


def evaluate_llm_gate(entries: list) -> dict:
    """LLM 편집 게이트를 같은 라벨로 채점한다. 호출 불가 시 None."""
    articles = [_as_article(e) for e in entries]
    kept, errors = editor.review(articles)
    if kept is None:
        return None

    kept_ids = {id(a) for a in kept}
    results = []
    for entry, article in zip(entries, articles):
        predicted_keep = id(article) in kept_ids
        expected_keep = entry["label"] == "keep"
        results.append({
            "entry": entry,
            "predicted_keep": predicted_keep,
            "predicted_category": article.get("category") if predicted_keep else None,
            "filter_reason": article.get("editor_reason"),
            "category_correct": (
                article.get("category") == entry.get("expected_category")
                if predicted_keep and expected_keep else None
            ),
        })

    keeps = [r for r in results if r["entry"]["label"] == "keep"]
    rejects = [r for r in results if r["entry"]["label"] == "reject"]
    judged = [r for r in results if r["category_correct"] is not None]
    return {
        "results": results,
        "total": len(results),
        "caught_rejects": sum(1 for r in rejects if not r["predicted_keep"]),
        "total_rejects": len(rejects),
        "kept_keeps": sum(1 for r in keeps if r["predicted_keep"]),
        "total_keeps": len(keeps),
        "category_correct": sum(1 for r in judged if r["category_correct"]),
        "category_judged": len(judged),
        "errors": errors,
    }


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole * 100:.0f}%)" if whole else "0/0 (–)"


def _print_summary(label: str, summary: dict) -> None:
    print(f"\n===== {label} =====")
    print(f"평가셋            {summary['total']}건")
    print(f"오발송 차단       {_pct(summary['caught_rejects'], summary['total_rejects'])}"
          "   ← 나가지 말았어야 할 기사를 실제로 막았는가")
    print(f"정상 기사 유지    {_pct(summary['kept_keeps'], summary['total_keeps'])}"
          "   ← 좋은 기사를 잘못 죽이지 않았는가")
    print(f"카테고리 정확도   {_pct(summary['category_correct'], summary['category_judged'])}")


def _print_misses(summary: dict) -> None:
    print("\n----- 놓친 오발송(차단했어야 함) -----")
    for r in summary["results"]:
        if r["entry"]["label"] == "reject" and r["predicted_keep"]:
            print(f"  [{r['entry']['reason']}] {r['entry']['title'][:70]}")

    print("\n----- 잘못 죽인 정상 기사 -----")
    for r in summary["results"]:
        if r["entry"]["label"] == "keep" and not r["predicted_keep"]:
            print(f"  [{r['filter_reason']}] {r['entry']['title'][:70]}")

    print("\n----- 카테고리 오분류 -----")
    for r in summary["results"]:
        if r["category_correct"] is False:
            print(f"  {r['entry']['title'][:60]}")
            print(f"     예상={r['entry']['expected_category']} / 실제={r['predicted_category']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="틀린 항목을 모두 출력")
    parser.add_argument("--llm", action="store_true", help="LLM 편집 게이트도 함께 채점")
    args = parser.parse_args(argv)

    entries = load_eval_set()

    keyword_summary = evaluate(entries)
    _print_summary("키워드 경로 (현재 운영)", keyword_summary)
    if args.verbose:
        _print_misses(keyword_summary)

    if args.llm:
        llm_summary = evaluate_llm_gate(entries)
        if llm_summary is None:
            print("\n⚠️ LLM 편집 게이트를 실행할 수 없습니다. "
                  "GEMINI_API_KEY 를 설정하고 다시 시도하세요.")
        else:
            _print_summary("LLM 편집 게이트", llm_summary)
            if args.verbose:
                _print_misses(llm_summary)
            for message in llm_summary["errors"]:
                print(f"  ⚠️ {message}")
            print("\n===== 차이 (LLM − 키워드) =====")
            print(f"오발송 차단    "
                  f"{llm_summary['caught_rejects'] - keyword_summary['caught_rejects']:+d}건")
            print(f"정상 기사 유지 "
                  f"{llm_summary['kept_keeps'] - keyword_summary['kept_keeps']:+d}건")

    missing_feed = sum(1 for e in entries if not e.get("feed"))
    if missing_feed:
        print(f"\n⚠️ {missing_feed}건은 feed 정보가 없어(슬랙 아카이브에서 추출) "
              "출처 기반 분기가 실제 실행과 다를 수 있습니다.\n"
              "   data/last_run_decisions.json 이 쌓이면 그쪽에서 다시 추출하세요.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
