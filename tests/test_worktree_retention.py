"""Real Git fixtures; Orca and agents remain mocked throughout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from flybridge_application.workflows import WorkflowService
from flybridge_cli.commands.workflow import _timeout_block
from flybridge_core import AdapterReferenceConflict, ReconcileStore, WorkflowStore
from flybridge_orca.client import OrcaClient, OrcaError


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def checkout(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.com")
    (repository / "tracked").write_text("baseline\n")
    (repository / ".gitignore").write_text("ignored\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "baseline")
    sha = git(repository, "rev-parse", "HEAD").strip()
    git(repository, "update-ref", "refs/remotes/origin/main", sha)
    worker = tmp_path / "worker"
    git(repository, "worktree", "add", "-b", "worker", str(worker))
    return repository, worker, sha


class FixtureOrca:
    """Only explicit rm maps to Git, and only in this fixture's temporary checkout."""

    def __init__(self, repository: Path, worker: Path):
        self.repository, self.worker = repository, worker
        self.calls: list[list[str]] = []
        self.removal = "complete"
        self.fail_status = False
        self.live = False
        self.before_remove = None
        self.client = OrcaClient("fixture-orca", runner=self.run)

    def run(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[0] == "git":
            if self.fail_status and "status" in arguments:
                return subprocess.CompletedProcess(arguments, 1, "", "inspection failed")
            return subprocess.run(arguments, **kwargs)  # noqa: PLW1510 -- adapter supplies check=False
        command = arguments[1:3]
        if command == ["worktree", "show"]:
            result = {"worktree": {"id": "repo::worker", "path": str(self.worker)}}
        elif command == ["terminal", "list"]:
            result = {"terminals": [{"connected": True, "writable": True}] if self.live else []}
        elif command == ["worktree", "rm"]:
            if self.before_remove:
                self.before_remove()
            if self.removal == "error":
                return subprocess.CompletedProcess(arguments, 1, "", "Permission denied")
            if self.removal == "metadata-only":
                admin = Path(git(self.worker, "rev-parse", "--absolute-git-dir").strip())
                assert admin.is_relative_to(self.repository)
                admin.rename(self.repository / "saved-worker-admin")
            if self.removal == "complete":
                args = ["worktree", "remove"]
                if "--force" in arguments:
                    args.append("--force")
                git(self.repository, *args, str(self.worker))
            result = {"removed": True}
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    @property
    def removes(self):
        return [call for call in self.calls if call[1:3] == ["worktree", "rm"]]


def retained(store: WorkflowStore, worker: Path, sha: str):
    workflow = store.create(worker, "single", "fixture", "Preserve work.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::worker",
        worktree_path=str(worker),
        terminal_handle="agent",
        start_sha=sha,
    )
    store.transition(workflow.id, "running")
    store.transition(workflow.id, "cancelled")
    store.retain_worktree(workflow.id)
    store.mark_external_reconciled(workflow.id)
    return workflow


@pytest.mark.parametrize(
    "change", ["unstaged", "staged", "untracked", "ignored", "unpublished", "submodule"]
)
def test_normal_explicit_removal_preserves_unpreserved_work(checkout, tmp_path, change):
    repository, worker, sha = checkout
    if change in {"unstaged", "staged"}:
        (worker / "tracked").write_text("valuable change\n")
        if change == "staged":
            git(worker, "add", "tracked")
    elif change in {"untracked", "ignored"}:
        (worker / change).write_text("valuable output\n")
    elif change == "unpublished":
        (worker / "tracked").write_text("new commit\n")
        git(worker, "commit", "-am", "unpublished")
    else:
        git(
            worker, "-c", "protocol.file.allow=always", "submodule", "add", str(repository), "child"
        )
        git(worker, "commit", "-am", "add child")
        (worker / "child" / "tracked").write_text("submodule change\n")
    store = WorkflowStore(tmp_path / "state")
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    with pytest.raises(OrcaError, match="unpreserved"):
        WorkflowService(store).remove_retained_worktree(workflow.id, "repo::worker", orca.client)
    assert orca.removes == []
    assert worker.exists()
    assert git(worker, "rev-parse", "HEAD")
    assert store.retained_worktrees(workflow.id)[0]["state"] == "retained"


@pytest.mark.parametrize("discard", [False, True])
def test_failed_inspection_never_authorizes_removal(checkout, tmp_path, discard):
    repository, worker, sha = checkout
    store = WorkflowStore(tmp_path / "state")
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    orca.fail_status = True
    with pytest.raises(OrcaError, match="inspection failed"):
        WorkflowService(store).remove_retained_worktree(
            workflow.id,
            "repo::worker",
            orca.client,
            discard_unpreserved=discard,
        )
    assert orca.removes == []
    assert worker.exists()


@pytest.mark.parametrize("discard", [False, True])
def test_explicit_removal_is_scoped_and_verified(checkout, tmp_path, discard):
    repository, worker, sha = checkout
    if discard:
        (worker / "untracked").write_text("explicitly discarded\n")
    store = WorkflowStore(tmp_path / "state")
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    result = WorkflowService(store).remove_retained_worktree(
        workflow.id,
        "repo::worker",
        orca.client,
        discard_unpreserved=discard,
    )
    assert result["state"] == "deleted"
    assert len(orca.removes) == 1
    assert ("--force" in orca.removes[0]) is discard
    assert "id:repo::worker" in orca.removes[0]
    assert not worker.exists()
    assert str(worker) not in git(repository, "worktree", "list", "--porcelain")
    assert git(repository, "status", "--porcelain") == ""
    assert store.retained_worktrees(workflow.id)[0]["state"] == "deleted"


@pytest.mark.parametrize("failure", ["error", "partial", "metadata-only"])
def test_removal_failure_survives_restart_without_automatic_retry(checkout, tmp_path, failure):
    repository, worker, sha = checkout
    state = tmp_path / "state"
    store = WorkflowStore(state)
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    orca.removal = failure
    with pytest.raises(OrcaError):
        WorkflowService(store).remove_retained_worktree(workflow.id, "repo::worker", orca.client)
    fresh = WorkflowStore(state)
    assert fresh.retained_worktrees(workflow.id)[0]["state"] == "remove_failed"
    WorkflowService(fresh).reconcile_stale(1, lambda *_: None, lambda *_: pytest.fail("retry"))
    with pytest.raises(ValueError, match="requires inspection"):
        WorkflowService(fresh).remove_retained_worktree(workflow.id, "repo::worker", orca.client)
    assert len(orca.removes) == 1
    assert worker.exists()


def test_removal_claim_blocks_attachment_and_parallel_removal(checkout, tmp_path):
    _repository, worker, sha = checkout
    store = WorkflowStore(tmp_path / "state")
    workflow = retained(store, worker, sha)
    store.claim_worktree_removal(workflow.id, "repo::worker")
    another = store.create(worker, "single", "another", "Do work.")
    store.begin_start(another.id)
    with pytest.raises(AdapterReferenceConflict, match="deletion"):
        store.attach_external(
            another.id,
            adapter_reference="repo::worker",
            worktree_path=str(worker),
            terminal_handle="new",
        )
    with pytest.raises(ValueError, match="requires inspection"):
        store.claim_worktree_removal(workflow.id, "repo::worker")


def test_live_or_transferred_agent_prevents_removal(checkout, tmp_path):
    repository, worker, sha = checkout
    store = WorkflowStore(tmp_path / "state")
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    orca.live = True
    with pytest.raises(OrcaError, match="live"):
        WorkflowService(store).remove_retained_worktree(workflow.id, "repo::worker", orca.client)
    orca.live = False
    other = store.create(worker, "single", "replacement", "Continue work.")
    store.begin_start(other.id)
    store.attach_external(
        other.id,
        adapter_reference="repo::worker",
        worktree_path=str(worker),
        terminal_handle="new",
        owns_worktree=False,
    )
    with pytest.raises(ValueError, match="agent owner"):
        WorkflowService(store).remove_retained_worktree(workflow.id, "repo::worker", orca.client)
    assert orca.removes == []


@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked", "submodule"])
def test_timeout_without_implementation_commit_preserves_git_and_can_harvest(
    checkout, tmp_path, change
):
    repository, worker_path, sha = checkout
    if change == "submodule":
        git(
            repository,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(repository),
            "child",
        )
        git(repository, "commit", "-am", "baseline child")
        sha = git(repository, "rev-parse", "HEAD").strip()
        git(worker_path, "reset", "--hard", sha)
        git(worker_path, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
        (worker_path / "child" / "tracked").write_text("nested nightly source\n")
    state = tmp_path / "state"
    store = WorkflowStore(state)
    manager, worker, _reviewer = store.create_orchestrated_plan(repository, "night", "Implement.")
    for role, path in ((manager, repository), (worker, worker_path)):
        store.begin_start(role.id)
        store.attach_external(
            role.id,
            adapter_reference=f"repo::{role.role}",
            worktree_path=str(path),
            terminal_handle=f"term-{role.role}",
            implementation_repository="example/repo",
            runtime_repository_id="repo",
            start_sha=sha,
        )
        store.transition(role.id, "running")
    store.transition(manager.id, "completed")
    (worker_path / "tracked").write_text("nightly source\n")
    (worker_path / "new").write_text("nightly output\n")
    if change == "staged":
        git(worker_path, "add", "tracked")
    closed = []

    class Runtime:
        def implementation_worktree_present(self, path):
            return Path(path).exists()

        def verify_implementation_identity(self, *_):
            pass

        def implementation_identity(self, _reference, path):
            return "example/repo", "repo", git(Path(path), "rev-parse", "HEAD").strip()

        def salvage_candidate(self, *_):
            pytest.fail("no implementation commit should not be pushed")

        def close_terminals(self, reference, handle):
            closed.append((reference, handle))

        def set_lifecycle(self, *_):
            pass

        def remove_worktree(self, *_):
            pytest.fail("automatic deletion")

        def integrate_worker_commit(self, manager_path, _worker_path, worker_sha, *, dry_run=False):
            before = git(Path(manager_path), "rev-parse", "HEAD").strip()
            git(Path(manager_path), "merge", "--ff-only", worker_sha)
            return {"method": "ff", "before": before, "after": worker_sha}

    result = _timeout_block(WorkflowService(store), Runtime(), store.get(worker.id), "role-timeout")
    assert result["salvage_push"]["skip_reason"] == "no_implementation_commits"
    assert git(worker_path, "rev-parse", "HEAD").strip() == sha
    assert "nightly source" in git(worker_path, "diff") + git(worker_path, "diff", "--cached")
    assert (worker_path / "new").read_text() == "nightly output\n"
    assert (worker_path / ".git").exists()
    assert closed
    fresh = WorkflowStore(state)
    assert fresh.retained_worktrees(worker.id)[0]["state"] == "retained"
    ReconcileStore(state).apply_orca_scan((), truncated=False)
    WorkflowService(fresh).reconcile_stale(1, lambda *_: None, lambda *_: pytest.fail("delete"))
    assert git(worker_path, "status", "--porcelain")
    if change == "submodule":
        assert "nested nightly source" in git(worker_path / "child", "diff")
        git(worker_path / "child", "config", "user.name", "Fixture")
        git(worker_path / "child", "config", "user.email", "fixture@example.com")
        git(worker_path / "child", "commit", "-am", "nested nightly implementation")
    git(worker_path, "add", ".")
    git(worker_path, "commit", "-m", "preserved nightly implementation")
    harvested = WorkflowService(fresh).harvest(manager.id, Runtime())
    assert harvested.method == "ff"
    assert (repository / "new").read_text() == "nightly output\n"


def test_schema_5_migration_preserves_workflows(tmp_path):
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "existing", "Keep history.")
    with store._connect() as connection:
        connection.execute("DROP TABLE retained_worktrees")
        connection.execute("PRAGMA user_version=5")
    fresh = WorkflowStore(tmp_path)
    assert fresh.get(workflow.id).objective == "Keep history."
    assert fresh.retained_worktrees() == ()


def test_checkout_change_between_inspections_blocks_deletion(checkout, tmp_path, monkeypatch):
    repository, worker, sha = checkout
    store = WorkflowStore(tmp_path / "state")
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    inspect = orca.client.inspect_worktree_removal
    calls = 0

    def racing_inspection(*args, **kwargs):
        nonlocal calls
        calls += 1
        facts = inspect(*args, **kwargs)
        if calls == 2:
            (worker / "tracked").write_text("concurrent change\n")
        return facts

    monkeypatch.setattr(orca.client, "inspect_worktree_removal", racing_inspection)
    with pytest.raises(OrcaError, match="unpreserved"):
        WorkflowService(store).remove_retained_worktree(workflow.id, "repo::worker", orca.client)
    assert orca.removes == []
    assert "concurrent change" in git(worker, "diff")


def test_retained_checkout_can_be_attached_and_resumed(checkout, tmp_path):
    from types import SimpleNamespace

    repository, worker, sha = checkout
    store = WorkflowStore(tmp_path / "state")
    original = retained(store, worker, sha)
    (worker / "tracked").write_text("preserved edit\n")
    next_role = store.create(worker, "single", "continuation", "Continue preserved work.")
    service = WorkflowService(store)
    active = service.start_existing(
        next_role.id,
        lambda: SimpleNamespace(
            worktree_id="repo::worker",
            worktree=str(worker),
            terminal="replacement",
            owns_worktree=False,
        ),
        lambda _: None,
        lambda *_: pytest.fail("unexpected terminal cleanup"),
        identify_external=lambda _: ("example/repo", "repo", sha),
    )
    assert active.worktree_path == str(worker)
    assert "preserved edit" in git(worker, "diff")

    class Runtime:
        def verify_worktree(self, reference, path):
            assert reference == "repo::worker" and path == str(worker)

        def verify_implementation_identity(self, *_):
            assert git(worker, "rev-parse", "HEAD").strip() == sha

        def terminal_is_valid(self, *_):
            return True

        def wait_for_agent(self, handle):
            assert handle == "replacement"

        def send_prompt(self, handle, prompt):
            assert handle == "replacement" and "Continue preserved work" in prompt

    resumed = service.resume(
        active.id, Runtime(), agent="codex", response_language="English", skill_paths=()
    )
    assert resumed.status == "running"
    assert store.retained_worktrees(original.id)[0]["state"] == "retained"
    assert "preserved edit" in git(worker, "diff")
    assert repository.exists()


def test_remove_worktree_cli_requires_target_and_calls_only_explicit_path(
    checkout, tmp_path, capsys, monkeypatch
):
    from conftest import write_config
    from flybridge_cli.main import main

    repository, worker, sha = checkout
    state = tmp_path / "state"
    config = write_config(tmp_path / "config.jsonc", state_dir=state)
    store = WorkflowStore(state)
    workflow = retained(store, worker, sha)
    orca = FixtureOrca(repository, worker)
    monkeypatch.setattr("flybridge_cli.commands.workflow._sync_orca", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("flybridge_cli.commands.workflow._adapter", lambda _: orca.client)
    assert (
        main(
            [
                "--config",
                str(config),
                "workflow",
                "remove-worktree",
                workflow.id,
                "--worktree-id",
                "repo::worker",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "deleted"
    assert len(orca.removes) == 1
