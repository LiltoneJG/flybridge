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


def test_supervisor_restores_configured_observer_and_reminds_live_holder(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    service = WorkflowService(store, queue)
    holder = _single(store, tmp_path / "holder", "holder")
    waiter = _single(store, tmp_path / "waiter", "waiter")
    store.set_queue_observer_enabled(waiter, True)
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


def test_single_report_updates_and_checkpoint_is_not_final(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "continued")
    batch_id = batches.create("parent", "worktree")
    batches.add_item(batch_id, 0, "/single", single_id, None)
    batches.seal(batch_id)
    batches.report_single(single_id, "blocked", "Need input.")
    batches.report_single(single_id, "blocked", "Need input.")
    with batches._connect() as connection:
        before = connection.execute("SELECT reported_at FROM single_reports").fetchone()[0]
    batches.report_single(single_id, "blocked", "Need input.")
    with batches._connect() as connection:
        assert connection.execute("SELECT reported_at FROM single_reports").fetchone()[0] == before
    batches.report_single(single_id, "blocked", "Updated blocker.")
    batches.report_single(single_id, "blocked", "Continuing.", final=False)
    assert batches.status(batch_id)["ready"] is False
    batches.report_single(single_id, "done", "Verified new work.")
    assert batches.status(batch_id)["items"][0]["state"] == "done"
    assert store.get(single_id).status == "running"


def test_resume_invalidates_same_terminal_report_even_for_identical_payload(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "resumed")
    terminal = store.get(single_id).terminal_handle
    batches.report_single(single_id, "blocked", "Need input.")
    store.mark_resumed(single_id)
    assert not batches.single_reported(single_id, terminal)
    batches.report_single(single_id, "blocked", "Need input.")
    assert batches.single_reported(single_id, terminal)


def test_checkpoint_can_report_active_work_but_final_cannot(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    single_id = _single(store, tmp_path / "single", "active")
    terminal = store.get(single_id).terminal_handle
    batches.report_single(single_id, "done", "Earlier work.")
    lease = queue.acquire("checks", single_id)
    assert not batches.single_reported(single_id, terminal)
    batches.report_single(single_id, "blocked", "Checks in progress.", final=False)
    assert not batches.single_reported(single_id, terminal)
    with pytest.raises(ValueError, match="active queue requests"):
        batches.report_single(single_id, "done", "Checks in progress.")
    queue.release(lease.lease_id or "")
    assert not batches.single_reported(single_id, terminal)


def test_standalone_final_report_is_not_timed_out_or_auto_completed(
    tmp_path: Path, monkeypatch
) -> None:
    store = WorkflowStore(tmp_path)
    service = WorkflowService(store, ResourceQueue(tmp_path))
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "reported")
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET activated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (single_id,),
        )
    batches.report_single(single_id, "blocked", "Await operator input.")
    config = SimpleNamespace(
        state_dir=tmp_path, role_timeout_seconds=1, queue_lease_timeout_seconds=1
    )
    runtime = SimpleNamespace(
        set_lifecycle=lambda *_args: None, close_terminals=lambda *_args: None
    )
    monkeypatch.setattr(workflow_command, "_adapter", lambda _config: runtime)
    monkeypatch.setattr(workflow_command, "_maintain_batch_watchers", lambda *_args: None)
    monkeypatch.setattr(workflow_command, "_maintain_queue", lambda *_args: None)
    result = workflow_command._supervise_once(service, config, single_id)
    assert result["action"] == "reported"
    assert store.get(single_id).status == "running"
    with batches._connect() as connection:
        assert connection.execute("SELECT outcome FROM single_reports").fetchone()[0] == "blocked"


def test_schema_six_single_reports_survive_migration(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "legacy")
    batches.report_single(single_id, "blocked", "Existing report.")
    with store._connect() as connection:
        connection.execute("DROP TABLE single_report_phases")
        connection.execute("PRAGMA user_version = 6")
    upgraded = BatchStore(tmp_path)
    assert upgraded.single_reported(single_id, store.get(single_id).terminal_handle)
    upgraded.report_single(single_id, "done", "Continued work.")


@pytest.mark.parametrize("change", ["report", "resume", "lease"])
def test_late_state_change_wins_over_single_timeout_claim(tmp_path: Path, change: str) -> None:
    from flybridge_core.workflows import LifecycleOperationConflict

    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "race")
    snapshot = store.get(single_id)
    if change == "report":
        batches.report_single(single_id, "blocked", "Late result.")
    elif change == "resume":
        store.mark_resumed(single_id)
    else:
        ResourceQueue(tmp_path).acquire("verification", single_id)
    with pytest.raises(LifecycleOperationConflict, match="stale"):
        store.claim_terminal_transition(single_id, "cancelled", timeout_snapshot=snapshot)
    assert store.get(single_id).status == "running"
    with store._connect() as connection:
        assert connection.execute("SELECT 1 FROM workflow_lifecycle_operations").fetchone() is None


def test_timeout_claim_wins_before_late_single_report(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "claimed")
    snapshot = store.get(single_id)
    store.claim_terminal_transition(single_id, "cancelled", timeout_snapshot=snapshot)
    with pytest.raises(ValueError, match="lifecycle operation"):
        batches.report_single(single_id, "done", "Too late.")
    assert batches.single_report(single_id) is None


def test_resume_invalidates_report_before_send_and_keeps_fast_new_report(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "fast")
    terminal = store.get(single_id).terminal_handle
    batches.report_single(single_id, "blocked", "Old result.")

    class Runtime:
        def verify_worktree(self, *_args) -> None:
            pass

        def terminal_is_valid(self, *_args) -> bool:
            return True

        def wait_for_agent(self, *_args) -> None:
            pass

        def send_prompt(self, *_args) -> None:
            assert not batches.single_reported(single_id, terminal)
            batches.report_single(single_id, "done", "Fast resumed result.")

    WorkflowService(store).resume(
        single_id, Runtime(), agent="codex", response_language="English", skill_paths=()
    )
    assert batches.single_reported(single_id, terminal)


def test_batch_notification_rechecks_after_parent_wait(tmp_path: Path, monkeypatch) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "notify-race")
    batch_id = batches.create("parent", "worktree")
    batches.add_item(batch_id, 0, "/single", single_id, None)
    batches.seal(batch_id)
    batches.report_single(single_id, "done", "Old result.")
    sent: list[str] = []

    class Runtime:
        def terminal_is_valid(self, *_args) -> bool:
            return True

        def wait_for_agent(self, *_args) -> None:
            if not sent:
                batches.report_single(single_id, "blocked", "Updated result.")

        def send_prompt(self, _terminal, prompt) -> None:
            sent.append(prompt)

    monkeypatch.setattr(workflow_command, "_adapter", lambda _config: Runtime())
    assert (
        workflow_command._cmd_batch_watch(
            SimpleNamespace(batch_id=batch_id, once=False),
            SimpleNamespace(state_dir=tmp_path),
            batches,
        )
        == 0
    )
    assert "Updated result." in sent[0]
    assert '"state": "blocked"' in sent[0]
    assert "Old result." not in sent[0]


def test_timeout_claim_prevents_late_resume_and_queue_acquisition(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "claimed-resume")
    store.claim_terminal_transition(single_id, "cancelled", timeout_snapshot=store.get(single_id))
    with pytest.raises(ValueError, match="lifecycle operation"):
        store.mark_resumed(single_id)
    with pytest.raises(ValueError, match="lifecycle operation"):
        ResourceQueue(tmp_path).acquire("checks", single_id)


def test_status_marks_old_terminal_report_stale(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "old-terminal")
    batches.report_single(single_id, "done", "Old terminal result.")
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET terminal_handle = 'new' WHERE id = ?", (single_id,)
        )
    assert batches.single_report(single_id)["current"] is False


def test_cli_checkpoint_and_final_report_use_distinct_contracts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from flybridge_cli.main import main

    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    single_id = _single(store, tmp_path / "single", "cli")
    batches = BatchStore(tmp_path)
    lease = queue.acquire("verification", single_id)
    monkeypatch.setattr(
        workflow_command, "_config", lambda _args: SimpleNamespace(state_dir=tmp_path)
    )
    monkeypatch.setattr(workflow_command, "_sync_orca", lambda *_args, **_kwargs: None)
    arguments = [
        "workflow",
        "single-report",
        single_id,
        "--outcome",
        "blocked",
        "--summary",
        "Parked.",
    ]
    assert main([*arguments, "--checkpoint"]) == 0
    assert batches.single_report(single_id)["final"] is False
    assert main(arguments) == 2
    assert "active queue requests" in capsys.readouterr().err
    queue.release(lease.lease_id or "")
    assert main(arguments) == 0
    assert batches.single_report(single_id)["final"] is True
    assert batches.single_report(single_id)["current"] is True


@pytest.mark.parametrize("resource_work", ["manual", "cancelled-wait", "job", "promoted-job"])
def test_new_resource_request_permanently_invalidates_old_final_report(
    tmp_path: Path, resource_work: str
) -> None:
    store = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    batches = BatchStore(tmp_path)
    single_id = _single(store, tmp_path / "single", "resource-history")
    terminal = store.get(single_id).terminal_handle
    batch_id = batches.create("parent", "parent-worktree")
    batches.add_item(batch_id, 0, "/single", single_id, None)
    batches.seal(batch_id)
    batches.report_single(single_id, "blocked", "Original report.")
    if resource_work in {"job", "promoted-job"}:
        if resource_work == "promoted-job":
            holder = queue.acquire("checks", "other-owner")
        request = queue.acquire(
            "checks",
            single_id,
            job_argv=["mock-checker"],
            cleanup_check="/mock/cleanup-proof",
            worktree_path=str(tmp_path),
        )
        if resource_work == "promoted-job":
            assert not request.granted
            assert queue.release(holder.lease_id or "") == request.request_id
        assert queue.claim_job(request.request_id) is not None
        queue.finish_job(request.request_id, command_exit_code=1, check_exit_code=0)
        assert not batches.single_reported(single_id, terminal)
        queue.acknowledge_result(request.request_id, single_id)
    elif resource_work == "cancelled-wait":
        queue.acquire("checks", "other-owner")
        request = queue.acquire("checks", single_id)
        assert not request.granted
        queue.cancel(request.request_id)
    else:
        request = queue.acquire("checks", single_id)
        queue.release(request.lease_id or "")
    assert not batches.single_reported(single_id, terminal)
    assert not batches.status(batch_id)["ready"]
    assert batches.single_report(single_id)["current"] is False
    batches.report_single(single_id, "blocked", "Original report.")
    assert batches.single_reported(single_id, terminal)
