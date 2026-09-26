from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flybridge_core import (
    AgentLaunchPreset,
    WorkflowMode,
    WorkflowRole,
    WorkflowStatus,
    merge_lifecycle_comment,
)

from .resolve import (
    UnresolvedAgentError,
    resolve_launch_command,
    resolve_prompt_command,
    uses_builtin_tui,
)

TIMEOUT_NAME_LOOKUP_ATTEMPTS = 3
TUI_IDLE_TIMEOUTS_MS = (60_000, 120_000)
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
    owns_worktree: bool = True


@dataclass(frozen=True)
class ListedWorktree:
    """Normalized `worktree ps` row. GitHub hint fields are Orca metadata only."""

    worktree_id: str
    path: str
    name: str
    workspace_status: str
    comment: str
    branch: str
    linked_issue: int | None
    linked_pull_request: tuple[int, str] | None
    project_id: str = ""


class OrcaClient:
    """Adapter for the version-matched Orca CLI JSON protocol."""

    def __init__(
        self,
        executable: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        launch_presets: dict[str, AgentLaunchPreset] | None = None,
    ) -> None:
        self.executable = executable
        self.runner = runner
        self.which = which
        self.launch_presets = launch_presets

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
        self,
        arguments: list[str],
        *,
        cwd: Path | None = None,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.runner is not subprocess.run:
            return self.runner(
                arguments,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
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
        self, arguments: list[str], *, cwd: Path | None = None, timeout: float = 180
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

    def list_worktrees(self) -> tuple[tuple[ListedWorktree, ...], bool]:
        """Return every Orca worktree from `worktree ps` and whether the list was truncated."""
        result = self._json(["worktree", "ps"])
        nodes = result.get("worktrees")
        if not isinstance(nodes, list):
            raise OrcaError("Orca worktree list is missing worktrees")
        truncated = result.get("truncated") is True
        worktrees: list[ListedWorktree] = []
        for node in nodes:
            worktrees.append(self._listed_worktree(node))
        return tuple(worktrees), truncated

    @staticmethod
    def _listed_worktree(node: object) -> ListedWorktree:
        if not isinstance(node, dict):
            raise OrcaError("Orca worktree list contained an unexpected item")
        worktree_id = node.get("worktreeId") or node.get("id")
        path = node.get("path")
        name = node.get("displayName") or node.get("name") or ""
        workspace_status = node.get("workspaceStatus") or ""
        comment = node.get("comment") or ""
        branch = node.get("branch") or ""
        if not isinstance(worktree_id, str) or not worktree_id:
            raise OrcaError("Orca worktree list item is missing id")
        if not isinstance(path, str) or not path:
            raise OrcaError("Orca worktree list item is missing path")
        if not isinstance(name, str):
            raise OrcaError("Orca worktree list item has an invalid name")
        if not isinstance(workspace_status, str):
            raise OrcaError("Orca worktree list item has an invalid workspace status")
        if not isinstance(comment, str):
            raise OrcaError("Orca worktree list item has an invalid comment")
        if not isinstance(branch, str):
            raise OrcaError("Orca worktree list item has an invalid branch")
        linked_issue = node.get("linkedIssue")
        if linked_issue is not None and (
            not isinstance(linked_issue, int) or isinstance(linked_issue, bool)
        ):
            raise OrcaError("Orca worktree list item has an invalid linked issue")
        linked_pr = node.get("linkedPR")
        pull_request: tuple[int, str] | None = None
        if linked_pr is not None:
            if not isinstance(linked_pr, dict):
                raise OrcaError("Orca worktree list item has an invalid linked pull request")
            number = linked_pr.get("number")
            state = linked_pr.get("state") or ""
            invalid_number = not isinstance(number, int) or isinstance(number, bool)
            if invalid_number or not isinstance(state, str):
                raise OrcaError("Orca worktree list item has an invalid linked pull request")
            pull_request = (number, state)
        project_id = node.get("projectId") or ""
        if not isinstance(project_id, str):
            raise OrcaError("Orca worktree list item has an invalid project id")
        return ListedWorktree(
            worktree_id,
            path,
            name,
            workspace_status,
            comment,
            branch,
            linked_issue,
            pull_request,
            project_id,
        )

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

    @staticmethod
    def _repository_id(worktree_id: str) -> str:
        repository_id, separator, _path = worktree_id.rpartition("::")
        if not separator or not repository_id:
            raise OrcaError("Orca worktree ID is missing its canonical repository ID")
        return repository_id

    @staticmethod
    def _github_repository(remote: str) -> str:
        text = remote.strip()
        if text.startswith("git@github.com:"):
            path = text.removeprefix("git@github.com:")
        else:
            parsed = urlparse(text)
            if parsed.hostname is None or parsed.hostname.lower() != "github.com":
                raise OrcaError("implementation origin is not a GitHub repository")
            path = parsed.path.lstrip("/")
        repository = path.removesuffix(".git").strip("/")
        if len(repository.split("/")) != 2 or any(not part for part in repository.split("/")):
            raise OrcaError("implementation origin has no GitHub nameWithOwner")
        return repository

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
        role: WorkflowRole | None = None,
        parent_worktree_id: str | None = None,
        base_branch: str | None = None,
        comment: str | None = None,
        github_issue_number: int | None = None,
        model: str | None = None,
    ) -> StartedWorkflow:
        repository_selector = f"path:{repository.resolve()}"
        if parent_worktree_id:
            repository_selector = f"id:{self._repository_id(parent_worktree_id)}"
        custom_agent_command = not uses_builtin_tui(agent, model, presets=self.launch_presets)
        arguments = [
            "worktree",
            "create",
            "--repo",
            repository_selector,
            "--name",
            name,
            "--setup",
            "skip",
        ]
        if not custom_agent_command:
            arguments.extend(["--prompt", prompt, "--agent", agent])
        if parent_worktree_id:
            arguments.extend(["--parent-worktree", f"id:{parent_worktree_id}"])
        else:
            arguments.append("--no-parent")
        if base_branch:
            arguments.extend(["--base-branch", base_branch])
        if comment:
            arguments.extend(["--comment", comment])
        if github_issue_number is not None:
            arguments.extend(["--issue", str(github_issue_number)])
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
        if custom_agent_command:
            bootstrap_terminal = ""
            try:
                if result.get("startupTerminal") is not None:
                    bootstrap_terminal = self._handle(result, "startupTerminal")
                    self.close_terminals(worktree_id, bootstrap_terminal)
                terminal = self.create_new_agent_terminal(
                    worktree_id,
                    worktree,
                    agent,
                    prompt,
                    title=self._agent_terminal_title(name, role, mode),
                    model=model,
                )
            except BaseException as exc:
                if isinstance(exc, GeneratorExit):
                    raise
                raise OrcaStartError(
                    f"custom agent startup failed after worktree allocation: {exc}",
                    worktree_id=worktree_id,
                    worktree_path=worktree,
                    terminal_handle=bootstrap_terminal,
                ) from exc
            return StartedWorkflow(name, worktree_id, worktree, terminal, mode)
        try:
            terminal = self._handle(result, "startupTerminal")
        except OrcaError as exc:
            raise OrcaStartError(
                "Orca did not return an owned startup terminal handle",
                worktree_id=worktree_id,
                worktree_path=worktree,
            ) from exc
        self.rename_terminal(terminal, self._agent_terminal_title(name, role, mode))
        return StartedWorkflow(name, worktree_id, worktree, terminal, mode)

    def attach(
        self,
        repository: Path,
        name: str,
        mode: WorkflowMode,
        agent: str,
        prompt: str,
        *,
        role: WorkflowRole | None = None,
        model: str | None = None,
    ) -> StartedWorkflow:
        """Bind a workflow to an existing Orca worktree without creating or deleting it."""
        worktree_id, worktree = self.existing_worktree(repository)
        terminal = self.create_agent_terminal(
            worktree_id,
            agent,
            title=self._agent_terminal_title(name, role, mode),
            model=model,
        )
        try:
            self.wait_for_agent(terminal)
            self.send_prompt(terminal, prompt)
        except BaseException:
            try:
                self.close_terminals(worktree_id, terminal)
            except (OrcaError, OSError, RuntimeError):
                pass
            raise
        return StartedWorkflow(name, worktree_id, worktree, terminal, mode, owns_worktree=False)

    def attach_new_agent(
        self,
        repository: Path,
        name: str,
        mode: WorkflowMode,
        agent: str,
        prompt: str,
        *,
        role: WorkflowRole | None = None,
        model: str | None = None,
    ) -> StartedWorkflow:
        """Attach without taking ownership, launching a new agent with an initial prompt."""
        worktree_id, worktree = self.existing_worktree(repository)
        terminal = self.create_new_agent_terminal(
            worktree_id,
            worktree,
            agent,
            prompt,
            title=self._agent_terminal_title(name, role, mode),
            model=model,
        )
        return StartedWorkflow(name, worktree_id, worktree, terminal, mode, owns_worktree=False)

    def existing_worktree(self, repository: Path) -> tuple[str, str]:
        """Resolve the exact Orca worktree already checked out at a path."""
        result = self._json(["worktree", "show", "--worktree", f"path:{repository.resolve()}"])
        worktree_id, worktree = self._worktree(result)
        if Path(worktree).resolve() != repository.resolve():
            raise OrcaError("Orca worktree path does not match the requested repository")
        return worktree_id, worktree

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

    def implementation_identity(self, worktree_id: str, worktree_path: str) -> tuple[str, str, str]:
        """Return GitHub, Orca runtime, and exact starting Git identities."""
        runtime_repository_id = self._repository_id(worktree_id)
        try:
            remote = self._run(
                ["git", "-C", worktree_path, "remote", "get-url", "origin"],
                timeout=60,
            )
            result = self._run(
                ["git", "-C", worktree_path, "rev-parse", "--verify", "HEAD"],
                timeout=60,
            )
        except OSError as exc:
            raise OrcaError(f"unable to inspect implementation start SHA: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("implementation start SHA inspection timed out") from exc
        if remote.returncode:
            raise OrcaError(remote.stderr.strip() or "implementation origin cannot be inspected")
        implementation_repository = self._github_repository(remote.stdout)
        start_sha = result.stdout.strip() if result.returncode == 0 else ""
        if not start_sha:
            raise OrcaError(
                result.stderr.strip() or "implementation worktree has no verifiable start SHA"
            )
        return implementation_repository, runtime_repository_id, start_sha

    def implementation_worktree_present(self, worktree_path: str) -> bool:
        """Return whether the persisted path is still a git worktree."""
        try:
            result = self._run(
                ["git", "-C", worktree_path, "rev-parse", "--is-inside-work-tree"],
                timeout=60,
            )
        except OSError:
            return False
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def verify_implementation_identity(
        self,
        worktree_id: str,
        worktree_path: str,
        implementation_repository: str,
        runtime_repository_id: str,
        start_sha: str,
    ) -> None:
        """Fail closed if a persisted worktree no longer has its recorded Git identity."""
        if self._repository_id(worktree_id) != runtime_repository_id:
            raise OrcaError("runtime repository does not match persisted workflow identity")
        try:
            remote = self._run(
                ["git", "-C", worktree_path, "remote", "get-url", "origin"],
                timeout=60,
            )
            result = self._run(
                ["git", "-C", worktree_path, "merge-base", "--is-ancestor", start_sha, "HEAD"],
                timeout=60,
            )
        except OSError as exc:
            raise OrcaError(f"unable to verify implementation start SHA: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("implementation start SHA verification timed out") from exc
        if remote.returncode or self._github_repository(remote.stdout) != implementation_repository:
            raise OrcaError("implementation repository does not match persisted workflow identity")
        if result.returncode:
            raise OrcaError(
                "implementation HEAD does not descend from the persisted workflow start SHA"
            )

    def verify_pristine_start(self, worktree_path: str, start_sha: str) -> None:
        """Require the exact starting commit and a clean implementation worktree."""
        try:
            head = self._run(
                ["git", "-C", worktree_path, "rev-parse", "--verify", "HEAD"], timeout=60
            )
            status_result = self._run(
                ["git", "-C", worktree_path, "status", "--porcelain"], timeout=60
            )
        except OSError as exc:
            raise OrcaError(f"unable to verify pristine implementation worktree: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("pristine implementation verification timed out") from exc
        if head.returncode or head.stdout.strip() != start_sha:
            raise OrcaError("implementation HEAD does not equal the persisted start SHA")
        if status_result.returncode or status_result.stdout.strip():
            raise OrcaError("implementation worktree is not clean")

    def verify_worker_ready(self, worktree_path: str, start_sha: str) -> None:
        """Require committed implementation work beyond the starting SHA and a clean tree."""
        try:
            head = self._run(
                ["git", "-C", worktree_path, "rev-parse", "--verify", "HEAD"], timeout=60
            )
            ancestor = self._run(
                ["git", "-C", worktree_path, "merge-base", "--is-ancestor", start_sha, "HEAD"],
                timeout=60,
            )
            status_result = self._run(
                ["git", "-C", worktree_path, "status", "--porcelain"], timeout=60
            )
        except OSError as exc:
            raise OrcaError(f"unable to verify worker implementation state: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError("worker implementation verification timed out") from exc
        if head.returncode or not head.stdout.strip():
            raise OrcaError("implementation worktree has no verifiable HEAD")
        if head.stdout.strip() == start_sha or ancestor.returncode:
            raise OrcaError(
                "worker must create at least one implementation commit beyond start SHA"
            )
        if status_result.returncode or status_result.stdout.strip():
            raise OrcaError("implementation worktree is not clean")

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

    def _git(self, worktree_path: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(["git", "-C", worktree_path, *arguments], timeout=60)
        except OSError as exc:
            raise OrcaError(f"unable to inspect worktree {worktree_path}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError(f"git timed out in {worktree_path}") from exc

    def integrate_worker_commit(
        self,
        manager_path: str,
        worker_path: str,
        worker_sha: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, str | None]:
        """Fetch a worker commit into the manager worktree, then fast-forward or merge."""
        wanted = worker_sha.strip()
        if not wanted:
            raise OrcaError("worker commit SHA is required")
        worker_head = self._git(worker_path, "rev-parse", "--verify", "HEAD")
        if worker_head.returncode or worker_head.stdout.strip() != wanted:
            raise OrcaError("worker HEAD does not match the harvest commit")
        if self.uncommitted_changes(worker_path):
            raise OrcaError("worker worktree is not clean")
        if self.uncommitted_changes(manager_path):
            raise OrcaError("manager worktree is not clean")
        manager_head = self._git(manager_path, "rev-parse", "--verify", "HEAD")
        before = manager_head.stdout.strip() if manager_head.returncode == 0 else ""
        if not before:
            raise OrcaError(
                manager_head.stderr.strip() or "manager worktree has no verifiable HEAD"
            )
        if before == wanted:
            if not dry_run:
                self.prepare_repository(Path(manager_path))
            return {"method": "already", "before": before, "after": before}
        ancestor = self._git(manager_path, "merge-base", "--is-ancestor", before, wanted)
        method = "ff" if ancestor.returncode == 0 else "merge"
        if dry_run:
            return {"method": method, "before": before, "after": wanted if method == "ff" else None}
        fetched = self._git(manager_path, "fetch", worker_path, wanted)
        if fetched.returncode:
            raise OrcaError(fetched.stderr.strip() or "unable to fetch worker commit")
        merge_arguments = (
            ("merge", "--ff-only", wanted) if method == "ff" else ("merge", "--no-edit", wanted)
        )
        merged = self._git(manager_path, *merge_arguments)
        if merged.returncode:
            self._git(manager_path, "merge", "--abort")
            raise OrcaError(merged.stderr.strip() or "unable to merge worker commit")
        after_head = self._git(manager_path, "rev-parse", "--verify", "HEAD")
        after = after_head.stdout.strip() if after_head.returncode == 0 else ""
        if not after:
            raise OrcaError("manager worktree has no verifiable HEAD after harvest")
        if method == "ff" and after != wanted:
            raise OrcaError("fast-forward harvest did not land on the worker commit")
        self.prepare_repository(Path(manager_path))
        if self.uncommitted_changes(manager_path):
            raise OrcaError("manager worktree is not clean after submodule synchronization")
        return {"method": method, "before": before, "after": after}

    def push_fast_forward(self, worktree_path: str) -> dict[str, str]:
        """Push HEAD to origin without --force. Non-fast-forward remotes fail closed."""
        if self.uncommitted_changes(worktree_path):
            raise OrcaError("worktree is not clean")
        pushed = self._git(worktree_path, "push", "origin", "HEAD")
        if pushed.returncode:
            raise OrcaError(pushed.stderr.strip() or "git push failed")
        return {"remote": "origin", "ref": "HEAD"}

    def _submodule_dirty_note(self, repository: Path) -> str:
        try:
            dirty = self.uncommitted_changes(str(repository))
        except (OrcaError, OSError, subprocess.TimeoutExpired):
            return "; manager worktree dirty-state inspection failed"
        if not dirty:
            return ""
        return "; manager worktree is dirty: " + ", ".join(dirty)

    def prepare_repository(self, repository: Path) -> None:
        """Run the defensive submodule update only when the repository declares submodules."""
        if not (repository / ".gitmodules").is_file():
            return
        env = os.environ.copy()
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
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
                env=env,
            )
        except OSError as exc:
            raise OrcaError(f"unable to prepare repository submodules: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaError(
                "repository submodule preparation timed out"
                + self._submodule_dirty_note(repository)
            ) from exc
        if result.returncode:
            raise OrcaError(
                (result.stderr.strip() or "repository submodule preparation failed")
                + self._submodule_dirty_note(repository)
            )

    def register_repository(self, repository: Path) -> dict[str, Any]:
        """Idempotently register a target repository with Orca before creating a worktree."""
        return self._json(["repo", "add", "--path", str(repository.resolve())])

    def worktree_comment(self, worktree_id: str) -> str:
        """Read the persisted Orca comment for one worktree."""
        try:
            result = self._json(["worktree", "show", "--worktree", f"id:{worktree_id}"])
        except OrcaError:
            listed, _truncated = self.list_worktrees()
            for worktree in listed:
                if worktree.worktree_id == worktree_id:
                    return worktree.comment
            return ""
        try:
            worktree = self._required_object(result, "worktree")
        except OrcaError:
            listed, _truncated = self.list_worktrees()
            for item in listed:
                if item.worktree_id == worktree_id:
                    return item.comment
            return ""
        comment = worktree.get("comment") or ""
        if not isinstance(comment, str):
            raise OrcaError("Orca worktree has an invalid comment")
        return comment

    def set_lifecycle(
        self,
        worktree_id: str,
        state: WorkflowStatus,
        detail: str | None = None,
        *,
        issue_urls: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        comments = {
            WorkflowStatus.RUNNING: "Flybridge workflow is running.",
            WorkflowStatus.COMPLETED: "Flybridge workflow completed.",
            WorkflowStatus.FAILED: "Flybridge workflow failed.",
            WorkflowStatus.CANCELLED: "Flybridge workflow cancelled.",
        }
        if state not in comments:
            raise ValueError(f"unknown workflow lifecycle state: {state}")
        existing = self.worktree_comment(worktree_id)
        lifecycle = (
            detail.strip() if isinstance(detail, str) and detail.strip() else comments[state]
        )
        comment = merge_lifecycle_comment(existing, lifecycle, extra_urls=issue_urls)
        arguments = [
            "worktree",
            "set",
            "--worktree",
            f"id:{worktree_id}",
            "--comment",
            comment,
        ]
        if state == WorkflowStatus.RUNNING:
            arguments.extend(["--workspace-status", "in-progress"])
        else:
            # Orca's default board models active versus inactive workspaces;
            # the comment and Flybridge record retain the terminal outcome.
            arguments.extend(["--workspace-status", "completed"])
        return self._json(arguments)

    def set_issue_comment(
        self,
        worktree_id: str,
        issue_url: str,
        *,
        github_issue_number: int | None = None,
    ) -> dict[str, Any]:
        """Keep the canonical issue URL in the Orca comment without changing lifecycle text."""
        existing = self.worktree_comment(worktree_id)
        lifecycle_lines = [
            line
            for line in existing.splitlines()
            if line.strip() and not line.strip().lower().startswith("https://github.com/")
        ]
        lifecycle = "\n".join(lifecycle_lines)
        comment = merge_lifecycle_comment(existing, lifecycle, extra_urls=(issue_url,))
        arguments = [
            "worktree",
            "set",
            "--worktree",
            f"id:{worktree_id}",
            "--comment",
            comment,
        ]
        if github_issue_number is not None:
            arguments.extend(["--issue", str(github_issue_number)])
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

    def create_coordinator(self, worktree_id: str, command: str) -> str:
        result = self._json(
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                "FLYBRIDGE COORDINATOR",
                "--command",
                command,
            ]
        )
        return self._handle(result, "terminal")

    @staticmethod
    def _agent_terminal_title(name: str, role: WorkflowRole | None, mode: WorkflowMode) -> str:
        """Return the initial tab title; agents may subsequently replace it."""
        role = role or (
            WorkflowRole.SINGLE if mode == WorkflowMode.SINGLE else WorkflowRole.MANAGER
        )
        prefix = {
            WorkflowRole.SINGLE: "S",
            WorkflowRole.MANAGER: "M",
            WorkflowRole.WORKER: "W",
            WorkflowRole.REVIEWER: "R",
        }[role]
        return f"[{prefix}] {name}"

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
        terminal = self._required_object(result, "terminal")
        return terminal.get("connected") is not False

    def terminal_worktree(self, terminal_handle: str) -> str:
        """Resolve and validate the worktree owning a notification target."""
        result = self._json(["terminal", "show", "--terminal", terminal_handle])
        if self._handle(result, "terminal") != terminal_handle:
            raise OrcaError("Orca returned a different terminal than requested")
        terminal = self._required_object(result, "terminal")
        worktree_id = terminal.get("worktreeId")
        if (
            terminal.get("connected") is False
            or not isinstance(worktree_id, str)
            or not worktree_id
        ):
            raise OrcaError("parent terminal is not connected to a worktree")
        return worktree_id

    def rename_terminal(self, terminal_handle: str, title: str) -> None:
        self._json(
            [
                "terminal",
                "rename",
                "--terminal",
                terminal_handle,
                "--title",
                title,
            ]
        )

    def create_agent_terminal(
        self,
        worktree_id: str,
        agent: str,
        *,
        title: str = "FLYBRIDGE AGENT",
        model: str | None = None,
    ) -> str:
        try:
            command = resolve_launch_command(
                agent,
                model,
                presets=self.launch_presets,
                which=self.which,
            )
        except UnresolvedAgentError as exc:
            raise OrcaError(str(exc)) from exc
        result = self._json(
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                title,
                "--command",
                command,
            ]
        )
        return self._handle(result, "terminal")

    def create_new_agent_terminal(
        self,
        worktree_id: str,
        worktree_path: str,
        agent: str,
        prompt: str,
        *,
        title: str = "FLYBRIDGE FRESH AGENT",
        model: str | None = None,
    ) -> str:
        """Start a new preset-backed agent TUI and send its initial prompt."""
        try:
            command = resolve_prompt_command(
                agent,
                model,
                prompt,
                presets=self.launch_presets,
                which=self.which,
            )
        except UnresolvedAgentError as exc:
            raise OrcaError(str(exc)) from exc
        if command is not None:
            # A one-shot agent may outlast Orca's terminal-create wait. Use a unique
            # title so an accepted create can be recovered after that timeout.
            launch_title = f"{title} {secrets.token_hex(8)}"
            try:
                result = self._json(
                    [
                        "terminal",
                        "create",
                        "--worktree",
                        f"id:{worktree_id}",
                        "--title",
                        launch_title,
                        "--command",
                        command,
                    ]
                )
            except OrcaError as exc:
                if exc.code != "timeout" and not isinstance(exc, OrcaTimeoutError):
                    raise
                recovered = self._find_terminal_by_title(worktree_id, launch_title)
                if recovered is None:
                    raise
                return recovered
            return self._handle(result, "terminal")
        terminal = self.create_agent_terminal(worktree_id, agent, title=title, model=model)
        try:
            self.wait_for_agent(terminal)
            self.send_prompt(terminal, prompt)
        except BaseException:
            try:
                self.close_terminals(worktree_id, terminal)
            except (OrcaError, OSError, RuntimeError):
                pass
            raise
        return terminal

    def _find_terminal_by_title(self, worktree_id: str, title: str) -> str | None:
        """Recover a terminal whose create was accepted but timed out waiting for startup."""
        result = self._json(["terminal", "list", "--worktree", f"id:{worktree_id}"])
        terminals = result.get("terminals")
        if not isinstance(terminals, list):
            return None
        matches = [
            terminal
            for terminal in terminals
            if isinstance(terminal, dict)
            and terminal.get("title") == title
            and terminal.get("worktreeId") == worktree_id
            and isinstance(terminal.get("handle"), str)
        ]
        return str(matches[0]["handle"]) if len(matches) == 1 else None

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
        arguments = [
            "terminal",
            "send",
            "--terminal",
            terminal_handle,
            "--text",
            prompt,
            "--enter",
        ]
        result = self._json(arguments, timeout=SEND_TIMEOUT_SECONDS)
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
