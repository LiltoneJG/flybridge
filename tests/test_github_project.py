from __future__ import annotations

import json
import subprocess

import pytest
from flybridge_core.config import GitHubConfig
from flybridge_github import GitHubProject, GitHubProjectError


def test_screen_returns_only_configured_todo_high_priority_issues() -> None:
    response = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 1,
                                    "title": "Eligible",
                                    "url": "https://example.test/1",
                                    "repository": {"nameWithOwner": "example/repo"},
                                },
                                "status": {"name": "Todo"},
                                "priority": {"name": "High"},
                            },
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 2,
                                    "title": "Not eligible",
                                    "url": "https://example.test/2",
                                    "repository": {"nameWithOwner": "example/repo"},
                                },
                                "status": {"name": "Todo"},
                                "priority": {"name": "Low"},
                            },
                        ],
                    }
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    candidates = GitHubProject(
        GitHubConfig(True, "example", "user", 1, "Status", "Todo", "Priority", ("High",)),
        runner=runner,
    ).screen()

    assert candidates[0].number == 1
    assert candidates[0].priority == "High"


def test_disabled_project_integration_cannot_screen() -> None:
    with pytest.raises(GitHubProjectError, match="disabled"):
        GitHubProject(GitHubConfig(False))


def test_screen_accepts_null_field_values() -> None:
    response = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"content": None, "status": None, "priority": None}],
                    }
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    project = GitHubProject(GitHubConfig(True, "example", "user", 1), runner=runner)

    assert project.screen() == []


def test_screen_rejects_a_repeated_pagination_cursor() -> None:
    response = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "same"},
                        "nodes": [],
                    }
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    project = GitHubProject(GitHubConfig(True, "example", "user", 1), runner=runner)

    with pytest.raises(GitHubProjectError, match="did not advance"):
        project.screen()


def test_screen_queries_configured_fields_directly_instead_of_truncating_field_values() -> None:
    response = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 98,
                                    "title": "Eligible after many unrelated fields",
                                    "url": "https://example.test/98",
                                    "repository": {"nameWithOwner": "example/repo"},
                                },
                                "status": {"name": "Todo"},
                                "priority": {"name": "High"},
                            }
                        ],
                    }
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        query = arguments[arguments.index("-f") + 1]
        assert "fieldValueByName" in query
        assert "fieldValues(first:20)" not in query
        assert "statusField=Status" in arguments
        assert "priorityField=Priority" in arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    candidates = GitHubProject(
        GitHubConfig(True, "example", "user", 1, "Status", "Todo", "Priority", ("High",)),
        runner=runner,
    ).screen()

    assert [candidate.number for candidate in candidates] == [98]


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
                },
                "status": {},
                "priority": {"name": "High"},
            },
            "Status field value",
        ),
    ],
)
def test_screen_rejects_malformed_candidate_data(item: dict, message: str) -> None:
    response = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [item],
                    }
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    project = GitHubProject(
        GitHubConfig(True, "example", "user", 1, "Status", "Todo", "Priority", ("High",)),
        runner=runner,
    )

    with pytest.raises(GitHubProjectError, match=message):
        project.screen()


def test_screen_intentionally_skips_non_issue_content() -> None:
    response = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "content": {"__typename": "PullRequest"},
                                "status": {"malformed": True},
                            }
                        ],
                    }
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    project = GitHubProject(GitHubConfig(True, "example", "user", 1), runner=runner)

    assert project.screen() == []


def test_screen_uses_organization_owner_type_and_a_second_page() -> None:
    first = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "page-one"},
                        "nodes": [
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 1,
                                    "title": "First",
                                    "url": "https://example.test/1",
                                    "repository": {"nameWithOwner": "example/repo"},
                                },
                                "status": {"name": "Todo"},
                                "priority": {"name": "High"},
                            }
                        ],
                    }
                }
            }
        }
    }
    second = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "content": {
                                    "__typename": "Issue",
                                    "number": 2,
                                    "title": "Second",
                                    "url": "https://example.test/2",
                                    "repository": {"nameWithOwner": "example/repo"},
                                },
                                "status": {"name": "Todo"},
                                "priority": {"name": "High"},
                            }
                        ],
                    }
                }
            }
        }
    }
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        payload = first if len(calls) == 1 else second
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    candidates = GitHubProject(
        GitHubConfig(
            True, "example-org", "organization", 1, "Status", "Todo", "Priority", ("High",)
        ),
        runner=runner,
    ).screen()

    assert [candidate.number for candidate in candidates] == [1, 2]
    assert "organization(login:$owner)" in calls[0][calls[0].index("-f") + 1]
    assert "cursor=page-one" in calls[1]


def test_screen_rejects_unsafe_gh_expansions() -> None:
    def runner(arguments, **_kwargs):
        raise AssertionError(f"gh should not run: {arguments}")

    project = GitHubProject(
        GitHubConfig(True, "@/etc/passwd", "user", 1, "Status", "Todo", "Priority", ("High",)),
        runner=runner,
    )

    with pytest.raises(GitHubProjectError, match="unsafe gh expansion"):
        project.screen()
