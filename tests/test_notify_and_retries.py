import pytest
import requests

from sprint_assistant.jira_client import JiraClient
from sprint_assistant.notify import to_slack_format


def test_slack_format():
    md = "# Standup brief\n\n## Needs attention\n- **SHOP-1** stuck"
    assert to_slack_format(md) == "*Standup brief*\n\n*Needs attention*\n• *SHOP-1* stuck"


class Resp:
    def __init__(self, status, headers=None, data=None):
        self.status_code, self.headers, self._data = status, headers or {}, data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._data


def client_with(team, responses):
    waits = []
    c = JiraClient("https://x.atlassian.net", "e", "t", team.settings, sleep=waits.append)
    queue = list(responses)

    def fake_request(method, url, timeout=None, **kw):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    c.session.request = fake_request
    return c, waits


def test_retries_on_rate_limit_using_retry_after(team):
    ok = Resp(200, data={"issues": [], "isLast": True})
    c, waits = client_with(team, [Resp(429, {"Retry-After": "7"}), Resp(503), ok])
    assert c.sprint_issues() == []
    assert waits == [7.0, 2]


def test_retries_connection_errors_then_gives_up(team):
    c, waits = client_with(team, [requests.ConnectionError()] * 4)
    with pytest.raises(requests.ConnectionError):
        c.sprint_issues()
    assert waits == [1, 2, 4]


def test_does_not_retry_auth_errors(team):
    c, waits = client_with(team, [Resp(401)])
    with pytest.raises(requests.HTTPError):
        c.get_issue("SHOP-1")
    assert waits == []
