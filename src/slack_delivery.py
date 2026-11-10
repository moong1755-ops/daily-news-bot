"""Slack delivery with one explicit table-rejection fallback; never retry ambiguity."""
import re

import requests

API_URL = "https://slack.com/api/chat.postMessage"


def valid_reference(reference):
    return (isinstance(reference, dict)
            and isinstance(reference.get("channel"), str)
            and re.fullmatch(r"[CG][A-Z0-9]+", reference["channel"])
            and isinstance(reference.get("ts"), str)
            and re.fullmatch(r"\d+\.\d+", reference["ts"]))


def _cell_text(cell):
    if cell["type"] == "raw_text":
        return cell["text"]
    parts = []
    for section in cell.get("elements", []):
        for element in section.get("elements", []):
            text = element.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f"<{element['url']}|{text}>" if element["type"] == "link" else text)
    return "".join(parts)


def without_tables(payload):
    """Preserve every table cell/link/date in a readable fallback, without truncation."""
    blocks = []
    for block in payload.get("blocks", []):
        if block.get("type") != "table":
            blocks.append(block)
            continue
        for row in block["rows"][1:]:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": " · ".join(_cell_text(cell) for cell in row)}})
    if len(blocks) > 50:
        raise requests.RequestException("Slack fallback exceeds block limit")
    return {**payload, "blocks": blocks}


def post_message(webhook, payload, *, session=requests, token="", channel=""):
    use_bot = bool(token and channel)
    kwargs = {"timeout": 20}
    url = webhook
    if use_bot:
        url = API_URL
        payload = {**payload, "channel": channel}
        kwargs["headers"] = {"Authorization": "Bearer " + token}
    response = session.post(url, json=payload, **kwargs)
    data = response.json() if use_bot and response.status_code == 200 else {}
    rejected = ((response.status_code == 400 and response.text.strip() in {"invalid_blocks", "invalid_blocks_format"})
                or (use_bot and data.get("ok") is False and data.get("error") in {"invalid_blocks", "invalid_blocks_format"}))
    fallback = rejected and any(b.get("type") == "table" for b in payload.get("blocks", []))
    if fallback:
        response = session.post(url, json=without_tables(payload), **kwargs)
        data = response.json() if use_bot and response.status_code == 200 else {}
    response.raise_for_status()
    if use_bot and data.get("ok") is not True:
        # No webhook retry after an API/network error: delivery could be ambiguous.
        raise requests.RequestException("Slack API did not confirm delivery")
    reference = {"channel": data.get("channel"), "ts": data.get("ts")}
    return response, (reference if valid_reference(reference) else None), bool(fallback)
