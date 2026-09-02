"""Small shared helpers for editorial priority and review CSVs.

This module intentionally does not classify articles by regex. The Daily editor LLM is the
single source of truth for importance/reason/subtype. Missing metadata means "use the old
ranking", which keeps the rollout safe when the editor is disabled or returns a partial JSON.
"""
from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from pathlib import Path

EDITORIAL_REVIEW_SHEET_URL = os.environ.get("EDITORIAL_REVIEW_SHEET_URL", "").strip()
VALID_IMPORTANCE_REASONS = {
    "policy_or_market_change",
    "systemic_capital",
    "major_deal",
    "industry_shift",
    "investment_evidence",
}
VALID_ALT_SUBTYPES = {
    "capital_formation",
    "venture_growth",
    "pe_ma",
    "exit_liquidity",
}
DAILY_REVIEW_FIELDS = (
    "edition_date",
    "selected",
    "category",
    "region",
    "alt_subtype",
    "importance",
    "importance_reason",
    "editor_score",
    "selection_score",
    "title",
    "source",
    "url",
)
WEEKLY_REVIEW_FIELDS = (
    "week",
    "weekly_selected",
    "category",
    "region",
    "alt_subtype",
    "importance",
    "importance_reason",
    "weekly_score",
    "title",
    "source",
    "url",
)


def importance(article: dict) -> int:
    value = article.get("importance")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value in (1, 2, 3) else 0
    if isinstance(value, str):
        normalized = value.strip()
        return int(normalized) if normalized in {"1", "2", "3"} else 0
    if isinstance(value, float) and value.is_integer():
        integer = int(value)
        return integer if integer in (1, 2, 3) else 0
    return 0


def priority_key(article: dict, score: float) -> tuple[int, float]:
    """Importance is primary; old score remains the tie-breaker.

    importance=0 means the new metadata is absent, so all such articles retain the exact old
    score ordering relative to each other.
    """
    return importance(article), float(score)


def select_alt_with_soft_diversity(
    ranked: list[dict],
    *,
    limit: int,
    score_fn,
    score_tolerance: float = 1.0,
) -> list[dict]:
    """Prefer subtype diversity only inside the same importance band.

    If the new metadata is missing, return the old top-N behavior. Diversity never allows a
    lower-importance article to beat a higher-importance one.
    """
    if limit <= 0:
        return []
    if not any(importance(article) for article in ranked):
        return list(ranked[:limit])

    pool = list(ranked)
    selected: list[dict] = []
    seen_subtypes: set[str] = set()
    while pool and len(selected) < limit:
        top_importance = max(importance(article) for article in pool)
        band = [article for article in pool if importance(article) == top_importance]
        band.sort(key=lambda article: float(score_fn(article)), reverse=True)
        best = band[0]
        chosen = best

        if selected:
            best_score = float(score_fn(best))
            diverse = [
                article
                for article in band
                if article.get("alt_subtype") in VALID_ALT_SUBTYPES
                and article.get("alt_subtype") not in seen_subtypes
                and float(score_fn(article)) >= best_score - float(score_tolerance)
            ]
            if diverse:
                chosen = max(diverse, key=lambda article: float(score_fn(article)))

        pool.remove(chosen)
        selected.append(chosen)
        subtype = str(chosen.get("alt_subtype") or "")
        if subtype in VALID_ALT_SUBTYPES:
            seen_subtypes.add(subtype)
    return selected


def _primary(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _common_row(article: dict) -> dict:
    return {
        "category": article.get("category") or "",
        "region": article.get("region") or "",
        "alt_subtype": article.get("alt_subtype") or "",
        "importance": importance(article) or "",
        "importance_reason": article.get("importance_reason") or "",
        "editor_score": article.get("editor_score") if article.get("editor_score") is not None else "",
        "selection_score": article.get("selection_score") if article.get("selection_score") is not None else article.get("relevance", ""),
        "weekly_score": article.get("weekly_score") if article.get("weekly_score") is not None else "",
        "title": article.get("title") or article.get("title_orig") or "",
        "source": _primary(article.get("source")),
        "url": _primary(article.get("link") or article.get("normalized_url") or article.get("url")),
    }


def _row(article: dict, edition_date: str, selected: bool, review_type: str) -> dict:
    row = _common_row(article)
    if review_type == "weekly":
        row.update({
            "week": edition_date,
            "weekly_selected": "TRUE" if selected else "FALSE",
        })
    else:
        row.update({
            "edition_date": edition_date,
            "selected": "TRUE" if selected else "FALSE",
        })
    return row


def _identity(article: dict) -> str:
    return _primary(article.get("link") or article.get("normalized_url") or article.get("url")) or str(
        article.get("editor_event_key") or article.get("title_orig") or article.get("title") or ""
    )


def write_review_csv(
    path: str | Path,
    *,
    edition_date: str,
    candidates: list[dict],
    selected: list[dict] | tuple[dict, ...],
    retention_days: int,
    review_type: str = "daily",
) -> None:
    if review_type not in {"daily", "weekly"}:
        raise ValueError("review_type must be 'daily' or 'weekly'")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected_ids = {_identity(article) for article in selected}
    review_candidates = [
        article for article in candidates
        if not article.get("editorial_excluded", False)
    ]
    fresh_rows = [
        _row(article, edition_date, _identity(article) in selected_ids, review_type)
        for article in review_candidates
    ]
    date_field = "week" if review_type == "weekly" else "edition_date"
    fieldnames = WEEKLY_REVIEW_FIELDS if review_type == "weekly" else DAILY_REVIEW_FIELDS

    retained_rows: list[dict] = []
    try:
        current_date = date.fromisoformat(edition_date)
        cutoff = current_date - timedelta(days=max(1, int(retention_days)))
    except ValueError:
        cutoff = None

    if destination.exists():
        try:
            with destination.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    row_date = row.get(date_field) or ""
                    if row_date == edition_date:
                        continue
                    if cutoff is not None:
                        try:
                            if date.fromisoformat(row_date) < cutoff:
                                continue
                        except ValueError:
                            continue
                    retained_rows.append(row)
        except (OSError, csv.Error):
            retained_rows = []

    # Newest edition first so the Sheet opens on the useful rows.
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(fresh_rows + retained_rows)
