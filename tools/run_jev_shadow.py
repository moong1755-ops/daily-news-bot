"""Annotate job/promotion suspicions alongside existing editorial decisions.

This command never sends Slack messages and never updates seen-state files.

    python -m tools.run_jev_shadow
    python -m tools.run_jev_shadow --limit 5 --offset 10
    python -m tools.run_jev_shadow --file data/last_run_decisions.json --output review.json

The default noise mode never judges suitability or chooses a category. Existing
dataset labels are comparison references, not freshly verified human answers.
Use --mode editorial only to reproduce the earlier broad experiment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.processor.jev import (
    NOISE_REVIEW_THRESHOLD, JevError, flag_noise, is_enabled, judge_article,
)


EVAL_PATH = Path(__file__).parent.parent / "data" / "eval_set.json"
CATEGORY_NAMES = {
    "impact": "임팩트",
    "ai": "AI",
    "vc_pe": "VC·PE",
    "macro": "거시·정책·지정학",
    "mbb_big4": "MBB·Big4",
    "outside_scope": "범위 밖",
}
STATUS_NAMES = {
    "suspected_noise": "의심 표시(검토 필요)",
    "uncertain": "애매함(검토 필요)",
    "no_flag": "채용·홍보 표시 없음(기사 적합성 판정 아님)",
}
MAX_SHADOW_LIMIT = 50


def _reject_non_finite_json(value: str):
    raise ValueError("JSON 입력에 비유한 숫자를 허용하지 않습니다.")


def load_articles(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle, parse_constant=_reject_non_finite_json)
    if isinstance(data, dict):
        articles = data.get("articles") if "articles" in data else data.get("decisions")
    else:
        articles = data
    if not isinstance(articles, list):
        raise ValueError("입력 JSON에 articles 또는 decisions 배열이 없습니다.")
    if not all(isinstance(item, dict) for item in articles):
        raise ValueError("기사 배열의 각 항목은 객체여야 합니다.")
    return articles


def _title(article: dict) -> str:
    return " ".join(str(article.get("title_orig") or article.get("title") or "제목 없음").split())


def _print_judgment(result: dict, mode: str) -> None:
    if mode == "noise":
        probabilities = result["probabilities"]
        print(f"    Jev 표시: {STATUS_NAMES[result['status']]}")
        print(f"    채용공고 확률 {probabilities['job_posting']:.1%} / "
              f"홍보 확률 {probabilities['promotion']:.1%}")
        print("    입력: " + ("제목만" if result["evidence_level"] == "title_only" else "제목과 설명"))
    else:
        desk = CATEGORY_NAMES.get(result["primary_desk"], result["primary_desk"])
        print(f"    실험 판정: 기사자격 확률 {result['qualified_probability']:.1%} / "
              f"{desk} / {result['article_form']}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=EVAL_PATH, help="평가할 JSON 파일")
    parser.add_argument("--limit", type=int, default=3, help="호출할 기사 수(기본 3)")
    parser.add_argument("--offset", type=int, default=0, help="시작 위치")
    parser.add_argument("--mode", choices=("noise", "editorial"), default="noise")
    parser.add_argument("--threshold", type=float, default=NOISE_REVIEW_THRESHOLD,
                        help="검토 표시선(차단 기준 아님), 0.5 초과 1 이하")
    parser.add_argument("--output", type=Path, help="새 JSON 보고서 경로(기존 파일 덮어쓰기 금지)")
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= MAX_SHADOW_LIMIT or args.offset < 0:
        parser.error(f"--limit은 1~{MAX_SHADOW_LIMIT}, --offset은 0 이상이어야 합니다.")
    if not 0.5 < args.threshold <= 1:
        parser.error("--threshold는 0.5 초과 1 이하여야 합니다.")
    if args.output and (args.output.exists() or args.output.resolve() == args.file.resolve()):
        print("보고서 저장 경로에 이미 파일이 있습니다. 새로운 경로를 지정하세요.")
        return 2
    try:
        articles = load_articles(args.file)
    except (OSError, ValueError) as exc:
        print(f"입력 파일을 읽지 못했습니다: {type(exc).__name__}")
        return 2
    selected = articles[args.offset : args.offset + args.limit]
    if not selected:
        print("선택된 기사가 없습니다.")
        return 1
    if not is_enabled():
        print("TYPESAFE_API_KEY가 현재 터미널에 없습니다.")
        print("새 터미널을 열거나 저장된 사용자 환경변수를 다시 불러온 뒤 실행하세요.")
        return 2
    total_input = 0
    total_output = 0
    failures = 0
    records = []
    print(f"Jev 비발송 비교: {len(selected)}건\n")
    if args.mode == "noise":
        print(f"검토 표시선 {args.threshold:.0%}: 시험용이며 자동 제외 기준이 아닙니다.\n")

    for index, article in enumerate(selected, start=args.offset + 1):
        record = {
            "index": index, "title": _title(article),
            "existing_label": article.get("label") or article.get("verdict"),
            "existing_category": article.get("expected_category") or article.get("category"),
            "existing_reason": article.get("reason") or article.get("editor_reason"),
            "label_review": article.get("review"),
        }
        try:
            result = (flag_noise(article, threshold=args.threshold) if args.mode == "noise"
                      else judge_article(article))
        except JevError as exc:
            failures += 1
            record["result"] = {"status": "unavailable", "automatic_action": "none", "error": str(exc)}
            records.append(record)
            print(f"[{index}] 검사 불가: {_title(article)}")
            print(f"    {exc}\n")
            continue

        record["result"] = result
        records.append(record)
        usage = result.get("usage", {})
        total_input += int(usage.get("input_tokens") or 0)
        total_output += int(usage.get("output_tokens") or 0)
        print(f"[{index}] {_title(article)}")
        print(f"    기존 기록(정답 확정 아님): {record['existing_label'] or '미표시'} / "
              f"{record['existing_category'] or '미표시'}")
        _print_judgment(result, args.mode)

    print(
        f"완료: 성공 {len(selected) - failures}건, 실패 {failures}건, "
        f"정상 응답 기준 토큰 input {total_input} / output {total_output}"
    )
    if failures:
        print("실패한 요청의 사용량은 합계에 포함할 수 없습니다.")
    if args.output:
        report = {
            "mode": args.mode, "automatic_action": "none", "records": records,
            "failures": failures, "usage": {"input_tokens": total_input, "output_tokens": total_output},
            "usage_scope": "successful_responses_only", "label_caveat": "existing labels, not verified ground truth",
        }
        try:
            # Validate serialization before opening a destination file.
            serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(serialized)
        except (OSError, ValueError, TypeError) as exc:
            print(f"보고서 저장 실패: {type(exc).__name__}")
            return 2
        print(f"비교 결과 저장: {args.output}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
