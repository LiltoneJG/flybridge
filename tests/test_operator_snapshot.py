from __future__ import annotations

import json
import threading
import time
from collections import Counter
from types import SimpleNamespace

from flybridge_application.git_probe import GitWorktreeState
from flybridge_cli.commands import inventory as inventory_command
from flybridge_cli.commands.operator_snapshot import _collect, enrich_snapshot
from flybridge_cli.parser import build_parser
from flybridge_github import GitHubPullRequests
from flybridge_github.operator_snapshot import GitHubOperatorFacts
from flybridge_orca import ListedWorktree


def _inventory() -> dict:
    return {
        "generated_at": "2026-09-30T00:00:00Z",
        "warnings": [],
        "failures": [],
        "worktrees": [
            {
                "orca": {
                    "path": "/a",
                    "comment": "https://github.com/o/parent/issues/3",
                    "github_hint": {"issues": [{"url": "https://github.com/o/parent/issues/3"}]},
                },
                "git": {"submodules": [{"github_repository": "o/child", "branch": "feature"}]},
                "pull_requests": [
                    {"repository": "o/parent", "number": 2, "matched_from": "parent"},
                    {"repository": "o/child", "number": 4, "matched_from": "submodule"},
                ],
            },
            {
                "orca": {
                    "path": "/b",
                    "comment": "",
                    "github_hint": {"issues": [{"url": "https://github.com/o/parent/issues/3"}]},
                },
                "git": {},
                "pull_requests": [
                    {"repository": "o/child", "number": 4, "matched_from": "parent"},
                ],
            },
            {
                "orca": {"path": "/unassigned", "comment": "", "github_hint": {"issues": []}},
                "git": {},
                "pull_requests": [],
            },
        ],
    }


class FakeFacts:
    def __init__(self) -> None:
        self.calls: Counter = Counter()

    def pull_request(self, repository: str, number: int) -> dict:
        self.calls[("pr", repository, number)] += 1
        if number == 2:
            time.sleep(0.02)  # completion order must not affect JSON order
        if number == 5:
            raise RuntimeError("partial API outage")
        return {
            "repository": repository,
            "number": number,
            "url": f"https://github.com/{repository}/pull/{number}",
            "body": "Related: #3; PR #169→#170" if number == 2 else "",
            "comments": [{"body": "PR #171; https://github.com/o/parent/issues/3"}],
            "assignees": ["alice"] if number == 4 else ["bob"],
            "unresolved_threads": [{"comments": [{"url": "https://github.com/thread"}]}]
            if number == 4
            else [],
            "checks": [{"status": "IN_PROGRESS"}]
            if number == 2
            else [{"status": "COMPLETED", "conclusion": "FAILURE"}],
            "failed_checks": []
            if number == 2
            else [{"summary": "failed", "url": "https://github.com/check"}],
            "warnings": [],
        }

    def issue(self, repository: str, number: int) -> dict:
        self.calls[("issue", repository, number)] += 1
        return {
            "repository": repository,
            "number": number,
            "url": f"https://github.com/{repository}/issues/{number}",
            "body": "details",
            "comments": [{"body": "comment", "url": "https://github.com/comment"}],
            "assignees": ["alice"],
            "linked_pull_requests": [{"repository": "o/parent", "number": 5}],
            "warnings": [],
        }


def test_snapshot_deduplicates_submodule_pr_and_issue_and_stabilizes_order() -> None:
    facts = FakeFacts()
    snapshot = enrich_snapshot(_inventory(), facts, assignee="alice")
    assert facts.calls == Counter(
        {
            ("pr", "o/parent", 2): 1,
            ("pr", "o/child", 4): 1,
            ("pr", "o/parent", 5): 1,
            ("issue", "o/parent", 3): 1,
        }
    )
    assert [item["url"] for item in snapshot["pull_requests"]] == [
        "https://github.com/o/child/pull/4",
        "https://github.com/o/parent/pull/2",
    ]
    assert snapshot["worktrees"][0]["pull_requests"][1]["matched_from"] == "submodule"
    assert snapshot["issues"][0]["matches_assignee"] is True
    assert snapshot["issues"][0]["comments"][0]["url"] == "https://github.com/comment"
    assert snapshot["worktrees"][0]["related_issue_urls"] == [
        "https://github.com/o/parent/issues/3"
    ]
    assert snapshot["pull_requests"][0]["matches_assignee"] is True
    assert snapshot["pull_requests"][1]["matches_assignee"] is False
    assert snapshot["pull_requests"][0]["unresolved_threads"]
    assert snapshot["pull_requests"][0]["failed_checks"][0]["url"]
    assert snapshot["pull_requests"][1]["checks"][0]["status"] == "IN_PROGRESS"
    assert any("o/parent#5 unavailable" in item for item in snapshot["warnings"])
    assert len(snapshot["worktrees"]) == 3
    assert snapshot["focused_worktrees"] == ["/a", "/b"]
    assert snapshot["focused_refs"]["issues"] == ["https://github.com/o/parent/issues/3"]
    assert snapshot["focused_refs"]["pull_requests"] == ["https://github.com/o/child/pull/4"]
    assert all(
        "/issues/17" not in url
        for row in snapshot["worktrees"]
        for url in row["related_issue_urls"]
    )


class FakeCli:
    executable = "gh"

    @staticmethod
    def cli_value(_field: str, value: str) -> str:
        return value

    def run(self, arguments: list[str], *, timeout: int) -> SimpleNamespace:
        assert timeout == 60
        query = next(item.removeprefix("query=") for item in arguments if item.startswith("query="))
        if "projectItems" in query:
            return SimpleNamespace(returncode=1, stderr="read:project denied", stdout="")
        if "pullRequest(number" in query:
            node = {
                "url": "https://github.com/o/r/pull/2",
                "body": "body",
                "state": "OPEN",
                "isDraft": False,
                "assignees": _connection([{"login": "alice"}]),
                "reviews": _connection(
                    [
                        {
                            "url": "https://github.com/review",
                            "state": "APPROVED",
                            "body": "ok",
                            "author": {"login": "human"},
                        }
                    ]
                ),
                "comments": _connection([]),
                "reviewThreads": _connection(
                    [
                        {
                            "isResolved": False,
                            "isOutdated": False,
                            "comments": _connection(
                                [{"url": "https://github.com/thread", "body": "fix"}]
                            ),
                        }
                    ]
                ),
                "statusCheckRollup": {
                    "contexts": _connection(
                        [
                            {
                                "__typename": "CheckRun",
                                "name": "ci",
                                "status": "COMPLETED",
                                "conclusion": "FAILURE",
                                "detailsUrl": "https://github.com/job",
                                "summary": "test failed",
                            },
                            {
                                "__typename": "CheckRun",
                                "name": "next",
                                "status": "IN_PROGRESS",
                                "conclusion": None,
                                "detailsUrl": "https://github.com/next",
                                "summary": None,
                            },
                        ]
                    )
                },
            }
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps({"data": {"repository": {"pullRequest": node}}}),
            )
        raise AssertionError("unexpected query")


def _connection(nodes: list[dict]) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": False}}


def test_pr_detail_keeps_failed_check_summary_thread_and_project_warning() -> None:
    detail = GitHubOperatorFacts(user="alice", cli=FakeCli()).pull_request("o/r", 2)
    assert detail["failed_checks"] == [
        {
            "name": "ci",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "url": "https://github.com/job",
            "summary": "test failed",
        }
    ]
    assert detail["checks"][1]["status"] == "IN_PROGRESS"
    assert detail["unresolved_threads"][0]["comments"][0]["url"] == "https://github.com/thread"
    assert detail["reviews"][0]["author"] == "human"
    assert any("Project facts unavailable" in item for item in detail["warnings"])


def test_project_scope_failure_is_cached_without_losing_issue_facts() -> None:
    class Cli(FakeCli):
        project_calls = 0

        def run(self, arguments: list[str], *, timeout: int) -> SimpleNamespace:
            query = next(
                item.removeprefix("query=") for item in arguments if item.startswith("query=")
            )
            if "projectItems" in query:
                self.project_calls += 1
                return SimpleNamespace(
                    returncode=1, stderr="required scopes: read:project", stdout=""
                )
            if "issue(number" in query:
                return SimpleNamespace(
                    returncode=0,
                    stderr="",
                    stdout=json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "issue": {
                                        "url": "https://github.com/o/r/issues/3",
                                        "title": "issue",
                                        "body": "body",
                                        "state": "OPEN",
                                        "assignees": _connection([{"login": "alice"}]),
                                        "comments": _connection(
                                            [
                                                {
                                                    "url": "https://github.com/comment",
                                                    "body": "evidence",
                                                    "author": {"login": "human"},
                                                }
                                            ]
                                        ),
                                        "timelineItems": _connection([]),
                                    }
                                }
                            }
                        }
                    ),
                )
            return super().run(arguments, timeout=timeout)

    cli = Cli()
    facts = GitHubOperatorFacts(user="alice", cli=cli)
    first = facts.issue("o/r", 3)
    second = facts.issue("o/r", 4)
    assert cli.project_calls == 1
    assert first["body"] == second["body"] == "body"
    assert first["comments"][0]["url"] == "https://github.com/comment"
    assert first["projects"] == []
    assert any("Project facts unavailable" in warning for warning in first["warnings"])
    assert any("Project facts unavailable" in warning for warning in second["warnings"])


def test_operator_snapshot_flags_follow_cli_conventions() -> None:
    args = build_parser().parse_args(
        [
            "operator",
            "snapshot",
            "--assignee",
            "alice",
            "--path-prefix",
            "/a",
            "--exclude-name",
            "old",
        ]
    )
    assert args.assignee == "alice"
    assert args.path_prefixes == ["/a"]
    assert args.exclude_names == ["old"]


def test_detail_collection_is_bounded_and_sorted_after_parallel_completion() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def fetch(repository: str, number: int) -> dict:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.005 * (8 - number))
        with lock:
            active -= 1
        return {"repository": repository, "number": number, "warnings": []}

    results, warnings = _collect({("o/r", number) for number in range(1, 8)}, fetch, "issue")
    assert list(results) == [("o/r", number) for number in range(1, 8)]
    assert 1 < peak <= 4
    assert warnings == []


def test_path_selection_precedes_github_queries_with_and_without_filter(
    tmp_path, monkeypatch
) -> None:
    paths = [tmp_path / "selected", tmp_path / "excluded"]
    for path in paths:
        path.mkdir()
    listed = tuple(
        ListedWorktree(str(index), str(path), path.name, "ready", "", "feature", None, None)
        for index, path in enumerate(paths)
    )
    monkeypatch.setattr(
        inventory_command,
        "_adapter",
        lambda _config: SimpleNamespace(list_worktrees=lambda: (listed, False)),
    )

    class Probe:
        def inspect(self, path: str) -> GitWorktreeState:
            repository = "o/selected" if path == str(paths[0]) else "o/excluded"
            return GitWorktreeState("feature", False, 0, 0, 0, repository, (repository,), ())

    monkeypatch.setattr(inventory_command, "GitWorktreeProbe", Probe)
    calls: list[tuple[str, ...]] = []

    class Pulls:
        def __init__(self, *, user: str):
            assert user == "alice"

        def list_open(self, repositories):
            calls.append(tuple(repositories))
            return {}, ()

    monkeypatch.setattr(inventory_command, "GitHubPullRequests", Pulls)
    monkeypatch.setattr(
        inventory_command,
        "supplement_pull_requests",
        lambda _client, _selected, _states, _open, **_kwargs: ({}, ()),
    )
    config = SimpleNamespace(
        reconcile=SimpleNamespace(exclude_worktrees=()),
        github=SimpleNamespace(enabled=True, login="alice", skip_repositories=()),
    )

    def run(prefixes: list[str]) -> dict:
        args = SimpleNamespace(
            path_prefixes=prefixes,
            exclude_prefixes=[],
            exclude_names=[],
            no_github=False,
            with_review_facts=False,
        )
        snapshot, failed = inventory_command.build_inventory(args, config)
        assert failed is False
        return snapshot

    selected = run([str(paths[0])])
    assert len(selected["worktrees"]) == 1
    assert calls[-1] == ("o/selected",)
    unfiltered = run([])
    assert len(unfiltered["worktrees"]) == 2
    assert calls[-1] == ("o/selected", "o/excluded")


def test_open_pr_queries_deduplicate_repositories_and_bound_concurrency(monkeypatch) -> None:
    client = GitHubPullRequests()
    active = 0
    peak = 0
    lock = threading.Lock()
    queried: Counter = Counter()

    def query(repository: str) -> tuple:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            queried[repository] += 1
        time.sleep(0.01)
        with lock:
            active -= 1
        return ()

    monkeypatch.setattr(client, "_list_repository_with_retry", query)
    repositories = [f"o/r{number}" for number in range(7)]
    facts, failures = client.list_open([*repositories, "o/r2", "o/r1"])
    assert list(facts) == repositories
    assert failures == ()
    assert queried == Counter(repositories)
    assert 1 < peak <= 4


def test_all_assignees_focuses_every_worktree_and_reference() -> None:
    snapshot = enrich_snapshot(_inventory(), FakeFacts(), assignee=None)
    assert snapshot["focused_worktrees"] == ["/a", "/b", "/unassigned"]
    assert len(snapshot["focused_refs"]["issues"]) == 1
    assert len(snapshot["focused_refs"]["pull_requests"]) == 2
