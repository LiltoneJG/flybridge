from __future__ import annotations

import json
import subprocess

import pytest
from flybridge_github import GitHubPullRequestError, GitHubPullRequests, PullRequestQueryFailure


def _pr_page(*nodes: dict, has_next: bool = False, cursor: str | None = None) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": list(nodes),
                }
            }
        }
    }


def _pr(number: int, head: str, **fields: object) -> dict:
    node = {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://example.test/{number}",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "headRefName": head,
        "author": {"login": "alice"},
        "assignees": {"nodes": [{"login": "alice"}]},
        "comments": {"totalCount": 2},
        "reviewThreads": {"nodes": [{"isResolved": True}, {"isResolved": False}]},
        "statusCheckRollup": {
            "state": "SUCCESS",
            "contexts": {
                "nodes": [
                    {
                        "__typename": "CheckRun",
                        "name": "ci",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "isRequired": True,
                    }
                ]
            },
        },
    }
    node.update(fields)
    return node


def test_list_open_matches_head_and_checks() -> None:
    response = _pr_page(_pr(1, "feature"), _pr(2, "other"))

    def runner(arguments, **_kwargs):
        assert arguments[0] == "gh"
        assert "query=" in arguments[arguments.index("-f") + 1]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    facts, failures = GitHubPullRequests(runner=runner).list_open(["example/repo"])
    matched = facts["example/repo"]

    assert failures == ()
    assert [fact.number for fact in matched] == [1, 2]
    assert matched[0].unresolved_review_threads == 1
    assert matched[0].issue_comment_count == 2
    assert matched[0].checks[0].name == "ci"
    assert matched[0].checks[0].is_required is True
    assert "isRequired" not in GitHubPullRequests._NODE_FIELDS
    assert matched[0].assignees == ("alice",)
    assert matched[0].author == "alice"


def test_list_open_pages_until_complete() -> None:
    pages = [
        _pr_page(_pr(1, "a"), has_next=True, cursor="c1"),
        _pr_page(_pr(2, "b")),
    ]
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(pages[len(calls) - 1]), "")

    facts, failures = GitHubPullRequests(runner=runner).list_open(["example/repo"])

    assert failures == ()
    assert [fact.number for fact in facts["example/repo"]] == [1, 2]
    assert any("cursor=c1" in token for token in calls[1])


def test_list_open_rejects_unsafe_repository_names() -> None:
    with pytest.raises(GitHubPullRequestError, match="unsafe"):
        GitHubPullRequests().list_open(["{owner}/repo"])


def test_list_open_rejects_graphql_errors() -> None:
    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"errors": [{"message": "nope"}]}), ""
        )

    facts, failures = GitHubPullRequests(runner=runner).list_open(["example/repo"])

    assert facts == {}
    assert failures == (
        PullRequestQueryFailure("example/repo", "GitHub pull request GraphQL query failed: nope"),
    )


def test_list_open_keeps_other_repositories_when_one_query_fails() -> None:
    good = _pr_page(_pr(3, "feature"))

    def runner(arguments, **_kwargs):
        if "name=missing" in arguments:
            return subprocess.CompletedProcess(
                arguments,
                1,
                "",
                "gh: Could not resolve to a Repository with the name 'example/missing'.",
            )
        if "name=repo" in arguments:
            return subprocess.CompletedProcess(arguments, 0, json.dumps(good), "")
        raise AssertionError(arguments)

    facts, failures = GitHubPullRequests(runner=runner).list_open(
        ["example/missing", "example/repo"]
    )

    assert [fact.number for fact in facts["example/repo"]] == [3]
    assert "example/missing" not in facts
    assert failures == (
        PullRequestQueryFailure(
            "example/missing",
            "gh: Could not resolve to a Repository with the name 'example/missing'.",
        ),
    )


def test_list_by_head_returns_merged_pull_requests() -> None:
    response = {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [_pr(8, "feature", state="MERGED")],
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        assert "head=feature" in arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    facts, failure = GitHubPullRequests(runner=runner).list_by_head("example/repo", "feature")

    assert failure is None
    assert facts[0].number == 8
    assert facts[0].state == "MERGED"


def test_get_reads_a_closed_pull_request_by_number() -> None:
    response = {
        "data": {
            "repository": {
                "pullRequest": _pr(186, "topic", state="CLOSED"),
            }
        }
    }

    def runner(arguments, **_kwargs):
        assert "number=186" in arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    fact, failure = GitHubPullRequests(runner=runner).get("example/repo", 186)

    assert failure is None
    assert fact is not None
    assert fact.state == "CLOSED"


def test_actor_is_bot_covers_app_bot_and_human() -> None:
    assert GitHubPullRequests.actor_is_bot({"__typename": "Bot", "login": "org-ci-app"})
    assert GitHubPullRequests.actor_is_bot(
        {"__typename": "User", "login": "org-ci-app", "resourcePath": "/apps/org-ci-app"}
    )
    assert GitHubPullRequests.actor_is_bot(
        {"__typename": "User", "login": "github-actions[bot]", "resourcePath": "/github-actions"}
    )
    assert not GitHubPullRequests.actor_is_bot(
        {"__typename": "User", "login": "alice", "resourcePath": "/alice"}
    )
    assert not GitHubPullRequests.actor_is_bot(None)


def test_get_review_facts_parses_authors_threads_and_truncation() -> None:
    response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "body": "Summary",
                    "baseRefName": "main",
                    "headRefName": "feature",
                    "baseRef": {"compare": {"behindBy": 2}},
                    "reviews": {
                        "pageInfo": {"hasNextPage": True},
                        "nodes": [
                            {
                                "author": {
                                    "__typename": "Bot",
                                    "login": "org-ci-app",
                                    "resourcePath": "/apps/org-ci-app",
                                },
                                "authorAssociation": "NONE",
                                "state": "COMMENTED",
                                "submittedAt": "2026-09-18T00:00:00Z",
                                "body": "AI snapshot",
                            },
                            {
                                "author": {
                                    "__typename": "User",
                                    "login": "alice",
                                    "resourcePath": "/alice",
                                },
                                "authorAssociation": "MEMBER",
                                "state": "APPROVED",
                                "submittedAt": "2026-09-18T01:00:00Z",
                                "body": "Approved",
                            },
                        ],
                    },
                    "comments": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "author": {
                                    "__typename": "Bot",
                                    "login": "github-actions",
                                    "resourcePath": "/apps/github-actions",
                                },
                                "authorAssociation": "NONE",
                                "createdAt": "2026-09-18T02:00:00Z",
                                "body": "CI ok",
                            }
                        ],
                    },
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "isResolved": False,
                                "isOutdated": True,
                                "resolvedBy": None,
                                "comments": {
                                    "pageInfo": {"hasNextPage": False},
                                    "nodes": [
                                        {
                                            "author": {
                                                "__typename": "User",
                                                "login": "bob",
                                                "resourcePath": "/bob",
                                            },
                                            "authorAssociation": "MEMBER",
                                            "createdAt": "2026-09-18T03:00:00Z",
                                            "body": "Please fix",
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        assert "number=9" in arguments
        assert "head=feature" in arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    facts, failure = GitHubPullRequests(runner=runner).get_review_facts(
        "example/repo", 9, head_ref_name="feature"
    )

    assert failure is None
    assert facts is not None
    assert facts["body"] == "Summary"
    assert facts["behind_by"] == 2
    assert facts["truncated"] is True
    assert facts["reviews"][0]["is_bot"] is True
    assert facts["reviews"][1]["is_bot"] is False
    assert facts["reviews"][1]["state"] == "APPROVED"
    assert facts["checks"] == []
    assert facts["comments"][0]["is_bot"] is True
    assert facts["threads"][0]["is_resolved"] is False
    assert facts["threads"][0]["comments"][0]["login"] == "bob"


def test_get_review_facts_returns_failure_without_raising() -> None:
    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 1, "", "gh: HTTP 502")

    facts, failure = GitHubPullRequests(runner=runner).get_review_facts("example/repo", 9)

    assert facts is None
    assert failure is not None
    assert failure.repository == "example/repo"
    assert "HTTP 502" in failure.message


def test_list_authored_pages_open_pull_requests() -> None:
    node = _pr(4, "feature")
    node["__typename"] = "PullRequest"
    node["repository"] = {"nameWithOwner": "example/repo"}
    pages = [
        {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    "nodes": [node],
                }
            }
        },
        {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "c2"},
                    "nodes": [{**node, "number": 5, "url": "https://example.test/5"}],
                }
            }
        },
    ]
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        query = arguments[arguments.index("-f") + 1]
        assert "search(" in query
        return subprocess.CompletedProcess(arguments, 0, json.dumps(pages[len(calls) - 1]), "")

    facts = GitHubPullRequests(runner=runner).list_authored("alice", ("OPEN",))

    assert [fact.number for fact in facts] == [4, 5]
    assert facts[0].author == "alice"
    assert facts[0].repository == "example/repo"
    assert any("q=author:alice is:pr is:open" in token for token in calls[0])
    assert any("cursor=c1" in token for token in calls[1])


def test_list_authored_rejects_unsafe_author() -> None:
    with pytest.raises(GitHubPullRequestError, match="unsafe"):
        GitHubPullRequests().list_authored("{owner}")


def test_list_open_marks_stale_base_ref() -> None:
    response = _pr_page(
        _pr(
            1,
            "feature",
            baseRefName="main",
            baseRefOid="aaa111",
            baseRef={"target": {"oid": "bbb222"}},
        ),
        _pr(
            2,
            "other",
            baseRefName="main",
            baseRefOid="ccc333",
            baseRef={"target": {"oid": "ccc333"}},
        ),
    )

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    facts, failures = GitHubPullRequests(runner=runner).list_open(["example/repo"])
    matched = facts["example/repo"]

    assert failures == ()
    assert matched[0].base_ref_stale is True
    assert matched[0].base_ref_oid == "aaa111"
    assert matched[0].base_ref_tip_oid == "bbb222"
    assert matched[1].base_ref_stale is False


def test_list_open_retries_transient_github_errors_once() -> None:
    pages = [
        subprocess.CompletedProcess(["gh"], 1, "", "gh: HTTP 502"),
        subprocess.CompletedProcess(
            ["gh"], 0, json.dumps(_pr_page(_pr(1, "feature", baseRefName="main"))), ""
        ),
    ]
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return pages[len(calls) - 1]

    facts, failures = GitHubPullRequests(runner=runner).list_open(["example/repo"])

    assert failures == ()
    assert [fact.number for fact in facts["example/repo"]] == [1]
    assert len(calls) == 2


def test_refresh_base_patches_the_recorded_base_ref() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if "graphql" in arguments:
            payload = {
                "data": {
                    "repository": {
                        "pullRequest": _pr(9, "feature", baseRefName="main"),
                    }
                }
            }
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        assert arguments[arguments.index("-X") + 1] == "PATCH"
        assert "repos/example/repo/pulls/9" in arguments
        assert "base=main" in arguments
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    payload = GitHubPullRequests(runner=runner).refresh_base("example/repo", 9)

    assert payload == {
        "repository": "example/repo",
        "number": 9,
        "base_ref_name": "main",
        "refreshed": True,
    }
    assert any("PATCH" in argument for call in calls for argument in call)
