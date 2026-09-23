from __future__ import annotations

from flybridge_application.board_refs import attach_issue_refs, worktree_comment_issue_urls


def _worktree(
    *,
    path: str,
    name: str,
    comment: str = "",
    repository: str = "example/cloud",
    branch: str = "feature",
    submodules: list[dict] | None = None,
    pull_requests: list[dict] | None = None,
    issues: list[dict] | None = None,
) -> dict:
    return {
        "orca": {
            "path": path,
            "name": name,
            "comment": comment,
            "github_hint": {"issues": issues or []},
        },
        "git": {
            "github_repository": repository,
            "branch": branch,
            "submodules": submodules or [],
        },
        "pull_requests": pull_requests or [],
    }


def test_board_refs_join_comment_issue_url() -> None:
    worktrees = [
        _worktree(
            path="/tmp/a",
            name="a",
            issues=[
                {
                    "repository": "example/cloud",
                    "number": 173,
                    "url": "https://github.com/example/cloud/issues/173",
                }
            ],
        )
    ]
    issues = [{"url": "https://github.com/example/cloud/issues/173"}]
    attached, unmatched = attach_issue_refs(issues, worktrees, {})

    assert attached[0]["worktrees"][0]["match"] == ["issue_url"]
    assert unmatched == []


def test_board_refs_join_project_issue_to_checkout_via_connected_pr() -> None:
    worktrees = [_worktree(path="/tmp/cloud", name="cloud", branch="feature/173-alerts")]
    issues = [{"url": "https://github.com/example/tracker/issues/90"}]
    development = {
        "example/tracker#90": {
            "pull_requests": [
                {
                    "repository": "example/cloud",
                    "number": 183,
                    "head_ref_name": "feature/173-alerts",
                }
            ],
            "linked_branches": [],
            "body_urls": ["https://github.com/example/cloud/issues/173"],
        }
    }
    attached, unmatched = attach_issue_refs(issues, worktrees, development)

    assert attached[0]["worktrees"][0]["path"] == "/tmp/cloud"
    assert "pull_request" in attached[0]["worktrees"][0]["match"]
    assert unmatched == []


def test_board_refs_join_parent_worktree_via_submodule_branch() -> None:
    worktrees = [
        _worktree(
            path="/tmp/parent",
            name="parent",
            branch="cloud-feature",
            submodules=[
                {
                    "path": "vendor",
                    "github_repository": "example/edge",
                    "branch": "edge-feature",
                }
            ],
        )
    ]
    issues = [{"url": "https://github.com/example/tracker/issues/68"}]
    development = {
        "example/tracker#68": {
            "pull_requests": [
                {
                    "repository": "example/edge",
                    "number": 92,
                    "head_ref_name": "edge-feature",
                }
            ],
            "linked_branches": [],
            "body_urls": [],
        }
    }
    attached, unmatched = attach_issue_refs(issues, worktrees, development)

    assert attached[0]["worktrees"][0]["path"] == "/tmp/parent"
    assert attached[0]["worktrees"][0]["match"] == ["pull_request"]
    assert unmatched == []


def test_board_refs_attaches_independent_clones_to_the_same_issue() -> None:
    worktrees = [
        _worktree(path="/tmp/cloud", name="cloud", repository="example/cloud", branch="feat"),
        _worktree(path="/tmp/edge", name="edge", repository="example/edge", branch="feat"),
    ]
    issues = [{"url": "https://github.com/example/tracker/issues/1"}]
    development = {
        "example/tracker#1": {
            "pull_requests": [
                {"repository": "example/cloud", "number": 1, "head_ref_name": "feat"},
                {"repository": "example/edge", "number": 2, "head_ref_name": "feat"},
            ],
            "linked_branches": [],
            "body_urls": [],
        }
    }
    attached, unmatched = attach_issue_refs(issues, worktrees, development)

    paths = [item["path"] for item in attached[0]["worktrees"]]
    assert paths == ["/tmp/cloud", "/tmp/edge"]
    assert unmatched == []


def test_board_refs_join_linked_branch() -> None:
    worktrees = [_worktree(path="/tmp/cloud", name="cloud", branch="feature/173-alerts")]
    issues = [{"url": "https://github.com/example/tracker/issues/90"}]
    development = {
        "example/tracker#90": {
            "pull_requests": [],
            "linked_branches": [
                {"repository": "example/cloud", "name": "feature/173-alerts"},
            ],
            "body_urls": [],
        }
    }
    attached, unmatched = attach_issue_refs(issues, worktrees, development)

    assert attached[0]["worktrees"][0]["match"] == ["linked_branch"]
    assert unmatched == []


def test_board_refs_reports_unmatched_worktrees() -> None:
    worktrees = [_worktree(path="/tmp/other", name="other", branch="unrelated")]
    issues = [{"url": "https://github.com/example/tracker/issues/1"}]
    attached, unmatched = attach_issue_refs(issues, worktrees, {})

    assert attached[0]["worktrees"] == []
    assert unmatched[0]["path"] == "/tmp/other"


def test_worktree_comment_issue_urls_are_unique() -> None:
    worktrees = [
        _worktree(
            path="/tmp/a",
            name="a",
            comment="https://github.com/example/cloud/issues/173",
            issues=[
                {
                    "repository": "example/cloud",
                    "number": 173,
                    "url": "https://github.com/example/cloud/issues/173",
                }
            ],
        )
    ]

    assert worktree_comment_issue_urls(worktrees) == (
        "https://github.com/example/cloud/issues/173",
    )
