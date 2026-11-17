from pathlib import Path

import pytest

from sprint_assistant.config import load_team_map
from sprint_assistant.jira_client import FixtureClient

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def team():
    return load_team_map(EXAMPLES / "team-map.example.yaml")


@pytest.fixture
def client(team):
    return FixtureClient(EXAMPLES / "sample_sprint.json", team.settings)
