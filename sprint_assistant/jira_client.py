"""Talking to Jira.

Two clients share the same small interface:

- JiraClient: real Jira Cloud REST API (v3), authenticated with an API token.
- FixtureClient: reads a JSON file shaped like Jira's API responses, so the
  whole tool can be run and tested without a Jira account.

Everything else in the package only calls these methods, so it never knows
which one it is talking to.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from .models import Issue, Settings

FIELDS = [
    "summary",
    "status",
    "assignee",
    "priority",
    "components",
    "labels",
    "created",
    "updated",
    "duedate",
]


def parse_jira_datetime(value: str | None) -> datetime | None:
    """Jira sends timestamps like 2026-11-10T09:12:33.000+0530."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def issue_from_jira(raw: dict, points_field: str | None) -> Issue:
    f = raw.get("fields") or {}
    status = f.get("status") or {}
    assignee = f.get("assignee") or {}
    priority = f.get("priority") or {}
    points = f.get(points_field) if points_field else None
    due = f.get("duedate")
    return Issue(
        key=raw["key"],
        summary=f.get("summary") or "",
        status=status.get("name", "Unknown"),
        status_category=(status.get("statusCategory") or {}).get("key", "new"),
        assignee_id=assignee.get("accountId"),
        assignee_name=assignee.get("displayName"),
        priority=priority.get("name"),
        components=[c["name"] for c in f.get("components") or []],
        labels=list(f.get("labels") or []),
        created=parse_jira_datetime(f.get("created")),
        updated=parse_jira_datetime(f.get("updated")),
        due=date.fromisoformat(due) if due else None,
        points=float(points) if isinstance(points, (int, float)) else None,
    )


# Jira Cloud returns 429 when you hit its rate limit, and occasionally 5xx.
RETRY_STATUSES = {429, 502, 503, 504}
MAX_ATTEMPTS = 4


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, settings: Settings, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.settings = settings
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json"})
        self._sleep = sleep  # injectable so tests don't actually wait

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Send a request, retrying on rate limits and temporary server errors.

        Waits for Jira's Retry-After header when it sends one, otherwise 1s, 2s, 4s.
        Any other error (401, 404, 400 bad JQL...) is raised straight away.
        """
        url = f"{self.base_url}{path}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.ConnectionError:
                if attempt == MAX_ATTEMPTS:
                    raise
                self._sleep(2 ** (attempt - 1))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else 2 ** (attempt - 1))
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    @classmethod
    def from_env(cls, settings: Settings) -> "JiraClient":
        missing = [v for v in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN") if not os.environ.get(v)]
        if missing:
            raise SystemExit(
                f"Missing environment variables: {', '.join(missing)}. "
                "Set them, or use --fixture to run against sample data."
            )
        return cls(os.environ["JIRA_BASE_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"], settings)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _fields(self) -> list[str]:
        sp = self.settings.story_points_field
        return FIELDS + ([sp] if sp else [])

    def _search(self, jql: str) -> list[Issue]:
        """Run a JQL search and follow pagination until the last page."""
        issues: list[Issue] = []
        token = None
        while True:
            body = {"jql": jql, "fields": self._fields(), "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/rest/api/3/search/jql", json=body).json()
            issues += [issue_from_jira(r, self.settings.story_points_field) for r in data.get("issues", [])]
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return issues

    def _sprint_clause(self) -> str:
        if self.settings.sprint_id:
            return f"sprint = {int(self.settings.sprint_id)}"
        return "sprint in openSprints()"

    def sprint_issues(self) -> list[Issue]:
        key = self.settings.project_key
        return self._search(f'project = "{key}" AND {self._sprint_clause()} ORDER BY Rank ASC')

    def backlog_issues(self) -> list[Issue]:
        key = self.settings.project_key
        return self._search(
            f'project = "{key}" AND sprint is EMPTY AND statusCategory != Done ORDER BY priority DESC, created ASC'
        )

    def recently_done(self, days: int = 30) -> list[Issue]:
        key = self.settings.project_key
        return self._search(f'project = "{key}" AND statusCategory = Done AND resolved >= -{int(days)}d')

    def get_issue(self, key: str) -> Issue:
        resp = self._request("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(self._fields())})
        return issue_from_jira(resp.json(), self.settings.story_points_field)

    def assign(self, key: str, account_id: str) -> None:
        # Jira Cloud assigns by accountId; emails are not accepted here.
        self._request("PUT", f"/rest/api/3/issue/{key}/assignee", json={"accountId": account_id})


class FixtureClient:
    """Serves issues from a local JSON file. assign() only changes memory."""

    def __init__(self, path: str | Path, settings: Settings):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.settings = settings
        pf = settings.story_points_field
        self._now = parse_jira_datetime(data["now"]) if data.get("now") else datetime.now(timezone.utc)
        self._sprint = [issue_from_jira(r, pf) for r in data.get("sprint", [])]
        self._backlog = [issue_from_jira(r, pf) for r in data.get("backlog", [])]
        self._done = [issue_from_jira(r, pf) for r in data.get("recently_done", [])]
        self.assignments: list[tuple[str, str]] = []

    def now(self) -> datetime:
        return self._now

    def sprint_issues(self) -> list[Issue]:
        return list(self._sprint)

    def backlog_issues(self) -> list[Issue]:
        return list(self._backlog)

    def recently_done(self, days: int = 30) -> list[Issue]:
        return list(self._done)

    def get_issue(self, key: str) -> Issue:
        for issue in self._sprint + self._backlog + self._done:
            if issue.key == key:
                return issue
        raise KeyError(f"{key} not found in fixture")

    def assign(self, key: str, account_id: str) -> None:
        issue = self.get_issue(key)
        issue.assignee_id = account_id
        self.assignments.append((key, account_id))
