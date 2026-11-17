import json
from datetime import timedelta

import pytest

from sprint_assistant.plans import ApprovalError, apply_plan, build_plan, load_plan, save_plan, suggest_owners


@pytest.fixture
def plan(team, client):
    sprint = client.sprint_issues()
    targets = [i for i in sprint if not i.assignee_id]
    props = suggest_owners(targets, team, sprint, client.recently_done(), client.now().date())
    return build_plan(props, team, client.now(), "test")


def rows(results):
    return {r["issue"]: r for r in results}


def test_dry_run_changes_nothing(team, client, plan):
    res = rows(apply_plan(plan, client, team, "Priya Nair", set(), execute=False, now=client.now()))
    assert res["SHOP-116"]["action"] == "would-assign"
    assert client.assignments == []


def test_unknown_approver_refused(team, client, plan):
    with pytest.raises(ApprovalError):
        apply_plan(plan, client, team, "Someone Else", set(), execute=True, now=client.now())
    assert client.assignments == []


def test_old_plan_refused(team, client, plan):
    later = client.now() + timedelta(hours=25)
    with pytest.raises(ApprovalError, match="older than"):
        apply_plan(plan, client, team, "Priya Nair", set(), execute=True, now=later)


def test_protected_and_low_confidence_need_explicit_approval(team, client, plan):
    res = rows(apply_plan(plan, client, team, "Priya Nair", set(), execute=True, now=client.now()))
    assert res["SHOP-115"]["action"] == "skip"
    assert "--approve SHOP-115" in res["SHOP-115"]["why"]
    assert ("SHOP-115", "acc-asha") not in client.assignments


def test_explicit_approval_applies(team, client, plan, tmp_path):
    team.settings.audit_log = str(tmp_path / "audit.jsonl")
    res = rows(apply_plan(plan, client, team, "Priya Nair", {"SHOP-115"}, execute=True, now=client.now()))
    assert res["SHOP-115"]["action"] == "assigned"
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == len(plan["items"])
    assert json.loads(lines[0])["approved_by"] == "Priya Nair"


def test_issue_changed_since_plan_is_skipped(team, client, plan):
    client.get_issue("SHOP-116").assignee_id = "acc-ira"  # someone grabbed it in Jira meanwhile
    res = rows(apply_plan(plan, client, team, "Priya Nair", set(), execute=True, now=client.now()))
    assert res["SHOP-116"]["action"] == "skip"
    assert "assignee changed" in res["SHOP-116"]["why"]


def test_edited_plan_file_is_rejected(plan, tmp_path):
    path = save_plan(plan, tmp_path / "plan.json")
    data = json.loads(path.read_text())
    data["items"][0]["to"] = "acc-ira"
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="edited"):
        load_plan(path)
