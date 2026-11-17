"""Reassignment proposals, plan files, and the approval-checked apply step.

The flow is deliberately split in two:

1. `suggest-owner` / `balance` only *read* Jira and write a plan file.
2. `apply` reads that plan, checks who is approving, re-checks every issue
   against live Jira, and only then changes assignees.

Nothing in step 1 can write to Jira, so a bad recommendation (or a bad LLM
summary) can't change anything by itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Issue, TeamMap
from .scoring import Candidate, confidence, rank_candidates
from .signals import Load, overloaded, priority_rank, workload


@dataclass
class Proposal:
    issue: Issue
    best: Candidate
    alternatives: list[Candidate]
    confidence: str
    reason: str  # why this issue is being looked at at all

    def blockers(self, team: TeamMap) -> list[str]:
        out = list(self.best.hard_blockers)
        protected = {lb.lower() for lb in self.issue.labels} & set(team.settings.never_auto_assign_labels)
        if protected:
            out.append(f"protected label: {', '.join(sorted(protected))}")
        return out


def suggest_owners(
    issues: list[Issue], team: TeamMap, all_open: list[Issue], recent_done: list[Issue], today: date
) -> list[Proposal]:
    loads = workload(all_open, team)
    proposals = []
    # Highest priority first, so urgent work gets first pick of free capacity.
    for issue in sorted(issues, key=lambda i: (priority_rank(i), i.key)):
        if issue.is_done:
            continue
        exclude = {issue.assignee_id} if issue.assignee_id else set()
        ranked = rank_candidates(issue, team, loads, recent_done, today, exclude)
        if not ranked:
            continue
        best = ranked[0]
        conf = confidence(best, ranked[1] if len(ranked) > 1 else None)
        reason = "unassigned" if not issue.assignee_id else "owner change requested"
        proposals.append(Proposal(issue, best, _alternatives(ranked), conf, reason))
        _move_load(loads, issue, best.member.account_id)
    return proposals


def balance_load(sprint: list[Issue], team: TeamMap, recent_done: list[Issue], today: date) -> list[Proposal]:
    """Move not-yet-started work off overloaded people, lowest priority first."""
    loads = workload(sprint, team)
    proposals = []
    for account_id, why in overloaded(team, loads):
        owner = team.member(account_id)
        movable = sorted(
            (i for i in sprint if i.assignee_id == account_id and i.status_category == "new"),
            key=lambda i: (-priority_rank(i), i.key),  # lowest priority first
        )
        for issue in movable:
            if not _still_overloaded(owner, loads[account_id]):
                break
            ranked = rank_candidates(issue, team, loads, recent_done, today, {account_id})
            if not ranked:
                break
            best = ranked[0]
            conf = confidence(best, ranked[1] if len(ranked) > 1 else None)
            if conf == "low" or best.risks:
                continue  # don't push work onto someone who would also be overloaded
            proposals.append(
                Proposal(issue, best, _alternatives(ranked), conf, f"{owner.name} overloaded: {'; '.join(why)}")
            )
            _move_load(loads, issue, best.member.account_id)
    return proposals


def _alternatives(ranked: list[Candidate]) -> list[Candidate]:
    """Next two candidates, plus anyone who scored higher but can't take it (e.g. on leave)."""
    best = ranked[0]
    return ranked[1:3] + [c for c in ranked[3:] if c.score > best.score]


def _still_overloaded(member, load: Load) -> bool:
    return load.in_progress > member.max_in_progress or load.points > member.capacity_points


def _move_load(loads: dict[str, Load], issue: Issue, to_id: str) -> None:
    """Update the running workload so later proposals see earlier ones."""
    pts = issue.points or 0
    started = 1 if issue.is_started else 0
    if issue.assignee_id in loads:
        old = loads[issue.assignee_id]
        old.points -= pts
        old.in_progress -= started
        old.open_issues -= 1
    new = loads[to_id]
    new.points += pts
    new.in_progress += started
    new.open_issues += 1


# ---- plan files -------------------------------------------------------------


def build_plan(proposals: list[Proposal], team: TeamMap, now: datetime, source: str) -> dict:
    items = []
    for p in proposals:
        items.append(
            {
                "issue": p.issue.key,
                "summary": p.issue.summary,
                "from": p.issue.assignee_id,
                "from_name": p.issue.assignee_name,
                "to": p.best.member.account_id,
                "to_name": p.best.member.name,
                "score": p.best.score,
                "confidence": p.confidence,
                "reasons": p.best.reasons + p.best.risks,
                "blockers": p.blockers(team),
                # Used at apply time to detect that the issue changed after planning.
                "issue_updated": p.issue.updated.isoformat() if p.issue.updated else None,
            }
        )
    body = {"created_at": now.isoformat(), "source": source, "project": team.settings.project_key, "items": items}
    body["plan_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    return body


def save_plan(plan: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def load_plan(path: str | Path) -> dict:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    claimed = plan.pop("plan_id", None)
    actual = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:12]
    if claimed != actual:
        raise SystemExit("Plan file was edited after it was generated (plan_id mismatch). Re-run the plan.")
    plan["plan_id"] = claimed
    return plan


# ---- apply ------------------------------------------------------------------


class ApprovalError(Exception):
    pass


def check_approver(team: TeamMap, approved_by: str) -> None:
    if approved_by.strip() not in team.settings.approvers:
        raise ApprovalError(f"'{approved_by}' is not in approval.approvers in the team map; refusing to apply.")


def apply_plan(
    plan: dict,
    client,
    team: TeamMap,
    approved_by: str,
    explicitly_approved: set[str],
    execute: bool,
    now: datetime,
) -> list[dict]:
    """Return one result row per plan item. Writes to Jira only if execute=True."""
    check_approver(team, approved_by)
    if plan.get("project") != team.settings.project_key:
        raise ApprovalError("Plan was made for a different project than this team map.")
    created = datetime.fromisoformat(plan["created_at"])
    if now - created > timedelta(hours=team.settings.plan_max_age_hours):
        raise ApprovalError(f"Plan is older than {team.settings.plan_max_age_hours}h; generate a fresh one.")

    results = []
    for item in plan["items"]:
        key = item["issue"]
        explicit = key in explicitly_approved
        row = {"issue": key, "to": item["to_name"], "action": "skip", "why": ""}
        target = team.member(item["to"])

        if target is None:
            row["why"] = "target is no longer on the team map"
        elif not target.is_available(now.date()):
            row["why"] = "target is unavailable today"
        elif item["blockers"] and not explicit:
            row["why"] = f"needs explicit approval (--approve {key}): {'; '.join(item['blockers'])}"
        elif item["confidence"] == "low" and not explicit:
            row["why"] = f"low confidence; needs explicit approval (--approve {key})"
        else:
            live = client.get_issue(key)
            live_updated = live.updated.isoformat() if live.updated else None
            if live.assignee_id != item["from"]:
                row["why"] = "assignee changed in Jira since the plan was made"
            elif live_updated != item["issue_updated"]:
                row["why"] = "issue was updated in Jira since the plan was made; re-plan"
            elif live.is_done:
                row["why"] = "issue is already done"
            elif execute:
                client.assign(key, item["to"])
                row["action"], row["why"] = "assigned", "approved"
            else:
                row["action"], row["why"] = "would-assign", "dry run (add --execute)"
        results.append(row)

    if execute:
        _audit(team.settings.audit_log, plan, approved_by, results, now)
    return results


def _audit(path: str, plan: dict, approved_by: str, results: list[dict], now: datetime) -> None:
    log = Path(path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(
                json.dumps(
                    {
                        "at": now.isoformat(),
                        "plan_id": plan["plan_id"],
                        "approved_by": approved_by,
                        **r,
                    }
                )
                + "\n"
            )
