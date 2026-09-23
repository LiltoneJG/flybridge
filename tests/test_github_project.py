from __future__ import annotations

import json
import subprocess

import pytest
from flybridge_core.config import GitHubBoard, GitHubConfig
from flybridge_github import GitHubProject, GitHubProjectError


@pytest.fixture(autouse=True)
def _skip_configured_login_token(monkeypatch) -> None:
    monkeypatch.setattr("flybridge_github.project.GitHubCli.token", lambda self: None)


def _config(*, login: str = "alice", boards: tuple[GitHubBoard, ...] | None = None) -> GitHubConfig:
    return GitHubConfig(
        True,
        login=login,
        boards=boards or (GitHubBoard("example", "user", 1),),
    )


def _issue(
    number: int,
    *,
    title: str = "Task",
    status: object = None,
    priority: object = None,
    assignees: list[str] | None = None,
) -> dict:
    return {
        "content": {
            "__typename": "Issue",
            "number": number,
            "title": title,
            "url": f"https://example.test/{number}",
            "repository": {"nameWithOwner": "example/repo"},
            "assignees": {"nodes": [{"login": login} for login in (assignees or ["alice"])]},
        },
        "status": {"name": "Todo"} if status is None else status,
        "priority": {"name": "High"} if priority is None else priority,
    }


def _page(
    *nodes: dict, owner: str = "user", has_next: bool = False, cursor: str | None = None
) -> dict:
    return {
        "data": {
            owner: {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        "nodes": list(nodes),
                    }
                }
            }
        }
    }


def test_screen_defaults_to_the_configured_login() -> None:
    response = _page(
        _issue(1, assignees=["alice"]),
        _issue(2, assignees=["bob"]),
    )

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    candidates = GitHubProject(_config(), runner=runner).screen(assignee="alice")

    assert [candidate.number for candidate in candidates] == [1]
    assert candidates[0].assignees == ("alice",)
    assert candidates[0].status == "Todo"
    assert candidates[0].owner == "example"
    assert candidates[0].project_number == 1


def test_screen_all_assignees_keeps_every_issue() -> None:
    response = _page(_issue(1, assignees=["alice"]), _issue(2, assignees=["bob"]))

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    candidates = GitHubProject(_config(), runner=runner).screen(assignee=None)

    assert [candidate.number for candidate in candidates] == [1, 2]


def test_screen_filters_status_and_priority_as_or_within_a_field() -> None:
    response = _page(
        _issue(1, status={"name": "Todo"}, priority={"name": "High"}),
        _issue(2, status={"name": "In progress"}, priority={"name": "High"}),
        _issue(3, status={"name": "Todo"}, priority={"name": "Low"}),
    )

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    candidates = GitHubProject(_config(), runner=runner).screen(
        assignee=None,
        statuses=("Todo",),
        priorities=("High", "Critical"),
    )

    assert [candidate.number for candidate in candidates] == [1]


def test_disabled_project_integration_cannot_screen() -> None:
    with pytest.raises(GitHubProjectError, match="disabled"):
        GitHubProject(GitHubConfig(False))


def test_screen_accepts_null_field_values() -> None:
    response = _page({"content": None, "status": None, "priority": None})

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    assert GitHubProject(_config(), runner=runner).screen(assignee=None) == []


def test_screen_rejects_a_repeated_pagination_cursor() -> None:
    response = _page(has_next=True, cursor="same")

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    with pytest.raises(GitHubProjectError, match="did not advance"):
        GitHubProject(_config(), runner=runner).screen(assignee=None)


def test_screen_queries_configured_fields_directly_instead_of_truncating_field_values() -> None:
    response = _page(_issue(98))

    def runner(arguments, **_kwargs):
        query = arguments[arguments.index("-f") + 1]
        assert "fieldValueByName" in query
        assert "fieldValues(first:20)" not in query
        assert "IssueFieldSingleSelectValue" in query
        assert "assignees(first:20)" in query
        assert "statusField=Status" in arguments
        assert "priorityField=Priority" in arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    candidates = GitHubProject(_config(), runner=runner).screen(assignee="alice")

    assert [candidate.number for candidate in candidates] == [98]


def test_screen_skips_unreadable_status_or_priority_only_when_filtering() -> None:
    response = _page(
        _issue(1, title="Empty status object", status={}, priority={"name": "High"}),
        _issue(
            2,
            title="Issue-linked priority without a name",
            status={"name": "Todo"},
            priority={"__typename": "ProjectV2ItemIssueFieldValue"},
        ),
        _issue(
            3,
            title="Eligible with GraphQL typename metadata",
            status={"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": "Todo"},
            priority={"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": "High"},
        ),
        _issue(
            4,
            title="Native issue priority",
            status={"name": "Todo"},
            priority={
                "__typename": "ProjectV2ItemIssueFieldValue",
                "issueFieldValue": {
                    "__typename": "IssueFieldSingleSelectValue",
                    "name": "High",
                },
            },
        ),
    )

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    project = GitHubProject(_config(), runner=runner)
    filtered = project.screen(assignee=None, statuses=("Todo",), priorities=("High",))
    unfiltered = project.screen(assignee=None)

    assert [candidate.number for candidate in filtered] == [3, 4]
    assert [candidate.number for candidate in unfiltered] == [1, 2, 3, 4]
    assert unfiltered[0].status is None
    assert unfiltered[1].priority is None


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            {
                "content": {"__typename": "Issue"},
                "status": {"name": "Todo"},
                "priority": {"name": "High"},
            },
            "eligible GitHub Project issue",
        ),
        (
            {
                "content": {
                    "__typename": "Issue",
                    "number": 1,
                    "title": "Malformed",
                    "url": "https://example.test/1",
                    "repository": {"nameWithOwner": "example/repo"},
                    "assignees": {"nodes": []},
                },
                "status": "Todo",
                "priority": {"name": "High"},
            },
            "Status field value",
        ),
    ],
)
def test_screen_rejects_malformed_candidate_data(item: dict, message: str) -> None:
    response = _page(item)

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    with pytest.raises(GitHubProjectError, match=message):
        GitHubProject(_config(), runner=runner).screen(assignee=None)


def test_screen_intentionally_skips_non_issue_content() -> None:
    response = _page({"content": {"__typename": "PullRequest"}, "status": {"malformed": True}})

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    assert GitHubProject(_config(), runner=runner).screen(assignee=None) == []


def test_screen_uses_organization_owner_type_and_a_second_page() -> None:
    first = _page(_issue(1), owner="organization", has_next=True, cursor="page-one")
    second = _page(_issue(2), owner="organization")
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        payload = first if len(calls) == 1 else second
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    candidates = GitHubProject(
        _config(boards=(GitHubBoard("example-org", "organization", 1),)),
        runner=runner,
    ).screen(assignee="alice")

    assert [candidate.number for candidate in candidates] == [1, 2]
    assert "organization(login:$owner)" in calls[0][calls[0].index("-f") + 1]
    assert "cursor=page-one" in calls[1]


def test_screen_selects_one_configured_board() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        owner = "example" if "owner=example" in arguments else "other"
        number = 1 if owner == "example" else 2
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(_page(_issue(number), owner="organization" if owner == "other" else "user")),
            "",
        )

    candidates = GitHubProject(
        _config(
            boards=(
                GitHubBoard("example", "user", 1),
                GitHubBoard("other", "organization", 2),
            )
        ),
        runner=runner,
    ).screen(boards=["2"], assignee=None)

    assert [candidate.number for candidate in candidates] == [2]
    assert all("number=2" in call for call in calls)


def test_screen_rejects_an_unknown_board_selector() -> None:
    project = GitHubProject(_config(), runner=lambda *a, **k: None)

    with pytest.raises(GitHubProjectError, match="not configured"):
        project.screen(boards=["999"], assignee=None)


def test_screen_rejects_unsafe_gh_expansions() -> None:
    def runner(arguments, **_kwargs):
        raise AssertionError(f"gh should not run: {arguments}")

    project = GitHubProject(
        _config(boards=(GitHubBoard("@/etc/passwd", "user", 1),)),
        runner=runner,
    )

    with pytest.raises(GitHubProjectError, match="unsafe gh expansion"):
        project.screen(assignee=None)
