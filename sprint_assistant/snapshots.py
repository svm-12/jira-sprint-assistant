"""Sprint snapshots, so the standup can say what actually changed since last time.

Jira's `updated` field changes on any edit or comment, so on its own it can't
tell you "SHOP-101 moved from To Do to In Progress". Each standup saves a small
snapshot (status + owner per issue), and the next standup compares against it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import Issue


def take(sprint: list[Issue], now: datetime) -> dict:
    return {
        "taken_at": now.isoformat(),
        "issues": {i.key: {"summary": i.summary, "status": i.status, "assignee": i.assignee_name} for i in sprint},
    }


def save(snapshot: dict, folder: str | Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(snapshot["taken_at"]).strftime("%Y%m%dT%H%M%S")
    path = folder / f"snapshot-{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest(folder: str | Path) -> Path | None:
    # File names sort by time because the timestamp is zero-padded.
    files = sorted(Path(folder).glob("snapshot-*.json"))
    return files[-1] if files else None


def diff(previous: dict, sprint: list[Issue]) -> dict:
    before = previous["issues"]
    now = {i.key: i for i in sprint}
    status_changes, owner_changes = [], []
    for key, issue in now.items():
        old = before.get(key)
        if not old:
            continue
        if old["status"] != issue.status:
            status_changes.append({"key": key, "summary": issue.summary, "from": old["status"], "to": issue.status})
        if old["assignee"] != issue.assignee_name:
            owner_changes.append(
                {
                    "key": key,
                    "summary": issue.summary,
                    "from": old["assignee"] or "unassigned",
                    "to": issue.assignee_name or "unassigned",
                }
            )
    return {
        "since": previous["taken_at"],
        "status_changes": status_changes,
        "owner_changes": owner_changes,
        "added": [{"key": k, "summary": i.summary} for k, i in now.items() if k not in before],
        "removed": [{"key": k, "summary": v["summary"]} for k, v in before.items() if k not in now],
    }
