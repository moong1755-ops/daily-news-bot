"""Explicit manual migration check; never reads news, archives or mail."""
import argparse
import json
import os
from pathlib import Path

import requests

from .config import WEEKLY_MARKET_INDICATORS
from .slack_delivery import API_URL, post_message


def check(*, send_test=False, session=requests):
    token = os.getenv("SLACK_BOT_TOKEN_NEXT", "")
    team = os.getenv("SLACK_WORKSPACE_ID", "")
    daily = os.getenv("SLACK_CHANNEL_ID", "")
    weekly = os.getenv("WEEKLY_SLACK_CHANNEL_ID", "")
    if not all((token, team, daily, weekly)) or daily == weekly:
        raise ValueError("Candidate token/workspace/distinct delivery channels are required")
    response = session.post("https://slack.com/api/auth.test", headers={"Authorization": "Bearer " + token}, timeout=20)
    response.raise_for_status()
    identity = response.json()
    if identity.get("ok") is not True or identity.get("team_id") != team or not identity.get("user_id"):
        raise ValueError("Candidate Slack identity/workspace verification failed")
    result = {"workspace": team, "bot_user_id": identity["user_id"],
              "daily_channel": daily, "weekly_channel": weekly, "messages": []}
    path = Path("data/slack_connection_check.json")
    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    save()
    if send_test:
        rows = [[{"type": "raw_text", "text": label} for label in ("지표", "연결 확인", "전주 대비", "기준")]]
        rows += [[{"type": "raw_text", "text": text} for text in (item["label"], "시험용", "—", "실제 시세 아님")]
                 for item in WEEKLY_MARKET_INDICATORS]
        table = {"type": "table", "column_settings": [{"align": "left"}, {"align": "right"}, {"align": "right"}, {"align": "left", "is_wrapped": True}], "rows": rows}
        for kind, channel in (("일간", daily), ("주간", weekly)):
            title = f"🧪 뉴스브리프 {kind} 연결 시험 — 실제 뉴스 발송 아님"
            _, reference, fallback = post_message("", {"text": title, "blocks": [
                {"type": "section", "text": {"type": "plain_text", "text": title}}, table]},
                session=session, token=token, channel=channel)
            result["messages"].append({"kind": kind, "reference": reference, "table_fallback": fallback})
            save()
            if not reference:
                raise ValueError("Slack accepted message but returned no usable parent reference; inspect channel before retry")
            if kind == "일간":
                reply = session.post(API_URL, headers={"Authorization": "Bearer " + token}, json={
                    "channel": channel, "thread_ts": reference["ts"], "reply_broadcast": False,
                    "text": "🧪 점검 댓글 연결 시험 완료 — 실제 품질 경고가 아닙니다.",
                    "unfurl_links": False}, timeout=20)
                reply.raise_for_status()
                if reply.json().get("ok") is not True:
                    raise ValueError("Thread reply verification failed; inspect channel before retry")
                result["thread_reply_verified"] = True
                save()
    print(json.dumps(result, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-test", action="store_true")
    args = parser.parse_args()
    try:
        check(send_test=args.send_test)
    except (requests.RequestException, ValueError, OSError):
        # Never print request objects, response bodies, tokens or exception URLs.
        print("Slack connection check failed. No automatic retry. Inspect channel before another send test.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
