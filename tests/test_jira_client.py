from sprint_assistant.jira_client import JiraClient, parse_jira_datetime


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def raw(key):
    return {
        "key": key,
        "fields": {
            "summary": key,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "updated": "2026-11-17T10:00:00.000+0530",
            "customfield_10016": 2,
        },
    }


def test_search_follows_pagination_and_builds_jql(team):
    client = JiraClient("https://example.atlassian.net/", "me@example.com", "token", team.settings)
    pages = [
        {"issues": [raw("SHOP-1"), raw("SHOP-2")], "nextPageToken": "abc", "isLast": False},
        {"issues": [raw("SHOP-3")], "isLast": True},
    ]
    calls = []

    def fake_request(method, url, json=None, timeout=None, **kw):
        calls.append((url, json))
        return FakeResponse(pages[len(calls) - 1])

    client.session.request = fake_request
    issues = client.sprint_issues()

    assert [i.key for i in issues] == ["SHOP-1", "SHOP-2", "SHOP-3"]
    assert issues[0].points == 2
    assert calls[0][0] == "https://example.atlassian.net/rest/api/3/search/jql"
    assert 'project = "SHOP"' in calls[0][1]["jql"]
    assert "customfield_10016" in calls[0][1]["fields"]
    assert calls[1][1]["nextPageToken"] == "abc"


def test_parses_jira_timestamps():
    dt = parse_jira_datetime("2026-11-10T09:12:33.000+0530")
    assert dt.utcoffset().total_seconds() == 5.5 * 3600
