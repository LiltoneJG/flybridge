from __future__ import annotations

import json
import subprocess

from flybridge_github.issues import GitHubIssueDevelopment


def test_fetch_reads_linked_branches_prs_and_body_urls() -> None:
    payload = {
        "data": {
            "i0": {
                "issue": {
                    "url": "https://github.com/example/repo/issues/1",
                    "body": "See https://github.com/example/repo/pull/9",
                    "linkedBranches": {
                        "nodes": [
                            {
                                "ref": {
                                    "name": "feature/1-fix",
                                    "repository": {"nameWithOwner": "example/repo"},
                                }
                            }
                        ]
                    },
                    "timelineItems": {
                        "nodes": [
                            {
                                "__typename": "CrossReferencedEvent",
                                "source": {
                                    "__typename": "PullRequest",
                                    "number": 9,
                                    "title": "Fix",
                                    "url": "https://github.com/example/repo/pull/9",
                                    "state": "OPEN",
                                    "headRefName": "feature/1-fix",
                                    "repository": {"nameWithOwner": "example/repo"},
                                },
                            }
                        ]
                    },
                }
            }
        }
    }

    def runner(arguments, **_kwargs):
        assert arguments[0] == "gh"
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    development = GitHubIssueDevelopment(runner=runner).fetch(
        "https://github.com/example/repo/issues/1"
    )

    assert development.join_key == "example/repo#1"
    assert development.linked_branches[0].name == "feature/1-fix"
    assert development.pull_requests[0].number == 9
    assert development.pull_requests[0].state == "OPEN"
    assert development.body_urls == ("https://github.com/example/repo/pull/9",)


def test_fetch_many_batches_issue_queries() -> None:
    payload = {
        "data": {
            "i0": {
                "issue": {
                    "url": "https://github.com/example/repo/issues/1",
                    "body": "",
                    "linkedBranches": {"nodes": []},
                    "timelineItems": {"nodes": []},
                }
            },
            "i1": {
                "issue": {
                    "url": "https://github.com/example/repo/issues/2",
                    "body": "",
                    "linkedBranches": {"nodes": []},
                    "timelineItems": {"nodes": []},
                }
            },
        }
    }
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    fetched = GitHubIssueDevelopment(runner=runner).fetch_many(
        (
            "https://github.com/example/repo/issues/1",
            "https://github.com/example/repo/issues/2",
        )
    )

    assert len(calls) == 1
    assert "i0:" in calls[0][4]
    assert "i1:" in calls[0][4]
    assert fetched["example/repo#1"].issue_url.endswith("/issues/1")
    assert fetched["example/repo#2"].issue_url.endswith("/issues/2")
