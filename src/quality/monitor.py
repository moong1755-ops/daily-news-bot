"""Independent delivery watchdog and weekly audit. Never sends a news edition."""
import argparse
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

import requests

from ..config import QUALITY_MONITOR_CONFIG as CONFIG
from ..weekly.archive import _record_edition_date
from .checks import LABELS, inspect_articles, issue
from .runtime import KST


def read_rows(path):
    if not path.exists():
        return []
    # Invalid state is not silently replaced: fail closed to avoid alert floods.
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_deliveries(directory, name):
    rows = read_rows(directory / (name + ".jsonl"))
    rows += read_rows(directory / (name + "_recovery.jsonl"))
    unique = {}
    for row in rows:
        identity = (str(row.get("github", {}).get("run_id") or ""),
                    row.get("ts") or row.get("sent_at"))
        unique[identity] = row
    return list(unique.values())


def collection_changes(report, history):
    if report.get("kind") != "daily" or "collection_counts" not in report:
        return []
    current_date = date.fromisoformat(report["date"])
    days = {}
    for row in history:
        if row.get("kind") != "daily" or row.get("preview") or "collection_counts" not in row:
            continue
        observed = date.fromisoformat(row["date"])
        if observed < current_date and (observed.weekday() < 5) == (current_date.weekday() < 5):
            days[observed] = row
    baseline = [days[day] for day in sorted(days)[-14:]]
    if len(baseline) < CONFIG["baseline_min_runs"]:
        return []
    keys = set().union(*(row["collection_counts"] for row in baseline))
    findings = []
    for key in sorted(keys):
        normal = median(row["collection_counts"].get(key, 0) for row in baseline)
        # Infrequent publishers are not considered broken just because today is quiet.
        if normal >= 5 and report["collection_counts"].get(key, 0) < normal * CONFIG["collection_drop_ratio"]:
            findings.append(issue("collection_drop", key))
    return findings


def delivery_findings(report, rows, conclusion):
    findings = list(report.get("issues", []))
    run_id = str(report.get("run_id") or "")
    matching = [row for row in rows if run_id and str(row.get("github", {}).get("run_id")) == run_id]
    kind = report.get("kind", "unknown")
    if conclusion != "success":
        findings.append(issue("workflow_failed", kind))
    if report.get("delivery") == "failed":
        findings.append(issue("delivery_failed", kind))
    if report.get("delivery") != "skipped" and not matching:
        findings.append(issue("delivery_unverified", kind))
    return findings


def watchdog(daily, weekly, now):
    today = now.date()
    findings = []
    if now.hour >= CONFIG["daily_deadline_hour_kst"] and not any(_record_edition_date(row) == today for row in daily):
        findings.append(issue("daily_late", today.isoformat()))
    monday = today - timedelta(days=today.weekday())
    expected_end = (monday - timedelta(days=1)).isoformat()
    if (today.weekday() > 0 or now.hour >= CONFIG["weekly_deadline_hour_kst"]) and not any(
        row.get("end_date") == expected_end for row in weekly
    ):
        findings.append(issue("weekly_late", expected_end))
    return findings


def weekly_review(daily, history, now, reviewed_periods):
    monday = now.date() - timedelta(days=now.weekday())
    end = monday - timedelta(days=1)
    period = end.isoformat()
    if period in reviewed_periods:
        return None
    start = end - timedelta(days=6)
    rows = [row for row in daily if (day := _record_edition_date(row)) and start <= day <= end]
    counts, seen_editions = Counter(), set()
    for row in rows:
        articles = row.get("articles", [])
        findings, _ = inspect_articles(articles)
        counts.update(finding["code"] for finding in findings)
        urls = tuple(sorted(str(a.get("normalized_url") or a.get("url") or "") for a in articles))
        identity = (_record_edition_date(row), urls)
        if urls and identity in seen_editions:
            counts["duplicate_delivery"] += 1
        seen_editions.add(identity)
    repeats = Counter()
    # Count affected days, not the number of times a workflow was rerun.
    affected = set()
    for record in history:
        if record.get("preview") or not start.isoformat() <= record.get("date", "") <= period:
            continue
        affected.update((record["date"], item["code"]) for item in record.get("issues", []))
    repeats.update(code for _, code in affected)
    findings = [issue("weekly_content", code, count=count) for code, count in sorted(counts.items())]
    findings += [issue("weekly_repeated", code, count=count) for code, count in sorted(repeats.items()) if count >= 3]
    return {"kind": "weekly_review", "date": now.date().isoformat(), "period": period,
            "editions": len(rows), "articles": sum(len(row.get("articles", [])) for row in rows), "issues": findings}


def due_alerts(findings, ledger, now):
    result, seen = [], set()
    for finding in findings:
        if finding.get("code") not in LABELS:
            continue
        key = finding["code"] + ":" + finding.get("scope", "")
        if key in seen:
            continue
        seen.add(key)
        previous = ledger.get(key)
        if previous and now - datetime.fromisoformat(previous) < timedelta(days=CONFIG["alert_cooldown_days"]):
            continue
        result.append((key, finding))
    return result


def notify(findings, ledger, now, webhook, run_id="", session=requests):
    pending = due_alerts(findings, ledger, now)
    if not pending:
        return False
    if not webhook:
        raise RuntimeError("Daily Slack alert webhook is not configured")
    # Never include raw source titles, URLs, provider exceptions or model text.
    labels = sorted({LABELS[f["code"]] for _, f in pending})
    text = "⚠️ 뉴스브리핑 자동 점검\n" + "\n".join("• " + label for label in labels)
    text += "\nAI 표시 항목은 재확인이 필요한 의심 사항입니다. 자동 재발송은 하지 않습니다."
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if run_id.isdigit() and repo == "moong1755-ops/daily-news-bot":
        text += f"\n<https://github.com/{repo}/actions/runs/{run_id}|점검 실행 기록>"
    try:
        response = session.post(webhook, json={"text": text, "unfurl_links": False}, timeout=20)
        response.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Quality alert delivery failed; check channel before retry") from None
    for key, _ in pending:
        ledger[key] = now.isoformat()
    return True


def main():
    if not CONFIG["enabled"]:
        print("Quality monitoring disabled in config")
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    now = datetime.now(KST)
    root = args.data_dir
    history = read_rows(root / "quality_history.jsonl")
    ledger_path = root / "quality_alert_state.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    daily, weekly = load_deliveries(root, "slack_archive"), load_deliveries(root, "weekly_archive")
    findings = []
    source_run = os.getenv("QUALITY_SOURCE_RUN_ID", "")
    if source_run:
        if args.report and args.report.exists():
            report = json.loads(args.report.read_text(encoding="utf-8"))
            if str(report.get("run_id")) != source_run:
                raise ValueError("Quality report run ID mismatch")
            if report.get("preview"):
                print("Preview/test run: no notification or state changes")
                return
            findings += delivery_findings(report, weekly if report.get("kind") == "weekly" else daily,
                                          os.getenv("QUALITY_SOURCE_CONCLUSION", "unknown"))
            findings += collection_changes(report, history)
            report["issues"] = findings.copy()
            history = [r for r in history if r.get("run_id") != source_run] + [report]
        else:
            findings.append(issue("report_missing", os.getenv("QUALITY_SOURCE_KIND", "workflow")))
            if os.getenv("QUALITY_SOURCE_CONCLUSION") != "success":
                findings.append(issue("workflow_failed", os.getenv("QUALITY_SOURCE_KIND", "workflow")))
    else:
        findings += watchdog(daily, weekly, now)
        review = weekly_review(daily, history, now, {r.get("period") for r in history if r.get("kind") == "weekly_review"})
        if review:
            findings += review["issues"]
            history.append(review)
    summary = "자동 점검: " + ("이상 없음" if not findings else ", ".join(sorted({LABELS[f["code"]] for f in findings})))
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    if not args.notify:
        print("Preview: Slack and persistent state unchanged")
        return
    notify(findings, ledger, now, os.getenv("SLACK_WEBHOOK_URL", ""), os.getenv("GITHUB_RUN_ID", ""))
    root.mkdir(parents=True, exist_ok=True)
    cutoff = (now.date() - timedelta(days=CONFIG["retention_days"])).isoformat()
    history = [r for r in history if r.get("date", "") >= cutoff]
    ledger = {k: v for k, v in ledger.items() if v[:10] >= cutoff}
    for path, content in (
        (root / "quality_history.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in history)),
        (ledger_path, json.dumps(ledger, ensure_ascii=False)),
    ):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


if __name__ == "__main__":
    main()
