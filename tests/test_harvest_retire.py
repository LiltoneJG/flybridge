"""Harvest and retire stay mocked: no live Orca or LLM process is started."""

from __future__ import annotations

from pathlib import Path

import pytest
from flybridge_application.workflows import WorkflowService
from flybridge_core import WorkflowStore


def _attach(
    store: WorkflowStore,
    workflow_id: str,
    *,
    name: str,
    owns_worktree: bool = True,
    start_sha: str = "aaa111",
) -> None:
    store.begin_start(workflow_id)
    store.attach_external(
        workflow_id,
        adapter_reference=f"repo::{name}",
        worktree_path=f"/tmp/{name}",
        terminal_handle=f"term-{name}",
        owns_worktree=owns_worktree,
        implementation_repository="example/repo",
        runtime_repository_id="repo",
        start_sha=start_sha,
    )
    store.transition(workflow_id, "running")
    store.transition(workflow_id, "completed")


def _finished_plan(tmp_path: Path, *, manager_owns: bool = True):
    store = WorkflowStore(tmp_path)
    manager, worker, reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    _attach(store, manager.id, name="manager", owns_worktree=manager_owns)
    _attach(store, worker.id, name="worker")
    _attach(store, reviewer.id, name="reviewer", start_sha="bbb222")
    store.set_orchestration_outcome(manager.id, "completed")
    return WorkflowService(store), manager, worker, reviewer


class RecordingRuntime:
    def __init__(self) -> None:
        self.closed: list[tuple[str, str | None]] = []
        self.removed: list[str] = []
        self.integrated: list[tuple[str, str, str, bool]] = []
        self.pushed: list[str] = []
        self.heads = {"/tmp/manager": "aaa111", "/tmp/worker": "bbb222", "/tmp/reviewer": "bbb222"}
        self.fail_integrate = False
        self.missing_paths: set[str] = set()
        self.identity_errors: dict[str, Exception] = {}

    def implementation_worktree_present(self, worktree_path: str) -> bool:
        return worktree_path not in self.missing_paths

    def verify_implementation_identity(self, _worktree_id, worktree_path, *_args) -> None:
        error = self.identity_errors.get(worktree_path)
        if error is not None:
            raise error

    def implementation_identity(self, _worktree_id: str, worktree_path: str):
        return ("example/repo", "repo", self.heads[worktree_path])

    def integrate_worker_commit(
        self, manager_path: str, worker_path: str, worker_sha: str, *, dry_run: bool = False
    ) -> dict[str, str | None]:
        if self.fail_integrate:
            raise RuntimeError("harvest failed")
        self.integrated.append((manager_path, worker_path, worker_sha, dry_run))
        before = self.heads[manager_path]
        if not dry_run:
            self.heads[manager_path] = worker_sha
        return {"method": "ff", "before": before, "after": worker_sha}

    def push_fast_forward(self, worktree_path: str) -> dict[str, str]:
        self.pushed.append(worktree_path)
        return {"remote": "origin", "ref": "HEAD"}

    def close_terminals(self, worktree_id: str, handle: str | None = None) -> dict[str, bool]:
        self.closed.append((worktree_id, handle))
        return {"closed": True}

    def remove_worktree(self, worktree_id: str) -> dict[str, bool]:
        self.removed.append(worktree_id)
        return {"removed": True}


def test_retire_keep_manager_closes_children_only(tmp_path: Path) -> None:
    service, manager, worker, reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()

    result = service.retire(manager.id, runtime, keep="manager")

    assert result.harvested.method == "ff"
    assert result.harvested.worker_sha == "bbb222"
    assert ("repo::worker", "term-worker") in result.closed_handles
    assert ("repo::reviewer", "term-reviewer") in result.closed_handles
    assert ("repo::manager", "term-manager") not in result.closed_handles
    assert result.removed_worktrees == ("repo::worker", "repo::reviewer")
    assert result.kept == ("repo::manager",)
    assert service.store.get(manager.id).external_reconciled_at is None
    assert service.store.get(worker.id).external_reconciled_at is not None
    assert service.store.get(reviewer.id).external_reconciled_at is not None
    assert "repo::manager" not in runtime.removed


def test_retire_keep_none_does_not_delete_attached_manager(tmp_path: Path) -> None:
    service, manager, _worker, _reviewer = _finished_plan(tmp_path, manager_owns=False)
    runtime = RecordingRuntime()

    result = service.retire(manager.id, runtime, keep="none")

    assert ("repo::manager", "term-manager") in result.closed_handles
    assert "repo::worker" in result.removed_worktrees
    assert "repo::reviewer" in result.removed_worktrees
    assert "repo::manager" not in result.removed_worktrees
    assert result.kept == ("repo::manager",)
    assert service.store.get(manager.id).external_reconciled_at is not None
    assert "repo::manager" not in runtime.removed


def test_harvest_and_retire_refuse_a_running_orchestration(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, _worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    service = WorkflowService(store)
    runtime = RecordingRuntime()

    with pytest.raises(ValueError, match="completed, blocked, or failed"):
        service.harvest(manager.id, runtime)
    with pytest.raises(ValueError, match="completed, blocked, or failed"):
        service.retire(manager.id, runtime, keep="manager")
    assert runtime.removed == []
    assert runtime.integrated == []


def test_retire_dry_run_does_not_mutate_git_or_resources(tmp_path: Path) -> None:
    service, manager, worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()

    result = service.retire(manager.id, runtime, keep="manager", dry_run=True)

    assert result.dry_run is True
    assert result.harvested.dry_run is True
    assert runtime.integrated == [("/tmp/manager", "/tmp/worker", "bbb222", True)]
    assert runtime.heads["/tmp/manager"] == "aaa111"
    assert runtime.closed == []
    assert runtime.removed == []
    assert service.store.get(worker.id).external_reconciled_at is None


def test_failed_harvest_does_not_close_or_remove(tmp_path: Path) -> None:
    service, _manager, worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    runtime.fail_integrate = True

    with pytest.raises(RuntimeError, match="harvest failed"):
        service.retire(worker.id, runtime, keep="manager")

    assert runtime.closed == []
    assert runtime.removed == []
    assert service.store.get(worker.id).external_reconciled_at is None


def test_harvest_refuses_worker_commits_added_after_local_approval(tmp_path: Path) -> None:
    service, manager, _worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    runtime.heads["/tmp/worker"] = "ccc333"

    with pytest.raises(ValueError, match="changed after local approval"):
        service.harvest(manager.id, runtime)

    assert runtime.integrated == []
    assert runtime.heads["/tmp/manager"] == "aaa111"


def test_delivery_check_requires_harvested_approved_sha(tmp_path: Path) -> None:
    service, manager, _worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()

    before = service.delivery_check(manager.id, runtime)
    assert before.eligible is False
    assert before.approved_sha == "bbb222"
    assert before.actual_sha == "aaa111"

    service.harvest(manager.id, runtime)

    after = service.delivery_check(manager.id, runtime)
    assert after.eligible is True
    assert after.approved_sha == "bbb222"
    assert after.actual_sha == "bbb222"
    assert after.reason is None


def test_deliver_approved_fast_forward_pushes_and_rejects_force(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    for workflow_id, name, sha in (
        (manager.id, "manager", "aaa111"),
        (worker.id, "worker", "bbb222"),
        (reviewer.id, "reviewer", "bbb222"),
    ):
        store.begin_start(workflow_id)
        store.attach_external(
            workflow_id,
            adapter_reference=f"repo::{name}",
            worktree_path=f"/tmp/{name}",
            terminal_handle=f"term-{name}",
            implementation_repository="example/repo",
            runtime_repository_id="repo",
            start_sha=sha,
        )
        store.transition(workflow_id, "running")
        if workflow_id != manager.id:
            store.transition(workflow_id, "completed")
    service = WorkflowService(store)
    runtime = RecordingRuntime()
    runtime.heads["/tmp/manager"] = "aaa111"

    with pytest.raises(ValueError, match="force push"):
        service.deliver_approved(manager.id, runtime, force_push=True)
    assert runtime.pushed == []

    delivered = service.deliver_approved(manager.id, runtime)
    assert delivered["check"].eligible is True
    assert delivered["pushed"] == {"remote": "origin", "ref": "HEAD"}
    assert runtime.pushed == ["/tmp/manager"]
    assert runtime.heads["/tmp/manager"] == "bbb222"
    assert store.orchestration_run(manager.id).status == "running"


def test_retire_keep_none_after_keep_manager_skips_gone_worker(tmp_path: Path) -> None:
    service, manager, _worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    service.retire(manager.id, runtime, keep="manager")
    runtime.fail_integrate = True
    runtime.closed.clear()
    runtime.removed.clear()
    runtime.integrated.clear()

    result = service.retire(manager.id, runtime, keep="none")

    assert result.harvested.skip_reason == "worker_worktree_gone"
    assert runtime.integrated == []
    assert ("repo::manager", "term-manager") in result.closed_handles
    assert "repo::manager" in result.removed_worktrees
    assert service.store.get(manager.id).external_reconciled_at is not None


def test_try_retire_keep_manager_records_cleanup_error_without_rolling_back(
    tmp_path: Path,
) -> None:
    service, manager, worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    runtime.fail_integrate = True

    result = service.try_retire_keep_manager(manager.id, runtime)

    assert result["ok"] is False
    assert "harvest failed" in str(result["error"])
    assert service.store.orchestration_run(manager.id).status == "completed"
    assert service.store.get(manager.id).cleanup_error == "harvest failed"
    assert service.store.get(worker.id).external_reconciled_at is None
    assert runtime.removed == []


def test_retire_skips_harvest_when_worker_path_is_gone(tmp_path: Path) -> None:
    service, manager, worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    runtime.missing_paths.add("/tmp/worker")
    runtime.fail_integrate = True

    result = service.retire(manager.id, runtime, keep="manager")

    assert result.harvested.skip_reason == "worker_worktree_gone"
    assert runtime.integrated == []
    assert "repo::worker" in result.removed_worktrees
    assert "repo::reviewer" in result.removed_worktrees
    assert service.store.get(worker.id).external_reconciled_at is not None
    assert service.store.get(manager.id).cleanup_error is None


def test_harvest_does_not_skip_identity_failure_on_present_worker(tmp_path: Path) -> None:
    service, manager, _worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    runtime.identity_errors["/tmp/worker"] = ValueError(
        "implementation repository does not match persisted workflow identity"
    )

    with pytest.raises(ValueError, match="does not match persisted workflow identity"):
        service.harvest(manager.id, runtime)

    assert runtime.integrated == []
    retry = service.try_retire_keep_manager(manager.id, runtime)
    assert retry["ok"] is False
    assert "does not match persisted workflow identity" in str(retry["error"])
    assert service.store.get(manager.id).cleanup_error is not None
    assert runtime.removed == []


def test_try_retire_keep_manager_succeeds_when_worker_path_is_gone(tmp_path: Path) -> None:
    service, manager, worker, _reviewer = _finished_plan(tmp_path)
    runtime = RecordingRuntime()
    runtime.missing_paths.add("/tmp/worker")

    result = service.try_retire_keep_manager(manager.id, runtime)

    assert result["ok"] is True
    harvested = result["result"].harvested
    assert harvested.skip_reason == "worker_worktree_gone"
    assert service.store.orchestration_run(manager.id).status == "completed"
    assert service.store.get(manager.id).cleanup_error is None
    assert service.store.get(worker.id).external_reconciled_at is not None
    assert "repo::worker" in runtime.removed
