"""Receiver safety tests use mock Orca and temporary queue state, never live sessions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import write_config
from flybridge_cli.commands import workflow as workflow_command
from flybridge_cli.dispatcher import QueueDispatcher
from flybridge_core import BatchStore, ResourceQueue, WorkflowStore, load_config
from flybridge_orca import OrcaClient, PromptDeliveryBlocked


def _owner(store: WorkflowStore, path: Path, name: str) -> str:
    record = store.create(path, "single", name, "Check resource.")
    store.transition(record.id, "starting")
    store.attach_external(
        record.id,
        adapter_reference=f"repo::{name}",
        worktree_path=str(path),
        terminal_handle=f"term-{name}",
    )
    store.transition(record.id, "running")
    return record.id


class MockReceiver:
    """Simulate PTY takeover, including unsafe shell effects if input were written."""

    def __init__(self, queue: ResourceQueue | None = None) -> None:
        self.mode = "exec"
        self.incarnation = "original-pty"
        self.after_show = "shell"
        self.stale = False
        self.calls: list[list[str]] = []
        self.shell_input: list[str] = []
        self.queue = queue
        self.lease: str | None = None

    def __call__(self, arguments, **_kwargs):
        self.calls.append(arguments)
        if arguments[1:3] == ["terminal", "show"]:
            if self.stale:
                payload = {
                    "ok": False,
                    "error": {"code": "terminal_handle_stale", "message": "stale"},
                }
            else:
                handle = arguments[arguments.index("--terminal") + 1]
                payload = {
                    "ok": True,
                    "result": {
                        "terminal": {
                            "handle": handle,
                            "connected": True,
                            "writable": True,
                            "worktreeId": "repo::temporary",
                            "agentIdentity": "codex",
                            "incarnationId": self.incarnation,
                            "receiverMode": self.mode,
                        }
                    },
                }
                # Voluntary exec exit or takeover occurs between observation and write.
                self.mode = self.after_show
                self.incarnation = "replacement-pty"
        elif arguments[1:3] == ["terminal", "wait"]:
            payload = {"ok": True, "result": {"wait": {"satisfied": True}}}
        elif arguments[1:3] == ["terminal", "send"]:
            prompt = arguments[arguments.index("--text") + 1]
            if self.mode == "shell":
                self.shell_input.append(prompt)
                # Model the accidental execution of backtick release in the shell.
                if self.queue is not None and self.lease:
                    self.queue.release(self.lease)
            payload = {
                "ok": True,
                "result": {"send": {"accepted": True, "prompt": {"stages": ["input_accepted"]}}},
            }
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")


@pytest.mark.parametrize("mode", ["exec", "shell", "tui", "unknown"])
def test_no_markdown_input_is_written_even_when_receiver_changes_after_show(mode: str) -> None:
    receiver = MockReceiver()
    receiver.mode = mode
    client = OrcaClient("mock-orca", runner=receiver, which=lambda _name: "/mock/agent")
    client.wait_for_agent("term-owner")  # Idle signal alone is not a safe receiver proof.
    with pytest.raises(PromptDeliveryBlocked) as blocked:
        client.send_prompt(
            "term-owner", "Grant `queue ack REQUEST --lease LEASE`; `queue release LEASE`."
        )
    assert blocked.value.code == "delivery_blocked"
    assert not blocked.value.input_accepted
    assert not blocked.value.turn_started
    assert receiver.shell_input == []
    assert not any(call[1:3] == ["terminal", "send"] for call in receiver.calls)


def test_stale_receiver_is_blocked_without_input() -> None:
    receiver = MockReceiver()
    receiver.stale = True
    with pytest.raises(PromptDeliveryBlocked, match="cannot be verified"):
        OrcaClient("mock-orca", runner=receiver).send_prompt("term-old", "Lease `ack`.")
    assert receiver.shell_input == []


def test_blocked_grant_is_durable_idempotent_and_does_not_reclaim_live_owner(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path / "config.jsonc", state_dir=tmp_path / "state"))
    dispatcher = QueueDispatcher(config)
    queue, store = dispatcher.queue, dispatcher.store
    holder = _owner(store, tmp_path, "holder")
    waiter = _owner(store, tmp_path, "waiter")
    first = queue.acquire("checks", holder)
    waiting = queue.acquire("checks", waiter)
    queue.release(first.lease_id or "")
    receiver = MockReceiver(queue)
    receiver.lease = waiting.request_id
    dispatcher.client = OrcaClient("mock-orca", runner=receiver)
    for _ in range(2):
        dispatcher._deliver_grants()
        with queue._connect() as connection:
            connection.execute("UPDATE queue_grant_notifications SET sent_at = NULL")
        dispatcher._check_dead_owners()
    pending = queue.pending_grants(waiter)[0]
    assert pending["attempts"] == 2
    assert "delivery_blocked" in pending["last_error"]
    assert pending["delivered_at"] is None
    assert pending["acknowledged_at"] is None
    assert queue.acquire("checks", waiter).request_id == waiting.request_id
    assert queue.inspect(waiting.request_id)["status"] == "leased"
    assert store.get(waiter).status == "running"
    assert receiver.shell_input == []
    assert not any(call[1:3] == ["terminal", "send"] for call in receiver.calls)
    # Explicit official ack is independent of prompt delivery and is idempotent.
    queue.acknowledge(waiting.request_id, waiting.request_id, waiter)
    queue.acknowledge(waiting.request_id, waiting.request_id, waiter)
    dispatcher._deliver_grants()
    assert queue.pending_grants(waiter) == []


def test_result_delivery_stays_unacknowledged_after_exec_exit(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "config.jsonc", state_dir=tmp_path / "state"))
    dispatcher = QueueDispatcher(config)
    owner = _owner(dispatcher.store, tmp_path, "job")
    request = dispatcher.queue.acquire(
        "checks",
        owner,
        job_argv=["mock-checker"],
        cleanup_check="/mock/proof",
        worktree_path=str(tmp_path),
    )
    dispatcher.queue.claim_job(request.request_id)
    dispatcher.queue.finish_job(request.request_id, command_exit_code=1, check_exit_code=0)
    receiver = MockReceiver()
    receiver.mode = "shell"
    dispatcher.client = OrcaClient("mock-orca", runner=receiver)
    dispatcher._deliver_results()
    result = dispatcher.queue.pending_results([owner])[0]
    assert "delivery_blocked" in result["last_error"]
    assert result["acknowledged_at"] is None
    assert receiver.shell_input == []
    with pytest.raises(ValueError, match="acknowledge queue job results"):
        BatchStore(config.state_dir).report_single(owner, "done", "Cannot skip the result.")


def test_unknown_agent_process_does_not_authorize_watchdog_lease_recovery(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "config.jsonc", state_dir=tmp_path / "state"))
    store, queue = WorkflowStore(config.state_dir), ResourceQueue(config.state_dir)
    owner = _owner(store, tmp_path, "live")
    lease = queue.acquire("checks", owner)
    receiver = MockReceiver()
    receiver.stale = True
    client = OrcaClient("mock-orca", runner=receiver)
    from flybridge_application import WorkflowService

    assert (
        workflow_command._apply_watchdog(WorkflowService(store, queue), config, client, owner)
        is None
    )
    assert queue.inspect(lease.request_id)["status"] == "leased"
    assert store.get(owner).status == "running"


def test_lease_reminder_never_reaches_shell_or_releases_lease(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from flybridge_application import WorkflowService

    config = load_config(write_config(tmp_path / "config.jsonc", state_dir=tmp_path / "state"))
    store, queue = WorkflowStore(config.state_dir), ResourceQueue(config.state_dir)
    holder, waiter = _owner(store, tmp_path, "holder"), _owner(store, tmp_path, "waiter")
    lease = queue.acquire("checks", holder)
    queue.acquire("checks", waiter)
    with queue._connect() as connection:
        connection.execute("UPDATE queue_requests SET updated_at='2000-01-01T00:00:00+00:00'")
    receiver = MockReceiver(queue)
    receiver.lease = lease.request_id
    client = OrcaClient("mock-orca", runner=receiver)
    monkeypatch.setattr(client, "terminal_is_valid", lambda *_args: True)
    monkeypatch.setattr(workflow_command, "ensure_dispatcher", lambda *_args: None)
    workflow_command._maintain_queue(WorkflowService(store, queue), config, client, holder)
    assert "delivery_blocked" in capsys.readouterr().err
    assert queue.reminder_due(lease.request_id, 0)
    assert queue.inspect(lease.request_id)["status"] == "leased"
    assert receiver.shell_input == []


def test_batch_notification_remains_pending_when_parent_becomes_shell(
    tmp_path: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    batches = BatchStore(tmp_path)
    batch = batches.create("term-parent", "repo::parent")
    batches.add_item(batch, 0, "/mock/start-failed", None, "start failed")
    batches.seal(batch)
    receiver = MockReceiver()
    client = OrcaClient("mock-orca", runner=receiver)
    monkeypatch.setattr(client, "terminal_is_valid", lambda *_args: True)
    monkeypatch.setattr(workflow_command, "_adapter", lambda _config: client)

    class EndIteration(Exception):
        pass

    def stop(_seconds):
        raise EndIteration

    monkeypatch.setattr(workflow_command.time, "sleep", stop)
    with pytest.raises(EndIteration):
        workflow_command._cmd_batch_watch(
            SimpleNamespace(batch_id=batch, once=False),
            SimpleNamespace(state_dir=tmp_path),
            batches,
        )
    status = batches.status(batch)
    assert status["status"] != "notified"
    assert "delivery_blocked" in status["notification_error"]
    assert receiver.shell_input == []
