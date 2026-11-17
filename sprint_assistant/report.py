"""Turn computed results into Markdown, and into a JSON 'facts' bundle for the LLM."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .models import Issue, TeamMap
from .plans import Proposal
from .signals import Finding, Load, days_since_update, overloaded, triage_bucket


def _who(issue: Issue) -> str:
    return issue.assignee_name or "unassigned"


def facts_for_standup(
    sprint: list[Issue],
    findings: list[Finding],
    loads: dict[str, Load],
    team: TeamMap,
    now: datetime,
    changes: dict | None = None,
) -> dict:
    """Everything the standup brief may mention. The LLM sees only this.

    `changes` is the diff against the previous snapshot (see snapshots.py). When
    there is no previous snapshot we fall back to "updated in the last 24 hours".
    """
    flagged = {f.issue.key for f in findings}
    # "Updated" is any change in Jira (status, comment, field edit); it is not only status moves.
    recent = [i for i in sprint if i.updated and (now - i.updated).total_seconds() < 24 * 3600 and i.key not in flagged]
    facts = {
        "project": team.settings.project_key,
        "as_of": now.isoformat(timespec="minutes"),
        "status_counts": dict(Counter(i.status for i in sprint)),
        "needs_attention": [
            {
                "key": f.issue.key,
                "summary": f.issue.summary,
                "status": f.issue.status,
                "owner": _who(f.issue),
                "flags": f.flags,
                "days_idle": days_since_update(f.issue, now),
            }
            for f in findings
        ],
        "overloaded": [{"name": team.member(aid).name, "why": why} for aid, why in overloaded(team, loads)],
    }
    if changes is not None:
        facts["changes_since_last_brief"] = changes
    else:
        facts["updated_last_24h"] = [
            {"key": i.key, "summary": i.summary, "status": i.status, "owner": _who(i)} for i in recent
        ]
    return facts


def render_standup(facts: dict) -> str:
    lines = [f"# Standup brief: {facts['project']} ({facts['as_of']})", ""]
    counts = ", ".join(f"{n} {s}" for s, n in sorted(facts["status_counts"].items()))
    lines += [f"Sprint status: {counts}.", ""]

    if "changes_since_last_brief" in facts:
        lines += _render_changes(facts["changes_since_last_brief"])
    else:
        lines.append("## Updated in the last 24 hours (no earlier snapshot to compare with)")
        if facts["updated_last_24h"]:
            lines += [
                f"- {m['key']} {m['summary']}: now {m['status']} ({m['owner']})" for m in facts["updated_last_24h"]
            ]
        else:
            lines.append("- No unflagged issues were updated.")
        lines.append("")

    lines.append("## Needs attention")
    if facts["needs_attention"]:
        for n in facts["needs_attention"]:
            idle = f", idle {n['days_idle']}d" if n["days_idle"] else ""
            lines.append(f"- {n['key']} {n['summary']} [{', '.join(n['flags'])}] ({n['owner']}, {n['status']}{idle})")
    else:
        lines.append("- Nothing flagged.")
    lines.append("")

    if facts["overloaded"]:
        lines.append("## Over capacity")
        lines += [f"- {o['name']}: {'; '.join(o['why'])}" for o in facts["overloaded"]]
        lines.append("")
    return "\n".join(lines)


def _render_changes(ch: dict) -> list[str]:
    since = datetime.fromisoformat(ch["since"]).strftime("%d %b %H:%M")
    lines = [f"## Since last brief ({since})"]
    lines += [f"- {c['key']} {c['summary']}: {c['from']} -> {c['to']}" for c in ch["status_changes"]]
    lines += [f"- {c['key']} {c['summary']}: owner {c['from']} -> {c['to']}" for c in ch["owner_changes"]]
    lines += [f"- {c['key']} {c['summary']}: added to sprint" for c in ch["added"]]
    lines += [f"- {c['key']} {c['summary']}: removed from sprint" for c in ch["removed"]]
    if len(lines) == 1:
        lines.append("- No status or owner changes.")
    return lines + [""]


def render_health(
    findings: list[Finding], sprint: list[Issue], team: TeamMap, loads: dict[str, Load], now: datetime
) -> str:
    open_issues = [i for i in sprint if not i.is_done]
    lines = [
        f"# Sprint health: {team.settings.project_key}",
        "",
        f"{len(open_issues)} open of {len(sprint)} issues; {len(findings)} need attention.",
        "",
        "## Findings (most urgent first)",
    ]
    if not findings:
        lines.append("- None.")
    for f in findings:
        idle = days_since_update(f.issue, now)
        lines.append(
            f"- **{f.issue.key}** {f.issue.summary} | {f.issue.status} | {_who(f.issue)} | "
            f"{', '.join(f.flags)}" + (f" | idle {idle}d" if idle else "")
        )
    lines += [
        "",
        "## Workload",
        "",
        "| Person | In progress | WIP limit | Points | Capacity |",
        "|---|---|---|---|---|",
    ]
    for m in team.members:
        ld = loads[m.account_id]
        lines.append(f"| {m.name} | {ld.in_progress} | {m.max_in_progress} | {ld.points:g} | {m.capacity_points:g} |")
    return "\n".join(lines) + "\n"


def render_triage(backlog: list[Issue], team: TeamMap) -> str:
    buckets: dict[str, list[Issue]] = {"ready": [], "needs-owner": [], "needs-detail": [], "blocked": []}
    for issue in backlog:
        buckets[triage_bucket(issue, team.settings)].append(issue)
    lines = [f"# Backlog triage: {team.settings.project_key}", ""]
    notes = {
        "ready": "Has owner, estimate and component/label. Sprint candidates.",
        "needs-owner": "Well defined but nobody owns it. Run suggest-owner on these.",
        "needs-detail": "Missing an estimate or any component/label.",
        "blocked": "Blocked status; resolve the dependency first.",
    }
    for name, items in buckets.items():
        lines += [f"## {name} ({len(items)})", f"_{notes[name]}_", ""]
        lines += [f"- {i.key} [{i.priority or '-'}] {i.summary}" for i in items] or ["- none"]
        lines.append("")
    return "\n".join(lines)


def render_proposals(title: str, proposals: list[Proposal], team: TeamMap, plan_path: str | None) -> str:
    lines = [f"# {title}", ""]
    if not proposals:
        lines.append("No reassignments proposed.")
        return "\n".join(lines) + "\n"
    for p in proposals:
        blockers = p.blockers(team)
        lines += [
            f"## {p.issue.key}: {p.issue.summary}",
            f"- Why looked at: {p.reason}",
            f"- Current owner: {_who(p.issue)}",
            f"- Proposed owner: **{p.best.member.name}** (score {p.best.score}, confidence {p.confidence})",
        ]
        lines += [f"  - {r}" for r in p.best.reasons + p.best.risks]
        if p.alternatives:
            alts = ", ".join(
                f"{a.member.name} ({a.score}" + (f", {'; '.join(a.hard_blockers)})" if a.hard_blockers else ")")
                for a in p.alternatives
            )
            lines.append(f"- Next best: {alts}")
        if blockers:
            lines.append(f"- Needs explicit approval: {'; '.join(blockers)}")
        lines.append("- Status: PENDING APPROVAL")
        lines.append("")
    if plan_path:
        lines.append(f"Plan saved to `{plan_path}`. Review it, then run:")
        lines.append(
            f'`sprint-assistant apply --plan {plan_path} --approved-by "<approver>"` (dry run), add `--execute` to write.'
        )
    return "\n".join(lines) + "\n"


def render_apply(results: list[dict], executed: bool) -> str:
    lines = [
        "# Apply " + ("(executed)" if executed else "(dry run)"),
        "",
        "| Issue | Target | Action | Why |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r['issue']} | {r['to']} | {r['action']} | {r['why']} |" for r in results]
    return "\n".join(lines) + "\n"
