"""One process per state directory delivers grants and runs queued commands."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flybridge_application.prompts import render_lease_grant_prompt
from flybridge_core import ResourceQueue, WorkflowStatus, WorkflowStore
from flybridge_orca import OrcaClient


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
    return result


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
        if _lock_held(state_dir) and dispatcher_status(queue)["running"]:
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
            valid = self.client.terminal_is_valid(record.adapter_reference, record.terminal_handle)
        except (OSError, RuntimeError, ValueError):
            return record, "unknown"
        return record, "valid" if valid else "invalid"

    def _send(self, owner_id: str, prompt: str) -> str | None:
        record, state = self._owner(owner_id)
        if state != "valid" or record is None:
            return f"owner terminal {state}"
        try:
            self.client.wait_for_agent(record.terminal_handle)
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
            self.queue.abandon_running_jobs()
            with ThreadPoolExecutor(max_workers=8) as pool:
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
                        self._check_dead_owners()
                        for item in self.queue.queued_jobs():
                            if len(running) >= 8:
                                break
                            request_id = str(item["request_id"])
                            if request_id not in running:
                                claimed = self.queue.claim_job(request_id)
                                if claimed is not None:
                                    running[request_id] = pool.submit(self._execute, claimed)
                        self._deliver_grants()
                        self._deliver_results()
                    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                        self._heartbeat(str(exc))
                    time.sleep(1)
