"""Single watchdog cleanup must never require an orchestration aggregate."""

from types import SimpleNamespace

import pytest
from conftest import write_config
from flybridge_application import WorkflowService
from flybridge_cli.commands import workflow as command
from flybridge_core import ResourceQueue, WorkflowStore, load_config


def _setup(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path)
    record = store.create(tmp_path, "single", "single", "Mock work.")
    store.transition(record.id, "starting")
    store.attach_external(
        record.id,
        adapter_reference="mock::owned",
        worktree_path=str(tmp_path),
        terminal_handle="agent",
    )
    store.transition(record.id, "running")
    store.add_owned_terminal(record.id, "supervisor", "coordinator")
    service = WorkflowService(store, ResourceQueue(tmp_path))
    closed = []
    runtime = SimpleNamespace(close_terminals=lambda ref, handle: closed.append(handle))
    monkeypatch.setattr(command, "_adapter", lambda _config: runtime)
    monkeypatch.setattr(command, "_maintain_queue", lambda *_args: None)
    monkeypatch.setattr(command, "_maintain_batch_watchers", lambda *_args: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("single must not use orchestration-only state")

    for method in (
        "orchestration_run",
        "record_coordinator_error",
        "set_orchestration_outcome",
        "release_coordinator_ownership",
        "clear_coordinator_errors",
    ):
        monkeypatch.setattr(store, method, forbidden)
    config = load_config(write_config(tmp_path / "config.jsonc", state_dir=tmp_path))
    return service, record.id, config, closed


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_single_terminal_finalization_closes_only_watchdog_without_run_lookup(
    tmp_path, monkeypatch, status
):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)
    service.store.transition(owner, status)
    assert (
        command._cmd_supervise(SimpleNamespace(workflow_id=owner, once=False), config, service) == 0
    )
    assert closed == ["supervisor"]
    assert service.store.get(owner).status == status
    assert tmp_path.exists()


@pytest.mark.parametrize(
    "error", [OSError("mock connection lost"), ValueError("mock malformed receiver")]
)
def test_single_supervisor_error_preserves_work_and_live_lease(
    tmp_path, monkeypatch, capsys, error
):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)
    lease = service.queue.acquire("mock-checks", owner)

    def fail(*_args):
        raise error

    monkeypatch.setattr(command, "_supervise_once", fail)
    result = command._cmd_supervise(SimpleNamespace(workflow_id=owner, once=True), config, service)
    assert result == (0 if isinstance(error, OSError) else 2)
    assert str(error) in capsys.readouterr().out
    assert service.store.get(owner).status == "running"
    assert service.queue.inspect(lease.request_id)["status"] == "leased"
    assert closed == ([] if isinstance(error, OSError) else ["supervisor"])


def test_single_cancel_during_transient_failure_finalizes_only_its_monitor(tmp_path, monkeypatch):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)

    def fail(*_args):
        service.store.transition(owner, "cancelled")
        raise OSError("mock lookup failed after operator cancellation")

    monkeypatch.setattr(command, "_supervise_once", fail)
    assert (
        command._cmd_supervise(SimpleNamespace(workflow_id=owner, once=False), config, service) == 0
    )
    assert closed == ["supervisor"]
    assert service.store.get(owner).status == "cancelled"


def test_single_transient_failure_retries_without_orchestration_state(tmp_path, monkeypatch):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)
    calls = []

    def supervise(*_args):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary failure")
        service.store.transition(owner, "completed")
        return {"action": "terminal", "workflow_id": owner, "status": "completed"}

    monkeypatch.setattr(command, "_supervise_once", supervise)
    monkeypatch.setattr(command.time, "sleep", lambda _seconds: None)
    assert (
        command._cmd_supervise(SimpleNamespace(workflow_id=owner, once=False), config, service) == 0
    )
    assert len(calls) == 2
    assert closed == ["supervisor"]
