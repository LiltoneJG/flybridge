from pathlib import Path
from types import SimpleNamespace

import pytest
from flybridge_application import WorkflowService
from flybridge_cli.commands import workflow as workflow_command
from flybridge_core import BatchStore, ResourceQueue, WorkflowStore


def _single(store: WorkflowStore, path: Path, name: str) -> str:
    workflow = store.create(path, "single", name, "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference=f"orca::{name}",
        worktree_path=str(path),
        terminal_handle=f"terminal-{name}",
    )
    store.transition(workflow.id, "running")
    return workflow.id


def test_batch_waits_for_single_report_and_orchestrated_outcome(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "single")
    manager = store.create_orchestrated_plan(tmp_path / "orchestrated", "managed", "Do work.")[0]
    batch_id = batches.create("parent-terminal", "parent-worktree")
    batches.add_item(batch_id, 0, "/single", single_id, None)
    batches.add_item(batch_id, 1, "/managed", manager.id, None)
    batches.add_item(batch_id, 2, "/failed", None, "start failed")
    batches.seal(batch_id)

    assert batches.status(batch_id)["ready"] is False
    batches.report_single(single_id, "done", "Checks passed and pushed.")
    assert batches.status(batch_id)["ready"] is False
    store.set_orchestration_outcome(manager.id, "blocked", error="review blocked")
    status = batches.status(batch_id)
    assert status["ready"] is True
    assert [item["state"] for item in status["items"]] == ["done", "blocked", "start_failed"]
    batches.mark_notified(batch_id)
    assert batches.status(batch_id)["status"] == "notified"


def test_single_report_requires_released_queue_and_current_terminal(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    single_id = _single(store, tmp_path / "single", "single")
    batch_id = batches.create("parent-terminal", "parent-worktree")
    batches.add_item(batch_id, 0, "/single", single_id, None)
    batches.seal(batch_id)
    lease = queue.acquire("verification", single_id)
    with pytest.raises(ValueError, match="active queue requests"):
        batches.report_single(single_id, "done", "Done.")
    queue.release(lease.lease_id or "")
    batches.report_single(single_id, "done", "Done.")
    assert batches.status(batch_id)["ready"] is True
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET terminal_handle = 'replacement' WHERE id = ?", (single_id,)
        )
    assert batches.status(batch_id)["ready"] is False
    batches.report_single(single_id, "blocked", "New agent blocked.")
    assert batches.status(batch_id)["items"][0]["state"] == "blocked"


def test_active_batch_path_is_reserved_until_parent_notification(tmp_path: Path) -> None:
    batches = BatchStore(tmp_path)
    batch_id = batches.create("parent-terminal", "parent-worktree")
    batches.add_item(batch_id, 0, "/worktree", None, "start failed")
    batches.seal(batch_id)
    assert batches.active_path("/worktree") == batch_id
    batches.mark_notified(batch_id)
    assert batches.active_path("/worktree") is None


def test_only_one_watcher_can_claim_a_batch_notification(tmp_path: Path) -> None:
    batches = BatchStore(tmp_path)
    batch_id = batches.create("parent-terminal", "parent-worktree")
    batches.seal(batch_id)
    assert batches.claim_notification(batch_id, "watcher-a") is True
    assert batches.claim_notification(batch_id, "watcher-b") is False
    batches.mark_notified(batch_id, token="watcher-a")
    assert batches.claim_notification(batch_id, "watcher-b") is False


def test_batch_watcher_retries_send_and_notifies_parent_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    batches = BatchStore(tmp_path)
    batch_id = batches.create("parent-terminal", "parent-worktree")
    batches.add_item(batch_id, 0, "/failed", None, "start failed")
    batches.seal(batch_id)

    class Runtime:
        def __init__(self) -> None:
            self.attempts = 0
            self.prompts: list[str] = []

        def terminal_is_valid(self, worktree: str, terminal: str) -> bool:
            return worktree == "parent-worktree" and terminal == "parent-terminal"

        def wait_for_agent(self, terminal: str) -> None:
            assert terminal == "parent-terminal"

        def send_prompt(self, terminal: str, prompt: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary send failure")
            self.prompts.append(prompt)

    runtime = Runtime()
    monkeypatch.setattr(workflow_command, "_adapter", lambda _config: runtime)
    monkeypatch.setattr(workflow_command.time, "sleep", lambda _seconds: None)
    config = SimpleNamespace(state_dir=tmp_path)
    args = SimpleNamespace(batch_id=batch_id, once=False)
    assert workflow_command._cmd_batch_watch(args, config, batches) == 0
    assert runtime.attempts == 2
    assert len(runtime.prompts) == 1
    assert "start_failed" in runtime.prompts[0]
    assert batches.status(batch_id)["status"] == "notified"
    assert workflow_command._cmd_batch_watch(args, config, batches) == 0
    assert len(runtime.prompts) == 1
    capsys.readouterr()


def test_supervisor_restores_waiter_observer_and_reminds_live_holder(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(store, queue)
    holder = _single(store, tmp_path / "holder", "holder")
    waiter = _single(store, tmp_path / "waiter", "waiter")
    lease = queue.acquire("verification", holder)
    queue.acquire("verification", waiter)
    with queue._connect() as connection:
        connection.execute(
            "UPDATE queue_requests SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (lease.request_id,),
        )

    class Runtime:
        def __init__(self) -> None:
            self.next_observer = 0
            self.closed: set[str] = set()
            self.prompts: list[str] = []

        def terminal_is_valid(self, _worktree: str, handle: str | None) -> bool:
            return bool(handle and handle not in self.closed)

        def create_observer(self, _worktree: str, _command: str) -> str:
            self.next_observer += 1
            return f"observer-{self.next_observer}"

        def close_terminals(self, _worktree: str, handle: str) -> None:
            self.closed.add(handle)

        def wait_for_agent(self, _handle: str) -> None:
            pass

        def send_prompt(self, _handle: str, prompt: str) -> None:
            self.prompts.append(prompt)

    runtime = Runtime()
    config = SimpleNamespace(
        state_dir=tmp_path, queue_lease_timeout_seconds=1, path=tmp_path / "config.jsonc"
    )
    workflow_command._maintain_queue(service, config, runtime, waiter)
    first_observer = store.owned_terminal_handles(waiter, kind="observer")[0]
    runtime.closed.add(first_observer)
    workflow_command._maintain_queue(service, config, runtime, waiter)
    assert store.owned_terminal_handles(waiter, kind="observer") == ["observer-2"]
    workflow_command._maintain_queue(service, config, runtime, holder)
    workflow_command._maintain_queue(service, config, runtime, holder)
    assert len(runtime.prompts) == 1
    assert lease.request_id in runtime.prompts[0]
    assert queue.inspect(lease.request_id)["status"] == "leased"
