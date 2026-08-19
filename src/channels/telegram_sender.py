import os
import requests
from datetime import datetime

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

def _post(text: str) -> bool:
    if not SLACK_WEBHOOK_URL:
        print("Error: SLACK_WEBHOOK_URL is not set.")
        return False
    try:
        response = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=10,
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Slack post failed: {e}")
        return False

def send_message(text: str) -> bool:
    success = _post(text)
    if not success:
        # Retry once
        success = _post(text)
    return success

def format_article(article: dict) -> str:
    title = article.get("title", "No title")
    field = article.get("field", "General Tech")
    tags = ", ".join(article.get("tags", [])) or "N/A"
    date = article.get("date", "Unknown date")
    summary = article.get("summary", "Summary unavailable")
    sources = article.get("source", [])
    links = article.get("link", [])

    if isinstance(sources, str):
        sources = [sources]
    if isinstance(links, str):
        links = [links]

    source_parts = []
    for i, src in enumerate(sources):
        link = links[i] if i < len(links) else "#"
        source_parts.append(f"<{link}|{src}>")
    sources_text = " | ".join(source_parts)

    return (
        f"📌 *{title}*\n\n"
        f"🗂 *Field:* {field}\n"
        f"🏷 *Tags:* {tags}\n"
        f"📅 *Date:* {date}\n\n"
        f"📝 *Summary:*\n{summary}\n\n"
        f"🔗 *Sources:* {sources_text}"
    )

def send_error_report(report: dict) -> None:
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    total = report.get("total_sources", 0)
    succeeded = report.get("succeeded_sources", 0)
    failed = report.get("failed_sources", [])
    skipped = report.get("skipped_articles", 0)
    no_summary = report.get("no_summary", 0)
    sent = report.get("articles_sent", 0)

    failed_list = "\n".join([f"  • {s}" for s in failed]) if failed else "  None"
    text = (
        f"🔧 *Daily Run Report — {date_str}*\n\n"
        f"✅ Sources succeeded: {succeeded}/{total}\n"
        f"❌ Failed sources:\n{failed_list}\n\n"
        f"⚠️ Articles skipped (missing fields): {skipped}\n"
        f"⚠️ Summaries unavailable: {no_summary}\n"
        f"📨 Articles sent: {sent}"
    )
    send_message(text)
