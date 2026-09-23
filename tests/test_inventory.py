from __future__ import annotations

import subprocess
from pathlib import Path

from flybridge_application.git_probe import (
    GitWorktreeProbe,
    GitWorktreeState,
    github_repository_from_remote,
)
from flybridge_application.inventory import (
    InventoryWorktree,
    apply_review_facts,
    attach_matched_review_facts,
    collect_inventory,
    path_is_included,
    pull_request_needs_review_facts,
    supplement_pull_requests,
    unique_review_fact_keys,
    worktree_is_excluded,
)
from flybridge_github.pull_requests import (
    PullRequestCheck,
    PullRequestFact,
    PullRequestQueryFailure,
)


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True)


def _init_repo(path: Path, *, branch: str = "feature") -> None:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=path, check=True, capture_output=True
    )
    _git(path, "config", "user.email", "inventory@example.invalid")
    _git(path, "config", "user.name", "Inventory")
    (path / "README.md").write_text("ok\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")
    if branch != "main":
        _git(path, "checkout", "-b", branch)


def test_git_probe_reports_dirty_unpushed_and_github_remote(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "work"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "HEAD:feature")
    _git(repo, "remote", "set-url", "origin", "git@github.com:example/repo.git")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    (repo / "extra.md").write_text("unpushed\n", encoding="utf-8")
    _git(repo, "add", "extra.md")
    _git(repo, "commit", "-m", "ahead")

    state = GitWorktreeProbe().inspect(str(repo))

    assert state.branch == "feature"
    assert state.dirty is True
    assert state.ahead == 1
    assert state.behind == 0
    assert state.unpushed_commits == 1
    assert state.github_repository == "example/repo"
    assert state.github_repositories == ("example/repo",)


def test_git_probe_includes_additional_github_remotes(tmp_path: Path) -> None:
    repo = tmp_path / "work"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", "https://github.com/example/fork.git")
    _git(repo, "remote", "add", "upstream", "https://github.com/example/repo.git")

    state = GitWorktreeProbe().inspect(str(repo))

    assert state.github_repository == "example/fork"
    assert state.github_repositories == ("example/fork", "example/repo")


def test_git_probe_accepts_detached_head_and_records_commit(tmp_path: Path) -> None:
    repo = tmp_path / "detached"
    _init_repo(repo)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "checkout", "--detach", sha)

    state = GitWorktreeProbe().inspect(str(repo))

    assert state.branch == ""
    assert state.commit_sha == sha


def test_git_probe_lists_submodules(tmp_path: Path) -> None:
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    _init_repo(child, branch="main")
    _init_repo(parent, branch="main")
    subprocess.run(
        [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child),
            "vendor",
        ],
        check=True,
        capture_output=True,
    )
    _git(parent, "commit", "-m", "add submodule")

    state = GitWorktreeProbe().inspect(str(parent))

    assert state.submodules[0].path == "vendor"
    assert state.submodules[0].sha
    assert state.submodules[0].dirty is False
    assert state.submodules[0].github_repository is None


def test_git_probe_reports_submodule_github_remote_and_branch(tmp_path: Path) -> None:
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    _init_repo(child, branch="feature")
    _init_repo(parent, branch="main")
    subprocess.run(
        [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-b",
            "feature",
            str(child),
            "vendor",
        ],
        check=True,
        capture_output=True,
    )
    _git(parent, "commit", "-m", "add submodule")
    vendor = parent / "vendor"
    _git(vendor, "remote", "set-url", "origin", "https://github.com/example/child.git")

    state = GitWorktreeProbe().inspect(str(parent))

    assert state.submodules[0].github_repository == "example/child"
    assert state.submodules[0].branch == "feature"
    assert "example/child" in state.github_repositories


def test_github_repository_from_remote_parses_https() -> None:
    assert github_repository_from_remote("https://github.com/example/repo.git") == "example/repo"


def test_inventory_filters_prefixes_and_omits_github(tmp_path: Path) -> None:
    keep = tmp_path / "keep"
    drop = tmp_path / "drop"
    _init_repo(keep)
    _init_repo(drop)
    probe = GitWorktreeProbe()
    snapshot = collect_inventory(
        (
            InventoryWorktree("id-keep", str(keep), "keep", "todo", "", {"issues": []}),
            InventoryWorktree("id-drop", str(drop), "drop", "todo", "", {"issues": []}),
        ),
        probe,
        include_github=False,
    )
    filtered = [
        worktree
        for worktree in snapshot["worktrees"]
        if path_is_included(worktree["orca"]["path"], (keep.resolve(),), ())
    ]

    assert snapshot["schema_version"] == 1
    assert "pull_requests" not in snapshot["worktrees"][0]
    assert [row["orca"]["name"] for row in filtered] == ["keep"]
    assert path_is_included(str(drop), (tmp_path.resolve(),), (drop.resolve(),)) is False


def test_inventory_excludes_matching_directory_name_when_prefix_is_missing(tmp_path: Path) -> None:
    checkout = tmp_path / "projects" / "flybridge-private"
    checkout.mkdir(parents=True)
    missing = tmp_path / "orca" / "workspaces" / "flybridge-private"

    assert path_is_included(str(checkout), (), (missing,)) is False
    assert path_is_included(str(tmp_path / "other"), (), (missing,)) is True


def test_inventory_excludes_directory_name_even_when_a_prefix_exists(tmp_path: Path) -> None:
    orca = tmp_path / "orca" / "workspaces" / "flybridge-private"
    orca.mkdir(parents=True)
    checkout = tmp_path / "projects" / "flybridge-private"
    checkout.mkdir(parents=True)

    assert path_is_included(str(checkout), (), (orca,), ()) is True
    assert path_is_included(str(checkout), (), (), ("flybridge-private",)) is False
    assert path_is_included(str(tmp_path / "keep"), (), (), ("flybridge-private",)) is True


def test_worktree_exclusion_matches_directory_name_or_glob_but_not_a_longer_name(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox_0 = tmp_path / "sandbox_0"
    private = tmp_path / "projects" / "flybridge-private"
    nested = tmp_path / "flybridge-private" / "child"
    private.mkdir(parents=True)
    nested.mkdir(parents=True)
    sandbox.mkdir()
    sandbox_0.mkdir()

    assert worktree_is_excluded(str(sandbox), ("sandbox",)) is True
    assert worktree_is_excluded(str(sandbox_0), ("sandbox",)) is False
    assert worktree_is_excluded(str(private), ("*-private",)) is True
    assert worktree_is_excluded(str(nested), ("*-private",)) is True
    assert worktree_is_excluded(str(sandbox_0), ("*-private",)) is False
    assert path_is_included(str(sandbox), (), (), (), ("sandbox",)) is False
    assert path_is_included(str(sandbox_0), (), (), (), ("sandbox",)) is True


def test_inventory_matches_open_pull_requests_by_head(tmp_path: Path) -> None:
    repo = tmp_path / "work"
    _init_repo(repo, branch="feature")
    _git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    fact = PullRequestFact(
        "example/repo",
        9,
        "Ready",
        "https://example.test/9",
        "OPEN",
        False,
        "MERGEABLE",
        "CLEAN",
        "APPROVED",
        "feature",
        ("alice",),
        (PullRequestCheck("ci", "COMPLETED", "SUCCESS"),),
        0,
        1,
    )
    snapshot = collect_inventory(
        (
            InventoryWorktree(
                "id",
                str(repo),
                "work",
                "in-progress",
                "https://github.com/example/repo/issues/1",
                {"issues": []},
                1,
                "github:example/repo",
            ),
        ),
        GitWorktreeProbe(),
        pull_requests_by_repository={"example/repo": (fact,)},
        truncated=True,
    )

    assert snapshot["failures"] == ["Orca worktree list was truncated"]
    assert snapshot["worktrees"][0]["pull_requests"][0]["number"] == 9
    assert snapshot["worktrees"][0]["orca"]["github_hint"]["issues"] == [
        {
            "repository": "example/repo",
            "number": 1,
            "url": "https://github.com/example/repo/issues/1",
        }
    ]


def test_inventory_records_git_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    snapshot = collect_inventory(
        (InventoryWorktree("id", str(missing), "gone", "todo", "", {}),),
        GitWorktreeProbe(),
        include_github=False,
    )

    assert snapshot["worktrees"][0]["git"] is None
    assert snapshot["worktrees"][0]["errors"]


def test_inventory_ignores_linked_issue_when_checkout_repo_differs(tmp_path: Path) -> None:
    repo = tmp_path / "work"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    snapshot = collect_inventory(
        (
            InventoryWorktree(
                "id",
                str(repo),
                "work",
                "todo",
                "",
                {"issues": []},
                171,
                "github:other/project",
            ),
        ),
        GitWorktreeProbe(),
        include_github=False,
    )

    assert snapshot["worktrees"][0]["orca"]["github_hint"]["issues"] == []


def test_inventory_keeps_pull_requests_when_another_repository_fails(tmp_path: Path) -> None:
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    _init_repo(good, branch="feature")
    _init_repo(bad, branch="other")
    _git(good, "remote", "add", "origin", "https://github.com/example/repo.git")
    _git(bad, "remote", "add", "origin", "https://github.com/example/missing.git")
    fact = PullRequestFact(
        "example/repo",
        9,
        "Ready",
        "https://example.test/9",
        "OPEN",
        False,
        "MERGEABLE",
        "CLEAN",
        "APPROVED",
        "feature",
        ("alice",),
        (PullRequestCheck("ci", "COMPLETED", "SUCCESS"),),
        0,
        1,
    )
    snapshot = collect_inventory(
        (
            InventoryWorktree("id-good", str(good), "good", "in-progress", "", {"issues": []}),
            InventoryWorktree("id-bad", str(bad), "bad", "in-progress", "", {"issues": []}),
        ),
        GitWorktreeProbe(),
        pull_requests_by_repository={"example/repo": (fact,)},
        pull_request_errors=(
            "gh: Could not resolve to a Repository with the name 'example/missing'.",
        ),
        unavailable_github_repositories=("example/missing",),
    )

    rows = {row["orca"]["name"]: row for row in snapshot["worktrees"]}
    assert rows["good"]["pull_requests"][0]["number"] == 9
    assert rows["good"]["errors"] == []
    assert rows["bad"]["pull_requests"] == []
    assert rows["bad"]["errors"] == ["GitHub pull requests were unavailable for example/missing"]
    assert snapshot["failures"] == [
        "gh: Could not resolve to a Repository with the name 'example/missing'."
    ]


def test_inventory_drops_failures_for_unselected_repositories(tmp_path: Path) -> None:
    good = tmp_path / "good"
    _init_repo(good, branch="feature")
    _git(good, "remote", "add", "origin", "https://github.com/example/repo.git")
    fact = PullRequestFact(
        "example/repo",
        9,
        "Ready",
        "https://example.test/9",
        "OPEN",
        False,
        "MERGEABLE",
        "CLEAN",
        "APPROVED",
        "feature",
        ("alice",),
        (PullRequestCheck("ci", "COMPLETED", "SUCCESS"),),
        0,
        1,
    )
    snapshot = collect_inventory(
        (InventoryWorktree("id-good", str(good), "good", "in-progress", "", {"issues": []}),),
        GitWorktreeProbe(),
        pull_requests_by_repository={"example/repo": (fact,)},
        pull_request_errors=(
            "gh: Could not resolve to a Repository with the name 'example/unused'.",
        ),
        unavailable_github_repositories=("example/unused",),
    )

    assert snapshot["failures"] == []
    assert snapshot["worktrees"][0]["errors"] == []
    assert snapshot["worktrees"][0]["pull_requests"][0]["number"] == 9


def test_inventory_matches_submodule_pull_requests(tmp_path: Path) -> None:
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    _init_repo(child, branch="edge-feature")
    _init_repo(parent, branch="cloud-feature")
    subprocess.run(
        [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-b",
            "edge-feature",
            str(child),
            "vendor",
        ],
        check=True,
        capture_output=True,
    )
    _git(parent, "commit", "-m", "add submodule")
    _git(parent, "remote", "add", "origin", "https://github.com/example/cloud.git")
    _git(parent / "vendor", "remote", "set-url", "origin", "https://github.com/example/edge.git")
    parent_fact = PullRequestFact(
        "example/cloud",
        1,
        "Cloud",
        "https://example.test/cloud/1",
        "OPEN",
        False,
        "MERGEABLE",
        "CLEAN",
        None,
        "cloud-feature",
        (),
        (),
        0,
        0,
    )
    child_fact = PullRequestFact(
        "example/edge",
        2,
        "Edge",
        "https://example.test/edge/2",
        "MERGED",
        False,
        None,
        None,
        None,
        "edge-feature",
        (),
        (),
        0,
        0,
    )
    snapshot = collect_inventory(
        (InventoryWorktree("id", str(parent), "parent", "in-progress", "", {"issues": []}),),
        GitWorktreeProbe(),
        pull_requests_by_repository={
            "example/cloud": (parent_fact,),
            "example/edge": (child_fact,),
        },
    )

    numbers = [item["number"] for item in snapshot["worktrees"][0]["pull_requests"]]
    assert numbers == [1, 2]
    assert snapshot["worktrees"][0]["pull_requests"][0]["matched_from"] == "parent"
    assert snapshot["worktrees"][0]["pull_requests"][1]["matched_from"] == "submodule"
    assert snapshot["worktrees"][0]["pull_requests"][1]["state"] == "MERGED"


def test_supplement_pull_requests_fetches_closed_heads_and_hinted_numbers() -> None:
    worktree = InventoryWorktree(
        "id",
        "/tmp/work",
        "work",
        "in-progress",
        "",
        {"issues": [], "pull_request": {"number": 12, "state": "CLOSED"}},
    )
    state = GitWorktreeState(
        "feature",
        False,
        0,
        0,
        0,
        "example/repo",
        ("example/repo",),
        (),
    )
    closed = PullRequestFact(
        "example/repo",
        9,
        "Closed",
        "https://example.test/9",
        "CLOSED",
        False,
        None,
        None,
        None,
        "feature",
        (),
        (),
        0,
        0,
    )
    hinted = PullRequestFact(
        "example/repo",
        12,
        "Hinted",
        "https://example.test/12",
        "MERGED",
        False,
        None,
        None,
        None,
        "other",
        (),
        (),
        0,
        0,
    )

    class Client:
        def list_by_head(self, repository, head_ref_name):
            assert repository == "example/repo"
            assert head_ref_name == "feature"
            return (closed,), None

        def get(self, repository, number):
            assert repository == "example/repo"
            assert number == 12
            return hinted, None

    facts, failures = supplement_pull_requests(Client(), (worktree,), {"id": state}, {})

    assert failures == ()
    assert [item.number for item in facts["example/repo"]] == [9, 12]
    assert facts["example/repo"][0].state == "CLOSED"


def test_review_facts_skip_draft_pending_and_closed() -> None:
    assert pull_request_needs_review_facts({"state": "OPEN", "is_draft": False, "checks": []})
    assert not pull_request_needs_review_facts({"state": "MERGED", "is_draft": False, "checks": []})
    assert not pull_request_needs_review_facts({"state": "OPEN", "is_draft": True, "checks": []})
    assert not pull_request_needs_review_facts(
        {
            "state": "OPEN",
            "is_draft": False,
            "checks": [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}],
        }
    )


def test_unique_review_fact_keys_include_top_level_authored_payloads() -> None:
    keys = unique_review_fact_keys(
        {
            "pull_requests": [
                {
                    "repository": "example/repo",
                    "number": 1,
                    "state": "OPEN",
                    "is_draft": False,
                    "checks": [{"name": "ci", "status": "QUEUED", "conclusion": None}],
                },
                {
                    "repository": "example/repo",
                    "number": 2,
                    "state": "OPEN",
                    "is_draft": False,
                    "checks": [],
                },
            ]
        }
    )
    assert keys == (("example/repo", 2),)


def test_attach_review_facts_deduplicates_and_isolates_failures() -> None:
    snapshot = {
        "failures": [],
        "worktrees": [
            {
                "pull_requests": [
                    {
                        "repository": "example/repo",
                        "number": 1,
                        "state": "OPEN",
                        "is_draft": False,
                        "head_ref_name": "feature",
                        "checks": [],
                    },
                    {
                        "repository": "example/repo",
                        "number": 2,
                        "state": "MERGED",
                        "is_draft": False,
                        "head_ref_name": "old",
                        "checks": [],
                    },
                ]
            },
            {
                "pull_requests": [
                    {
                        "repository": "example/repo",
                        "number": 1,
                        "state": "OPEN",
                        "is_draft": False,
                        "head_ref_name": "feature",
                        "checks": [],
                    }
                ]
            },
        ],
    }
    calls: list[tuple[str, int]] = []

    class Client:
        def get_review_facts(self, repository, number, *, head_ref_name=None):
            calls.append((repository, number))
            assert head_ref_name == "feature"
            if number == 1:
                return {"body": "ok", "reviews": [], "comments": [], "threads": []}, None
            return None, PullRequestQueryFailure("example/repo", "nope")

    attached = attach_matched_review_facts(snapshot, Client())
    assert calls == [("example/repo", 1)]
    assert attached["worktrees"][0]["pull_requests"][0]["review_facts"]["body"] == "ok"
    assert attached["worktrees"][1]["pull_requests"][0]["review_facts"]["body"] == "ok"
    assert "review_facts" not in attached["worktrees"][0]["pull_requests"][1]


def test_apply_review_facts_records_per_pull_request_failure() -> None:
    snapshot = {
        "failures": ["existing"],
        "worktrees": [
            {
                "pull_requests": [
                    {"repository": "example/repo", "number": 3, "state": "OPEN"},
                    {"repository": "example/repo", "number": 4, "state": "OPEN"},
                ]
            }
        ],
    }
    updated = apply_review_facts(
        snapshot,
        {("example/repo", 3): {"body": "kept"}},
        ("example/repo#4: gh: HTTP 502",),
    )
    assert updated["worktrees"][0]["pull_requests"][0]["review_facts"]["body"] == "kept"
    assert "review_facts" not in updated["worktrees"][0]["pull_requests"][1]
    assert updated["failures"] == ["existing", "example/repo#4: gh: HTTP 502"]
    assert unique_review_fact_keys(
        {
            "worktrees": [
                {
                    "pull_requests": [
                        {"repository": "example/repo", "number": 3, "state": "OPEN", "checks": []}
                    ]
                }
            ]
        }
    ) == (("example/repo", 3),)


def test_repository_skip_patterns_match_prefixes() -> None:
    from flybridge_application.inventory import (
        classify_pull_request_failure,
        filter_github_query_targets,
        format_pull_request_failures,
        repository_is_skipped,
    )
    from flybridge_github import PullRequestQueryFailure

    assert repository_is_skipped(
        "flybridge-review-fixture/disposable", ("flybridge-review-fixture/",)
    )
    assert not repository_is_skipped("example/repo", ("flybridge-review-fixture/",))
    assert not repository_is_skipped(
        "flybridge-review-fixture-extra/disposable",
        ("flybridge-review-fixture/",),
    )
    assert filter_github_query_targets(
        ("example/repo", "flybridge-acceptance/disposable"),
        ("flybridge-acceptance/",),
    ) == ("example/repo",)
    transient = PullRequestQueryFailure("google/googletest", "gh: HTTP 502")
    missing = PullRequestQueryFailure(
        "example/missing",
        "gh: Could not resolve to a Repository with the name 'example/missing'.",
    )
    assert classify_pull_request_failure(transient) == "transient"
    assert classify_pull_request_failure(missing) == "missing"
    failures, warnings, unavailable = format_pull_request_failures(
        (transient, missing),
        ("google/googletest", "example/missing"),
    )
    assert warnings == ("google/googletest: gh: HTTP 502",)
    assert unavailable == ("example/missing",)
    assert "HTTP 502" not in "".join(failures)
