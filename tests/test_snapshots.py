from sprint_assistant import llm, report, snapshots
from sprint_assistant.signals import health_findings, workload

from .conftest import EXAMPLES


def test_diff_finds_status_owner_added_removed(client):
    prev = snapshots.load(EXAMPLES / "snapshot-previous.json")
    ch = snapshots.diff(prev, client.sprint_issues())
    assert {(c["key"], c["from"], c["to"]) for c in ch["status_changes"]} == {
        ("SHOP-101", "To Do", "In Progress"),
        ("SHOP-111", "In Review", "Done"),
        ("SHOP-112", "In Progress", "Blocked"),
    }
    assert [(c["key"], c["to"]) for c in ch["owner_changes"]] == [("SHOP-113", "Dev Patil")]
    assert [c["key"] for c in ch["added"]] == ["SHOP-116"]
    assert [c["key"] for c in ch["removed"]] == ["SHOP-105"]


def test_save_and_find_latest(client, tmp_path):
    first = snapshots.take(client.sprint_issues(), client.now())
    snapshots.save(first, tmp_path)
    later = dict(first, taken_at="2026-11-19T10:00:00+05:30")
    newest = snapshots.save(later, tmp_path)
    assert snapshots.latest(tmp_path) == newest
    assert snapshots.latest(tmp_path / "empty") is None


def test_standup_uses_changes_and_llm_guard_knows_their_keys(team, client):
    sprint = client.sprint_issues()
    ch = snapshots.diff(snapshots.load(EXAMPLES / "snapshot-previous.json"), sprint)
    facts = report.facts_for_standup(
        sprint, health_findings(sprint, team.settings, client.now()), workload(sprint, team), team, client.now(), ch
    )
    text = report.render_standup(facts)
    assert "Since last brief" in text and "SHOP-105 Export orders as CSV: removed" in text
    assert "updated_last_24h" not in facts
    # SHOP-105 only appears in the changes, so the LLM is allowed to mention it.
    assert "SHOP-105" in llm.known_keys(facts)
