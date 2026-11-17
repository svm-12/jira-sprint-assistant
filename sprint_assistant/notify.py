"""Post a report to Slack through an incoming webhook.

Slack doesn't render Markdown headings or **bold**, so the text is converted
to Slack's own format first.
"""

from __future__ import annotations

import os
import re

import requests


def to_slack_format(markdown: str) -> str:
    out = []
    for line in markdown.splitlines():
        line = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", line)  # headings -> bold line
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)  # **bold** -> *bold*
        line = re.sub(r"^- ", "• ", line)
        out.append(line)
    return "\n".join(out)


def post_to_slack(markdown: str, webhook_url: str | None = None) -> None:
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        raise SystemExit("Set SLACK_WEBHOOK_URL to use --post-slack.")
    resp = requests.post(url, json={"text": to_slack_format(markdown)}, timeout=15)
    resp.raise_for_status()
