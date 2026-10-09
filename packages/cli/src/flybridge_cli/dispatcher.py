"""One process per state directory delivers grants and runs queued commands."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flybridge_application.prompts import render_lease_grant_prompt
from flybridge_core import ResourceQueue, WorkflowStatus, WorkflowStore
from flybridge_orca import OrcaClient


@contextmanager
def _drain_signals(handler, queue, instance):
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, old_handler in previous.items():
            signal.signal(sig, old_handler)
        with queue._connect() as connection:
            connection.execute(
                "UPDATE queue_dispatcher_control SET paused=1, phase='stopped' WHERE instance=?",
                (instance,),
            )


def _process_identity(pid: int) -> str | None:
    """Linux incarnation identity for status only; never a licence to send a signal."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return f"{boot}:{stat[19]}"
    except (OSError, IndexError, ValueError):
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lock_path(state_dir: Path) -> Path:
    return state_dir / "queue-dispatcher.lock"


def _lock_held(state_dir: Path) -> bool:
    with _lock_path(state_dir).open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return False


def dispatcher_status(queue: ResourceQueue) -> dict[str, object]:
    with queue._connect() as connection:
        row = connection.execute(
            "SELECT * FROM queue_dispatcher_state WHERE singleton=1"
        ).fetchone()
    result = dict(row) if row is not None else {}
    heartbeat = result.get("heartbeat_at")
    result["running"] = bool(
        heartbeat
        and datetime.fromisoformat(str(heartbeat)) > datetime.now(UTC) - timedelta(seconds=5)
    )
    with queue._connect() as connection:
        control = connection.execute(
            "SELECT * FROM queue_dispatcher_control WHERE singleton=1"
        ).fetchone()
        result["running_jobs"] = connection.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE status='running'"
        ).fetchone()[0]
    result["control"] = dict(control) if control else None
    result["lock_held"] = _lock_held(queue.path.parent)
    result["legacy_or_unknown"] = bool(
        result["lock_held"]
        and (
            not control
            or not control["instance"]
            or control["phase"] == "stopped"
            or not control["process_identity"]
            or control["pid"] != result.get("pid")
            or _process_identity(control["pid"]) != control["process_identity"]
        )
    )
    result["running"] = bool(result["running"] and result["lock_held"])
    result["stop_complete"] = bool(
        control and control["paused"] and not result["lock_held"] and not result["running_jobs"]
    )
    result["limitation"] = (
        "legacy/unknown daemon does not honor drain; no PID is signalled; parent-managed migration required"
        if result["legacy_or_unknown"]
        else None
    )
    return result


def _paused(queue: ResourceQueue) -> bool:
    with queue._connect() as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM queue_dispatcher_control WHERE singleton=1 AND paused=1"
            ).fetchone()
        )


def request_dispatcher_stop(queue: ResourceQueue) -> None:
    """Persist a claim/start barrier. Never signal a PID or revoke resource ownership."""
    with queue._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO queue_dispatcher_control(singleton, paused) VALUES (1, 1) "
            "ON CONFLICT(singleton) DO UPDATE SET paused=1"
        )


def start_dispatcher(config_path: Path, state_dir: Path) -> None:
    queue = ResourceQueue(state_dir)
    with _lock_path(state_dir).open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "dispatcher still owns lock; wait for drain or legacy migration"
            ) from exc
        with queue._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM queue_jobs WHERE status='running'").fetchone():
                raise RuntimeError("running jobs have unknown liveness; preserve and inspect them")
            connection.execute(
                "INSERT INTO queue_dispatcher_control(singleton, paused) VALUES (1, 0) "
                "ON CONFLICT(singleton) DO UPDATE SET paused=0"
            )
    ensure_dispatcher(config_path, state_dir)


def recover_dispatcher_job(
    queue: ResourceQueue, request_id: str, *, execution_stopped: bool, cleanup_confirmed: bool
) -> None:
    """Record operator-proven orphan completion; never terminate or replay its command."""
    if not execution_stopped or not cleanup_confirmed:
        raise ValueError("execution-stopped and cleanup-confirmed are both required")
    with _lock_path(queue.path.parent).open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("dispatcher still owns lock; recovery refused") from exc
        with queue._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = connection.execute(
                "SELECT paused FROM queue_dispatcher_control WHERE singleton=1"
            ).fetchone()
            if not control or not control["paused"]:
                raise ValueError("persist dispatcher stop barrier before recovery")
            row = connection.execute(
                "SELECT q.resource FROM queue_jobs j JOIN queue_requests q ON q.id=j.request_id "
                "WHERE j.request_id=? AND j.status='running'",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("running queue job was not found")
            resource = str(row["resource"])
            block = connection.execute(
                "SELECT request_id FROM queue_resource_blocks WHERE resource=?",
                (resource,),
            ).fetchone()
            if block and block["request_id"] != request_id:
                raise ValueError("resource has a different recovery owner")
            connection.execute(
                "UPDATE queue_jobs SET status='failed', finished_at=?, "
                "error='operator confirmed execution stopped and cleanup' "
                "WHERE request_id=?",
                (_now(), request_id),
            )
            connection.execute(
                "UPDATE queue_requests SET status='released', updated_at=? "
                "WHERE id=? AND status='leased'",
                (_now(), request_id),
            )
            connection.execute(
                "DELETE FROM queue_resource_blocks WHERE request_id=?", (request_id,)
            )
            connection.execute(
                "INSERT OR IGNORE INTO queue_result_notifications(request_id) VALUES (?)",
                (request_id,),
            )
            queue._event(connection, resource, request_id, "recovered")
            queue._promote(connection, resource)


def ensure_dispatcher(config_path: Path, state_dir: Path) -> None:
    """Launch a detached process if no process holds the per-state-dir lock."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _lock_path(state_dir).open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        finally:
            # Releasing before spawn permits competing launchers; the child lock
            # still ensures exactly one dispatcher performs work.
            pass
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    queue = ResourceQueue(state_dir)
    with queue._connect() as connection:
        if connection.execute(
            "SELECT 1 FROM queue_dispatcher_control WHERE singleton=1 AND paused=1"
        ).fetchone():
            return
        if connection.execute("SELECT 1 FROM queue_jobs WHERE status='running'").fetchone():
            raise RuntimeError("running jobs have unknown liveness; automatic restart refused")
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "flybridge_cli.main",
            "--config",
            str(config_path),
            "queue",
            "dispatcher",
            "serve",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    queue = ResourceQueue(state_dir)
    for _ in range(30):
        status = dispatcher_status(queue)
        if status.get("control") and status["control"]["paused"]:
            return
        if _lock_held(state_dir) and status["running"]:
            return
        if child.poll() is not None and not _lock_held(state_dir):
            break
        time.sleep(0.1)
    raise RuntimeError("queue dispatcher did not start; inspect queue dispatcher status")


class QueueDispatcher:
    def __init__(self, config) -> None:
        self.config = config
        self.queue = ResourceQueue(config.state_dir)
        self.store = WorkflowStore(config.state_dir)
        self.client = OrcaClient(config.orca_executable)

    def _heartbeat(self, error: str | None = None) -> None:
        with self.queue._connect() as connection:
            connection.execute(
                "INSERT INTO queue_dispatcher_state VALUES (1, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET pid=excluded.pid, "
                "heartbeat_at=excluded.heartbeat_at, last_error=excluded.last_error",
                (os.getpid(), _now(), error),
            )

    def _owner(self, owner_id: str):
        try:
            record = self.store.get(owner_id)
        except ValueError:
            return None, "invalid"
        if (
            record.status != WorkflowStatus.RUNNING
            or not record.adapter_reference
            or not record.terminal_handle
        ):
            return record, "invalid"
        try:
            state = self.client.agent_owner_state(record.adapter_reference, record.terminal_handle)
        except (OSError, RuntimeError, ValueError, AttributeError):
            return record, "unknown"
        return record, state if state in {"valid", "invalid"} else "unknown"

    def _send(self, owner_id: str, prompt: str) -> str | None:
        record, state = self._owner(owner_id)
        if state != "valid" or record is None:
            return f"delivery_blocked: owner agent {state}; no prompt input written"
        try:
            self.client.send_prompt(record.terminal_handle, prompt)
        except (OSError, RuntimeError, ValueError) as exc:
            return str(exc)
        return None

    def _deliver_grants(self) -> None:
        for item in self.queue.delivery_candidates():
            request_id = str(item["request_id"])
            prompt = render_lease_grant_prompt(
                resource=str(item["resource"]),
                request_id=request_id,
                lease_id=request_id,
                workflow_id=str(item["owner"]),
                config_path=self.config.path,
            )
            error = self._send(str(item["owner"]), prompt)
            if error is None:
                self.queue.mark_grant_delivered(request_id)
            else:
                self.queue.note_grant_error(request_id, error)

    def _deliver_results(self) -> None:
        for item in self.queue.result_candidates():
            request_id = str(item["request_id"])
            acknowledge = shlex.join(
                [
                    "flybridge",
                    "--config",
                    str(self.config.path),
                    "queue",
                    "ack-result",
                    request_id,
                    "--owner",
                    str(item["owner"]),
                ]
            )
            prompt = (
                f"Flybridge queue job {request_id} finished with status {item['status']}. "
                f"Command exit: {item['command_exit_code']}; cleanup check exit: "
                f"{item['check_exit_code']}. If recovery_required, the resource remains "
                "blocked until external cleanup is confirmed. Acknowledge with "
                f"{acknowledge}."
            )
            error = self._send(str(item["owner"]), prompt)
            self.queue.note_result_delivery(request_id, error)

    def _check_dead_owners(self) -> None:
        for item in self.queue.active_requests(
            attention_after_seconds=self.config.queue_lease_timeout_seconds
        ):
            record, state = self._owner(str(item["owner"]))
            if state != "invalid":
                continue
            request_id = str(item["request_id"])
            if item["job_status"] != "running":
                try:
                    self.queue.cancel(request_id)
                except ValueError:
                    continue
            if record is not None and record.status == WorkflowStatus.RUNNING:
                try:
                    self.store.transition(
                        record.id,
                        WorkflowStatus.FAILED,
                        error="resource owner terminal unavailable",
                    )
                except ValueError:
                    pass

    def _execute(self, item: dict[str, object]) -> None:
        request_id = str(item["request_id"])
        argv = json.loads(str(item["argv_json"]))
        cwd = str(item["worktree_path"])
        env = os.environ.copy()
        env.update(
            {
                "FLYBRIDGE_QUEUE_RESOURCE": str(item["resource"]),
                "FLYBRIDGE_QUEUE_OWNER": str(item["owner"]),
                "FLYBRIDGE_QUEUE_REQUEST_ID": request_id,
            }
        )
        command_exit = 127
        check_exit = None
        error = None
        try:
            completed = subprocess.run(argv, cwd=cwd, env=env, check=False, start_new_session=True)
            command_exit = completed.returncode
        except (OSError, ValueError) as exc:
            error = f"command launch failed: {exc}"
        try:
            completed = subprocess.run(
                [str(item["cleanup_check"])], cwd=cwd, env=env, check=False, start_new_session=True
            )
            check_exit = completed.returncode
        except (OSError, ValueError) as exc:
            error = f"cleanup check failed to launch: {exc}"
        # A launch failure is safe to release only when the explicit check proves it.
        if check_exit == 0:
            error = None
        self.queue.finish_job(
            request_id, command_exit_code=command_exit, check_exit_code=check_exit, error=error
        )

    def serve(self) -> int:
        with _lock_path(self.config.state_dir).open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            instance = str(uuid.uuid4())
            with self.queue._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                control = connection.execute(
                    "SELECT paused FROM queue_dispatcher_control WHERE singleton=1"
                ).fetchone()
                if control and control["paused"]:
                    return 0
                if connection.execute("SELECT 1 FROM queue_jobs WHERE status='running'").fetchone():
                    return 1
                connection.execute(
                    "INSERT INTO queue_dispatcher_control(singleton, paused, instance, pid, "
                    "process_identity, phase) VALUES (1, 0, ?, ?, ?, 'serving') "
                    "ON CONFLICT(singleton) DO UPDATE SET instance=excluded.instance, "
                    "pid=excluded.pid, process_identity=excluded.process_identity, phase='serving'",
                    (instance, os.getpid(), _process_identity(os.getpid())),
                )
            stopping = False

            def drain_signal(_signum, _frame):
                nonlocal stopping
                stopping = True

            with (
                _drain_signals(drain_signal, self.queue, instance),
                ThreadPoolExecutor(max_workers=8) as pool,
            ):
                running = {}
                while True:
                    try:
                        self._heartbeat()
                        for request_id, future in list(running.items()):
                            if future.done():
                                try:
                                    future.result()
                                except Exception as exc:  # noqa: BLE001 - unknown job errors require recovery
                                    self.queue.abandon_job(
                                        request_id, f"job executor failed: {exc}"
                                    )
                                del running[request_id]
                        if stopping:
                            request_dispatcher_stop(self.queue)
                        with self.queue._connect() as connection:
                            paused = connection.execute(
                                "SELECT paused FROM queue_dispatcher_control WHERE singleton=1"
                            ).fetchone()[0]
                            if paused:
                                connection.execute(
                                    "UPDATE queue_dispatcher_control SET phase=? WHERE instance=?",
                                    ("draining" if running else "stopped", instance),
                                )
                        if paused:
                            if not running:
                                return 0
                            time.sleep(0.1)
                            continue
                        self._check_dead_owners()
                        for item in self.queue.queued_jobs():
                            if len(running) >= 8:
                                break
                            if stopping:
                                request_dispatcher_stop(self.queue)
                            request_id = str(item["request_id"])
                            if request_id not in running:
                                claimed = self.queue.claim_job(request_id)
                                if claimed is not None:
                                    running[request_id] = pool.submit(self._execute, claimed)
                        if stopping:
                            request_dispatcher_stop(self.queue)
                        if not _paused(self.queue):
                            self._deliver_grants()
                        if not _paused(self.queue):
                            self._deliver_results()
                    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                        self._heartbeat(str(exc))
                    time.sleep(1)
