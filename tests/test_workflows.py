import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flybridge_application import WorkflowService
from flybridge_core import ReconcileStore, ResourceQueue, WorkflowArtifactStore, WorkflowStore
from flybridge_core.workflows import SCHEMA_VERSION
from flybridge_orca.client import OrcaStartError, OrcaTimeoutError


def test_workflow_lifecycle_is_durable_and_explicit(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    requested = workflows.create(tmp_path, "single", "example", "Implement the change.")

    starting = workflows.transition(requested.id, "starting")
    running = workflows.transition(starting.id, "running", adapter_reference="path:/tmp/example")
    completed = workflows.transition(running.id, "completed")

    assert requested.status == "requested"
    assert running.adapter_reference == "path:/tmp/example"
    assert completed.status == "completed"
    assert WorkflowStore(tmp_path).get(requested.id) == completed
    assert workflows.active() == []


def test_registered_source_urls_return_only_persisted_issue_and_pull_request_refs(
    tmp_path: Path,
) -> None:
    workflows = WorkflowStore(tmp_path)
    workflow = workflows.create(tmp_path, "single", "sources", "Review.")
    refs = ReconcileStore(tmp_path)
    refs.link_reference(workflow.run_id, "https://github.com/example/repo/pull/12", "related")
    refs.link_reference(workflow.run_id, "https://github.com/example/repo/issues/7", "primary")
    refs.add_inferred_reference(
        workflow.run_id,
        "https://github.com/example/repo/pull/99",
        relation="candidate",
        source="branch",
    )

    assert workflows.registered_source_urls(workflow.run_id) == (
        "https://github.com/example/repo/issues/7",
        "https://github.com/example/repo/pull/12",
    )


def test_workflow_persists_verified_implementation_identity_and_start_sha(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    requested = service.store.create(
        tmp_path,
        "single",
        "identity",
        "Implement.",
        issue_url="https://github.com/example/tracker/issues/7",
    )

    running = service.start_existing(
        requested.id,
        lambda: SimpleNamespace(
            worktree_id="github:example/implementation::/tmp/worktree",
            worktree="/tmp/worktree",
            terminal="terminal",
        ),
        lambda _reference: None,
        lambda *_args: None,
        identify_external=lambda _external: (
            "example/implementation",
            "github:example/implementation",
            "abc123",
        ),
    )

    reopened = WorkflowStore(tmp_path).get(running.id)
    assert reopened.implementation_repository == "example/implementation"
    assert reopened.runtime_repository_id == "github:example/implementation"
    assert reopened.start_sha == "abc123"
    assert reopened.issue_url == "https://github.com/example/tracker/issues/7"


def test_resume_fails_closed_when_persisted_implementation_identity_mismatches(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "identity", "Implement.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="github:example/implementation::/tmp/worktree",
        worktree_path="/tmp/worktree",
        terminal_handle="terminal",
        implementation_repository="example/implementation",
        runtime_repository_id="github:example/implementation",
        start_sha="abc123",
    )
    store.transition(workflow.id, "running")

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def verify_implementation_identity(self, *_args) -> None:
            raise RuntimeError(
                "implementation repository does not match persisted workflow identity"
            )

        def terminal_is_valid(self, *_args) -> bool:
            raise AssertionError("terminal must not be touched after an identity mismatch")

    with pytest.raises(RuntimeError, match="does not match"):
        WorkflowService(store).resume(
            workflow.id,
            Runtime(),
            agent="codex",
            response_language="English",
            skill_paths=(),
        )


@pytest.mark.parametrize(
    ("implementation_repository", "expected"),
    [
        ("example/implementation", 42),
        ("example/tracker", None),
    ],
)
def test_orca_issue_link_requires_exact_implementation_repository_match(
    tmp_path: Path,
    monkeypatch,
    implementation_repository: str,
    expected: int | None,
) -> None:
    class Probe:
        def inspect(self, _path: str):
            return SimpleNamespace(github_repository=implementation_repository)

    monkeypatch.setattr("flybridge_application.workflows.GitWorktreeProbe", Probe)

    issue_number = WorkflowService.matching_github_issue_number(
        tmp_path,
        "https://github.com/example/implementation/issues/42",
    )

    assert issue_number == expected


def test_role_ready_verifies_artifact_identity_and_pristine_manager_state(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager, _worker, _reviewer = store.create_orchestrated_plan(
        tmp_path, "autonomous", "Implement."
    )
    store.begin_start(manager.id)
    store.attach_external(
        manager.id,
        adapter_reference="repo::manager",
        worktree_path="/tmp/manager",
        terminal_handle="manager-terminal",
        implementation_repository="example/repo",
        runtime_repository_id="repo",
        start_sha="start",
    )
    store.transition(manager.id, "running")
    service.put_artifact(manager.id, "plan", "Acceptance criteria and implementation plan.")
    calls: list[tuple[str, str]] = []

    class Runtime:
        def verify_implementation_identity(self, *_args) -> None:
            calls.append(("identity", "start"))

        def verify_pristine_start(self, path: str, sha: str) -> None:
            calls.append((path, sha))

    readiness = service.role_ready(manager.id, Runtime(), summary="Implement the verified plan.")
    repeated = service.role_ready(manager.id, Runtime(), summary="Implement the verified plan.")

    assert readiness.role == "manager"
    assert repeated.id == readiness.id
    assert readiness.attempt == 1
    assert calls == [
        ("identity", "start"),
        ("/tmp/manager", "start"),
        ("identity", "start"),
        ("/tmp/manager", "start"),
    ]
    assert store.get(manager.id).status == "running"
    assert store.owned_terminal_handles(manager.id) == ["manager-terminal"]

    service.put_artifact(manager.id, "plan", "A newer mutable plan slot.")
    assert (
        service.verify_readiness_artifact(readiness)
        == "Acceptance criteria and implementation plan."
    )


def test_role_ready_requires_worker_commit_and_reviewer_outcome(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    _manager, worker, reviewer = store.create_orchestrated_plan(tmp_path, "gates", "Implement.")
    for workflow, reference in ((worker, "worker"), (reviewer, "reviewer")):
        store.begin_start(workflow.id)
        store.attach_external(
            workflow.id,
            adapter_reference=f"repo::{reference}",
            worktree_path=f"/tmp/{reference}",
            terminal_handle=f"{reference}-terminal",
            implementation_repository="example/repo",
            runtime_repository_id="repo",
            start_sha="start",
        )
        store.transition(workflow.id, "running")
    service.put_artifact(worker.id, "verification", "All acceptance checks passed.")
    service.put_artifact(reviewer.id, "review", "Implementation is approved.")

    class Runtime:
        def verify_implementation_identity(self, *_args) -> None:
            pass

        def verify_worker_ready(self, _path: str, _sha: str) -> None:
            raise RuntimeError("worker must create at least one implementation commit")

        def verify_pristine_start(self, _path: str, _sha: str) -> None:
            pass

    with pytest.raises(RuntimeError, match="implementation commit"):
        service.role_ready(worker.id, Runtime(), summary="Ready for review.")
    with pytest.raises(ValueError, match="requires approved or changes-requested"):
        service.role_ready(reviewer.id, Runtime(), summary="Reviewed.")


def test_role_ready_rejects_single_workflows_before_runtime_verification(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    single = service.store.create(tmp_path, "single", "single-ready", "Implement.")

    with pytest.raises(ValueError, match="role-ready requires an orchestrated role"):
        service.role_ready(single.id, object(), summary="Finished.")


def test_review_attempt_model_reopens_fixed_roles_for_two_cycles(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, reviewer = store.create_orchestrated_plan(
        tmp_path, "cycles", "Implement.", max_review_cycles=3
    )
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager")
    store.transition(manager.id, "completed")
    store.transition(worker.id, "starting")
    store.attach_external(
        worker.id,
        adapter_reference="worker",
        worktree_path="/tmp/worker",
        terminal_handle="worker-terminal",
    )
    store.transition(worker.id, "running")
    store.transition(worker.id, "completed")
    store.transition(reviewer.id, "starting")
    store.attach_external(
        reviewer.id,
        adapter_reference="reviewer",
        worktree_path="/tmp/reviewer",
        terminal_handle="reviewer-terminal",
    )
    store.transition(reviewer.id, "running")
    readiness = store.record_role_readiness(
        reviewer.id,
        summary="Fix the review findings.",
        outcome="changes-requested",
        artifact_kind="review",
        artifact_sha256="a" * 64,
        artifact_content="Changes requested.",
    )
    store.transition(reviewer.id, "completed")
    store.mark_external_reconciled(reviewer.id)

    run = store.prepare_next_review_cycle(
        manager.id,
        worker_start_sha="reviewed-sha",
        reviewer_readiness_id=readiness.id,
    )

    assert run.current_review_cycle == 2
    assert store.get(worker.id).status == "running"
    assert store.get(worker.id).start_sha == "reviewed-sha"
    assert store.get(reviewer.id).status == "requested"
    assert store.get(reviewer.id).adapter_reference is None


def test_role_readiness_history_orders_cycles_without_artifact_bodies(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, reviewer = store.create_orchestrated_plan(
        tmp_path, "history", "Implement.", max_review_cycles=3
    )
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager")
    store.record_role_readiness(
        manager.id,
        summary="Implement the plan.",
        artifact_kind="plan",
        artifact_sha256="a" * 64,
        artifact_content="Secret plan body.",
    )
    store.transition(manager.id, "completed")
    store.transition(worker.id, "starting")
    store.attach_external(
        worker.id,
        adapter_reference="repo::worker",
        worktree_path="/tmp/worker",
        terminal_handle="worker-terminal",
        implementation_repository="example/repo",
        runtime_repository_id="repo",
        start_sha="sha-0",
    )
    store.transition(worker.id, "running")
    store.record_role_readiness(
        worker.id,
        summary="Review the implementation.",
        artifact_kind="verification",
        artifact_sha256="b" * 64,
        artifact_content="Secret verification body.",
    )
    store.transition(worker.id, "completed")
    store.transition(reviewer.id, "starting")
    store.attach_external(
        reviewer.id,
        adapter_reference="repo::reviewer",
        worktree_path="/tmp/reviewer",
        terminal_handle="reviewer-terminal",
    )
    store.transition(reviewer.id, "running")
    first_review = store.record_role_readiness(
        reviewer.id,
        summary="Fix the review findings.",
        outcome="changes-requested",
        artifact_kind="review",
        artifact_sha256="c" * 64,
        artifact_content="Secret review body.",
    )
    store.transition(reviewer.id, "completed")
    store.mark_external_reconciled(reviewer.id)
    store.prepare_next_review_cycle(
        manager.id,
        worker_start_sha="reviewed-sha",
        reviewer_readiness_id=first_review.id,
    )
    store.record_role_readiness(
        worker.id,
        summary="Review the fix.",
        artifact_kind="verification",
        artifact_sha256="d" * 64,
        artifact_content="Secret second verification.",
    )

    history = store.role_readiness_history(manager.id)

    assert [item.role for item in history] == ["manager", "worker", "reviewer", "worker"]
    assert [item.attempt for item in history] == [1, 1, 1, 2]
    assert [item.summary for item in history] == [
        "Implement the plan.",
        "Review the implementation.",
        "Fix the review findings.",
        "Review the fix.",
    ]
    assert not any(hasattr(item, "artifact_content") for item in history)
    assert all("Secret" not in item.summary for item in history)
    dumped = history[0].__dict__
    assert "artifact_content" not in dumped
    with pytest.raises(ValueError, match="workflow was not found"):
        store.role_readiness_history("missing")


def _reviewed_cycle_plan(store: WorkflowStore, tmp_path: Path):
    """Build a root whose worker and reviewer finished one review cycle."""
    manager, worker, reviewer = store.create_orchestrated_plan(
        tmp_path, "sha-cycles", "Implement.", max_review_cycles=3
    )
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager")
    store.transition(manager.id, "completed")
    store.transition(worker.id, "starting")
    store.attach_external(
        worker.id,
        adapter_reference="repo::worker",
        worktree_path="/tmp/worker",
        terminal_handle="worker-terminal",
        implementation_repository="example/repo",
        runtime_repository_id="repo",
        start_sha="sha-0",
    )
    store.transition(worker.id, "running")
    store.transition(worker.id, "completed")
    store.transition(reviewer.id, "starting")
    store.attach_external(
        reviewer.id,
        adapter_reference="repo::reviewer",
        worktree_path="/tmp/reviewer",
        terminal_handle="reviewer-terminal",
    )
    store.transition(reviewer.id, "running")
    readiness = store.record_role_readiness(
        reviewer.id,
        summary="Fix the review findings.",
        outcome="changes-requested",
        artifact_kind="review",
        artifact_sha256="a" * 64,
        artifact_content="Changes requested.",
    )
    store.transition(reviewer.id, "completed")
    store.mark_external_reconciled(reviewer.id)
    return manager, worker, reviewer, readiness


class _HeadTrackingRuntime:
    """Apply the real readiness-gate semantics against a simulated repository HEAD."""

    def __init__(self, head: str) -> None:
        self.head = head

    def implementation_identity(self, _worktree_id: str, _worktree_path: str):
        return "example/repo", "repo", self.head

    def verify_implementation_identity(self, *_args) -> None:
        pass

    def verify_worker_ready(self, _path: str, start_sha: str) -> None:
        if self.head == start_sha:
            raise RuntimeError(
                "worker must create at least one implementation commit beyond start SHA"
            )

    def verify_pristine_start(self, _path: str, start_sha: str) -> None:
        if self.head != start_sha:
            raise RuntimeError("implementation HEAD does not equal the persisted start SHA")


def test_next_review_cycle_requires_a_commit_made_after_the_review(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager, worker, _reviewer, readiness = _reviewed_cycle_plan(store, tmp_path)
    runtime = _HeadTrackingRuntime("sha-1")

    service.begin_next_review_cycle(manager.id, readiness.id, runtime)

    reopened = store.get(worker.id)
    assert reopened.start_sha == "sha-1"
    assert reopened.implementation_repository == "example/repo"
    assert reopened.runtime_repository_id == "repo"
    service.put_artifact(worker.id, "verification", "Unchanged evidence.")
    with pytest.raises(RuntimeError, match="implementation commit"):
        service.role_ready(worker.id, runtime, summary="Review the fix.")

    runtime.head = "sha-2"
    service.put_artifact(worker.id, "verification", "Fix verified.")
    readiness = service.role_ready(worker.id, runtime, summary="Review the fix.")

    assert readiness.attempt == 2


def test_next_review_cycle_fails_closed_when_worker_identity_changed(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager, worker, _reviewer, readiness = _reviewed_cycle_plan(store, tmp_path)
    runtime = _HeadTrackingRuntime("sha-1")
    runtime.implementation_identity = lambda *_args: ("other/repo", "repo", "sha-1")

    with pytest.raises(ValueError, match="repository identity changed"):
        service.begin_next_review_cycle(manager.id, readiness.id, runtime)

    assert store.get(worker.id).status == "completed"
    assert store.orchestration_run(manager.id).current_review_cycle == 1


def _running_manager_with_coordinator(store: WorkflowStore, tmp_path: Path):
    manager, _worker, _reviewer = store.create_orchestrated_plan(
        tmp_path, "coordinator", "Implement."
    )
    store.transition(manager.id, "starting")
    store.attach_external(
        manager.id,
        adapter_reference="repo::manager",
        worktree_path="/tmp/manager",
        terminal_handle="agent",
    )
    store.transition(manager.id, "running")
    store.add_owned_terminal(manager.id, "coordinator-terminal", "coordinator")
    return manager


def test_coordinator_release_records_termination_without_closing_the_terminal(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager = _running_manager_with_coordinator(store, tmp_path)

    released = service.release_coordinator(manager.id, "orchestration_completed")

    run = store.orchestration_run(manager.id)
    assert released == ("coordinator-terminal",)
    assert run.coordinator_handle == "coordinator-terminal"
    assert run.coordinator_release_reason == "orchestration_completed"
    assert run.coordinator_released_at is not None
    assert store.owned_terminal_handles(manager.id, kind="coordinator") == []


def test_reviewer_outcome_and_readiness_consumption_replay_atomically(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager = _running_manager_with_coordinator(store, tmp_path)
    _worker, reviewer = store.children(manager.id)
    store.transition(reviewer.id, "starting")
    store.attach_external(
        reviewer.id,
        adapter_reference="repo::reviewer",
        worktree_path="/tmp/reviewer",
        terminal_handle="reviewer-terminal",
    )
    store.transition(reviewer.id, "running")
    readiness = store.record_role_readiness(
        reviewer.id,
        summary="Approved.",
        outcome="approved",
        artifact_kind="review",
        artifact_sha256="a" * 64,
        artifact_content="Approved.",
    )

    first = store.finalize_reviewer_outcome(
        readiness.id,
        status="completed",
        error=None,
        coordinator_reason="orchestration_completed",
    )
    replayed = WorkflowStore(tmp_path).finalize_reviewer_outcome(
        readiness.id,
        status="completed",
        error=None,
        coordinator_reason="different_replay_reason",
    )

    assert first.status == replayed.status == "completed"
    assert store.role_readiness(manager.id, "reviewer", 1).consumed_at is not None
    assert replayed.coordinator_release_reason == "orchestration_completed"
    assert store.owned_terminal_handles(manager.id, kind="coordinator") == []


def test_blocked_readiness_is_separate_from_reviewer_outcome_and_finalizes_atomically(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    manager = _running_manager_with_coordinator(store, tmp_path)
    readiness = store.record_role_readiness(
        manager.id,
        summary="Live fleet is unavailable.",
        blocked_reason="Live fleet is unavailable.",
        artifact_kind="plan",
        artifact_sha256="a" * 64,
        artifact_content="Verified scope and blocked verification.",
    )
    store.transition(manager.id, "cancelled")

    blocked = store.finalize_blocked_readiness(readiness.id)
    replayed = WorkflowStore(tmp_path).finalize_blocked_readiness(readiness.id)

    assert readiness.outcome is None
    assert readiness.blocked_reason == "Live fleet is unavailable."
    assert blocked.status == replayed.status == "blocked"
    assert "manager blocked: Live fleet is unavailable." == blocked.error
    assert store.role_readiness(manager.id, "manager", 1).consumed_at is not None
    assert blocked.coordinator_release_reason == "orchestration_blocked"


def test_next_review_cycle_cas_rejects_consumed_readiness_without_partial_updates(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, reviewer, readiness = _reviewed_cycle_plan(store, tmp_path)
    store.consume_role_readiness(readiness.id)

    with pytest.raises(ValueError, match="cannot open"):
        store.prepare_next_review_cycle(
            manager.id,
            worker_start_sha="sha-1",
            reviewer_readiness_id=readiness.id,
        )

    assert store.get(worker.id).status == "completed"
    assert store.get(reviewer.id).status == "completed"
    assert store.orchestration_run(manager.id).current_review_cycle == 1


def test_attach_coordinator_replaces_stale_owned_handle(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager = _running_manager_with_coordinator(store, tmp_path)
    created: list[str] = []

    class Runtime:
        def terminal_is_valid(self, _worktree_id: str, handle: str) -> bool:
            assert handle == "coordinator-terminal"
            return False

        def create_coordinator(self, _worktree_id: str, command: str) -> str:
            created.append(command)
            return "replacement-coordinator"

        def close_terminals(self, *_args) -> None:
            pass

    handle = service.attach_coordinator(manager.id, Runtime(), "flybridge workflow supervise")

    assert handle == "replacement-coordinator"
    assert created == ["flybridge workflow supervise"]
    assert store.owned_terminal_handles(manager.id, kind="coordinator") == [
        "replacement-coordinator"
    ]


def test_operator_cleanup_can_close_a_released_coordinator_tab(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager = _running_manager_with_coordinator(store, tmp_path)
    service.release_coordinator(manager.id, "orchestration_completed")
    closed: list[tuple[str, str]] = []

    class Runtime:
        def close_terminals(self, worktree_id: str, handle: str) -> None:
            closed.append((worktree_id, handle))

    handles = service.close_coordinator(manager.id, Runtime(), "operator_cleanup")

    assert handles == ("coordinator-terminal",)
    assert closed == [("repo::manager", "coordinator-terminal")]
    assert (
        store.orchestration_run(manager.id).coordinator_release_reason == "orchestration_completed"
    )


def test_release_coordinator_is_noop_after_first_release(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager = _running_manager_with_coordinator(store, tmp_path)

    first = service.release_coordinator(manager.id, "orchestration_completed")
    second = service.release_coordinator(manager.id, "orchestration_terminal")

    run = store.orchestration_run(manager.id)
    assert first == ("coordinator-terminal",)
    assert second == ()
    assert run.coordinator_release_reason == "orchestration_completed"


def test_coordinator_error_limit_blocks_after_persisted_exponential_retries(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    manager = _running_manager_with_coordinator(store, tmp_path)

    first = store.record_coordinator_error(
        manager.id,
        "timeout one",
        max_errors=2,
        initial_delay_seconds=1,
        max_delay_seconds=10,
    )
    second = WorkflowStore(tmp_path).record_coordinator_error(
        manager.id,
        "timeout two",
        max_errors=2,
        initial_delay_seconds=1,
        max_delay_seconds=10,
    )

    assert first.status == "running"
    assert first.coordinator_error_count == 1
    assert first.coordinator_retry_at is not None
    assert second.status == "blocked"
    assert second.coordinator_error_count == 2
    assert "retry limit reached" in second.error


def test_coordinator_retry_cas_preserves_pending_blocker_and_clears_release_state(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    manager = _running_manager_with_coordinator(store, tmp_path)
    readiness = store.record_role_readiness(
        manager.id,
        summary="Live environment unavailable.",
        blocked_reason="Live environment unavailable.",
        artifact_kind="plan",
        artifact_sha256="a" * 64,
        artifact_content="Blocked scope.",
    )
    store.set_orchestration_outcome(
        manager.id,
        "blocked",
        error="TypeError: unexpected keyword argument 'blocked_reason'",
    )
    store.release_coordinator_ownership(manager.id, "orchestration_blocked")
    before = store.orchestration_run(manager.id)

    retried = store.retry_orchestration(manager.id, expected_updated_at=before.updated_at)

    assert retried.status == "running"
    assert retried.error is None
    assert retried.coordinator_handle is None
    assert retried.coordinator_released_at is None
    assert retried.coordinator_release_reason is None
    assert retried.coordinator_error_count == 0
    assert store.pending_blocker(manager.id).id == readiness.id


def test_coordinator_retry_rejects_consumed_blocker_and_completed_run(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager = _running_manager_with_coordinator(store, tmp_path)
    readiness = store.record_role_readiness(
        manager.id,
        summary="Deliberate blocker.",
        blocked_reason="Deliberate blocker.",
        artifact_kind="plan",
        artifact_sha256="a" * 64,
        artifact_content="Blocked scope.",
    )
    store.transition(manager.id, "cancelled")
    blocked = store.finalize_blocked_readiness(readiness.id)

    with pytest.raises(ValueError, match="consumed declared blocker"):
        store.retry_orchestration(manager.id, expected_updated_at=blocked.updated_at)

    completed_store = WorkflowStore(tmp_path / "completed")
    completed_manager = _running_manager_with_coordinator(completed_store, tmp_path)
    completed = completed_store.set_orchestration_outcome(completed_manager.id, "completed")
    with pytest.raises(ValueError, match="completed orchestration"):
        completed_store.retry_orchestration(
            completed_manager.id, expected_updated_at=completed.updated_at
        )


def test_coordinator_retry_rejects_consumed_maximum_review_cycle(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, _worker, reviewer = store.create_orchestrated_plan(
        tmp_path, "max-cycle", "Review.", max_review_cycles=1
    )
    store.transition(reviewer.id, "starting")
    store.transition(reviewer.id, "running", adapter_reference="reviewer")
    readiness = store.record_role_readiness(
        reviewer.id,
        summary="Changes required.",
        outcome="changes-requested",
        artifact_kind="review",
        artifact_sha256="a" * 64,
        artifact_content="Changes required.",
    )
    store.consume_role_readiness(readiness.id)
    blocked = store.set_orchestration_outcome(
        manager.id, "blocked", error="review changes requested at maximum cycle 1"
    )

    with pytest.raises(ValueError, match="maximum review cycle"):
        store.retry_orchestration(manager.id, expected_updated_at=blocked.updated_at)


def test_manager_completion_excludes_durable_coordinator_from_cleanup(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store)
    manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "cleanup", "Plan.")
    store.transition(manager.id, "starting")
    store.attach_external(
        manager.id,
        adapter_reference="manager",
        worktree_path="/tmp/manager",
        terminal_handle="agent",
    )
    store.transition(manager.id, "running")
    store.add_owned_terminal(manager.id, "observer", "observer")
    store.add_owned_terminal(manager.id, "coordinator", "coordinator")
    store.record_handoff(manager.id, worker.id, "Implement the plan.")
    closed: list[str] = []

    service.finish(
        manager.id,
        "completed",
        lambda _reference: None,
        close_external=lambda _reference, handle: closed.append(str(handle)),
    )

    assert closed == ["agent", "observer"]
    assert store.owned_terminal_handles(manager.id, kind="coordinator") == ["coordinator"]


def test_workflow_rejects_invalid_transitions(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    workflow = workflows.create(
        tmp_path, "orchestrated", "example", "Implement the change.", role="manager"
    )

    with pytest.raises(ValueError, match="single workflows"):
        workflows.create(tmp_path, "single", "invalid", "Implement.", role="manager")

    with pytest.raises(ValueError, match="cannot transition"):
        workflows.transition(workflow.id, "completed")


def test_workflow_store_rejects_an_unsupported_existing_schema(tmp_path: Path) -> None:
    database = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE workflows (id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="legacy Flybridge state"):
        WorkflowStore(tmp_path)


def test_schema_opens_the_current_state_version(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "current", "Implement.")

    reopened = WorkflowStore(tmp_path)

    assert reopened.get(workflow.id).id == workflow.id
    with reopened._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "workflow_owned_adapter_reference" in indexes
    assert "workflow_manager_role" in indexes
    assert "workflow_handoff_target" in indexes
    assert "workflow_active_name" in indexes
    assert "workflow_name" not in indexes
    with sqlite3.connect(reopened.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_schema_rejects_an_unsupported_state_version(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    store.create(tmp_path, "single", "unsupported", "Implement.")
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="unsupported Flybridge state schema"):
        WorkflowStore(tmp_path)


def test_schema_rejects_a_foreign_database_without_workflow_tables(tmp_path: Path) -> None:
    database = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="legacy Flybridge state"):
        WorkflowStore(tmp_path)


def test_schema_rejects_a_current_version_database_without_any_table(tmp_path: Path) -> None:
    database = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    with pytest.raises(RuntimeError, match="legacy Flybridge state"):
        WorkflowStore(tmp_path)


@pytest.mark.parametrize(
    "statement, message",
    [
        ("DROP TABLE workflow_lifecycle_operations", "table workflow_lifecycle_operations"),
        ("ALTER TABLE workflows DROP COLUMN cleanup_error", "definitions differ for workflows"),
        ("DROP INDEX workflow_manager_role", "index workflow_manager_role is missing"),
    ],
)
def test_schema_rejects_a_damaged_current_version_database(
    statement: str, message: str, tmp_path: Path
) -> None:
    store = WorkflowStore(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(statement)

    with pytest.raises(RuntimeError, match=message):
        WorkflowStore(tmp_path)


def test_orchestrated_plan_assigns_reviewer_slots(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, first, second = store.create_orchestrated_plan(
        tmp_path, "slots", "Implement.", reviewer_count=2
    )

    assert worker.slot == 0
    assert [item.name for item in (first, second)] == [
        f"slots-reviewer-0-{first.id[:8]}",
        f"slots-reviewer-1-{second.id[:8]}",
    ]
    assert worker.name == f"slots-worker-{worker.id[:8]}"
    assert [item.slot for item in (first, second)] == [0, 1]
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager")
    store.record_handoff(manager.id, worker.id, "Implement the plan.")
    store.transition(manager.id, "completed")
    store.transition(worker.id, "starting")
    store.transition(worker.id, "running", adapter_reference="worker")
    store.record_handoff(worker.id, first.id, "Review both.")
    store.record_handoff(worker.id, second.id, "Review both.")
    store.transition(worker.id, "completed")

    assert store.get(worker.id).status.value == "completed"


def test_manager_cannot_own_duplicate_worker_or_reviewer_records(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, _worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")

    with pytest.raises(ValueError, match="manager already has a worker"):
        store.create(
            tmp_path,
            "orchestrated",
            "other-worker",
            "Implement.",
            role="worker",
            parent_id=manager.id,
        )
    with pytest.raises(ValueError, match="manager already has a reviewer"):
        store.create(
            tmp_path,
            "orchestrated",
            "other-reviewer",
            "Implement.",
            role="reviewer",
            parent_id=manager.id,
        )


def test_orchestrated_plan_advances_in_fixed_role_order(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    manager, *children = service.store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    service.store.transition(manager.id, "starting")
    service.store.attach_external(
        manager.id,
        adapter_reference="manager-id",
        worktree_path="/tmp/manager",
        terminal_handle="t",
    )
    service.store.transition(manager.id, "running")
    service.store.transition(manager.id, "completed")

    assert service.plan_children(manager.id) == children

    assert [child.role for child in children] == ["worker", "reviewer"]
    service.store.record_handoff(manager.id, children[0].id, "Implementation handoff.")
    assert service.next_ready_child(manager.id).role == "worker"
    service.store.transition(children[0].id, "starting")
    service.store.transition(children[0].id, "running", adapter_reference="worker-id")
    service.store.transition(children[0].id, "completed")
    service.store.record_handoff(children[0].id, children[1].id, "Review handoff.")
    assert service.next_ready_child(manager.id).role == "reviewer"


def test_handoff_rejects_a_completed_workflow_outside_the_role_plan(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    _manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    unrelated = store.create(tmp_path, "single", "unrelated", "Complete.")
    store.transition(unrelated.id, "starting")
    store.attach_external(
        unrelated.id,
        adapter_reference="unrelated-id",
        worktree_path="/tmp/unrelated",
        terminal_handle="terminal",
    )
    store.transition(unrelated.id, "running")
    store.transition(unrelated.id, "completed")

    with pytest.raises(ValueError, match="role-plan edge"):
        store.record_handoff(unrelated.id, worker.id, "Wrong source.")


def test_role_plan_is_atomic_when_a_child_name_conflicts(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    store.create_orchestrated_plan(tmp_path, "task", "Existing.")

    with pytest.raises(ValueError, match="role-plan name"):
        store.create_orchestrated_plan(tmp_path, "task", "Implement.")

    assert [workflow.name for workflow in store.active() if workflow.parent_id is None] == ["task"]


def test_failed_activation_keeps_owned_metadata_and_compensates(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    requested = service.store.create(tmp_path, "single", "task", "Implement.")
    cleaned: list[str] = []

    with pytest.raises(RuntimeError, match="lifecycle failed"):
        service.start_existing(
            requested.id,
            lambda: SimpleNamespace(worktree_id="owned-id", worktree="/tmp/owned", terminal="term"),
            lambda _reference: (_ for _ in ()).throw(RuntimeError("lifecycle failed")),
            lambda reference, _terminal: cleaned.append(reference),
        )

    failed = service.store.get(requested.id)
    assert failed.status == "failed"
    assert failed.adapter_reference == "owned-id"
    assert failed.worktree_path == "/tmp/owned"
    assert cleaned == ["owned-id"]


def test_manager_start_failure_cancels_requested_children_and_queue(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    manager, worker, reviewer = service.store.create_orchestrated_plan(
        tmp_path, "task", "Implement."
    )
    request = queue.acquire("exclusive", manager.id)

    with pytest.raises(RuntimeError, match="create failed"):
        service.start_existing(
            manager.id,
            lambda: (_ for _ in ()).throw(RuntimeError("create failed")),
            lambda _reference: None,
            lambda _reference, _handle: None,
        )

    assert service.store.get(manager.id).status == "failed"
    assert service.store.get(worker.id).status == "cancelled"
    assert service.store.get(reviewer.id).status == "cancelled"
    assert queue.inspect(request.request_id)["status"] == "cancelled"


def test_partial_external_start_is_recorded_if_removal_fails(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    requested = service.store.create(tmp_path, "single", "partial", "Implement.")

    with pytest.raises(ValueError, match="owned terminal"):
        service.start_existing(
            requested.id,
            lambda: SimpleNamespace(worktree_id="partial-id", worktree="/tmp/partial", terminal=""),
            lambda _reference: None,
            lambda _reference, _terminal: None,
            lambda _reference: (_ for _ in ()).throw(RuntimeError("remove failed")),
        )

    failed = service.store.get(requested.id)
    assert failed.status == "failed"
    assert failed.adapter_reference == "partial-id"
    assert failed.external_reconciled_at is None
    assert "remove failed" in (failed.error or "")


def test_exact_partial_create_response_is_compensated_and_reconciled(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    requested = service.store.create(tmp_path, "single", "partial", "Implement.")
    closed: list[tuple[str, str | None]] = []
    removed: list[str] = []

    def create_external():
        raise OrcaStartError(
            "Orca worktree creation timed out after allocation",
            worktree_id="partial-id",
            worktree_path="/tmp/partial",
            terminal_handle="partial-terminal",
        )

    with pytest.raises(RuntimeError, match="timed out after allocation"):
        service.start_existing(
            requested.id,
            create_external,
            lambda _reference: None,
            lambda reference, handle: closed.append((reference, handle)),
            removed.append,
        )

    failed = service.store.get(requested.id)
    assert failed.status == "failed"
    assert failed.adapter_reference == "partial-id"
    assert closed == [("partial-id", "partial-terminal")]
    assert removed == ["partial-id"]


def test_keyboard_interrupt_during_start_fails_the_requested_workflow(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    requested = service.store.create(tmp_path, "single", "interrupted", "Implement.")

    def create_external():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        service.start_existing(
            requested.id,
            create_external,
            lambda _reference: None,
            lambda *_args: None,
        )

    failed = service.store.get(requested.id)
    assert failed.status == "failed"
    assert "KeyboardInterrupt" in (failed.error or "")


def test_keyboard_interrupt_during_finish_is_recoverable_by_cleanup(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "finish-interrupted", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.transition(workflow.id, "running")

    with pytest.raises(KeyboardInterrupt):
        service.finish(
            workflow.id,
            "completed",
            lambda _reference: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    operation = service.store.lifecycle_operation(workflow.id)
    assert operation is not None
    assert operation[1] == "KeyboardInterrupt: "
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []

    service.reconcile_stale(60, lambda *_args: None, removed.append)

    current = service.store.get(workflow.id)
    assert current.status == "cancelled"
    assert current.external_reconciled_at is not None
    assert removed == ["owned-id"]


def test_cleanup_skips_a_workflow_that_completed_during_reconciliation(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "race-complete", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.transition(workflow.id, "running")
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []
    completing = False
    real_get = service.store.get

    def wrapped_get(workflow_id: str):
        nonlocal completing
        record = real_get(workflow_id)
        if not completing and record.status == "running":
            completing = True
            service.finish(workflow_id, "completed", lambda _reference: None)
            return real_get(workflow_id)
        return record

    service.store.get = wrapped_get  # type: ignore[method-assign]
    service.reconcile_stale(60, lambda *_args: None, removed.append)

    assert removed == []
    assert real_get(workflow.id).status == "completed"


def test_cleanup_skips_in_flight_terminal_claims(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "claimed", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.transition(workflow.id, "running")
    service.store.claim_terminal_transition(workflow.id, "completed")
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []

    result = service.reconcile_stale(60, lambda *_args: None, removed.append)

    assert result.reconciled == []
    assert any("lifecycle operation is in progress" in error for error in result.errors)
    assert removed == []
    assert service.store.get(workflow.id).status == "running"


def test_cleanup_takes_over_a_stale_activation_claim(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "stale-activation", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.claim_activation(workflow.id)
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
        connection.execute(
            "UPDATE workflow_lifecycle_operations SET updated_at = ? WHERE workflow_id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []

    reconciled = service.reconcile_stale(60, lambda *_args: None, removed.append)

    current = service.store.get(workflow.id)
    assert [record.id for record in reconciled] == [workflow.id]
    assert current.status == "cancelled"
    assert current.external_reconciled_at is not None
    assert service.store.lifecycle_operation(workflow.id) is None
    assert removed == ["owned-id"]


def test_cleanup_retries_after_external_removal_precedes_database_completion(
    tmp_path: Path, monkeypatch
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "cleanup-crash", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.transition(workflow.id, "running")
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []
    real_complete = service.store.complete_stale_reconciliation

    def interrupt_completion(_workflow_id: str):
        raise RuntimeError("simulated crash before database completion")

    monkeypatch.setattr(service.store, "complete_stale_reconciliation", interrupt_completion)
    result = service.reconcile_stale(60, lambda *_args: None, removed.append)
    assert any("simulated crash" in error for error in result.errors)

    current = service.store.get(workflow.id)
    assert current.status == "running"
    assert current.external_reconciled_at is None
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflow_lifecycle_operations SET updated_at = ? WHERE workflow_id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    monkeypatch.setattr(service.store, "complete_stale_reconciliation", real_complete)

    service.reconcile_stale(60, lambda *_args: None, removed.append)

    current = service.store.get(workflow.id)
    assert current.status == "cancelled"
    assert current.external_reconciled_at is not None
    assert removed == ["owned-id", "owned-id"]


def test_unowned_creation_timeout_is_diagnosed_without_removal(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    requested = service.store.create(tmp_path, "single", "task", "Implement.")
    closed: list[tuple[str, str | None]] = []
    removed: list[str] = []

    def create_external():
        raise OrcaTimeoutError(
            "Orca worktree creation timed out without an ownership receipt; a worktree named "
            "task exists (id foreign, path /tmp/foreign) but Flybridge cannot prove it created "
            "that worktree, so it was left in place",
            possible_orphan_id="foreign",
            possible_orphan_path="/tmp/foreign",
        )

    with pytest.raises(RuntimeError, match="without an ownership receipt"):
        service.start_existing(
            requested.id,
            create_external,
            lambda _reference: None,
            lambda reference, handle: closed.append((reference, handle)),
            removed.append,
        )

    failed = service.store.get(requested.id)
    assert failed.status == "failed"
    assert failed.adapter_reference is None
    assert closed == []
    assert removed == []
    assert "id foreign" in (failed.error or "")


def test_explicit_cleanup_reconciles_a_failed_start_once(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "task", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.transition(workflow.id, "failed", error="activation failed")
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []

    reconciled = service.reconcile_stale(60, lambda *_args: None, removed.append)

    assert [record.id for record in reconciled] == [workflow.id]
    assert removed == ["owned-id"]
    assert service.store.get(workflow.id).status == "failed"
    assert service.store.stale_reconcilable(60) == []


def test_stale_cleanup_attempts_all_terminals_before_removing_worktree(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "multi-terminal", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="agent",
    )
    service.store.add_owned_terminal(workflow.id, "observer", "observer")
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    attempted: list[str] = []
    removed: list[str] = []

    def close(_reference: str, handle: str | None) -> None:
        attempted.append(str(handle))
        if handle == "agent":
            raise RuntimeError("terminal unavailable")

    service.reconcile_stale(60, close, removed.append)
    assert attempted == ["agent", "observer"]
    assert removed == ["owned-id"]
    assert service.store.get(workflow.id).external_reconciled_at is not None


def test_stale_cleanup_failure_preserves_running_state_and_lease(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    workflow = service.store.create(tmp_path, "single", "stale", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="agent",
    )
    service.store.transition(workflow.id, "running")
    lease = queue.acquire("exclusive", workflow.id)
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )

    result = service.reconcile_stale(
        60,
        lambda *_args: None,
        lambda _reference: (_ for _ in ()).throw(RuntimeError("Orca unavailable")),
    )

    assert result.reconciled == []
    assert any("worktree removal failed" in error for error in result.errors)
    assert service.store.get(workflow.id).status == "running"
    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_stale_manager_cleanup_cascades_successors_and_queue_requests(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    manager, worker, reviewer = service.store.create_orchestrated_plan(
        tmp_path, "task", "Implement."
    )
    service.store.transition(manager.id, "starting")
    service.store.attach_external(
        manager.id,
        adapter_reference="manager-id",
        worktree_path="/tmp/manager",
        terminal_handle="agent",
    )
    service.store.transition(manager.id, "running")
    requests = [
        queue.acquire(resource, owner)
        for resource, owner in (
            ("manager", manager.id),
            ("worker", worker.id),
            ("reviewer", reviewer.id),
        )
    ]
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", manager.id),
        )

    service.reconcile_stale(60, lambda *_args: None, lambda _reference: None)

    assert [service.store.get(record.id).status for record in (manager, worker, reviewer)] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]
    assert [queue.inspect(request.request_id)["status"] for request in requests] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]


def test_requested_manager_cancellation_cascades_and_releases_queue(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    manager, worker, reviewer = service.store.create_orchestrated_plan(
        tmp_path, "task", "Implement."
    )
    lease = queue.acquire("exclusive", manager.id)

    cancelled = service.finish(manager.id, "cancelled", lambda _reference: None)

    assert cancelled.status == "cancelled"
    assert service.store.get(worker.id).status == "cancelled"
    assert service.store.get(reviewer.id).status == "cancelled"
    assert queue.inspect(lease.request_id)["status"] == "cancelled"


def test_completed_workflow_releases_its_queue_requests(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    workflow = service.store.create(tmp_path, "single", "task", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="terminal",
    )
    service.store.transition(workflow.id, "running")
    lease = queue.acquire("exclusive", workflow.id)

    service.finish(workflow.id, "completed", lambda _reference: None)

    assert queue.inspect(lease.request_id)["status"] == "cancelled"


def test_finish_close_failure_is_terminal_and_durably_recoverable(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    workflow = service.store.create(tmp_path, "single", "task", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="terminal",
    )
    service.store.transition(workflow.id, "running")
    lease = queue.acquire("exclusive", workflow.id)
    lifecycle: list[str] = []

    with pytest.raises(RuntimeError, match="owned terminal cleanup failed"):
        service.finish(
            workflow.id,
            "completed",
            lifecycle.append,
            close_external=lambda *_args: (_ for _ in ()).throw(RuntimeError("close failed")),
        )

    finished = service.store.get(workflow.id)
    assert finished.status == "completed"
    assert "close failed" in (finished.cleanup_error or "")
    assert lifecycle == ["owned-id"]
    assert queue.inspect(lease.request_id)["status"] == "cancelled"
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    closed: list[str] = []
    removed: list[str] = []
    assert (
        service.reconcile_stale(
            60, lambda _reference, handle: closed.append(str(handle)), removed.append
        )[0].status
        == "completed"
    )
    assert closed == ["terminal"]
    assert removed == []
    assert service.store.get(workflow.id).cleanup_error is None


def test_concurrent_start_cancellation_compensates_the_returned_worktree(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    manager, worker, reviewer = service.store.create_orchestrated_plan(
        tmp_path, "task", "Implement."
    )
    closed: list[str] = []
    removed: list[str] = []

    def create_external():
        service.finish(manager.id, "cancelled", lambda _reference: None)
        return SimpleNamespace(
            worktree_id="orphan-id", worktree="/tmp/orphan", terminal="orphan-terminal"
        )

    with pytest.raises(ValueError, match="only be attached while starting"):
        service.start_existing(
            manager.id,
            create_external,
            lambda _reference: None,
            lambda _reference, handle: closed.append(str(handle)),
            removed.append,
        )

    assert service.store.get(manager.id).status == "cancelled"
    assert service.store.get(worker.id).status == "cancelled"
    assert service.store.get(reviewer.id).status == "cancelled"
    assert closed == ["orphan-terminal"]
    assert removed == ["orphan-id"]


def test_activation_and_cancellation_are_serialized_without_reactivation(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "race", "Implement.")
    activation_entered = threading.Event()
    release_activation = threading.Event()
    calls: list[str] = []
    errors: list[Exception] = []

    def activate(_reference: str) -> None:
        calls.append("activate")
        activation_entered.set()
        assert release_activation.wait(5)

    def start() -> None:
        try:
            service.start_existing(
                workflow.id,
                lambda: SimpleNamespace(
                    worktree_id="owned-id", worktree="/tmp/owned", terminal="terminal"
                ),
                activate,
                lambda *_args: None,
            )
        except (AssertionError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    def cancel() -> None:
        try:
            activation_entered.wait(5)
            service.finish(workflow.id, "cancelled", lambda _reference: calls.append("cancel"))
        except (AssertionError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    start_thread = threading.Thread(target=start)
    cancel_thread = threading.Thread(target=cancel)
    start_thread.start()
    cancel_thread.start()
    assert activation_entered.wait(5)
    release_activation.set()
    start_thread.join(5)
    cancel_thread.join(5)

    assert errors == []
    assert calls == ["activate", "cancel"]
    assert service.store.get(workflow.id).status == "cancelled"


def test_starting_cancel_remains_reconcilable_if_external_update_fails(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    workflow = service.store.create(tmp_path, "single", "task", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="terminal",
    )
    request = queue.acquire("exclusive", workflow.id)

    with pytest.raises(RuntimeError, match="unavailable"):
        service.finish(
            workflow.id,
            "cancelled",
            lambda _reference: (_ for _ in ()).throw(RuntimeError("Orca unavailable")),
        )

    assert service.store.get(workflow.id).status == "cancelled"
    assert queue.inspect(request.request_id)["status"] == "cancelled"
    assert [record.id for record in service.store.unreconciled_terminal()] == [workflow.id]
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []
    reconciled = service.reconcile_stale(60, lambda *_args: None, removed.append)
    assert [record.status for record in reconciled] == ["cancelled"]
    assert removed == ["owned-id"]


def test_failed_role_can_retry_only_after_external_reconciliation(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "task", "Implement.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="terminal",
    )
    store.transition(workflow.id, "failed", error="failed")

    with pytest.raises(ValueError, match="externally reconciled"):
        store.retry_failed(workflow.id)

    store.mark_external_reconciled(workflow.id)
    retried = store.retry_failed(workflow.id)

    assert retried.status == "requested"
    assert retried.adapter_reference is None
    assert retried.error is None
    assert store.owned_terminal_handles(workflow.id) == []


def test_retry_cancels_queue_ownership_left_by_a_failed_workflow(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(store)
    service.queue = queue
    workflow = store.create(tmp_path, "single", "retry-lease", "Implement.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "failed", error="failed")
    request = queue.acquire("exclusive", workflow.id)

    retried = service.retry_failed(workflow.id)

    assert retried.status == "requested"
    assert queue.inspect(request.request_id)["status"] == "cancelled"


def test_retry_reopens_cancelled_role_plan_successors(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    manager, worker, reviewer = service.store.create_orchestrated_plan(
        tmp_path, "task", "Implement."
    )
    service.store.transition(manager.id, "starting")
    service.store.transition(manager.id, "failed", error="failed")
    service.store.cancel_requested_successors(manager.id)

    service.store.retry_failed(manager.id)

    assert service.store.get(worker.id).status == "requested"
    assert service.store.get(reviewer.id).status == "requested"


def test_service_initialization_reconciles_terminal_workflow_queue_gap(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    workflow = store.create(tmp_path, "single", "terminal", "Implement.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "failed", error="failed")
    request = queue.acquire("exclusive", workflow.id)

    WorkflowService(store, queue)
    WorkflowService(store, queue)

    assert queue.inspect(request.request_id)["status"] == "cancelled"


def test_service_initialization_cancels_unknown_queue_owners(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    request = queue.acquire("exclusive", "external-owner")

    WorkflowService(store, queue)

    assert queue.inspect(request.request_id)["status"] == "cancelled"


def test_workflow_names_are_unique_to_prevent_double_start(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    workflows.create(tmp_path, "single", "same-name", "Implement.")

    with pytest.raises(ValueError, match="workflow name already exists"):
        workflows.create(tmp_path, "single", "same-name", "Start twice.")


def test_cancelled_workflow_names_can_be_reused(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    previous = workflows.create(tmp_path, "single", "same-name", "Implement.")
    workflows.transition(previous.id, "cancelled")

    reused = workflows.create(tmp_path, "single", "same-name", "Start again.")

    assert reused.name == "same-name"
    assert reused.id != previous.id
    with pytest.raises(ValueError, match="workflow name already exists"):
        workflows.create(tmp_path, "single", "same-name", "Still active.")


def test_owned_terminal_handles_are_durable_and_deduplicated(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    workflow = workflows.create(tmp_path, "single", "example", "Implement.")
    workflows.transition(workflow.id, "starting")
    workflows.attach_external(
        workflow.id,
        adapter_reference="worktree-id",
        worktree_path="/tmp/worktree",
        terminal_handle="agent-terminal",
    )
    workflows.add_owned_terminal(workflow.id, "observer-terminal", "observer")
    workflows.add_owned_terminal(workflow.id, "observer-terminal", "observer")

    assert WorkflowStore(tmp_path).owned_terminal_handles(workflow.id) == [
        "agent-terminal",
        "observer-terminal",
    ]


def test_observer_start_failure_marks_workflow_failed_and_closes_owned_terminal(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "observer-failure", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="worktree-id",
        worktree_path="/tmp/worktree",
        terminal_handle="agent-terminal",
    )
    service.store.transition(workflow.id, "running")
    closed: list[str] = []

    class Runtime:
        def create_observer(self, _worktree_id: str, _command: str) -> str:
            raise RuntimeError("terminal unavailable")

        def set_lifecycle(self, *_args, **_kwargs) -> None:
            pass

        def close_terminals(self, _worktree_id: str, handle: str) -> None:
            closed.append(handle)

    with pytest.raises(RuntimeError, match="queue observer startup failed"):
        service.attach_observer(workflow.id, Runtime(), "flybridge queue watch")

    assert closed == ["agent-terminal"]
    assert service.store.get(workflow.id).status == "failed"


def test_observer_persistence_failure_closes_the_unrecorded_handle(
    tmp_path: Path, monkeypatch
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "observer-persistence", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="worktree-id",
        worktree_path="/tmp/worktree",
        terminal_handle="agent-terminal",
    )
    service.store.transition(workflow.id, "running")
    closed: list[str] = []

    class Runtime:
        def create_observer(self, _worktree_id: str, _command: str) -> str:
            return "observer-terminal"

        def set_lifecycle(self, *_args, **_kwargs) -> None:
            pass

        def close_terminals(self, _worktree_id: str, handle: str) -> None:
            closed.append(handle)

    monkeypatch.setattr(
        service.store,
        "add_owned_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    with pytest.raises(RuntimeError, match="queue observer startup failed"):
        service.attach_observer(workflow.id, Runtime(), "flybridge queue watch")

    assert closed == ["agent-terminal", "observer-terminal"]


def test_observer_does_not_attach_after_a_concurrent_terminal_transition(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = _running_workflow(service.store, tmp_path)
    closed: list[str] = []

    class Runtime:
        def create_observer(self, _worktree_id: str, _command: str) -> str:
            service.store.claim_terminal_transition(workflow.id, "completed")
            service.store.complete_terminal_transition(workflow.id, "completed")
            return "late-observer"

        def close_terminals(self, _worktree_id: str, handle: str) -> None:
            closed.append(handle)

    with pytest.raises(RuntimeError, match="ownership changed during startup"):
        service.attach_observer(workflow.id, Runtime(), "flybridge queue watch")

    assert service.store.get(workflow.id).status == "completed"
    assert closed == ["late-observer"]
    assert service.store.owned_terminal_handles(workflow.id) == ["agent-old"]


def test_observer_failure_cannot_override_an_existing_terminal_outcome(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = _running_workflow(service.store, tmp_path)
    service.store.claim_terminal_transition(workflow.id, "completed")

    class Runtime:
        def create_observer(self, _worktree_id: str, _command: str) -> str:
            raise RuntimeError("observer unavailable")

        def set_lifecycle(self, *_args, **_kwargs) -> None:
            raise AssertionError("a conflicting lifecycle outcome must not be sent")

        def close_terminals(self, *_args) -> None:
            raise AssertionError("the running agent remains owned by the claimed outcome")

    with pytest.raises(RuntimeError, match="another terminal outcome"):
        service.attach_observer(workflow.id, Runtime(), "flybridge queue watch")

    assert service.store.get(workflow.id).status == "running"
    completed = service.finish(workflow.id, "completed", lambda _reference: None)
    assert completed.status == "completed"


def test_worktree_reference_is_unique_and_invalid_metadata_is_rejected(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    first = workflows.create(tmp_path, "single", "first", "Implement.")
    second = workflows.create(tmp_path, "single", "second", "Implement.")
    for workflow in (first, second):
        workflows.transition(workflow.id, "starting")

    workflows.attach_external(
        first.id,
        adapter_reference="same-worktree",
        worktree_path="/tmp/first",
        terminal_handle="first-terminal",
    )
    with pytest.raises(ValueError, match="already owned"):
        workflows.attach_external(
            second.id,
            adapter_reference="same-worktree",
            worktree_path="/tmp/second",
            terminal_handle="second-terminal",
        )


def test_historical_worktree_reference_can_be_owned_by_a_new_active_workflow(
    tmp_path: Path,
) -> None:
    workflows = WorkflowStore(tmp_path)
    historical = workflows.create(tmp_path, "single", "historical", "Implement.")
    workflows.transition(historical.id, "starting")
    workflows.attach_external(
        historical.id,
        adapter_reference="reused-worktree",
        worktree_path="/tmp/historical",
        terminal_handle="historical-terminal",
    )
    workflows.transition(historical.id, "running")
    workflows.transition(historical.id, "completed")
    workflows.mark_external_reconciled(historical.id)
    current = workflows.create(tmp_path, "single", "current", "Implement.")
    workflows.transition(current.id, "starting")

    attached = workflows.attach_external(
        current.id,
        adapter_reference="reused-worktree",
        worktree_path="/tmp/current",
        terminal_handle="current-terminal",
    )

    assert attached.adapter_reference == "reused-worktree"
    assert workflows.find_by_adapter_reference("reused-worktree").id == current.id


def test_historical_reference_conflict_closes_only_the_unowned_contender_terminal(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    owner = service.store.create(tmp_path, "single", "owner", "Implement.")
    service.store.transition(owner.id, "starting")
    service.store.attach_external(
        owner.id,
        adapter_reference="owned-worktree",
        worktree_path="/tmp/owner",
        terminal_handle="owner-terminal",
    )
    service.store.transition(owner.id, "running")
    service.store.transition(owner.id, "completed")
    contender = service.store.create(tmp_path, "single", "contender", "Implement differently.")
    closed: list[tuple[str, str | None]] = []
    removed: list[str] = []

    with pytest.raises(ValueError, match=f"already owned by workflow {owner.id}"):
        service.start_existing(
            contender.id,
            lambda: SimpleNamespace(
                worktree_id="owned-worktree",
                worktree="/tmp/contender",
                terminal="contender-terminal",
            ),
            lambda _reference: None,
            lambda reference, handle: closed.append((reference, handle)),
            lambda reference: removed.append(reference),
        )

    assert closed == [("owned-worktree", "contender-terminal")]
    assert removed == []
    assert service.store.get(owner.id).status == "completed"
    assert service.store.get(contender.id).status == "failed"


def test_reused_unreconciled_name_is_rejected_before_external_start(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    owner = service.store.create(tmp_path, "single", "reused-name", "First objective.")
    service.store.transition(owner.id, "starting")
    service.store.attach_external(
        owner.id,
        adapter_reference="owned-worktree",
        worktree_path="/tmp/owner",
        terminal_handle="owner-terminal",
    )
    service.store.transition(owner.id, "running")
    service.store.transition(owner.id, "cancelled")
    contender = service.store.create(tmp_path, "single", "reused-name", "Second objective.")

    class Runtime:
        def start(self, *_args, **_kwargs):
            raise AssertionError("external start must not run before durable ownership is clear")

        def close_terminals(self, *_args) -> None:
            raise AssertionError("no contender terminal was allocated")

        def remove_worktree(self, *_args) -> None:
            raise AssertionError("the prior owner's worktree must not be removed")

    with pytest.raises(
        ValueError,
        match=rf"still owned by workflow {owner.id}.*workflow cleanup --apply",
    ):
        service.launch_existing(
            contender.id,
            Runtime(),
            agent="cursor",
            response_language="English",
            skill_paths=(),
        )

    assert service.store.get(owner.id).external_reconciled_at is None
    assert service.store.get(contender.id).status == "failed"


def test_handoff_duplicate_is_reported_with_a_domain_error(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager-id")
    store.transition(manager.id, "completed")
    store.record_handoff(manager.id, worker.id, "First handoff.")

    with pytest.raises(ValueError, match="source already has"):
        store.record_handoff(manager.id, worker.id, "Duplicate handoff.")


def test_concurrent_handoff_is_recorded_once_transactionally(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager-id")
    store.transition(manager.id, "completed")
    barrier = threading.Barrier(2, timeout=5)
    successes = []
    errors: list[Exception] = []

    def record(summary: str) -> None:
        try:
            barrier.wait()
            successes.append(store.record_handoff(manager.id, worker.id, summary))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=record, args=("First concurrent handoff.",)),
        threading.Thread(target=record, args=("Second concurrent handoff.",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    persisted = store.handoff_for(worker.id)
    assert persisted == successes[0]
    assert persisted.summary in {"First concurrent handoff.", "Second concurrent handoff."}
    assert store.get(manager.id).status == "completed"
    assert store.get(worker.id).status == "requested"


def test_handoff_can_be_recorded_while_the_source_role_is_running(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager-id")

    handoff = store.record_handoff(manager.id, worker.id, "Implementation handoff.")

    assert handoff.summary == "Implementation handoff."
    assert store.get(manager.id).status == "running"


def test_orchestrated_predecessor_cannot_complete_before_successor_handoff(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    store.transition(manager.id, "starting")
    store.attach_external(
        manager.id,
        adapter_reference="manager-id",
        worktree_path="/tmp/manager",
        terminal_handle="manager-terminal",
    )
    store.transition(manager.id, "running")

    with pytest.raises(ValueError, match="successor handoff is recorded"):
        store.claim_terminal_transition(manager.id, "completed")

    store.record_handoff(manager.id, worker.id, "Implementation handoff.")
    claim = store.claim_terminal_transition(manager.id, "completed")
    completed = store.complete_terminal_transition(manager.id, "completed")

    assert claim.adapter_reference == "manager-id"
    assert completed.status == "completed"

    store.transition(worker.id, "starting")
    store.attach_external(
        worker.id,
        adapter_reference="worker-id",
        worktree_path="/tmp/worker",
        terminal_handle="worker-terminal",
    )
    store.transition(worker.id, "running")
    with pytest.raises(ValueError, match="successor handoff is recorded"):
        store.claim_terminal_transition(worker.id, "completed")
    store.record_handoff(worker.id, reviewer.id, "Review handoff.")
    store.claim_terminal_transition(worker.id, "completed")
    assert store.complete_terminal_transition(worker.id, "completed").status == "completed"


def test_orchestrated_predecessor_can_fail_without_a_handoff(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, _worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    store.transition(manager.id, "starting")
    store.attach_external(
        manager.id,
        adapter_reference="manager-id",
        worktree_path="/tmp/manager",
        terminal_handle="manager-terminal",
    )
    store.transition(manager.id, "running")

    store.claim_terminal_transition(manager.id, "failed", error="plan blocked")
    failed = store.complete_terminal_transition(manager.id, "failed", error="plan blocked")

    assert failed.status == "failed"


def test_handoff_rejects_a_requested_source(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")

    with pytest.raises(ValueError, match="running or completed source"):
        store.record_handoff(manager.id, worker.id, "Too early.")


def _running_workflow(
    store: WorkflowStore,
    tmp_path: Path,
    objective: str = "Implement.",
    *,
    issue_url: str | None = None,
):
    workflow = store.create(tmp_path, "single", f"task-{objective}", objective, issue_url=issue_url)
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/existing",
        worktree_path="/tmp/existing",
        terminal_handle="agent-old",
    )
    return store.transition(workflow.id, "running")


def _running_worker(store: WorkflowStore, tmp_path: Path):
    manager, worker, _reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    store.transition(manager.id, "starting")
    store.attach_external(
        manager.id,
        adapter_reference="manager-id",
        worktree_path="/tmp/manager",
        terminal_handle="manager-terminal",
    )
    store.transition(manager.id, "running")
    WorkflowArtifactStore(store.path.parent, store).put(
        manager.id, "plan", "# Plan\n\nImplement it."
    )
    store.record_handoff(manager.id, worker.id, "Use the planned implementation path.")
    store.claim_terminal_transition(manager.id, "completed")
    store.complete_terminal_transition(manager.id, "completed")
    store.transition(worker.id, "starting")
    store.attach_external(
        worker.id,
        adapter_reference="worker-id",
        worktree_path="/tmp/worker",
        terminal_handle="worker-terminal",
    )
    return store.transition(worker.id, "running")


def test_resume_after_store_reopen_reuses_a_valid_agent_terminal(tmp_path: Path) -> None:
    issue_url = "https://github.com/example/repo/issues/42"
    workflow = _running_workflow(WorkflowStore(tmp_path / "state"), tmp_path, issue_url=issue_url)
    calls: list[tuple[str, str]] = []

    class Runtime:
        def verify_worktree(self, worktree_id: str, worktree_path: str) -> None:
            calls.append((worktree_id, worktree_path))

        def terminal_is_valid(self, worktree_id: str, terminal_handle: str | None) -> bool:
            calls.append((worktree_id, str(terminal_handle)))
            return True

        def wait_for_agent(self, terminal_handle: str) -> None:
            calls.append(("wait", terminal_handle))

        def send_prompt(self, terminal_handle: str, prompt: str) -> None:
            calls.append((terminal_handle, prompt))

    reopened = WorkflowService(WorkflowStore(tmp_path / "state"))
    resumed = reopened.resume(
        workflow.id,
        Runtime(),
        agent="codex",
        response_language="Japanese",
        skill_paths=(),
        resource_names=("device",),
    )

    assert resumed.terminal_handle == "agent-old"
    assert calls[0] == ("repo::/tmp/existing", "/tmp/existing")
    assert calls[1] == ("repo::/tmp/existing", "agent-old")
    assert calls[2] == ("wait", "agent-old")
    assert calls[3][0] == "agent-old"
    assert "Continue this objective: Implement." in calls[3][1]
    assert f"Primary issue: {issue_url}" in calls[3][1]
    assert "single role" in calls[3][1]
    assert "Respond in Japanese." in calls[3][1]
    assert "`device`" not in calls[3][1]
    assert "device" in calls[3][1]
    assert "queue" in calls[3][1]
    assert "Flybridge MCP" not in calls[3][1]


def test_resume_replaces_stale_agent_and_recreates_its_observer(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)
    store.add_owned_terminal(workflow.id, "observer", "observer")
    calls: list[tuple[str, str]] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            return False

        def create_new_agent_terminal(
            self,
            worktree_id: str,
            worktree_path: str,
            agent: str,
            prompt: str,
            **_kwargs,
        ) -> str:
            calls.append((worktree_id, worktree_path, agent, prompt))
            return "agent-new"

        def close_terminals(self, _worktree_id: str, terminal_handle: str) -> None:
            if terminal_handle != "observer":
                raise AssertionError("successful replacement must remain open")
            calls.append(("close", terminal_handle))

        def create_observer(self, _worktree_id: str, command: str) -> str:
            calls.append(("observer", command))
            return "observer-new"

    resumed = WorkflowService(store).resume(
        workflow.id,
        Runtime(),
        agent="codex",
        response_language="English",
        skill_paths=(),
        observer_command="flybridge queue watch --notify-workflow workflow",
    )

    assert resumed.terminal_handle == "agent-new"
    assert WorkflowStore(tmp_path / "state").owned_terminal_handles(workflow.id) == [
        "agent-new",
        "observer-new",
    ]
    assert calls[0][:3] == (
        "repo::/tmp/existing",
        "/tmp/existing",
        "codex",
    )
    assert "Continue this objective: Implement." in calls[0][3]
    assert calls[1] == ("close", "observer")
    assert calls[2] == ("observer", "flybridge queue watch --notify-workflow workflow")


def test_resume_enables_observer_when_requested(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)
    created: list[str] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            return True

        def wait_for_agent(self, _terminal_handle: str) -> None:
            pass

        def send_prompt(self, _terminal_handle: str, _prompt: str) -> None:
            pass

        def create_observer(self, _worktree_id: str, command: str) -> str:
            created.append(command)
            return "observer-new"

    resumed = WorkflowService(store).resume(
        workflow.id,
        Runtime(),
        agent="codex",
        response_language="English",
        skill_paths=(),
        observer_command="flybridge queue watch --notify-workflow workflow",
        observer_enabled=True,
    )

    assert resumed.queue_observer_enabled is True
    assert created == ["flybridge queue watch --notify-workflow workflow"]
    assert store.owned_terminal_handles(workflow.id, kind="observer") == ["observer-new"]


def test_resume_restores_the_predecessor_handoff(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    worker = _running_worker(store, tmp_path)
    prompts: list[str] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            return True

        def wait_for_agent(self, _terminal_handle: str) -> None:
            pass

        def send_prompt(self, _terminal_handle: str, prompt: str) -> None:
            prompts.append(prompt)

    WorkflowService(store).resume(
        worker.id,
        Runtime(),
        agent="codex",
        response_language="English",
        skill_paths=(),
    )

    assert "Previous-role handoff: Use the planned implementation path." in prompts[0]
    assert "Manager plan artifact:" in prompts[0]
    assert "Implement it." in prompts[0]


def test_reviewer_resume_reads_only_sources_registered_for_its_run(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    manager, worker, reviewer = store.create_orchestrated_plan(
        tmp_path, "review-sources", "Review."
    )
    store.transition(manager.id, "starting")
    store.transition(manager.id, "running", adapter_reference="manager")
    store.transition(manager.id, "completed")
    store.transition(worker.id, "starting")
    store.attach_external(
        worker.id,
        adapter_reference="worker",
        worktree_path="/tmp/worker",
        terminal_handle="worker-terminal",
    )
    store.transition(worker.id, "running")
    store.record_handoff(worker.id, reviewer.id, "Review the committed implementation.")
    store.transition(worker.id, "completed")
    artifacts = WorkflowArtifactStore(tmp_path / "state", store)
    artifacts.put(manager.id, "plan", "Review the registered scope.")
    artifacts.put(worker.id, "verification", "Implementation verified.")
    store.transition(reviewer.id, "starting")
    store.attach_external(
        reviewer.id,
        adapter_reference="reviewer",
        worktree_path="/tmp/reviewer",
        terminal_handle="reviewer-terminal",
    )
    store.transition(reviewer.id, "running")
    refs = ReconcileStore(tmp_path / "state")
    refs.link_reference(manager.run_id, "https://github.com/example/repo/issues/7", "primary")
    refs.link_reference(manager.run_id, "https://github.com/example/repo/pull/12", "related")
    prompts: list[str] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            return True

        def wait_for_agent(self, _terminal_handle: str) -> None:
            pass

        def send_prompt(self, _terminal_handle: str, prompt: str) -> None:
            prompts.append(prompt)

    WorkflowService(store).resume(
        reviewer.id,
        Runtime(),
        agent="cursor",
        response_language="English",
        skill_paths=(),
    )

    assert "Registered review sources:" in prompts[0]
    assert "https://github.com/example/repo/issues/7" in prompts[0]
    assert "https://github.com/example/repo/pull/12" in prompts[0]


def test_new_agent_replaces_the_persisted_handle_without_reusing_a_session(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)
    calls: list[tuple[str, ...]] = []

    class Runtime:
        def verify_worktree(self, worktree_id: str, worktree_path: str) -> None:
            calls.append(("verify", worktree_id, worktree_path))

        def create_new_agent_terminal(
            self, worktree_id: str, worktree_path: str, agent: str, prompt: str, **_kwargs
        ) -> str:
            calls.append(("fresh", worktree_id, worktree_path, agent, prompt))
            return "agent-fresh"

        def close_terminals(self, *_args) -> None:
            raise AssertionError("successful fresh start must remain open")

    restarted = WorkflowService(store).restart_with_new_agent(
        workflow.id, Runtime(), agent="codex", response_language="English", skill_paths=()
    )

    assert restarted.terminal_handle == "agent-fresh"
    assert store.owned_terminal_handles(workflow.id) == ["agent-fresh"]
    assert calls[0] == ("verify", "repo::/tmp/existing", "/tmp/existing")
    assert calls[1][:4] == ("fresh", "repo::/tmp/existing", "/tmp/existing", "codex")
    assert "You are the single role in a single Flybridge workflow." in calls[1][4]
    assert "Continue this objective:" not in calls[1][4]
    assert "Implement." in calls[1][4]


def test_new_agent_restart_restores_the_predecessor_handoff(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    worker = _running_worker(store, tmp_path)
    prompts: list[str] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def create_new_agent_terminal(
            self, _worktree_id: str, _worktree_path: str, _agent: str, prompt: str, **_kwargs
        ) -> str:
            prompts.append(prompt)
            return "agent-fresh"

        def close_terminals(self, *_args) -> None:
            raise AssertionError("successful fresh start must remain open")

    WorkflowService(store).restart_with_new_agent(
        worker.id,
        Runtime(),
        agent="codex",
        response_language="English",
        skill_paths=(),
    )

    assert "Previous-role handoff:" in prompts[0]
    assert "Use the planned implementation path." in prompts[0]
    assert "Manager plan artifact:" in prompts[0]


def test_resume_keeps_the_recorded_agent_when_replacement_creation_fails(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)
    closed: list[str] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            return False

        def create_new_agent_terminal(
            self, _worktree_id: str, _worktree_path: str, _agent: str, _prompt: str, **_kwargs
        ) -> str:
            raise RuntimeError("terminal is not writable")

        def create_agent_terminal(self, _worktree_id: str, _agent: str, **_kwargs) -> str:
            return "agent-replacement"

    with pytest.raises(RuntimeError, match="not writable"):
        WorkflowService(store).resume(
            workflow.id,
            Runtime(),
            agent="codex",
            response_language="English",
            skill_paths=(),
        )

    assert store.get(workflow.id).terminal_handle == "agent-old"
    assert store.owned_terminal_handles(workflow.id) == ["agent-old"]
    assert closed == []


def test_resume_rejects_non_running_workflow_before_runtime_calls(tmp_path: Path) -> None:
    workflow = WorkflowStore(tmp_path).create(tmp_path, "single", "requested", "Implement.")

    with pytest.raises(ValueError, match="only a running workflow"):
        WorkflowService(WorkflowStore(tmp_path)).resume(
            workflow.id,
            object(),
            agent="codex",
            response_language="English",
            skill_paths=(),
        )


def test_duplicate_active_root_is_prevented_but_override_and_other_objective_start(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    first = _running_workflow(service.store, tmp_path, "Same objective.")
    created: list[str] = []

    def create_external():
        created.append("created")
        index = len(created)
        return SimpleNamespace(
            worktree_id=f"repo::/tmp/{index}",
            worktree=f"/tmp/{index}",
            terminal=f"agent-{index}",
        )

    duplicate = service.store.create(tmp_path, "single", "duplicate", first.objective)
    with pytest.raises(ValueError, match="--allow-duplicate"):
        service.start_existing(
            duplicate.id, create_external, lambda _reference: None, lambda *_args: None
        )
    assert created == []

    allowed = service.store.create(tmp_path, "single", "allowed", first.objective)
    assert (
        service.start_existing(
            allowed.id,
            create_external,
            lambda _reference: None,
            lambda *_args: None,
            allow_duplicate=True,
        ).status
        == "running"
    )
    different = service.store.create(tmp_path, "single", "different", "Different objective.")
    assert (
        service.start_existing(
            different.id, create_external, lambda _reference: None, lambda *_args: None
        ).status
        == "running"
    )


def test_duplicate_start_claims_create_exactly_one_external_worktree(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    workflow = service.store.create(tmp_path, "single", "claim", "Implement.")
    barrier = threading.Barrier(6, timeout=5)
    created: list[str] = []
    successes = []
    errors: list[Exception] = []

    def start() -> None:
        try:
            barrier.wait()
            successes.append(
                service.start_existing(
                    workflow.id,
                    lambda: (
                        created.append("created")
                        or SimpleNamespace(
                            worktree_id="claimed-id",
                            worktree="/tmp/claimed",
                            terminal="claimed-terminal",
                        )
                    ),
                    lambda _reference: None,
                    lambda *_args: None,
                )
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert successes[0].status == "running"
    assert len(errors) == 5
    assert all(isinstance(error, ValueError) for error in errors)
    assert created == ["created"]


def test_concurrent_retry_and_start_has_one_consistent_outcome(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    workflow = service.store.create(tmp_path, "single", "retry-race", "Implement.")
    service.store.begin_start(workflow.id)
    service.store.transition(workflow.id, "failed", error="injected failure")
    barrier = threading.Barrier(2, timeout=5)
    retry_results = []
    start_results = []
    start_errors: list[Exception] = []
    unexpected_errors: list[Exception] = []
    created: list[str] = []

    def retry() -> None:
        try:
            barrier.wait()
            retry_results.append(service.store.retry_failed(workflow.id))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            unexpected_errors.append(exc)

    def start() -> None:
        try:
            barrier.wait()
            start_results.append(
                service.start_existing(
                    workflow.id,
                    lambda: (
                        created.append("created")
                        or SimpleNamespace(
                            worktree_id="retry-id",
                            worktree="/tmp/retry",
                            terminal="retry-terminal",
                        )
                    ),
                    lambda _reference: None,
                    lambda *_args: None,
                )
            )
        except ValueError as exc:
            start_errors.append(exc)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            unexpected_errors.append(exc)

    threads = [threading.Thread(target=retry), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert unexpected_errors == []
    assert len(retry_results) == 1
    assert len(start_results) + len(start_errors) == 1
    final = service.store.get(workflow.id)
    if start_results:
        assert final.status == "running"
        assert final.adapter_reference == "retry-id"
        assert created == ["created"]
    else:
        assert final.status == "requested"
        assert created == []


def test_concurrent_resume_replaces_stale_terminal_once_and_closes_loser(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)
    validity_barrier = threading.Barrier(2, timeout=5)
    handle_lock = threading.Lock()
    replacements: list[str] = []
    closed: list[str] = []
    successes = []
    errors: list[Exception] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            validity_barrier.wait()
            return False

        def create_new_agent_terminal(
            self,
            _worktree_id: str,
            _worktree_path: str,
            _agent: str,
            _prompt: str,
            **_kwargs,
        ) -> str:
            with handle_lock:
                handle = f"replacement-{len(replacements)}"
                replacements.append(handle)
                return handle

        def close_terminals(self, _worktree_id: str, terminal_handle: str) -> None:
            closed.append(terminal_handle)

        def wait_for_agent(self, _terminal_handle: str) -> None:
            pass

        def send_prompt(self, _terminal_handle: str, _prompt: str) -> None:
            pass

    def resume() -> None:
        try:
            successes.append(
                WorkflowService(store).resume(
                    workflow.id,
                    Runtime(),
                    agent="codex",
                    response_language="English",
                    skill_paths=(),
                )
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=resume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert len(replacements) == 2
    persisted = store.get(workflow.id).terminal_handle
    assert persisted in replacements
    assert closed == [next(handle for handle in replacements if handle != persisted)]
    assert store.owned_terminal_handles(workflow.id) == [persisted]


def test_lifecycle_callbacks_never_run_under_a_sqlite_write_transaction(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    workflow = service.store.create(tmp_path, "single", "no-lock", "Implement.")

    def assert_database_is_writable(_reference: str) -> None:
        with service.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE workflows SET updated_at = updated_at WHERE id = ?", (workflow.id,)
            )

    running = service.start_existing(
        workflow.id,
        lambda: SimpleNamespace(
            worktree_id="no-lock-id", worktree="/tmp/no-lock", terminal="agent"
        ),
        assert_database_is_writable,
        lambda *_args: None,
    )
    completed = service.finish(running.id, "completed", assert_database_is_writable)

    assert completed.status == "completed"


def test_concurrent_terminal_commands_claim_exactly_one_external_outcome(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    workflow = _running_workflow(service.store, tmp_path)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    barrier = threading.Barrier(3, timeout=5)
    external_outcomes: list[str] = []
    successes = []
    errors: list[Exception] = []

    def finish(target: str) -> None:
        try:
            barrier.wait()

            def update(_reference: str) -> None:
                external_outcomes.append(target)
                callback_entered.set()
                assert release_callback.wait(5)

            successes.append(service.finish(workflow.id, target, update))
        except (AssertionError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=finish, args=(target,))
        for target in ("completed", "failed", "cancelled")
    ]
    for thread in threads:
        thread.start()
    assert callback_entered.wait(5)
    release_callback.set()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(errors) == 2
    assert external_outcomes == [successes[0].status.value]
    assert service.store.get(workflow.id).status == successes[0].status


def test_failed_terminal_update_keeps_one_recoverable_outcome_claim(
    tmp_path: Path,
) -> None:
    queue = ResourceQueue(tmp_path / "state")
    service = WorkflowService(WorkflowStore(tmp_path / "state"), queue)
    workflow = _running_workflow(service.store, tmp_path)
    lease = queue.acquire("exclusive", workflow.id)

    with pytest.raises(RuntimeError, match="Orca unavailable"):
        service.finish(
            workflow.id,
            "completed",
            lambda _reference: (_ for _ in ()).throw(RuntimeError("Orca unavailable")),
        )

    assert queue.inspect(lease.request_id)["status"] == "leased"
    contradictory_calls: list[str] = []
    with pytest.raises(ValueError, match="another terminal outcome"):
        service.finish(workflow.id, "failed", contradictory_calls.append)
    assert contradictory_calls == []
    with service.store._connect() as connection:
        operation = connection.execute(
            "SELECT target, error FROM workflow_lifecycle_operations WHERE workflow_id = ?",
            (workflow.id,),
        ).fetchone()
    assert dict(operation) == {"target": "completed", "error": "Orca unavailable"}

    retried = service.finish(workflow.id, "completed", lambda _reference: None)

    assert retried.status == "completed"
    assert queue.inspect(lease.request_id)["status"] == "cancelled"
    with service.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow.id,),
            ).fetchone()
            is None
        )


def test_terminal_outcome_is_idempotently_retryable_after_a_claiming_process_exits(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    workflow = _running_workflow(service.store, tmp_path)
    service.store.claim_terminal_transition(workflow.id, "completed")
    updates: list[str] = []

    completed = service.finish(workflow.id, "completed", updates.append)

    assert completed.status == "completed"
    assert updates == [workflow.adapter_reference]


def test_completed_transition_remains_recoverable_until_owned_terminals_close(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)

    claim = store.claim_terminal_transition(workflow.id, "completed")
    assert claim.adapter_reference == workflow.adapter_reference
    completed = store.complete_terminal_transition(workflow.id, "completed")

    assert completed.cleanup_error == "owned terminal cleanup pending"
    assert [record.id for record in store.unreconciled_terminal()] == [workflow.id]


def test_starting_cancel_reads_ownership_committed_while_it_waits(
    tmp_path: Path,
) -> None:
    service = WorkflowService(WorkflowStore(tmp_path / "state"))
    workflow = service.store.create(tmp_path, "single", "ownership-race", "Implement.")
    service.store.begin_start(workflow.id)
    started = threading.Event()
    updated: list[str] = []
    results = []
    errors: list[Exception] = []

    locker = service.store._connect()
    locker.execute("BEGIN IMMEDIATE")
    locker.execute(
        "UPDATE workflows SET adapter_reference = ?, worktree_path = ?, terminal_handle = ? "
        "WHERE id = ?",
        ("current-owner", "/tmp/current", "agent", workflow.id),
    )
    locker.execute(
        "INSERT INTO owned_terminals VALUES (?, ?, 'agent', ?)",
        (workflow.id, "agent", "2026-01-01T00:00:00+00:00"),
    )

    def cancel() -> None:
        try:
            started.set()
            results.append(service.finish(workflow.id, "cancelled", updated.append))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    thread = threading.Thread(target=cancel)
    thread.start()
    assert started.wait(5)
    locker.commit()
    locker.close()
    thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert results[0].status == "cancelled"
    assert updated == ["current-owner"]


def test_concurrent_root_plan_reservation_leaves_no_requested_orphan_plan(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "state")
    barrier = threading.Barrier(2, timeout=5)
    plans = []
    errors: list[Exception] = []

    def reserve(name: str) -> None:
        try:
            barrier.wait()
            plans.append(store.reserve_root_plan(tmp_path, "orchestrated", name, "Same objective."))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reserve, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(plans) == 1
    assert len(errors) == 1
    assert "--allow-duplicate" in str(errors[0])
    persisted = [plans[0][0], *store.children(plans[0][0].id)]
    assert len(persisted) == 3
    assert [record.status for record in persisted] == [
        "starting",
        "requested",
        "requested",
    ]
    assert {record.id for record in store.active()} == {record.id for record in persisted}


def test_attached_worktree_is_not_removed_on_failed_start_or_stale_cleanup(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "attached", "Fix the existing PR.")
    removed: list[str] = []
    closed: list[tuple[str, str | None]] = []

    running = service.start_existing(
        workflow.id,
        lambda: SimpleNamespace(
            worktree_id="repo::/tmp/existing",
            worktree="/tmp/existing",
            terminal="term-attach",
            owns_worktree=False,
        ),
        lambda _reference: None,
        lambda reference, handle: closed.append((reference, handle)),
        lambda reference: removed.append(reference),
    )

    assert running.owns_worktree is False
    assert removed == []

    failed = service.store.create(tmp_path, "single", "attached-fail", "Fix again.")
    with pytest.raises(RuntimeError, match="activation failed"):
        service.start_existing(
            failed.id,
            lambda: SimpleNamespace(
                worktree_id="repo::/tmp/other",
                worktree="/tmp/other",
                terminal="term-fail",
                owns_worktree=False,
            ),
            lambda _reference: (_ for _ in ()).throw(RuntimeError("activation failed")),
            lambda reference, handle: closed.append((reference, handle)),
            lambda reference: removed.append(reference),
        )
    assert "repo::/tmp/other" not in removed

    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", running.id),
        )
    reconciled = service.reconcile_stale(
        60,
        lambda reference, handle: closed.append((reference, handle)),
        removed.append,
    )

    assert [item.id for item in reconciled] == [running.id]
    assert removed == []
    assert service.store.get(running.id).status == "cancelled"


def test_attached_cancel_marks_the_adapter_reference_reconciled(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(WorkflowStore(tmp_path), queue)
    workflow = service.store.create(tmp_path, "single", "attached", "Fix.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/existing",
        worktree_path="/tmp/existing",
        terminal_handle="term",
        owns_worktree=False,
    )
    service.store.transition(workflow.id, "running")
    lease = queue.acquire("exclusive", workflow.id)

    finished = service.finish(workflow.id, "cancelled", lambda _reference: None)

    assert finished.status == "cancelled"
    assert finished.external_reconciled_at is not None
    assert queue.inspect(lease.request_id)["status"] == "cancelled"
    assert service.store.find_unreconciled_by_adapter_reference("repo::/tmp/existing") is None


def test_cleanup_continues_after_one_worktree_removal_failure(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    first = service.store.create(tmp_path, "single", "first", "Implement.")
    second = service.store.create(tmp_path, "single", "second", "Implement.")
    for workflow in (first, second):
        service.store.transition(workflow.id, "starting")
        service.store.attach_external(
            workflow.id,
            adapter_reference=f"id:{workflow.name}",
            worktree_path=f"/tmp/{workflow.name}",
            terminal_handle="term",
        )
        service.store.transition(workflow.id, "running")
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE workflows SET updated_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", workflow.id),
            )
    removed: list[str] = []

    def remove(reference: str) -> None:
        if reference == "id:first":
            raise RuntimeError("selector_not_found")
        if reference == "id:second":
            removed.append(reference)
            return
        raise RuntimeError(f"unexpected {reference}")

    result = service.reconcile_stale(60, lambda *_args: None, remove)

    assert {record.id for record in result.reconciled} == {first.id, second.id}
    assert result.errors == ()
    assert removed == ["id:second"]
    assert service.store.get(first.id).external_reconciled_at is not None
    assert service.store.get(second.id).external_reconciled_at is not None


def test_cleanup_records_a_missing_workflow_id_and_continues(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "kept", "Implement.")
    service.store.transition(workflow.id, "starting")
    service.store.attach_external(
        workflow.id,
        adapter_reference="owned-id",
        worktree_path="/tmp/owned",
        terminal_handle="term",
    )
    service.store.transition(workflow.id, "running")
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []

    result = service.reconcile_stale(
        60,
        lambda *_args: None,
        removed.append,
        workflow_ids=("missing-id", workflow.id),
    )

    assert [record.id for record in result.reconciled] == [workflow.id]
    assert any("missing-id" in error for error in result.errors)
    assert removed == ["owned-id"]


def test_running_objective_can_be_replaced(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "task", "Old objective.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "running")
    updated = store.update_objective(workflow.id, "New objective.")
    assert updated.objective == "New objective."
    listed = store.list_workflows(statuses=("running",))
    assert [item.id for item in listed] == [workflow.id]


def test_schema_migrates_activated_at_from_version_1(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "migrate", "Implement.")
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE workflows DROP COLUMN activated_at")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    reopened = WorkflowStore(tmp_path)
    with reopened._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workflows)")}
    assert "activated_at" in columns
    assert reopened.get(workflow.id).activated_at is None


def test_complete_activation_records_activated_at(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    workflow = store.create(tmp_path, "single", "timed", "Implement.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::timed",
        worktree_path="/tmp/timed",
        terminal_handle="term-timed",
        implementation_repository="example/repo",
        runtime_repository_id="repo",
        start_sha="abc",
    )
    store.claim_activation(workflow.id)
    running = store.complete_activation(workflow.id)
    assert running.activated_at is not None
    assert running.status.value == "running"


def test_ghost_name_ownership_is_reconciled_before_start(tmp_path: Path) -> None:
    from flybridge_application.workflows import WorkflowService
    from flybridge_core import AdapterReferenceConflict

    store = WorkflowStore(tmp_path)
    prior = store.create(tmp_path, "single", "same-name", "First.")
    store.begin_start(prior.id)
    store.attach_external(
        prior.id,
        adapter_reference="repo::ghost",
        worktree_path="/tmp/ghost",
        terminal_handle="term-ghost",
    )
    store.transition(prior.id, "running")
    store.transition(prior.id, "failed", error="gone")
    successor = store.create(tmp_path, "single", "same-name", "Second.")
    service = WorkflowService(store)

    class GhostRuntime:
        def verify_worktree(self, *_args) -> None:
            raise RuntimeError("selector_not_found")

        def terminal_is_valid(self, *_args) -> bool:
            return False

    service._reconcile_name_ghost(successor, GhostRuntime())
    assert store.get(prior.id).external_reconciled_at is not None

    live = store.create(tmp_path, "single", "live-name", "Live.")
    store.begin_start(live.id)
    store.attach_external(
        live.id,
        adapter_reference="repo::live",
        worktree_path="/tmp/live",
        terminal_handle="term-live",
    )
    store.transition(live.id, "running")
    store.transition(live.id, "failed", error="worktree still present")
    collision = store.create(tmp_path, "single", "live-name", "Collision.")

    class LiveRuntime:
        def verify_worktree(self, *_args) -> None:
            return None

        def terminal_is_valid(self, *_args) -> bool:
            return True

    with pytest.raises(AdapterReferenceConflict, match="still owned"):
        service._reconcile_name_ghost(collision, LiveRuntime())
