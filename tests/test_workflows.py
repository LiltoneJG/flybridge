import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flybridge_application import WorkflowService
from flybridge_core import ResourceQueue, WorkflowStore
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

    with pytest.raises(RuntimeError, match="unsupported Flybridge state schema"):
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


def test_schema_rejects_historical_state_versions(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    store.create(tmp_path, "single", "legacy", "Implement.")
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(RuntimeError, match="unsupported Flybridge state schema"):
        WorkflowStore(tmp_path)


def test_schema_rejects_a_foreign_database_without_workflow_tables(tmp_path: Path) -> None:
    database = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 9")

    with pytest.raises(RuntimeError, match="unsupported Flybridge state schema"):
        WorkflowStore(tmp_path)


def test_schema_rejects_a_current_version_database_without_any_table(tmp_path: Path) -> None:
    database = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    with pytest.raises(RuntimeError, match="table workflows is missing"):
        WorkflowStore(tmp_path)


@pytest.mark.parametrize(
    "statement, message",
    [
        ("DROP TABLE workflow_lifecycle_operations", "table workflow_lifecycle_operations"),
        ("ALTER TABLE workflows DROP COLUMN cleanup_error", "missing columns cleanup_error"),
        ("DROP INDEX workflow_manager_role", "missing indexes workflow_manager_role"),
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
    store.create(tmp_path, "single", "task-worker", "Existing.")

    with pytest.raises(ValueError, match="role-plan name"):
        store.create_orchestrated_plan(tmp_path, "task", "Implement.")

    assert [workflow.name for workflow in store.active()] == ["task-worker"]


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

    with pytest.raises(RuntimeError, match="lifecycle operation is in progress"):
        service.reconcile_stale(60, lambda *_args: None, removed.append)

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
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.reconcile_stale(60, lambda *_args: None, removed.append)

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

    with pytest.raises(RuntimeError, match="worktree removal failed"):
        service.reconcile_stale(
            60,
            lambda *_args: None,
            lambda _reference: (_ for _ in ()).throw(RuntimeError("Orca unavailable")),
        )

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

        def set_lifecycle(self, *_args) -> None:
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

        def set_lifecycle(self, *_args) -> None:
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

        def set_lifecycle(self, *_args) -> None:
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


def test_historical_reference_conflict_never_cleans_up_the_existing_worktree(
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

    assert closed == []
    assert removed == []
    assert service.store.get(owner.id).status == "completed"
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


def _running_workflow(store: WorkflowStore, tmp_path: Path, objective: str = "Implement."):
    workflow = store.create(tmp_path, "single", f"task-{objective}", objective)
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/existing",
        worktree_path="/tmp/existing",
        terminal_handle="agent-old",
    )
    return store.transition(workflow.id, "running")


def test_resume_after_store_reopen_reuses_a_valid_agent_terminal(tmp_path: Path) -> None:
    workflow = _running_workflow(WorkflowStore(tmp_path / "state"), tmp_path)
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
    assert "single role" in calls[3][1]
    assert "Respond in Japanese." in calls[3][1]
    assert "`device`" not in calls[3][1]
    assert "device" in calls[3][1]
    assert "queue" in calls[3][1]
    assert "Flybridge MCP" not in calls[3][1]


def test_resume_replaces_one_stale_agent_handle_without_removing_observer(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = _running_workflow(store, tmp_path)
    store.add_owned_terminal(workflow.id, "observer", "observer")
    calls: list[tuple[str, str]] = []

    class Runtime:
        def verify_worktree(self, _worktree_id: str, _worktree_path: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _terminal_handle: str | None) -> bool:
            return False

        def create_agent_terminal(self, worktree_id: str, agent: str) -> str:
            calls.append((worktree_id, agent))
            return "agent-new"

        def wait_for_agent(self, terminal_handle: str) -> None:
            calls.append(("wait", terminal_handle))

        def send_prompt(self, terminal_handle: str, prompt: str) -> None:
            calls.append((terminal_handle, prompt))

        def close_terminals(self, _worktree_id: str, _terminal_handle: str) -> None:
            raise AssertionError("successful replacement must remain open")

    resumed = WorkflowService(store).resume(
        workflow.id,
        Runtime(),
        agent="codex",
        response_language="English",
        skill_paths=(),
    )

    assert resumed.terminal_handle == "agent-new"
    assert WorkflowStore(tmp_path / "state").owned_terminal_handles(workflow.id) == [
        "observer",
        "agent-new",
    ]
    assert calls[0] == ("repo::/tmp/existing", "codex")
    assert calls[1] == ("wait", "agent-new")
    assert calls[2][0] == "agent-new"


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

        def create_agent_terminal(self, _worktree_id: str, _agent: str) -> str:
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
