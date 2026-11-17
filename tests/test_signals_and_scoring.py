from sprint_assistant.plans import balance_load, suggest_owners
from sprint_assistant.scoring import confidence, rank_candidates
from sprint_assistant.signals import health_findings, issue_flags, overloaded, workload


def by_key(issues):
    return {i.key: i for i in issues}


def test_flags(team, client):
    now = client.now()
    issues = by_key(client.sprint_issues())
    assert issue_flags(issues["SHOP-112"], team.settings, now) == ["blocked"]
    assert set(issue_flags(issues["SHOP-115"], team.settings, now)) == {"unassigned", "stale", "overdue"}
    assert issue_flags(issues["SHOP-110"], team.settings, now) == ["stuck-in-review"]
    assert issue_flags(issues["SHOP-117"], team.settings, now) == ["no-estimate"]
    assert issue_flags(issues["SHOP-111"], team.settings, now) == []  # done


def test_findings_sorted_by_severity(team, client):
    findings = health_findings(client.sprint_issues(), team.settings, client.now())
    assert findings[0].issue.key == "SHOP-112"  # blocked comes first
    assert findings[-1].issue.key == "SHOP-117"  # missing estimate comes last


def test_workload_and_overload(team, client):
    loads = workload(client.sprint_issues(), team)
    assert loads["acc-asha"].points == 16
    assert loads["acc-dev"].in_progress == 3
    assert [aid for aid, _ in overloaded(team, loads)] == ["acc-asha"]


def test_unavailable_person_is_never_top_pick(team, client):
    issues = by_key(client.sprint_issues())
    loads = workload(client.sprint_issues(), team)
    ranked = rank_candidates(issues["SHOP-114"], team, loads, client.recently_done(), client.now().date())
    meera = next(c for c in ranked if c.member.name == "Meera Iyer")
    assert meera.score > ranked[0].score  # she is the best fit on paper...
    assert ranked[0].member.name != "Meera Iyer"  # ...but is away, so not picked
    assert "unavailable today" in meera.hard_blockers


def test_confidence_rules(team, client):
    issues = by_key(client.sprint_issues())
    loads = workload(client.sprint_issues(), team)
    ranked = rank_candidates(issues["SHOP-116"], team, loads, client.recently_done(), client.now().date())
    assert ranked[0].member.name == "Rohan Mehta"
    assert confidence(ranked[0], ranked[1]) == "high"

    tie_a, tie_b = ranked[0], ranked[0]
    assert confidence(tie_a, tie_b) == "low"


def test_suggestions_account_for_earlier_suggestions(team, client):
    sprint = client.sprint_issues()
    targets = [i for i in sprint if not i.assignee_id]
    props = {p.issue.key: p for p in suggest_owners(targets, team, sprint, client.recently_done(), client.now().date())}
    # SHOP-114 goes to Dev first; that fills Dev's capacity, so SHOP-115 can't
    # also go to Dev cleanly and ends up a low-confidence tie.
    assert props["SHOP-114"].best.member.name == "Dev Patil"
    assert props["SHOP-115"].confidence == "low"
    assert "protected label: security" in props["SHOP-115"].blockers(team)


def test_balance_moves_low_priority_unstarted_work(team, client):
    props = balance_load(client.sprint_issues(), team, client.recently_done(), client.now().date())
    assert [(p.issue.key, p.best.member.name) for p in props] == [("SHOP-103", "Dev Patil")]
