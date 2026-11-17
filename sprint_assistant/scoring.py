"""Owner scoring.

Every candidate gets a number from fixed, visible rules, and each point
added or removed is recorded as a reason. That makes a recommendation
explainable ("why Asha?") and testable, which a prompt-only score is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .models import Issue, Member, TeamMap
from .signals import Load

# Points per signal. Kept in one place so they are easy to tune and test.
COMPONENT_MATCH = 4
LABEL_MATCH = 3
SKILL_MATCH = 3
RECENT_SIMILAR_WORK = 2
BACKUP_OWNER = 1

NOT_ALLOWLISTED = -5
OVER_WIP_LIMIT = -4
OVER_CAPACITY = -3


@dataclass
class Candidate:
    member: Member
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    # A hard blocker means: never apply this automatically.
    hard_blockers: list[str] = field(default_factory=list)

    def add(self, points: int, why: str) -> None:
        self.score += points
        (self.reasons if points > 0 else self.risks).append(f"{points:+d} {why}")


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def score_member(
    issue: Issue,
    member: Member,
    load: Load,
    team: TeamMap,
    recent_done: list[Issue],
    today: date,
) -> Candidate:
    c = Candidate(member)
    comps = {x.lower() for x in issue.components}
    labels = {x.lower() for x in issue.labels}

    if comps & set(member.components):
        c.add(COMPONENT_MATCH, f"owns component {', '.join(sorted(comps & set(member.components)))}")
    if labels & set(member.labels):
        c.add(LABEL_MATCH, f"owns label {', '.join(sorted(labels & set(member.labels)))}")

    skill_hits = set(member.skills) & (comps | labels | _words(issue.summary))
    if skill_hits:
        c.add(SKILL_MATCH, f"skill match: {', '.join(sorted(skill_hits))}")

    similar = [
        d.key
        for d in recent_done
        if d.assignee_id == member.account_id
        and ({x.lower() for x in d.components} & comps or {x.lower() for x in d.labels} & labels)
    ]
    if similar:
        c.add(RECENT_SIMILAR_WORK, f"recently finished similar work ({', '.join(similar[:3])})")

    current = team.member(issue.assignee_id)
    if current and current.backup_account_id == member.account_id:
        c.add(BACKUP_OWNER, f"backup for {current.name}")

    if not member.allowlisted:
        c.add(NOT_ALLOWLISTED, "not on the assignment allowlist")
        c.hard_blockers.append("not allowlisted")
    if not member.is_available(today):
        c.hard_blockers.append("unavailable today")
    # Started work counts against the WIP limit immediately. Unstarted work can be
    # queued for someone at their limit, but not for someone already over it.
    wip_after = load.in_progress + (1 if issue.is_started else 0)
    if wip_after > member.max_in_progress:
        c.add(OVER_WIP_LIMIT, f"over WIP limit ({wip_after}/{member.max_in_progress} in progress)")
    if load.points + (issue.points or 0) > member.capacity_points:
        c.add(
            OVER_CAPACITY,
            f"would exceed capacity ({load.points + (issue.points or 0):g}/{member.capacity_points:g} pts)",
        )
    return c


def rank_candidates(
    issue: Issue,
    team: TeamMap,
    loads: dict[str, Load],
    recent_done: list[Issue],
    today: date,
    exclude: set[str] | None = None,
) -> list[Candidate]:
    exclude = exclude or set()
    cands = [
        score_member(issue, m, loads[m.account_id], team, recent_done, today)
        for m in team.members
        if m.account_id not in exclude
    ]
    # People who can actually take the work come first, then highest score;
    # name is a stable tie-breaker so output is repeatable.
    cands.sort(key=lambda c: (bool(c.hard_blockers), -c.score, c.member.name))
    return cands


def confidence(best: Candidate, runner_up: Candidate | None) -> str:
    """high / medium / low, following the rules in the README."""
    margin = best.score - runner_up.score if runner_up else 99
    if best.hard_blockers or best.score < 3:
        return "low"
    if best.score >= 6 and not best.risks and margin >= 2:
        return "high"
    if margin == 0:
        return "low"  # a tie means a human should pick
    return "medium"
