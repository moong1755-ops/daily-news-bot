"""Runtime telemetry contains counts and issue codes, never credentials or mail bodies."""
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import QUALITY_MONITOR_CONFIG, RSS_SOURCE_METADATA
from ..utils.newsletters import newsletter_sources
from .checks import inspect_articles, inspect_markets, semantic_audit, issue

KST = timezone(timedelta(hours=9))


def enabled():
    return QUALITY_MONITOR_CONFIG["enabled"] and os.getenv("QUALITY_MONITORING") == "1"


def report_path():
    return Path(os.getenv("QUALITY_REPORT_PATH") or Path(__file__).resolve().parents[2] / "data/quality_last_run.json")


def update(**changes):
    if not enabled():
        return
    path = report_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if record.get("run_id") != os.getenv("GITHUB_RUN_ID", "local"):
            record = {}
        findings = changes.pop("issues", [])
        record.update(changes)
        record.setdefault("issues", []).extend(findings)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except (OSError, ValueError):
        print("::warning::Quality telemetry storage failed")


def start(kind, preview=False):
    if enabled():
        path = report_path()
        # The file is an ephemeral artifact, never a delivery/archive database.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"run_id": os.getenv("GITHUB_RUN_ID", "local"),
                "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "1"),
                "kind": kind, "preview": preview, "date": datetime.now(KST).date().isoformat(),
                "issues": [], "delivery": "not_attempted"}), encoding="utf-8")
        except OSError:
            print("::warning::Quality telemetry storage failed")


def collection(articles, error_count, newsletter_enabled=True):
    counts = Counter()
    for article in articles:
        feeds = article.get("feed") or ""
        feeds = feeds if isinstance(feeds, list) else [feeds]
        for feed in feeds:
            if feed in RSS_SOURCE_METADATA:
                counts["source:" + feed] += 1
                counts["category:" + str(RSS_SOURCE_METADATA[feed].get("category", ""))] += 1
        for source in newsletter_sources(article):
            counts["newsletter:" + source] += 1
    issues = []
    if error_count:
        issues.append(issue("collection_errors", "collection", count=error_count))
    if not newsletter_enabled:
        issues.append(issue("newsletter_unavailable", "gmail"))
    update(collection_counts=dict(counts), collected=len(articles), issues=issues)


def final_check(articles):
    if not enabled():
        return
    from ..processor.reranker import generate_quality_json
    try:
        issues, _ = inspect_articles(articles)
        issues.extend(semantic_audit(articles, generate_quality_json))
        if not articles:
            issues.append(issue("empty_selection", "final"))
    except Exception:
        issues = [issue("audit_invalid", "runtime")]
    update(selected=len(articles), issues=issues)


def markets_check(markets, end_date):
    if enabled():
        try:
            update(issues=inspect_markets(markets, end_date))
        except Exception:
            update(issues=[issue("audit_invalid", "market_check")])
