"""Independent delivery watchdog and weekly audit. Never sends a news edition."""
import argparse
import hashlib
import json
import os
from html import escape
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

import requests

from ..config import QUALITY_MONITOR_CONFIG as CONFIG
from ..weekly.archive import _record_edition_date
from .checks import LABELS, inspect_articles, issue
from .runtime import KST
from ..slack_delivery import API_URL, valid_reference

CRITICAL_CODES = {"workflow_failed", "delivery_failed", "delivery_unverified", "archive_failed",
                  "report_missing", "daily_late", "weekly_late", "duplicate_delivery"}


def article_context(finding, articles):
    """Resolve only an archive index; model-generated URLs/reasons are never published."""
    index = finding.get("article")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(articles):
        article = articles[index]
        finding = {**finding, "article_title": str(article.get("title") or ""),
                   "article_url": _public_url(str(article.get("normalized_url") or article.get("url") or ""))}
    return finding


def _public_url(value):
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return ""
        if any(marker in parts.hostname for marker in ("hubspotlinks", "safelinks", "click.", "email.")):
            return ""
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in {"idxno", "id", "article_id"}])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return ""


def describe(finding):
    code = finding["code"]
    prefix = "재확인 필요" if finding.get("advisory") or code.startswith("llm_") else "규칙·실행 기록 점검"
    text = f"• [{prefix}] {LABELS[code]}"
    if code in {"weekly_content", "weekly_repeated"} and finding.get("scope") in LABELS:
        text += " — " + LABELS[finding["scope"]]
    if isinstance(finding.get("count"), int):
        text += f" ({finding['count']}건)"
    title = finding.get("article_title")
    if title:
        title = escape(title, quote=False).replace("|", "｜")
        url = _public_url(finding.get("article_url", ""))
        text += "\n  ↳ " + (f"<{url}|{title}>" if url else title)
    return text


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
    findings = [dict(f) for f in report.get("issues", [])]
    run_id = str(report.get("run_id") or "")
    matching = [row for row in rows if run_id and str(row.get("github", {}).get("run_id")) == run_id]
    kind = report.get("kind", "unknown")
    if conclusion != "success":
        findings.append(issue("workflow_failed", kind))
    if report.get("delivery") == "failed":
        findings.append(issue("delivery_failed", kind))
    if report.get("delivery") != "skipped" and not matching:
        findings.append(issue("delivery_unverified", kind))
    articles = matching[0].get("articles", []) if matching else []
    event = run_id + ":" + str(report.get("run_attempt") or "1")
    return [{**article_context(f, articles), "event": event} for f in findings]


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
    examples = {}
    for row in rows:
        articles = row.get("articles", [])
        findings, _ = inspect_articles(articles)
        counts.update(finding["code"] for finding in findings)
        for finding in findings:
            examples.setdefault(finding["code"], article_context(finding, articles))
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
    for finding in findings:
        example = examples.get(finding["scope"], {})
        finding.update({k: example[k] for k in ("article_title", "article_url") if k in example})
    findings += [issue("weekly_repeated", code, count=count) for code, count in sorted(repeats.items()) if count >= 3]
    latest = max(rows, key=lambda r: r.get("ts", ""), default={})
    return {"kind": "weekly_review", "date": now.date().isoformat(), "period": period,
            "slack_reference": latest.get("slack_reference"),
            "editions": len(rows), "articles": sum(len(row.get("articles", [])) for row in rows), "issues": findings}


def due_alerts(findings, ledger, now):
    result, seen = [], set()
    for finding in findings:
        if finding.get("code") not in LABELS:
            continue
        key = finding["code"] + ":" + finding.get("scope", "")
        if finding["code"] in CRITICAL_CODES:
            key += ":" + str(finding.get("event") or now.date().isoformat())
        elif finding.get("article_url"):
            key += ":" + hashlib.sha256(finding["article_url"].encode("utf-8")).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        previous = ledger.get(key)
        if previous and now - datetime.fromisoformat(previous) < timedelta(days=CONFIG["alert_cooldown_days"]):
            continue
        result.append((key, finding))
    return result


def notify(findings, ledger, now, webhook, run_id="", session=requests, reference=None):
    pending = due_alerts(findings, ledger, now)
    if not pending:
        return False
    token = os.getenv("SLACK_BOT_TOKEN", "")
    channel = os.getenv("SLACK_CHANNEL_ID", "")
    use_bot = bool(token and channel)
    if os.getenv("SLACK_DELIVERY_VERSION") == "v2" and not use_bot:
        raise RuntimeError("Slack v2 alert credentials/channel missing; legacy delivery is disabled")
    use_thread = bool(token and valid_reference(reference)
                      and reference["channel"] == os.getenv("SLACK_CHANNEL_ID")
                      and not any(f["code"] in CRITICAL_CODES for _, f in pending))
    if not webhook and not use_bot:
        raise RuntimeError("Daily Slack alert webhook is not configured")
    # Details come only from persisted delivered articles, never model free text.
    text = "⚠️ 뉴스브리핑 자동 점검\n" + "\n".join(describe(f) for _, f in pending)
    text += "\nAI 표시 항목은 재확인이 필요한 의심 사항입니다. 자동 재발송은 하지 않습니다."
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if run_id.isdigit() and repo == "moong1755-ops/daily-news-bot":
        text += f"\n<https://github.com/{repo}/actions/runs/{run_id}|점검 실행 기록>"
    try:
        payload = {"text": text, "unfurl_links": False, "unfurl_media": False}
        kwargs = {"timeout": 20}
        if use_thread:
            payload.update(channel=reference["channel"], thread_ts=reference["ts"], reply_broadcast=False)
        if use_bot:
            payload["channel"] = channel
            kwargs["headers"] = {"Authorization": "Bearer " + token}
        response = session.post(API_URL if use_bot else webhook, json=payload, **kwargs)
        response.raise_for_status()
        if use_bot and response.json().get("ok") is not True:
            raise requests.RequestException("Slack did not confirm reply")
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
    reference = None
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
            reference = report.get("slack_reference")
            report["issues"] = findings.copy()
            history = [r for r in history if r.get("run_id") != source_run] + [report]
        else:
            findings.append(issue("report_missing", os.getenv("QUALITY_SOURCE_KIND", "workflow")))
            if os.getenv("QUALITY_SOURCE_CONCLUSION") != "success":
                findings.append(issue("workflow_failed", os.getenv("QUALITY_SOURCE_KIND", "workflow")))
        for finding in findings:
            finding.setdefault("event", source_run + ":" + os.getenv("QUALITY_SOURCE_ATTEMPT", "1"))
    else:
        findings += watchdog(daily, weekly, now)
        review = weekly_review(daily, history, now, {r.get("period") for r in history if r.get("kind") == "weekly_review"})
        if review:
            findings += review["issues"]
            reference = review.get("slack_reference")
            history.append(review)
    summary = "자동 점검: " + ("이상 없음" if not findings else ", ".join(sorted({LABELS[f["code"]] for f in findings})))
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    if not args.notify:
        print("Preview: Slack and persistent state unchanged")
        return
    notify(findings, ledger, now, os.getenv("SLACK_WEBHOOK_URL", ""), os.getenv("GITHUB_RUN_ID", ""), reference=reference)
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
