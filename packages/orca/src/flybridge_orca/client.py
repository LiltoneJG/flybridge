from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flybridge_core import WorkflowMode, WorkflowStatus

TIMEOUT_NAME_LOOKUP_ATTEMPTS = 3
TUI_IDLE_TIMEOUTS_MS = (60_000, 120_000)
SEND_WAIT_SECONDS = 10
SEND_TIMEOUT_SECONDS = 45


class OrcaError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", stdout: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.stdout = stdout


class OrcaStartError(OrcaError):
    """A create response failed after Orca may have allocated a worktree."""

    def __init__(
        self,
        message: str,
        *,
        worktree_id: str = "",
        worktree_path: str = "",
        terminal_handle: str = "",
    ) -> None:
        super().__init__(message)
        self.worktree_id = worktree_id
        self.worktree_path = worktree_path
        self.terminal_handle = terminal_handle


class OrcaTimeoutError(OrcaError):
    """A timed-out call that carries no ownership evidence.

    Possible orphan details are deliberately not named ``worktree_id`` or
    ``worktree_path``: compensation reads those names to force-remove a worktree,
    and an unverified observation must never reach that path.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        possible_orphan_id: str = "",
        possible_orphan_path: str = "",
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.possible_orphan_id = possible_orphan_id
        self.possible_orphan_path = possible_orphan_path


@dataclass(frozen=True)
class StartedWorkflow:
    name: str
    worktree_id: str
    worktree: str
    terminal: str
    mode: WorkflowMode


class OrcaClient:
    """Adapter for the version-matched Orca CLI JSON protocol."""

    def __init__(
        self,
        executable: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.runner = runner

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        try:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass

    @classmethod
    def _reap_process(
        cls, process: subprocess.Popen[str], *, timeout: float = 5
    ) -> tuple[str, str]:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return stdout or "", stderr or ""
        except (subprocess.TimeoutExpired, OSError) as exc:
            stdout = getattr(exc, "stdout", "") or ""
            stderr = getattr(exc, "stderr", "") or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            cls._stop_process(process)
            try:
                final_stdout, final_stderr = process.communicate(timeout=timeout)
                return final_stdout or stdout, final_stderr or stderr
            except (subprocess.TimeoutExpired, OSError) as final_exc:
                final_stdout = getattr(final_exc, "stdout", "") or stdout
                final_stderr = getattr(final_exc, "stderr", "") or stderr
                if isinstance(final_stdout, bytes):
                    final_stdout = final_stdout.decode(errors="replace")
                if isinstance(final_stderr, bytes):
                    final_stderr = final_stderr.decode(errors="replace")
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                try:
                    process.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, OSError):
                    cls._stop_process(process)
                    process.wait()
                return final_stdout, final_stderr

    def _run(
        self, arguments: list[str], *, cwd: Path | None = None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if self.runner is not subprocess.run:
            return self.runner(
                arguments,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as exc:
            partial_stdout = getattr(exc, "stdout", "") or ""
            partial_stderr = getattr(exc, "stderr", "") or ""
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode(errors="replace")
            if isinstance(partial_stderr, bytes):
                partial_stderr = partial_stderr.decode(errors="replace")
            self._stop_process(process)
            stdout, stderr = self._reap_process(process)
            exc.stdout = stdout or partial_stdout
            exc.stderr = stderr or partial_stderr
            raise
        return subprocess.CompletedProcess(
            arguments, process.returncode, stdout or "", stderr or ""
        )

    def _json(
        self, arguments: list[str], *, cwd: Path | None = None, timeout: float = 60
    ) -> dict[str, Any]:
        try:
            result = self._run(
                [self.executable, *arguments, "--json"],
                cwd=cwd,
                timeout=timeout,
            )
        except OSError as exc:
            raise OrcaError(f"unable to run Orca CLI: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
            raise OrcaTimeoutError("Orca CLI timed out", stdout=stdout) from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            if result.returncode:
                raise OrcaError(
                    result.stderr.strip() or result.stdout.strip() or "Orca CLI failed"
                ) from exc
            raise OrcaError("Orca returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise OrcaError("Orca returned an unexpected response")
        if payload.get("ok") is False:
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                message = str(error.get("message") or code or "Orca operation failed")
                raise OrcaError(message, code=code)
            detail = error or payload.get("message") or "Orca operation failed"
            raise OrcaError(str(detail))
        if result.returncode:
            raise OrcaError(
                result.stderr.strip() or result.stdout.strip() or "Orca CLI failed",
                stdout=result.stdout,
            )
        value = payload.get("result", payload)
        if not isinstance(value, dict):
            raise OrcaError("Orca returned an unexpected result")
        return value

    @staticmethod
    def _exact_partial_allocation(stdout: str, name: str) -> tuple[str, str, str] | None:
        """Read ownership evidence only from the timed-out create's own JSON receipt.

        Only this receipt proves that Flybridge's own create call allocated the
        worktree, so it must identify the requested name exactly and carry a
        non-empty worktree ID before compensation may act on it.
        """
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("ok") is False:
            return None
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return None
        worktree = result.get("worktree")
        if not isinstance(worktree, dict):
            return None
        worktree_id = worktree.get("id")
        if not isinstance(worktree_id, str) or not worktree_id:
            return None
        returned_name = worktree.get("name")
        if not isinstance(returned_name, str) or returned_name != name:
            return None
        worktree_path = worktree.get("path")
        terminal = result.get("startupTerminal")
        handle = terminal.get("handle") if isinstance(terminal, dict) else None
        return (
            worktree_id,
            worktree_path if isinstance(worktree_path, str) else "",
            handle if isinstance(handle, str) else "",
        )

    def _worktree_by_name(self, repository: Path, name: str) -> dict[str, Any] | None:
        try:
            result = self._json(
                ["worktree", "show", "--worktree", f"name:{name}"],
                cwd=repository,
            )
        except OrcaError as exc:
            if self._is_missing_selector(exc):
                return None
            raise
        self._worktree(result)
        return result

    def _unowned_timeout(
        self, exc: OrcaTimeoutError, repository: Path, name: str
    ) -> OrcaTimeoutError:
        """Describe a possible orphan allocation without claiming ownership of it.

        The requested name can be held by a worktree any other actor created, so
        this bounded lookup only enriches the diagnosis operators act on.
        """
        candidate_id = candidate_path = ""
        lookup_error = ""
        for _attempt in range(TIMEOUT_NAME_LOOKUP_ATTEMPTS):
            try:
                candidate = self._worktree_by_name(repository, name)
            except OrcaError as lookup_exc:
                lookup_error = str(lookup_exc)
                break
            if candidate is not None:
                candidate_id, candidate_path = self._worktree(candidate)
                break
        detail = "Orca worktree creation timed out without an ownership receipt"
        if candidate_id:
            detail += (
                f"; a worktree named {name} exists (id {candidate_id}, path {candidate_path}) "
                "but Flybridge cannot prove it created that worktree, so it was left in place"
            )
        elif lookup_error:
            detail += f"; the possible orphan lookup for {name} failed: {lookup_error}"
        else:
            detail += f"; no worktree named {name} was visible afterwards"
        return OrcaTimeoutError(
            detail,
            stdout=exc.stdout,
            possible_orphan_id=candidate_id,
            possible_orphan_path=candidate_path,
        )

    def verify(self) -> dict[str, Any]:
        """Validate the live Orca runtime and version-matched orchestration guide."""
        status = self._json(["status"])
        runtime = status.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("reachable") is not True:
            raise OrcaError("Orca runtime is not reachable")
        guide = self._json(["skills", "get", "orchestration", "--full"])
        if guide.get("name") != "orchestration" or not guide.get("full"):
            raise OrcaError("Orca orchestration skill is unavailable or not version-matched")
        return {"app_version": runtime.get("appVersion", ""), "skill": guide.get("name")}

    @staticmethod
    def _is_missing_selector(error: OrcaError) -> bool:
        return error.code in {
            "selector_not_found",
            "terminal_not_found",
            "terminal_handle_stale",
            "terminal_already_closed",
        }

    @staticmethod
    def _required_object(result: dict[str, Any], key: str) -> dict[str, Any]:
        value = result.get(key)
        if not isinstance(value, dict):
            raise OrcaError(f"Orca result is missing {key}")
        return value

    @classmethod
    def _worktree(cls, result: dict[str, Any]) -> tuple[str, str]:
        worktree = cls._required_object(result, "worktree")
        worktree_id = worktree.get("id")
        worktree_path = worktree.get("path")
        if not isinstance(worktree_id, str) or not worktree_id:
            raise OrcaError("Orca worktree is missing id")
        if not isinstance(worktree_path, str) or not worktree_path:
            raise OrcaError("Orca worktree is missing path")
        return worktree_id, worktree_path

    @classmethod
    def _handle(cls, result: dict[str, Any], key: str) -> str:
        terminal = cls._required_object(result, key)
        handle = terminal.get("handle")
        if not isinstance(handle, str) or not handle:
            raise OrcaError(f"Orca {key} is missing handle")
        return handle

    def start(
        self,
        repository: Path,
        name: str,
        mode: WorkflowMode,
        agent: str,
        prompt: str,
        *,
        parent_worktree_id: str | None = None,
        base_branch: str | None = None,
    ) -> StartedWorkflow:
        arguments = [
            "worktree",
            "create",
            "--repo",
            f"path:{repository.resolve()}",
            "--name",
            name,
            "--setup",
            "skip",
            "--prompt",
            prompt,
            "--agent",
            agent,
        ]
        if parent_worktree_id:
            arguments.extend(["--parent-worktree", f"id:{parent_worktree_id}"])
        else:
            arguments.append("--no-parent")
        if base_branch:
            arguments.extend(["--base-branch", base_branch])
        if self._worktree_by_name(repository, name) is not None:
            raise OrcaError(f"Orca worktree name already exists: {name}")
        try:
            result = self._json(arguments)
        except OrcaTimeoutError as exc:
            allocation = self._exact_partial_allocation(exc.stdout, name)
            if allocation is None:
                raise self._unowned_timeout(exc, repository, name) from exc
            worktree_id, worktree, terminal = allocation
            raise OrcaStartError(
                "Orca worktree creation timed out after allocation",
                worktree_id=worktree_id,
                worktree_path=worktree,
                terminal_handle=terminal,
            ) from exc
        except BaseException as exc:
            if isinstance(exc, GeneratorExit):
                raise
            allocation = self._exact_partial_allocation(str(getattr(exc, "stdout", "") or ""), name)
            if allocation is not None:
                worktree_id, worktree, terminal = allocation
                exc.worktree_id = worktree_id
                exc.worktree_path = worktree
                exc.terminal_handle = terminal
            raise
        worktree_id, worktree = self._worktree(result)
        try:
            terminal = self._handle(result, "startupTerminal")
        except OrcaError as exc:
            raise OrcaStartError(
                "Orca did not return an owned startup terminal handle",
                worktree_id=worktree_id,
                worktree_path=worktree,
            ) from exc
        return StartedWorkflow(name, worktree_id, worktree, terminal, mode)

    def current_branch(self, worktree_path: str) -> str:
        try:
            result = self._run(
                ["git", "-C", worktree_path, "branch", "--show-current"],
                timeout=60,
            )
        except OSError as exc:
            raise OrcaError(f"unable to inspect parent worktree branch: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("parent worktree branch inspection timed out") from exc
        branch = result.stdout.strip() if result.returncode == 0 else ""
        if not branch:
            raise OrcaError("parent worktree has no checked-out branch")
        return branch

    def uncommitted_changes(self, worktree_path: str) -> tuple[str, ...]:
        """Report the changes a branch-based child worktree would not inherit."""
        try:
            result = self._run(
                ["git", "-C", worktree_path, "status", "--porcelain"],
                timeout=60,
            )
        except OSError as exc:
            raise OrcaError(f"unable to inspect parent worktree changes: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("parent worktree change inspection timed out") from exc
        if result.returncode:
            raise OrcaError(result.stderr.strip() or "parent worktree change inspection failed")
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def prepare_repository(self, repository: Path) -> None:
        """Run the defensive submodule update only when the repository declares submodules."""
        if not (repository / ".gitmodules").is_file():
            return
        try:
            result = self._run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                    "--checkout",
                ],
                timeout=120,
            )
        except OSError as exc:
            raise OrcaError(f"unable to prepare repository submodules: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("repository submodule preparation timed out") from exc
        if result.returncode:
            raise OrcaError(result.stderr.strip() or "repository submodule preparation failed")

    def register_repository(self, repository: Path) -> dict[str, Any]:
        """Idempotently register a target repository with Orca before creating a worktree."""
        return self._json(["repo", "add", "--path", str(repository.resolve())])

    def set_lifecycle(
        self, worktree_id: str, state: WorkflowStatus, detail: str | None = None
    ) -> dict[str, Any]:
        comments = {
            WorkflowStatus.RUNNING: "Flybridge workflow is running.",
            WorkflowStatus.COMPLETED: "Flybridge workflow completed.",
            WorkflowStatus.FAILED: "Flybridge workflow failed.",
            WorkflowStatus.CANCELLED: "Flybridge workflow cancelled.",
        }
        if state not in comments:
            raise ValueError(f"unknown workflow lifecycle state: {state}")
        arguments = [
            "worktree",
            "set",
            "--worktree",
            f"id:{worktree_id}",
            "--comment",
            detail or comments[state],
        ]
        if state == WorkflowStatus.RUNNING:
            arguments.extend(["--workspace-status", "in-progress"])
        else:
            # Orca's default board models active versus inactive workspaces;
            # the comment and Flybridge record retain the terminal outcome.
            arguments.extend(["--workspace-status", "completed"])
        return self._json(arguments)

    def create_observer(self, worktree_id: str, command: str) -> str:
        result = self._json(
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                "FLYBRIDGE QUEUE",
                "--command",
                command,
            ]
        )
        return self._handle(result, "terminal")

    @staticmethod
    def _signal(result: dict[str, Any], container: str, key: str) -> bool | None:
        container_value = result.get(container)
        if not isinstance(container_value, dict):
            return None
        value = container_value.get(key)
        return value if isinstance(value, bool) else None

    def verify_worktree(self, worktree_id: str, worktree_path: str) -> None:
        result = self._json(["worktree", "show", "--worktree", f"id:{worktree_id}"])
        returned_id, returned_path = self._worktree(result)
        if returned_id != worktree_id:
            raise OrcaError("Orca returned a different worktree than the persisted reference")
        if returned_path != worktree_path:
            raise OrcaError("Orca worktree path does not match persisted ownership")

    def terminal_is_valid(self, worktree_id: str, terminal_handle: str | None) -> bool:
        if not terminal_handle:
            return False
        try:
            result = self._json(["terminal", "show", "--terminal", terminal_handle])
        except OrcaError as exc:
            if self._is_missing_selector(exc):
                return False
            raise
        returned_handle = self._handle(result, "terminal")
        if returned_handle != terminal_handle:
            raise OrcaError("Orca returned a different terminal than the persisted handle")
        return True

    def create_agent_terminal(self, worktree_id: str, agent: str) -> str:
        result = self._json(
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                "FLYBRIDGE AGENT",
                "--command",
                agent,
            ]
        )
        return self._handle(result, "terminal")

    def wait_for_agent(self, terminal_handle: str) -> None:
        for timeout_ms in TUI_IDLE_TIMEOUTS_MS:
            result = self._json(
                [
                    "terminal",
                    "wait",
                    "--terminal",
                    terminal_handle,
                    "--for",
                    "tui-idle",
                    "--timeout-ms",
                    str(timeout_ms),
                ],
                timeout=timeout_ms / 1000 + 5,
            )
            if self._signal(result, "wait", "satisfied") is True:
                return
        raise OrcaTimeoutError("replacement agent terminal did not become TUI-idle")

    def send_prompt(self, terminal_handle: str, prompt: str) -> None:
        result = self._json(
            [
                "terminal",
                "send",
                "--terminal",
                terminal_handle,
                "--text",
                prompt,
                "--enter",
                "--wait-submit",
                str(SEND_WAIT_SECONDS),
            ],
            timeout=SEND_TIMEOUT_SECONDS,
        )
        if self._signal(result, "send", "accepted") is not True:
            raise OrcaError("Orca did not accept the workflow resume prompt")

    def close_terminals(
        self, worktree_id: str, terminal_handle: str | None = None
    ) -> dict[str, Any]:
        """Close only the terminal persisted as Flybridge-owned.

        A worktree can contain user terminals, so falling back to ``--all`` would
        violate ownership boundaries. Callers without a recorded handle must
        reconcile the record manually instead of closing unrelated terminals.
        """
        if not terminal_handle:
            raise OrcaError("cannot close workflow terminals without an owned terminal handle")
        try:
            return self._json(["terminal", "close", "--terminal", terminal_handle])
        except OrcaError as exc:
            if self._is_missing_selector(exc):
                return {"already_closed": True}
            raise

    def remove_worktree(self, worktree_id: str) -> dict[str, Any]:
        """Remove only an explicitly persisted Flybridge-owned stale worktree."""
        return self._json(["worktree", "rm", "--worktree", f"id:{worktree_id}", "--force"])
