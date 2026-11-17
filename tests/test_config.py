import pytest

from sprint_assistant.config import ConfigError, load_team_map


def write(tmp_path, text):
    p = tmp_path / "team.yaml"
    p.write_text(text)
    return p


def test_example_team_map_loads(team):
    assert team.settings.project_key == "SHOP"
    assert len(team.members) == 5
    assert team.settings.approvers == ["Priya Nair"]


def test_reports_every_problem_at_once(tmp_path):
    path = write(
        tmp_path,
        """
jira: {project_key: shop-team}
team:
  - name: A
    account_id: x
    backup_account_id: nobody
  - name: B
    account_id: x
    max_in_progress: 0
""",
    )
    with pytest.raises(ConfigError) as exc:
        load_team_map(path)
    text = "\n".join(exc.value.problems)
    assert "not a valid Jira key" in text
    assert "duplicate account_id" in text
    assert "max_in_progress" in text
    assert "backup_account_id nobody" in text
    assert "approvers" in text


def test_bad_unavailable_dates(tmp_path):
    path = write(
        tmp_path,
        """
jira: {project_key: SHOP}
approval: {approvers: [Lead]}
team:
  - name: A
    account_id: a
    unavailable: [{from: 2026-11-20, to: 2026-11-18}]
""",
    )
    with pytest.raises(ConfigError, match="'to' is before 'from'"):
        load_team_map(path)
