import sqlite3
import threading
from multiprocessing import Process, Queue
from pathlib import Path
from stat import S_IMODE

import flybridge_core.queue as queue_module
import pytest
from flybridge_core import ResourceQueue, WorkflowStore


def _acquire_from_process(state_dir: str, owner: str, results: Queue) -> None:
    try:
        queue = ResourceQueue(Path(state_dir))
        acquired = queue.acquire(f"process-resource-{owner}", owner)
        results.put((owner, acquired.granted, None))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        results.put((owner, False, str(exc)))


def test_fifo_lease_promotion(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("exclusive-check", "one")
    second = queue.acquire("exclusive-check", "two")

    assert first.granted
    assert first.request_id == first.lease_id
    assert second.position == 1
    assert queue.release(first.lease_id or "", resource="exclusive-check") == second.request_id
    assert queue.status("exclusive-check") == [
        {"resource": "exclusive-check", "status": "leased", "count": 1},
        {"resource": "exclusive-check", "status": "released", "count": 1},
    ]


def test_queue_requires_wal_journaling(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)

    with sqlite3.connect(queue.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_independent_processes_share_wal_state_without_lock_errors(tmp_path: Path) -> None:
    ResourceQueue(tmp_path)
    results: Queue = Queue()
    workers = [
        Process(target=_acquire_from_process, args=(str(tmp_path), f"owner-{index}", results))
        for index in range(6)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in workers]
    assert all(granted and error is None for _owner, granted, error in outcomes)


def test_waiter_can_inspect_its_own_promotion(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire(" shared ", " first ")
    waiting = queue.acquire("shared", "second")

    assert queue.inspect(waiting.request_id)["status"] == "waiting"
    assert queue.inspect(waiting.request_id)["lease_id"] is None

    queue.release(first.lease_id or "")

    promoted = queue.inspect(waiting.request_id)
    assert promoted["status"] == "leased"
    assert promoted["lease_id"] == waiting.request_id
    assert promoted["owner"] == "second"


def test_acquire_is_idempotent_for_an_active_owner(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)

    first = queue.acquire("shared", "owner")
    repeated = queue.acquire("shared", "owner")

    assert repeated == first
    assert queue.status("shared") == [{"resource": "shared", "status": "leased", "count": 1}]


def test_owner_requests_lists_waiting_and_leased_rows(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("shared", "holder")
    waiting = queue.acquire("shared", "waiter")

    holder_rows = queue.owner_requests("holder")
    waiter_rows = queue.owner_requests("waiter")

    assert holder_rows[0]["request_id"] == first.request_id
    assert holder_rows[0]["status"] == "leased"
    assert waiter_rows[0]["request_id"] == waiting.request_id
    assert waiter_rows[0]["status"] == "waiting"
    assert waiter_rows[0]["lease_id"] is None


def test_simultaneous_queue_acquire_grants_exactly_one_lease(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    barrier = threading.Barrier(8, timeout=5)
    results = []
    errors: list[Exception] = []

    def acquire(owner: str) -> None:
        try:
            barrier.wait()
            results.append(queue.acquire("shared", owner))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(f"owner-{index}",)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 8
    assert sum(result.granted for result in results) == 1
    assert len({result.request_id for result in results}) == 8
    assert queue.status("shared") == [
        {"resource": "shared", "status": "leased", "count": 1},
        {"resource": "shared", "status": "waiting", "count": 7},
    ]


def test_simultaneous_queue_acquire_by_one_owner_is_idempotent(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    barrier = threading.Barrier(6, timeout=5)
    results = []
    errors: list[Exception] = []

    def acquire() -> None:
        try:
            barrier.wait()
            results.append(queue.acquire("shared", "same-owner"))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=acquire) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(set(results)) == 1
    assert results[0].granted
    assert queue.status("shared") == [{"resource": "shared", "status": "leased", "count": 1}]


def test_cancelling_active_lease_promotes_the_next_request(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("exclusive-check", "one")
    queue.acquire("exclusive-check", "two")

    assert queue.cancel(first.lease_id or "")
    assert queue.status("exclusive-check") == [
        {"resource": "exclusive-check", "status": "cancelled", "count": 1},
        {"resource": "exclusive-check", "status": "leased", "count": 1},
    ]


def test_waiting_request_can_be_cancelled_by_request_id(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("exclusive-check", "one")
    waiting = queue.acquire("exclusive-check", "two")

    assert waiting.lease_id is None
    assert queue.cancel(waiting.request_id) is None
    assert queue.status("exclusive-check") == [
        {"resource": "exclusive-check", "status": "cancelled", "count": 1},
        {"resource": "exclusive-check", "status": "leased", "count": 1},
    ]
    with pytest.raises(ValueError, match="does not belong"):
        queue.release(first.lease_id or "", resource="another-resource")


def test_cancel_owner_removes_waiting_and_leased_requests(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("one", "workflow")
    waiting = queue.acquire("two", "blocker")
    owned_waiting = queue.acquire("two", "workflow")

    assert first.granted
    assert not owned_waiting.granted
    assert queue.cancel_owner("workflow") == []
    assert queue.inspect(first.request_id)["status"] == "cancelled"
    assert queue.inspect(owned_waiting.request_id)["status"] == "cancelled"
    assert queue.inspect(waiting.request_id)["status"] == "leased"


def test_cancel_owner_promotes_the_next_waiter(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    active = queue.acquire("shared", "workflow")
    waiting = queue.acquire("shared", "next")

    assert queue.cancel_owner("workflow") == [waiting.request_id]
    assert queue.inspect(active.request_id)["status"] == "cancelled"
    assert queue.inspect(waiting.request_id)["status"] == "leased"


def test_explicit_stale_recovery_promotes_fifo_request(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("exclusive-check", "one")
    second = queue.acquire("exclusive-check", "two")
    with queue._connect() as connection:
        connection.execute(
            "UPDATE requests SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", first.request_id),
        )

    assert queue.recover_stale(60) == [second.request_id]
    assert queue.status("exclusive-check") == [
        {"resource": "exclusive-check", "status": "cancelled", "count": 1},
        {"resource": "exclusive-check", "status": "leased", "count": 1},
    ]
    assert [event["event"] for event in queue.events()] == [
        "leased",
        "queued",
        "recovered",
        "leased",
    ]


@pytest.mark.parametrize("max_age", [True, "60", float("nan"), float("inf"), 0, -1])
def test_stale_recovery_requires_a_finite_positive_age(tmp_path: Path, max_age: object) -> None:
    queue = ResourceQueue(tmp_path)
    lease = queue.acquire("exclusive-check", "owner")

    with pytest.raises(ValueError, match="finite positive"):
        queue.recover_stale(max_age)

    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_leased_requests_can_be_scoped_to_owners(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("exclusive-check", "owner-a")
    queue.acquire("exclusive-check", "owner-b")
    second = queue.acquire("other-check", "owner-a")
    scoped = queue.leased_requests(owners={"owner-a"})
    assert [item["request_id"] for item in scoped] == [first.request_id, second.request_id]


def test_expire_waiting_cancels_old_waiters(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("exclusive-check", "one")
    second = queue.acquire("exclusive-check", "two")
    with queue._connect() as connection:
        connection.execute(
            "UPDATE queue_requests SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", second.request_id),
        )

    expired = queue.expire_waiting(60)
    assert [item["request_id"] for item in expired] == [second.request_id]
    assert queue.inspect(first.request_id)["status"] == "leased"
    assert queue.inspect(second.request_id)["status"] == "cancelled"


def test_expiration_can_be_scoped_to_workflow_owners(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    selected_lease = queue.acquire("lease-a", "owner-a")
    other_lease = queue.acquire("lease-b", "owner-b")
    queue.acquire("wait-a", "holder-a")
    selected_wait = queue.acquire("wait-a", "owner-a")
    queue.acquire("wait-b", "holder-b")
    other_wait = queue.acquire("wait-b", "owner-b")
    with queue._connect() as connection:
        connection.execute(
            "UPDATE requests SET created_at = ?, updated_at = ?",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
        )

    expired_leases = queue.expire_stale_leases(60, owners={"owner-a"})
    expired_waits = queue.expire_waiting(60, owners={"owner-a"})

    assert [item["request_id"] for item in expired_leases] == [selected_lease.request_id]
    assert [item["request_id"] for item in expired_waits] == [selected_wait.request_id]
    assert queue.inspect(other_lease.request_id)["status"] == "leased"
    assert queue.inspect(other_wait.request_id)["status"] == "waiting"


def test_event_reads_are_bounded(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    for owner in ("one", "two", "three"):
        queue.acquire(owner, owner)

    events = queue.events(limit=2)

    assert len(events) == 2
    assert events[0]["sequence"] < events[1]["sequence"]


def test_event_history_retains_only_the_configured_bound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(queue_module, "MAX_QUEUE_EVENTS", 3)
    queue = ResourceQueue(tmp_path)
    for owner in ("one", "two", "three", "four"):
        queue.acquire(owner, owner)

    events = queue.events()

    assert len(events) == 3
    assert events[0]["sequence"] == 2


def test_state_directories_and_databases_have_private_permissions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)
    queue_path = state_dir / "queue.sqlite3"
    queue_path.touch(mode=0o644)
    workflow_path = state_dir / "workflows.sqlite3"
    workflow_path.touch(mode=0o644)

    ResourceQueue(state_dir)
    WorkflowStore(state_dir)

    assert S_IMODE(state_dir.stat().st_mode) == 0o700
    assert S_IMODE(queue_path.stat().st_mode) == 0o600
    assert S_IMODE(workflow_path.stat().st_mode) == 0o600


def test_queue_schema_rejects_an_unsupported_version_without_its_tables(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 7")

    with pytest.raises(RuntimeError, match="legacy Flybridge state"):
        ResourceQueue(tmp_path)


def test_queue_schema_rejects_a_current_version_database_that_is_incomplete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE requests (id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {queue_module.SCHEMA_VERSION}")

    with pytest.raises(RuntimeError, match="legacy Flybridge state"):
        ResourceQueue(tmp_path)


def test_queue_schema_rejects_a_database_missing_its_uniqueness_indexes(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    with sqlite3.connect(queue.path) as connection:
        connection.execute("DROP INDEX one_resource_lease")

    with pytest.raises(RuntimeError, match="index one_resource_lease is missing"):
        ResourceQueue(tmp_path)


def test_queue_schema_rejects_changed_constraints_with_the_same_object_names(
    tmp_path: Path,
) -> None:
    queue = ResourceQueue(tmp_path)
    with sqlite3.connect(queue.path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name = 'queue_requests'",
            (
                "status TEXT NOT NULL CHECK(status IN ('waiting','leased','released','cancelled'))",
                "status TEXT NOT NULL",
            ),
        )
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(RuntimeError, match="definitions differ for queue_requests"):
        ResourceQueue(tmp_path)


def test_state_storage_rejects_symbolic_links(tmp_path: Path) -> None:
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(real_state, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        ResourceQueue(linked_state)

    with pytest.raises(OSError, match="symbolic link"):
        ResourceQueue(linked_state / "nested")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "outside.sqlite3"
    target.touch()
    (state_dir / "queue.sqlite3").symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        ResourceQueue(state_dir)


def test_unknown_queue_owners_are_cancelled_during_reconciliation(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    workflows = WorkflowStore(tmp_path)
    unknown = queue.acquire("exclusive", "missing-owner")

    queue.reconcile_terminal_workflows(workflows)

    assert queue.inspect(unknown.request_id)["status"] == "cancelled"


def test_queue_reconciliation_skips_transient_workflow_database_errors(tmp_path: Path) -> None:
    queue = ResourceQueue(tmp_path)
    workflows = WorkflowStore(tmp_path)
    workflow = workflows.create(tmp_path, "single", "owner", "Do work.")
    workflows.transition(workflow.id, "starting")
    workflows.transition(workflow.id, "running")
    leased = queue.acquire("exclusive", workflow.id)

    def broken_get(_workflow_id: str):
        raise sqlite3.OperationalError("no such table: workflows")

    workflows.get = broken_get  # type: ignore[method-assign]
    queue.reconcile_terminal_workflows(workflows)

    assert queue.inspect(leased.request_id)["status"] == "leased"
