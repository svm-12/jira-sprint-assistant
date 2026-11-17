"""Deterministic sprint-health checks.

These are plain rules over issue fields. They decide *what* is flagged; the
LLM (if used at all) only decides how to phrase it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Issue, Settings, TeamMap

PRIORITY_RANK = {"highest": 0, "high": 1, "medium": 2, "low": 3, "lowest": 4}


def priority_rank(issue: Issue) -> int:
    return PRIORITY_RANK.get((issue.priority or "").lower(), 2)


def days_since_update(issue: Issue, now: datetime) -> int | None:
    if not issue.updated:
        return None
    return (now - issue.updated).days


def issue_flags(issue: Issue, settings: Settings, now: datetime) -> list[str]:
    """Return the problems with one issue. Done issues never get flags."""
    if issue.is_done:
        return []
    flags = []
    status = issue.status.lower()
    idle = days_since_update(issue, now)
    if status in settings.blocked_statuses:
        flags.append("blocked")
    if not issue.assignee_id:
        flags.append("unassigned")
    if idle is not None and idle >= settings.stale_after_days:
        flags.append("stuck-in-review" if status in settings.review_statuses else "stale")
    if issue.due and issue.due < now.date():
        flags.append("overdue")
    if issue.points is None:
        flags.append("no-estimate")
    return flags


# Severity order used when sorting findings (lower = more urgent).
FLAG_SEVERITY = {"blocked": 0, "overdue": 1, "stuck-in-review": 2, "unassigned": 3, "stale": 4, "no-estimate": 5}


@dataclass
class Load:
    in_progress: int = 0
    open_issues: int = 0
    points: float = 0.0
    keys: list[str] = field(default_factory=list)


def workload(issues: list[Issue], team: TeamMap) -> dict[str, Load]:
    """Open work per team member. Points count everything not done."""
    loads = {m.account_id: Load() for m in team.members}
    for issue in issues:
        if issue.is_done or issue.assignee_id not in loads:
            continue
        load = loads[issue.assignee_id]
        load.open_issues += 1
        load.points += issue.points or 0
        load.keys.append(issue.key)
        if issue.is_started:
            load.in_progress += 1
    return loads


def overloaded(team: TeamMap, loads: dict[str, Load]) -> list[tuple[str, list[str]]]:
    """Members over their WIP limit or sprint capacity, with the reasons."""
    out = []
    for m in team.members:
        load = loads[m.account_id]
        reasons = []
        if load.in_progress > m.max_in_progress:
            reasons.append(f"{load.in_progress} in progress (limit {m.max_in_progress})")
        if load.points > m.capacity_points:
            reasons.append(f"{load.points:g} points (capacity {m.capacity_points:g})")
        if reasons:
            out.append((m.account_id, reasons))
    return out


@dataclass
class Finding:
    issue: Issue
    flags: list[str]

    @property
    def severity(self) -> int:
        return min(FLAG_SEVERITY.get(f, 9) for f in self.flags)


def health_findings(issues: list[Issue], settings: Settings, now: datetime) -> list[Finding]:
    findings = []
    for issue in issues:
        flags = issue_flags(issue, settings, now)
        if flags:
            findings.append(Finding(issue, flags))
    findings.sort(key=lambda f: (f.severity, priority_rank(f.issue), f.issue.key))
    return findings


def triage_bucket(issue: Issue, settings: Settings) -> str:
    """Classify a backlog issue into one bucket for the triage report."""
    if issue.status.lower() in settings.blocked_statuses:
        return "blocked"
    if issue.points is None or not (issue.components or issue.labels):
        return "needs-detail"
    if not issue.assignee_id:
        return "needs-owner"
    return "ready"
