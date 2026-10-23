"""Check weekly market sources without Slack, LLM calls, or archive writes."""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

from .archive import weekly_date_window
from .market_data import MarketSnapshot, collect_market_snapshots


def report_market_health(markets: tuple[MarketSnapshot, ...]) -> tuple[str, ...]:
    """Keep delivery success separate from market-data availability in Actions."""
    failed = tuple(market for market in markets if not market.available)
    issues = tuple(f"{market.label}: {market.error or '관측값 없음'}" for market in failed)
    status = f"시장지표 {len(markets) - len(failed)}/{len(markets)}개 정상"
    print(f"{'⚠️' if failed else '✅'} {status}")
    for issue in issues:
        print(f"⚠️ 시장지표 {issue}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            escaped = issue.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning title=주간 시장지표 미수집::{escaped}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        lines = ["### 주간 시장지표 점검", "", status]
        lines.extend(f"- {issue}" for issue in issues)
        try:
            with Path(summary_path).open("a", encoding="utf-8") as summary:
                summary.write("\n".join(lines) + "\n\n")
        except OSError:
            print("⚠️ 시장지표 실행 요약 저장 실패 — 점검 결과는 위 로그 참조")
    return issues


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", type=date.fromisoformat, help="종료 일요일 (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    if args.end_date is None:
        start, end = weekly_date_window()
    else:
        end = args.end_date
        if end.weekday() != 6:
            parser.error("--end-date는 완전히 끝난 주의 일요일이어야 합니다")
        start = end - timedelta(days=6)

    print(f"시장지표 연결 점검: {start.isoformat()}~{end.isoformat()}")
    markets = collect_market_snapshots(start, end)
    issues = report_market_health(markets)
    for market in markets:
        if not market.available:
            continue
        comparison, latest = market.comparison, market.latest
        unit = "bp" if market.change_unit == "basis_points" else "%"
        change = f"{market.change:+.2f}{unit}" if market.change is not None else "비교값 없음"
        dates = f"{comparison.observed_on}→{latest.observed_on}" if comparison else str(latest.observed_on)
        print(f"{market.label} {latest.value:,.2f} {change} · {dates} · {market.value_basis or market.provider}")
        print(f"  출처: {market.source_url}")
    return 1 if issues or not markets else 0


if __name__ == "__main__":
    raise SystemExit(main())
