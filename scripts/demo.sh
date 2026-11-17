#!/usr/bin/env bash
# Walk through every command on the sample data. No Jira account or API key needed.
# Usage: ./scripts/demo.sh   (from the repo root)
set -euo pipefail

cd "$(dirname "$0")/.."
rm -rf output
SA="sprint-assistant --team-map examples/team-map.example.yaml --fixture examples/sample_sprint.json"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Validate the team map"
$SA validate

step "Sprint health"
$SA health

step "Standup brief, compared with yesterday's snapshot"
$SA standup --previous examples/snapshot-previous.json

step "Backlog triage"
$SA triage

step "Suggest owners for unassigned sprint work (writes a plan, changes nothing)"
$SA suggest-owner --plan-out output/plan.json

step "Apply as someone who isn't an approver (refused)"
$SA apply --plan output/plan.json --approved-by "Random Person" || true

step "Dry run as the approver"
$SA apply --plan output/plan.json --approved-by "Priya Nair"

step "Execute, explicitly approving the security ticket"
$SA apply --plan output/plan.json --approved-by "Priya Nair" --approve SHOP-115 --execute

step "Audit log"
cat output/audit.jsonl

step "Balance load"
$SA balance --plan-out output/balance-plan.json
