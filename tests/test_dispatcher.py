from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from conftest import write_config
from flybridge_cli.dispatcher import QueueDispatcher
from flybridge_cli.main import main
from flybridge_core import BatchStore, ResourceQueue, WorkflowStore, load_config


class FakeOrca:
    def __init__(self) -> None:
        self.valid = True
        self.invalid_handles: set[str] = set()
        self.lookup_error = False
        self.sent: list[tuple[str, str]] = []

    def terminal_is_valid(self, _reference: str, handle: str) -> bool:
        if self.lookup_error:
            raise RuntimeError("Orca temporarily unavailable")
        return self.valid and handle not in self.invalid_handles

    def wait_for_agent(self, _handle: str) -> None:
        return None

    def send_prompt(self, handle: str, prompt: str) -> None:
        self.sent.append((handle, prompt))


def _running(store: WorkflowStore, path: Path, name: str) -> str:
    workflow = store.create(path, "single", name, "Check shared resource.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference=f"repo::{path / name}",
        worktree_path=str(path),
        terminal_handle=f"term-{name}",
    )
    store.transition(workflow.id, "running")
    return workflow.id


def _setup(tmp_path: Path) -> tuple[QueueDispatcher, ResourceQueue, WorkflowStore, FakeOrca]:
    config_path = write_config(tmp_path / "config.jsonc", state_dir=tmp_path / "state")
    config = load_config(config_path)
    dispatcher = QueueDispatcher(config)
    runtime = FakeOrca()
    dispatcher.client = runtime
    return dispatcher, dispatcher.queue, dispatcher.store, runtime


def test_promotion_before_observer_start_is_delivered_and_acknowledged(tmp_path: Path) -> None:
    dispatcher, queue, store, runtime = _setup(tmp_path)
    holder = _running(store, tmp_path, "holder")
    waiter = _running(store, tmp_path, "waiter")
    first = queue.acquire("rig", holder)
    waiting = queue.acquire("rig", waiter)
    queue.release(first.request_id, owner=holder)

    dispatcher._deliver_grants()
    assert len(runtime.sent) == 1
    assert waiting.request_id in runtime.sent[0][1]
    assert queue.pending_grants(waiter)[0]["attempts"] == 1

    queue.acknowledge(waiting.request_id, waiting.request_id, waiter)
    dispatcher._deliver_grants()
    assert len(runtime.sent) == 1
    assert queue.pending_grants(waiter) == []


def test_dead_waiter_is_cancelled_without_moving_the_live_holder(tmp_path: Path) -> None:
    dispatcher, queue, store, runtime = _setup(tmp_path)
    holder = _running(store, tmp_path, "holder")
    waiter = _running(store, tmp_path, "waiter")
    first = queue.acquire("rig", holder)
    waiting = queue.acquire("rig", waiter)
    runtime.invalid_handles.add("term-waiter")

    dispatcher._check_dead_owners()

    assert queue.inspect(first.request_id)["status"] == "leased"
    assert queue.inspect(waiting.request_id)["status"] == "cancelled"
    assert queue.blocks("rig") == []
    assert store.get(waiter).status.value == "failed"


def test_transient_orca_lookup_error_does_not_revoke_a_lease(tmp_path: Path) -> None:
    dispatcher, queue, store, runtime = _setup(tmp_path)
    holder = _running(store, tmp_path, "holder")
    first = queue.acquire("rig", holder)
    runtime.lookup_error = True

    dispatcher._check_dead_owners()

    assert queue.inspect(first.request_id)["status"] == "leased"
    assert queue.blocks("rig") == []


def test_dead_owner_during_running_job_is_failed_and_stays_blocked(tmp_path: Path) -> None:
    dispatcher, queue, store, runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    waiter = _running(store, tmp_path, "waiter")
    first = queue.acquire(
        "rig", owner, job_argv=["checker"], cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
    )
    waiting = queue.acquire("rig", waiter)
    assert queue.claim_job(first.request_id) is not None
    runtime.invalid_handles.add("term-owner")

    dispatcher._check_dead_owners()

    assert store.get(owner).status.value == "failed"
    assert queue.inspect(waiting.request_id)["status"] == "waiting"
    assert queue.blocks("rig")[0]["request_id"] == first.request_id


def test_job_releases_only_after_cleanup_proof_and_reports_command_failure(tmp_path: Path) -> None:
    dispatcher, queue, store, runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    waiter = _running(store, tmp_path, "waiter")
    job = queue.acquire(
        "rig", owner, job_argv=["checker", "--all"],
        cleanup_check="/tmp/prove-clean", worktree_path=str(tmp_path),
    )
    waiting = queue.acquire("rig", waiter)
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3 if len(calls) == 1 else 0)

    from flybridge_cli import dispatcher as dispatcher_module

    original = dispatcher_module.subprocess.run
    dispatcher_module.subprocess.run = run
    try:
        claimed = queue.claim_job(job.request_id)
        assert claimed is not None
        dispatcher._execute(claimed)
    finally:
        dispatcher_module.subprocess.run = original

    assert calls == [["checker", "--all"], ["/tmp/prove-clean"]]
    assert queue.inspect(job.request_id)["status"] == "released"
    assert queue.inspect(waiting.request_id)["status"] == "leased"
    assert queue.blocks("rig") == []
    assert queue.result_candidates()[0]["status"] == "failed"
    dispatcher._deliver_results()
    assert "status failed" in runtime.sent[0][1]
    queue.acknowledge_result(job.request_id, owner)
    assert queue.result_candidates() == []


def test_unverified_job_and_crashed_dispatcher_block_fifo(tmp_path: Path) -> None:
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    waiter = _running(store, tmp_path, "waiter")
    first = queue.acquire(
        "rig", owner, job_argv=["checker"], cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
    )
    waiting = queue.acquire("rig", waiter)
    assert queue.claim_job(first.request_id) is not None
    assert queue.abandon_running_jobs() == [first.request_id]
    assert queue.inspect(waiting.request_id)["status"] == "waiting"
    assert queue.claim_job(first.request_id) is None
    assert queue.blocks("rig")[0]["reason"] == "dispatcher_stopped"
    assert queue.resolve(first.request_id, cleanup_confirmed=True) == waiting.request_id


def test_failed_cleanup_proof_blocks_next_job(tmp_path: Path) -> None:
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    waiter = _running(store, tmp_path, "waiter")
    first = queue.acquire(
        "rig", owner, job_argv=["checker"], cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
    )
    waiting = queue.acquire("rig", waiter)
    assert queue.claim_job(first.request_id) is not None
    assert queue.finish_job(first.request_id, command_exit_code=0, check_exit_code=1) is None
    assert queue.inspect(waiting.request_id)["status"] == "waiting"
    assert queue.blocks("rig")[0]["reason"] == "cleanup_unverified"


def test_successful_proof_releases_an_abandoned_owner_without_replaying(tmp_path: Path) -> None:
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    waiter = _running(store, tmp_path, "waiter")
    first = queue.acquire(
        "rig", owner, job_argv=["checker"], cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
    )
    waiting = queue.acquire("rig", waiter)
    assert queue.claim_job(first.request_id) is not None
    queue.cancel_owner(owner)
    assert queue.blocks("rig")

    assert queue.finish_job(first.request_id, command_exit_code=0, check_exit_code=0) == waiting.request_id
    assert queue.blocks("rig") == []
    assert queue.inspect(waiting.request_id)["status"] == "leased"


def test_single_report_waits_for_job_result_ack(tmp_path: Path) -> None:
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    job = queue.acquire(
        "rig", owner, job_argv=["checker"], cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
    )
    assert queue.claim_job(job.request_id) is not None
    queue.finish_job(job.request_id, command_exit_code=0, check_exit_code=0)
    batches = BatchStore(tmp_path / "state")

    with pytest.raises(ValueError, match="acknowledge queue job results"):
        batches.report_single(owner, "done", "Checks passed")
    queue.acknowledge_result(job.request_id, owner)
    batches.report_single(owner, "done", "Checks passed")


def test_queue_run_cli_registers_an_argv_without_starting_a_real_service(
    tmp_path: Path, capsys
) -> None:
    dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    proof = tmp_path / "proof"
    proof.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    proof.chmod(0o700)

    assert main([
        "--config", str(dispatcher.config.path), "queue", "run", "rig", "--owner",
        owner, "--cleanup-check", str(proof), "--", "checker", "--all",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["granted"] is True
    claimed = queue.claim_job(payload["request_id"])
    assert claimed is not None
    assert json.loads(str(claimed["argv_json"])) == ["checker", "--all"]


def test_version_three_queue_state_migrates_in_place(tmp_path: Path) -> None:
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    lease = queue.acquire("rig", owner)
    with sqlite3.connect(queue.path) as connection:
        for table in (
            "queue_dispatcher_state", "queue_result_notifications", "queue_jobs",
            "queue_resource_blocks",
        ):
            connection.execute(f"DROP TABLE {table}")
        for column in ("last_error", "attempts", "acknowledged_at", "sent_at"):
            connection.execute(f"ALTER TABLE queue_grant_notifications DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 3")

    migrated = ResourceQueue(tmp_path / "state")
    assert migrated.inspect(lease.request_id)["status"] == "leased"
    assert migrated.blocks() == []
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
