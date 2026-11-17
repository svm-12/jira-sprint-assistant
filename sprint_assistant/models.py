"""Plain data types shared by the rest of the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Issue:
    key: str
    summary: str
    status: str
    # Jira's own grouping of statuses: "new" (to do), "indeterminate" (in progress), "done".
    status_category: str
    assignee_id: str | None
    assignee_name: str | None
    priority: str | None
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    created: datetime | None = None
    updated: datetime | None = None
    due: date | None = None
    points: float | None = None

    @property
    def is_done(self) -> bool:
        return self.status_category == "done"

    @property
    def is_started(self) -> bool:
        return self.status_category == "indeterminate"


@dataclass
class Member:
    name: str
    account_id: str
    skills: list[str]
    components: list[str]
    labels: list[str]
    max_in_progress: int
    capacity_points: float
    backup_account_id: str | None
    # Inclusive (start, end) date ranges when the person is away.
    unavailable: list[tuple[date, date]]
    allowlisted: bool

    def is_available(self, on: date) -> bool:
        return not any(start <= on <= end for start, end in self.unavailable)


@dataclass
class Settings:
    project_key: str
    board_id: str | None
    sprint_id: str | None
    stale_after_days: int
    blocked_statuses: list[str]
    review_statuses: list[str]
    story_points_field: str | None
    never_auto_assign_labels: list[str]
    approvers: list[str]
    audit_log: str
    plan_max_age_hours: int


@dataclass
class TeamMap:
    settings: Settings
    members: list[Member]

    def member(self, account_id: str | None) -> Member | None:
        for m in self.members:
            if m.account_id == account_id:
                return m
        return None
