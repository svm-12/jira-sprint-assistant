"""Load and validate the team map YAML.

Validation happens up front so a typo in the config fails loudly before
anything touches Jira, instead of quietly producing odd recommendations.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from .models import Member, Settings, TeamMap

# Jira project keys are uppercase letters/digits starting with a letter (e.g. SHOP, WEB2).
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid team map:\n" + "\n".join(f"  - {p}" for p in problems))


def _as_date(value, where: str, problems: list[str]) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        problems.append(f"{where}: '{value}' is not a YYYY-MM-DD date")
        return None


def load_team_map(path: str | Path) -> TeamMap:
    path = Path(path)
    if not path.exists():
        raise ConfigError([f"team map not found: {path}"])
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    problems: list[str] = []

    jira = data.get("jira") or {}
    rules = data.get("rules") or {}
    approval = data.get("approval") or {}

    project_key = str(jira.get("project_key", "")).strip()
    if not PROJECT_KEY_RE.match(project_key):
        problems.append(f"jira.project_key '{project_key}' is not a valid Jira key (uppercase, e.g. SHOP)")

    stale_after = rules.get("stale_after_days", 3)
    if not isinstance(stale_after, int) or stale_after < 1:
        problems.append("rules.stale_after_days must be a positive integer")
        stale_after = 3

    members: list[Member] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(data.get("team") or []):
        where = f"team[{i}]"
        name = str(raw.get("name", "")).strip()
        account_id = str(raw.get("account_id", "")).strip()
        if not name:
            problems.append(f"{where}: missing name")
        if not account_id:
            problems.append(f"{where} ({name}): missing account_id")
        elif account_id in seen_ids:
            problems.append(f"{where} ({name}): duplicate account_id {account_id}")
        seen_ids.add(account_id)

        max_wip = raw.get("max_in_progress", 2)
        capacity = raw.get("capacity_points", 8)
        if not isinstance(max_wip, int) or max_wip < 1:
            problems.append(f"{where} ({name}): max_in_progress must be a positive integer")
            max_wip = 1
        if not isinstance(capacity, (int, float)) or capacity <= 0:
            problems.append(f"{where} ({name}): capacity_points must be a positive number")
            capacity = 1

        unavailable = []
        for j, rng in enumerate(raw.get("unavailable") or []):
            start = _as_date(rng.get("from"), f"{where}.unavailable[{j}].from", problems)
            end = _as_date(rng.get("to"), f"{where}.unavailable[{j}].to", problems)
            if start and end:
                if end < start:
                    problems.append(f"{where}.unavailable[{j}]: 'to' is before 'from'")
                unavailable.append((start, end))

        members.append(
            Member(
                name=name,
                account_id=account_id,
                skills=[s.lower() for s in raw.get("skills") or []],
                components=[c.lower() for c in raw.get("components") or []],
                labels=[lb.lower() for lb in raw.get("labels") or []],
                max_in_progress=max_wip,
                capacity_points=float(capacity),
                backup_account_id=raw.get("backup_account_id"),
                unavailable=unavailable,
                allowlisted=bool(raw.get("allowlisted", True)),
            )
        )

    if not members:
        problems.append("team: at least one member is required")

    for m in members:
        if m.backup_account_id and m.backup_account_id not in seen_ids:
            problems.append(f"{m.name}: backup_account_id {m.backup_account_id} is not on the team")
        if m.backup_account_id == m.account_id:
            problems.append(f"{m.name}: cannot be their own backup")

    approvers = [str(a).strip() for a in approval.get("approvers") or [] if str(a).strip()]
    if not approvers:
        problems.append("approval.approvers: list at least one person who can approve reassignments")

    if problems:
        raise ConfigError(problems)

    settings = Settings(
        project_key=project_key,
        board_id=str(jira["board_id"]) if jira.get("board_id") else None,
        sprint_id=str(jira["sprint_id"]) if jira.get("sprint_id") else None,
        stale_after_days=stale_after,
        blocked_statuses=[s.lower() for s in rules.get("blocked_statuses", ["Blocked", "On Hold"])],
        review_statuses=[s.lower() for s in rules.get("review_statuses", ["In Review", "Testing"])],
        story_points_field=jira.get("story_points_field"),
        never_auto_assign_labels=[lb.lower() for lb in rules.get("never_auto_assign_labels", ["security"])],
        approvers=approvers,
        audit_log=approval.get("audit_log", "output/audit.jsonl"),
        plan_max_age_hours=int(approval.get("plan_max_age_hours", 24)),
    )
    return TeamMap(settings=settings, members=members)
