"""Command-line entry point: `sprint-assistant <command> [options]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import llm, notify, plans, report, snapshots
from .config import ConfigError, load_team_map
from .jira_client import FixtureClient, JiraClient
from .signals import health_findings, workload


def _client(args, team):
    if args.fixture:
        return FixtureClient(args.fixture, team.settings)
    return JiraClient.from_env(team.settings)


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)


def cmd_validate(args, team, client) -> int:
    s = team.settings
    print(f"OK: project {s.project_key}, {len(team.members)} members, approvers: {', '.join(s.approvers)}")
    return 0


def cmd_health(args, team, client) -> int:
    now = client.now()
    sprint = client.sprint_issues()
    findings = health_findings(sprint, team.settings, now)
    text = report.render_health(findings, sprint, team, workload(sprint, team), now)
    _emit(text, args.out)
    if args.post_slack:
        notify.post_to_slack(text)
        print("Posted to Slack.", file=sys.stderr)
    return 0


def cmd_triage(args, team, client) -> int:
    _emit(report.render_triage(client.backlog_issues(), team), args.out)
    return 0


SNAPSHOT_DIR = "output/snapshots"


def cmd_standup(args, team, client) -> int:
    now = client.now()
    sprint = client.sprint_issues()
    findings = health_findings(sprint, team.settings, now)

    previous_path = args.previous or snapshots.latest(SNAPSHOT_DIR)
    changes = snapshots.diff(snapshots.load(previous_path), sprint) if previous_path else None

    facts = report.facts_for_standup(sprint, findings, workload(sprint, team), team, now, changes)
    text = report.render_standup(facts)
    if args.llm:
        brief, warnings = llm.write_brief(facts, args.model)
        for w in warnings:
            print(f"warning: {w}; using the template brief instead.", file=sys.stderr)
        text = brief or text
    _emit(text, args.out)

    if not args.no_snapshot:
        snapshots.save(snapshots.take(sprint, now), SNAPSHOT_DIR)
    if args.post_slack:
        notify.post_to_slack(text)
        print("Posted to Slack.", file=sys.stderr)
    return 0


def _plan_and_report(args, team, client, proposals, title, source) -> int:
    plan_path = None
    if proposals:
        plan = plans.build_plan(proposals, team, client.now(), source)
        plan_path = args.plan_out or f"output/plan-{plan['plan_id']}.json"
        plans.save_plan(plan, plan_path)
    _emit(report.render_proposals(title, proposals, team, plan_path), args.out)
    return 0


def cmd_suggest_owner(args, team, client) -> int:
    today = client.now().date()
    sprint = client.sprint_issues()
    pool = {i.key: i for i in sprint + client.backlog_issues()}
    if args.issues:
        missing = [k for k in args.issues if k not in pool]
        if missing:
            print(f"Not found in the sprint or backlog: {', '.join(missing)}", file=sys.stderr)
            return 2
        targets = [pool[k] for k in args.issues]
    else:
        targets = [i for i in sprint if not i.assignee_id and not i.is_done]
    proposals = plans.suggest_owners(targets, team, sprint, client.recently_done(), today)
    return _plan_and_report(args, team, client, proposals, "Owner suggestions", "suggest-owner")


def cmd_balance(args, team, client) -> int:
    proposals = plans.balance_load(client.sprint_issues(), team, client.recently_done(), client.now().date())
    return _plan_and_report(args, team, client, proposals, "Workload balancing", "balance")


def cmd_apply(args, team, client) -> int:
    plan = plans.load_plan(args.plan)
    try:
        results = plans.apply_plan(
            plan, client, team, args.approved_by, set(args.approve or []), args.execute, client.now()
        )
    except plans.ApprovalError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 3
    _emit(report.render_apply(results, args.execute), args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sprint-assistant", description="Jira sprint health and approval-gated reassignment."
    )
    p.add_argument("--team-map", default="team-map.yaml", help="team map YAML (default: team-map.yaml)")
    p.add_argument("--fixture", help="read issues from this JSON file instead of Jira")
    p.add_argument("--out", help="write the report here instead of printing it")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check the team map")
    s = sub.add_parser("health", help="flag blocked, stale, overdue and unassigned sprint work")
    s.add_argument("--post-slack", action="store_true", help="also post to $SLACK_WEBHOOK_URL")
    sub.add_parser("triage", help="sort the backlog into ready / needs-owner / needs-detail / blocked")

    s = sub.add_parser("standup", help="daily brief")
    s.add_argument("--llm", action="store_true", help="have an LLM write the brief from the computed facts")
    s.add_argument("--model", help="model name (default: $SPRINT_ASSISTANT_MODEL or claude-sonnet-5-5)")
    s.add_argument("--previous", help="snapshot to compare with (default: latest in output/snapshots)")
    s.add_argument("--no-snapshot", action="store_true", help="don't save a snapshot for next time")
    s.add_argument("--post-slack", action="store_true", help="also post to $SLACK_WEBHOOK_URL")

    s = sub.add_parser("suggest-owner", help="propose owners; writes a plan file, never changes Jira")
    s.add_argument("issues", nargs="*", help="issue keys (default: unassigned sprint issues)")
    s.add_argument("--plan-out")

    s = sub.add_parser("balance", help="propose moves off overloaded people; writes a plan file")
    s.add_argument("--plan-out")

    s = sub.add_parser("apply", help="apply a plan file after approval checks (dry run unless --execute)")
    s.add_argument("--plan", required=True)
    s.add_argument("--approved-by", required=True)
    s.add_argument(
        "--approve", action="append", metavar="ISSUE", help="explicitly approve a flagged or low-confidence item"
    )
    s.add_argument("--execute", action="store_true", help="actually write to Jira")
    return p


COMMANDS = {
    "validate": cmd_validate,
    "health": cmd_health,
    "triage": cmd_triage,
    "standup": cmd_standup,
    "suggest-owner": cmd_suggest_owner,
    "balance": cmd_balance,
    "apply": cmd_apply,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        team = load_team_map(args.team_map)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    client = None if args.command == "validate" else _client(args, team)
    return COMMANDS[args.command](args, team, client)


if __name__ == "__main__":
    sys.exit(main())
