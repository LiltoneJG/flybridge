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


def test_watchdog_records_result_missing_when_owner_process_exits(tmp_path, monkeypatch):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)
    service = WorkflowService(WorkflowStore(tmp_path), ResourceQueue(tmp_path))
    lifecycle = []
    runtime = SimpleNamespace(
        agent_owner_state=lambda *_args: "invalid",
        set_lifecycle=lambda *_args: lifecycle.append(_args),
        close_terminals=lambda _ref, handle: closed.append(handle),
    )
    monkeypatch.setattr(
        service,
        "salvage_progress",
        lambda *_args: {
            "workflow_id": owner,
            "sha": None,
            "pushed": False,
            "skip_reason": "no_commits",
            "error": None,
            "created_at": "now",
            "push": None,
        },
    )

    result = command._apply_watchdog(service, config, runtime, owner)

    assert result["action"] == "blocked"
    assert result["reason"] == "result-missing"
    assert result["result"]["outcome"] == "result-missing"
    assert result["result"]["source"] == "monitor"
    assert service.store.get(owner).status == "cancelled"
    recorded = service.store.execution_result(owner)
    assert recorded.outcome == "result-missing"
    assert recorded.current is False
    assert lifecycle


def test_watchdog_replays_persisted_result_missing_after_interrupted_finalization(
    tmp_path, monkeypatch
):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)
    workflow = service.store.get(owner)
    persisted = service.record_execution_result(
        owner,
        expected_terminal_handle=workflow.terminal_handle,
        outcome="result-missing",
        source="monitor",
        summary="Owner process exited without a final result.",
        unresolved=("No final execution result was received.",),
    )
    service = WorkflowService(WorkflowStore(tmp_path), ResourceQueue(tmp_path))
    runtime = SimpleNamespace(
        agent_owner_state=lambda *_args: "invalid",
        set_lifecycle=lambda *_args: None,
        close_terminals=lambda _ref, handle: closed.append(handle),
    )
    monkeypatch.setattr(
        service,
        "salvage_progress",
        lambda *_args: {
            "workflow_id": owner,
            "sha": None,
            "pushed": False,
            "skip_reason": "no_commits",
            "error": None,
            "created_at": "now",
            "push": None,
        },
    )

    replayed = command._apply_watchdog(service, config, runtime, owner)

    assert replayed["action"] == "blocked"
    assert replayed["result"]["updated_at"] == persisted.updated_at
    assert service.store.get(owner).status == "cancelled"


def test_watchdog_surfaces_question_waiting_without_timing_out(tmp_path, monkeypatch):
    service, owner, config, closed = _setup(tmp_path, monkeypatch)
    workflow = service.store.get(owner)
    waiting = service.record_execution_result(
        owner,
        expected_terminal_handle=workflow.terminal_handle,
        outcome="question-waiting",
        source="agent",
        summary="Which target should be used?",
        unresolved=("Target selection requires operator input.",),
    )
    service = WorkflowService(WorkflowStore(tmp_path), ResourceQueue(tmp_path))
    runtime = SimpleNamespace(agent_owner_state=lambda *_args: "invalid")

    result = command._apply_watchdog(service, config, runtime, owner)

    assert result["action"] == "question-waiting"
    assert result["result"]["updated_at"] == waiting.updated_at
    assert service.store.get(owner).status == "running"
    assert closed == []


def test_watchdog_surfaces_explicit_runtime_error_after_restart(tmp_path, monkeypatch):
    service, owner, config, _closed = _setup(tmp_path, monkeypatch)
    workflow = service.store.get(owner)
    service.record_execution_result(
        owner,
        expected_terminal_handle=workflow.terminal_handle,
        outcome="runtime-error",
        source="agent",
        summary="Tool transport failed.",
        unresolved=("Retry requires operator approval.",),
    )
    service = WorkflowService(WorkflowStore(tmp_path), ResourceQueue(tmp_path))
    runtime = SimpleNamespace(agent_owner_state=lambda *_args: "valid")

    result = command._apply_watchdog(service, config, runtime, owner)

    assert result["action"] == "result-received"
    assert result["outcome"] == "runtime-error"
    assert result["result"]["source"] == "agent"
