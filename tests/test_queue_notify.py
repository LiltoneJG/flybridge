import sqlite3
from pathlib import Path

from flybridge_application import QueueLeaseNotifier, WorkflowService
from flybridge_core import ResourceQueue, WorkflowStore


class FakeRuntime:
    def __init__(self, *, fail_wait: bool = False, terminal_valid: bool = True) -> None:
        self.fail_wait = fail_wait
        self.terminal_valid = terminal_valid
        self.waited: list[str] = []
        self.sent: list[tuple[str, str]] = []

    def terminal_is_valid(self, _worktree_id: str, terminal_handle: str | None) -> bool:
        return bool(terminal_handle) and self.terminal_valid

    def wait_for_agent(self, terminal_handle: str) -> None:
        self.waited.append(terminal_handle)
        if self.fail_wait:
            raise RuntimeError("replacement agent terminal did not become TUI-idle")

    def send_prompt(self, terminal_handle: str, prompt: str) -> None:
        self.sent.append((terminal_handle, prompt))


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


def test_notifier_does_not_send_on_an_immediate_grant(tmp_path: Path) -> None:
    store, owner = _running(tmp_path, "holder", "term-holder")
    queue = ResourceQueue(tmp_path)
    granted = queue.acquire("probe", owner)
    runtime = FakeRuntime()
    notifier = QueueLeaseNotifier(queue, store, runtime, owner)

    for event in queue.events():
        notifier.note_event(event)

    assert granted.granted
    assert notifier.notify_due() is None
    assert runtime.sent == []


def test_unavailable_owner_detaches_observer_without_releasing_its_lease(tmp_path: Path) -> None:
    store, owner = _running(tmp_path, "holder", "term-holder")
    store.add_owned_terminal(owner, "observer", "observer")
    queue = ResourceQueue(tmp_path)
    lease = queue.acquire("probe", owner)
    notifier = QueueLeaseNotifier(queue, store, FakeRuntime(terminal_valid=False), owner)

    assert notifier.owner_terminal_is_available() is False
    assert notifier.detach_if_owner_terminal_unavailable() == ["observer"]
    assert store.owned_terminal_handles(owner, kind="observer") == []
    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_notifier_sends_once_after_queued_request_is_promoted(tmp_path: Path) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("probe", holder)
    waiting = queue.acquire("probe", waiter)
    runtime = FakeRuntime()
    notifier = QueueLeaseNotifier(queue, waiter_store, runtime, waiter)
    queue.release(first.lease_id or "")
    for event in queue.events():
        notifier.note_event(event)

    first_result = notifier.notify_due()
    second_result = notifier.notify_due()

    assert waiting.granted is False
    assert first_result is not None
    assert first_result["event"] == "notify_sent"
    assert first_result["lease_id"] == waiting.request_id
    assert runtime.sent[0][0] == "term-waiter"
    assert "lease-id" in runtime.sent[0][1]
    assert second_result is None
    assert len(runtime.sent) == 1


def test_notifier_ignores_another_owner_promotion(tmp_path: Path) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    _waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    other_store, other = _running(tmp_path / "other", "other", "term-other")
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("probe", holder)
    queue.acquire("probe", waiter)
    runtime = FakeRuntime()
    notifier = QueueLeaseNotifier(queue, other_store, runtime, other)
    queue.release(first.lease_id or "")
    for event in queue.events():
        notifier.note_event(event)

    assert notifier.notify_due() is None
    assert runtime.sent == []


def test_notifier_retries_after_tui_idle_failure(tmp_path: Path) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("probe", holder)
    waiting = queue.acquire("probe", waiter)
    runtime = FakeRuntime(fail_wait=True)
    notifier = QueueLeaseNotifier(queue, waiter_store, runtime, waiter)
    queue.release(first.lease_id or "")
    for event in queue.events():
        notifier.note_event(event)

    failed = notifier.notify_due()
    runtime.fail_wait = False
    sent = notifier.notify_due()

    assert failed is not None
    assert failed["event"] == "notify_failed"
    assert sent is not None
    assert sent["event"] == "notify_sent"
    assert sent["lease_id"] == waiting.request_id
    assert len(runtime.sent) == 1


def test_notifier_retries_after_transient_workflow_database_failure(
    tmp_path: Path, monkeypatch
) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    queue = ResourceQueue(tmp_path)
    first = queue.acquire("probe", holder)
    waiting = queue.acquire("probe", waiter)
    runtime = FakeRuntime()
    notifier = QueueLeaseNotifier(queue, waiter_store, runtime, waiter)
    queue.release(first.lease_id or "")
    for event in queue.events():
        notifier.note_event(event)
    original_get = waiter_store.get
    calls = 0

    def transient_get(workflow_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("no such table: workflows")
        return original_get(workflow_id)

    monkeypatch.setattr(waiter_store, "get", transient_get)

    failed = notifier.notify_due()
    sent = notifier.notify_due()

    assert failed is not None
    assert failed["event"] == "notify_failed"
    assert failed["error"] == "no such table: workflows"
    assert sent is not None
    assert sent["event"] == "notify_sent"
    assert sent["lease_id"] == waiting.request_id


def test_notifier_delivers_promotion_missed_while_observer_was_stopped(tmp_path: Path) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    queue = ResourceQueue(tmp_path)
    lease = queue.acquire("probe", holder)
    waiting = queue.acquire("probe", waiter)
    queue.release(lease.lease_id or "")

    runtime = FakeRuntime()
    restarted = QueueLeaseNotifier(queue, waiter_store, runtime, waiter)
    assert restarted.notify_due()["lease_id"] == waiting.request_id
    assert QueueLeaseNotifier(queue, waiter_store, runtime, waiter).notify_due() is None
    assert len(runtime.sent) == 1


def test_version_two_migration_recovers_unnotified_promotion(tmp_path: Path) -> None:
    _store, holder = _running(tmp_path, "holder", "term-holder")
    waiter_store, waiter = _running(tmp_path / "waiter", "waiter", "term-waiter")
    queue = ResourceQueue(tmp_path)
    lease = queue.acquire("probe", holder)
    waiting = queue.acquire("probe", waiter)
    queue.release(lease.lease_id or "")
    with sqlite3.connect(queue.path) as connection:
        for table in (
            "queue_grant_notifications",
            "queue_lease_reminders",
            "batch_items",
            "batch_runs",
            "single_reports",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 2")

    reopened = ResourceQueue(tmp_path)
    runtime = FakeRuntime()
    notifier = QueueLeaseNotifier(reopened, waiter_store, runtime, waiter)
    assert notifier.notify_due()["lease_id"] == waiting.request_id
