"""The visible observer owns a terminal but never delivers a lease."""

import sqlite3
from pathlib import Path

from flybridge_application import QueueLeaseNotifier, WorkflowService
from flybridge_core import ResourceQueue, WorkflowStore


class FakeRuntime:
    def __init__(self, *, terminal_valid: bool = True) -> None:
        self.terminal_valid = terminal_valid

    def terminal_is_valid(self, _worktree_id: str, terminal_handle: str | None) -> bool:
        return bool(terminal_handle) and self.terminal_valid


def _running(tmp_path: Path, name: str, handle: str) -> tuple[WorkflowStore, str]:
    store = WorkflowStore(tmp_path)
    workflow = WorkflowService(store).store.create(tmp_path, "single", name, "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference=f"repo::/tmp/{name}",
        worktree_path=f"/tmp/{name}",
        terminal_handle=handle,
    )
    store.transition(workflow.id, "running")
    return store, workflow.id


def test_unavailable_owner_detaches_display_observer_without_releasing_lease(
    tmp_path: Path,
) -> None:
    store, owner = _running(tmp_path, "holder", "term-holder")
    store.add_owned_terminal(owner, "observer", "observer")
    queue = ResourceQueue(tmp_path)
    lease = queue.acquire("probe", owner)
    observer = QueueLeaseNotifier(queue, store, FakeRuntime(terminal_valid=False), owner)

    assert observer.owner_terminal_is_available() is False
    assert observer.detach_if_owner_terminal_unavailable() == ["observer"]
    assert store.owned_terminal_handles(owner, kind="observer") == []
    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_version_two_migration_recovers_unacknowledged_promotion(tmp_path: Path) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    _waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    queue = ResourceQueue(tmp_path)
    lease = queue.acquire("probe", holder)
    waiting = queue.acquire("probe", waiter)
    queue.release(lease.request_id)
    with sqlite3.connect(queue.path) as connection:
        for table in (
            "queue_grant_notifications",
            "queue_lease_reminders",
            "batch_items",
            "batch_runs",
            "single_reports",
            "queue_dispatcher_state",
            "queue_result_notifications",
            "queue_jobs",
            "queue_resource_blocks",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE IF EXISTS queue_dispatcher_control")
        connection.execute("PRAGMA user_version = 2")

    reopened = ResourceQueue(tmp_path)
    assert reopened.delivery_candidates()[0]["request_id"] == waiting.request_id
