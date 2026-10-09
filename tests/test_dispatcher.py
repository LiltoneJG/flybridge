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
from flybridge_core.workflows import SCHEMA_VERSION


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

    def agent_owner_state(self, reference: str, handle: str) -> str:
        return "valid" if self.terminal_is_valid(reference, handle) else "invalid"

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
        "rig",
        owner,
        job_argv=["checker"],
        cleanup_check="/tmp/prove-clean",
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
        "rig",
        owner,
        job_argv=["checker", "--all"],
        cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
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
        "rig",
        owner,
        job_argv=["checker"],
        cleanup_check="/tmp/prove-clean",
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
        "rig",
        owner,
        job_argv=["checker"],
        cleanup_check="/tmp/prove-clean",
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
        "rig",
        owner,
        job_argv=["checker"],
        cleanup_check="/tmp/prove-clean",
        worktree_path=str(tmp_path),
    )
    waiting = queue.acquire("rig", waiter)
    assert queue.claim_job(first.request_id) is not None
    queue.cancel_owner(owner)
    assert queue.blocks("rig")

    assert (
        queue.finish_job(first.request_id, command_exit_code=0, check_exit_code=0)
        == waiting.request_id
    )
    assert queue.blocks("rig") == []
    assert queue.inspect(waiting.request_id)["status"] == "leased"


def test_single_report_waits_for_job_result_ack(tmp_path: Path) -> None:
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "owner")
    job = queue.acquire(
        "rig",
        owner,
        job_argv=["checker"],
        cleanup_check="/tmp/prove-clean",
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

    assert (
        main(
            [
                "--config",
                str(dispatcher.config.path),
                "queue",
                "run",
                "rig",
                "--owner",
                owner,
                "--cleanup-check",
                str(proof),
                "--",
                "checker",
                "--all",
            ]
        )
        == 0
    )
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
            "queue_dispatcher_state",
            "queue_result_notifications",
            "queue_jobs",
            "queue_resource_blocks",
        ):
            connection.execute(f"DROP TABLE {table}")
        for column in ("last_error", "attempts", "acknowledged_at", "sent_at"):
            connection.execute(f"ALTER TABLE queue_grant_notifications DROP COLUMN {column}")
        connection.execute("DROP TABLE IF EXISTS queue_dispatcher_control")
        connection.execute("PRAGMA user_version = 3")

    migrated = ResourceQueue(tmp_path / "state")
    assert migrated.inspect(lease.request_id)["status"] == "leased"
    assert migrated.blocks() == []
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_stop_barrier_preserves_fifo_manual_lease_and_result(tmp_path, monkeypatch):
    from flybridge_cli.dispatcher import ensure_dispatcher, request_dispatcher_stop

    dispatcher, queue, store, runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    manual = queue.acquire("manual", owner)
    completed = queue.acquire(
        "result", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    queue.claim_job(completed.request_id)
    queue.finish_job(completed.request_id, command_exit_code=0, check_exit_code=0)
    job = queue.acquire(
        "rig", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    waiter = queue.acquire("rig", _running(store, tmp_path, "waiter"))
    request_dispatcher_stop(queue)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("spawn while paused"))
    ensure_dispatcher(dispatcher.config.path, dispatcher.config.state_dir)
    assert dispatcher.serve() == 0
    assert queue.claim_job(job.request_id) is None
    assert _job_status(queue, job.request_id) == "queued"
    assert queue.inspect(waiter.request_id)["status"] == "waiting"
    assert queue.inspect(manual.request_id)["status"] == "leased"
    assert len(queue.result_candidates()) == 1
    assert runtime.sent == []


def test_restart_preserves_unknown_running_job(tmp_path, monkeypatch):
    from flybridge_cli.dispatcher import ensure_dispatcher, start_dispatcher

    dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    job = queue.acquire(
        "rig", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    queue.claim_job(job.request_id)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unsafe restart"))
    for launch in (ensure_dispatcher, start_dispatcher):
        with pytest.raises(RuntimeError, match="unknown liveness"):
            launch(dispatcher.config.path, dispatcher.config.state_dir)
    assert dispatcher.serve() == 1
    assert _job_status(queue, job.request_id) == "running"
    assert queue.inspect(job.request_id)["status"] == "leased"
    assert queue.blocks() == []
    assert queue.result_candidates() == []


def test_legacy_stop_never_signals_pid_and_start_refuses_lock(tmp_path, monkeypatch):
    import fcntl

    from flybridge_cli.dispatcher import (
        dispatcher_status,
        request_dispatcher_stop,
        start_dispatcher,
    )

    dispatcher, queue, _store, _runtime = _setup(tmp_path)
    monkeypatch.setattr("os.kill", lambda *a: pytest.fail("PID signal forbidden"))
    with (dispatcher.config.state_dir / "queue-dispatcher.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request_dispatcher_stop(queue)
        assert dispatcher_status(queue)["legacy_or_unknown"]
        with pytest.raises(RuntimeError, match="still owns lock"):
            start_dispatcher(dispatcher.config.path, dispatcher.config.state_dir)
    assert dispatcher_status(queue)["control"]["paused"] == 1


@pytest.mark.parametrize("stop_kind", ["request", "SIGTERM", "SIGINT"])
def test_stop_during_active_job_waits_without_delivery_or_new_claim(
    tmp_path, monkeypatch, stop_kind
):
    from flybridge_cli import dispatcher as module

    dispatcher, queue, store, runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    first = queue.acquire(
        "rig", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    second = queue.acquire(
        "other", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    ticks = 0

    class Future:
        def done(self):
            return ticks >= 2

        def result(self):
            queue.finish_job(first.request_id, command_exit_code=0, check_exit_code=0)

    class Pool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, function, item):
            assert item["request_id"] == first.request_id
            if stop_kind == "request":
                module.request_dispatcher_stop(queue)
            else:
                module.signal.getsignal(getattr(module.signal, stop_kind))(0, None)
            return Future()

    def sleep(_seconds):
        nonlocal ticks
        ticks += 1
        assert ticks < 5

    monkeypatch.setattr(module, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(dispatcher, "_deliver_grants", lambda: pytest.fail("delivery during drain"))
    monkeypatch.setattr(
        dispatcher, "_deliver_results", lambda: pytest.fail("delivery during drain")
    )
    assert dispatcher.serve() == 0
    assert _job_status(queue, first.request_id) == "succeeded"
    assert _job_status(queue, second.request_id) == "queued"
    assert queue.result_candidates()
    assert runtime.sent == []


def _job_status(queue, request_id):
    with queue._connect() as connection:
        return connection.execute(
            "SELECT status FROM queue_jobs WHERE request_id=?", (request_id,)
        ).fetchone()[0]


def test_claim_stop_race_serializes_and_preserves_job(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from flybridge_cli.dispatcher import request_dispatcher_stop

    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    job = queue.acquire(
        "rig", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return queue.claim_job(job.request_id)

    def stop():
        barrier.wait()
        request_dispatcher_stop(queue)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimant = pool.submit(claim)
        stopper = pool.submit(stop)
        claimed = claimant.result()
        stopper.result()
    assert _job_status(queue, job.request_id) == ("running" if claimed else "queued")
    assert queue.claim_job(job.request_id) is None
    assert queue.inspect(job.request_id)["status"] == "leased"
    assert queue.blocks() == []


def test_explicit_restart_clears_only_selected_state_barrier(tmp_path, monkeypatch):
    from flybridge_cli import dispatcher as module

    dispatcher, queue, _store, _runtime = _setup(tmp_path)
    other = ResourceQueue(tmp_path / "other-state")
    module.request_dispatcher_stop(queue)
    module.request_dispatcher_stop(other)
    launched = []
    monkeypatch.setattr(module, "ensure_dispatcher", lambda *args: launched.append(args))
    module.start_dispatcher(dispatcher.config.path, dispatcher.config.state_dir)
    assert not module.dispatcher_status(queue)["control"]["paused"]
    assert module.dispatcher_status(other)["control"]["paused"]
    assert launched == [(dispatcher.config.path, dispatcher.config.state_dir)]


def test_status_rejects_reused_pid_incarnation(tmp_path, monkeypatch):
    import fcntl
    import os

    from flybridge_cli import dispatcher as module

    dispatcher, queue, _store, _runtime = _setup(tmp_path)
    dispatcher._heartbeat()
    with queue._connect() as connection:
        connection.execute(
            "INSERT INTO queue_dispatcher_control(singleton, paused, instance, pid, "
            "process_identity, phase) VALUES (1, 0, 'old', ?, 'old-incarnation', 'serving')",
            (os.getpid(),),
        )
    monkeypatch.setattr(module, "_process_identity", lambda pid: "new-incarnation")
    with (dispatcher.config.state_dir / "queue-dispatcher.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = module.dispatcher_status(queue)
        assert status["legacy_or_unknown"]
        assert not status["stop_complete"]


def test_schema_seven_migration_preserves_running_job_and_manual_lease(tmp_path):
    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    manual = queue.acquire("manual", owner)
    job = queue.acquire(
        "rig", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    queue.claim_job(job.request_id)
    with queue._connect() as connection:
        connection.execute("DROP TABLE queue_dispatcher_control")
        connection.execute("PRAGMA user_version=7")
    migrated = ResourceQueue(tmp_path / "state")
    assert _job_status(migrated, job.request_id) == "running"
    assert migrated.inspect(manual.request_id)["status"] == "leased"
    assert migrated.blocks() == []


def test_explicit_orphan_recovery_requires_proof_lock_and_barrier(tmp_path):
    import fcntl

    from flybridge_cli.dispatcher import recover_dispatcher_job, request_dispatcher_stop

    dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    waiter_owner = _running(store, tmp_path, "waiter")
    job = queue.acquire(
        "rig", owner, job_argv=["mock"], cleanup_check="mock", worktree_path=str(tmp_path)
    )
    waiter = queue.acquire("rig", waiter_owner)
    queue.claim_job(job.request_id)
    for stopped, cleaned in ((False, True), (True, False)):
        with pytest.raises(ValueError, match="both required"):
            recover_dispatcher_job(
                queue, job.request_id, execution_stopped=stopped, cleanup_confirmed=cleaned
            )
    with pytest.raises(ValueError, match="barrier"):
        recover_dispatcher_job(
            queue, job.request_id, execution_stopped=True, cleanup_confirmed=True
        )
    request_dispatcher_stop(queue)
    with (dispatcher.config.state_dir / "queue-dispatcher.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="still owns lock"):
            recover_dispatcher_job(
                queue, job.request_id, execution_stopped=True, cleanup_confirmed=True
            )
    assert _job_status(queue, job.request_id) == "running"
    recover_dispatcher_job(queue, job.request_id, execution_stopped=True, cleanup_confirmed=True)
    assert _job_status(queue, job.request_id) == "failed"
    assert queue.inspect(waiter.request_id)["status"] == "leased"
    assert queue.claim_job(job.request_id) is None
    assert queue.result_candidates()[0]["command_exit_code"] is None
    assert queue.result_candidates()[0]["check_exit_code"] is None
    assert queue.blocks() == []


def test_known_four_field_schema_eight_migrates_transactionally(tmp_path):
    from flybridge_core.database import _DISPATCHER_V8_CONTROL

    _dispatcher, queue, store, _runtime = _setup(tmp_path)
    owner = _running(store, tmp_path, "holder")
    manual = queue.acquire("manual", owner)
    with queue._connect() as connection:
        connection.execute("DROP TABLE queue_dispatcher_control")
        connection.execute(_DISPATCHER_V8_CONTROL)
        connection.execute("INSERT INTO queue_dispatcher_control VALUES (1, 1, 'old', 'stopped')")
        connection.execute("PRAGMA user_version=8")
    migrated = ResourceQueue(tmp_path / "state")
    with migrated._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        row = connection.execute("SELECT * FROM queue_dispatcher_control").fetchone()
        assert row["paused"] == 1 and row["instance"] == "old"
        assert row["pid"] is None and row["process_identity"] is None
    assert migrated.inspect(manual.request_id)["status"] == "leased"


def test_unknown_schema_eight_control_rejected_without_mutation(tmp_path):
    import hashlib

    _dispatcher, queue, _store, _runtime = _setup(tmp_path)
    with queue._connect() as connection:
        connection.execute("PRAGMA user_version=8")
    before = hashlib.sha256(queue.path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="intermediate dispatcher control"):
        ResourceQueue(tmp_path / "state")
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest() == before
