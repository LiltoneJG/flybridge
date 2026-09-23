from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from flybridge_core import WorkflowMode, WorkflowRole, WorkflowStatus


class WorkflowRuntime(Protocol):
    """IDE/worktree operations required by the application layer.

    Implementations may target Orca or another IDE; application workflows only
    depend on this adapter boundary and never on a vendor SDK.
    """

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
    ) -> Any: ...

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
    ) -> Any: ...

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
    ) -> Any: ...

    def current_branch(self, worktree_path: str) -> str: ...

    def uncommitted_changes(self, worktree_path: str) -> tuple[str, ...]: ...

    def integrate_worker_commit(
        self,
        manager_path: str,
        worker_path: str,
        worker_sha: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, str | None]: ...

    def implementation_identity(
        self, worktree_id: str, worktree_path: str
    ) -> tuple[str, str, str]: ...

    def implementation_worktree_present(self, worktree_path: str) -> bool: ...

    def verify_implementation_identity(
        self,
        worktree_id: str,
        worktree_path: str,
        implementation_repository: str,
        runtime_repository_id: str,
        start_sha: str,
    ) -> None: ...

    def verify_pristine_start(self, worktree_path: str, start_sha: str) -> None: ...

    def verify_worker_ready(self, worktree_path: str, start_sha: str) -> None: ...

    def prepare_repository(self, repository: Path) -> None: ...

    def register_repository(self, repository: Path) -> dict[str, Any]: ...

    def set_lifecycle(
        self,
        worktree_id: str,
        state: WorkflowStatus,
        detail: str | None = None,
        *,
        issue_urls: tuple[str, ...] = (),
    ) -> dict[str, Any]: ...

    def create_observer(self, worktree_id: str, command: str) -> str: ...

    def create_coordinator(self, worktree_id: str, command: str) -> str: ...

    def verify_worktree(self, worktree_id: str, worktree_path: str) -> None: ...

    def terminal_is_valid(self, worktree_id: str, terminal_handle: str | None) -> bool: ...

    def create_agent_terminal(
        self, worktree_id: str, agent: str, *, model: str | None = None
    ) -> str: ...

    def create_new_agent_terminal(
        self,
        worktree_id: str,
        worktree_path: str,
        agent: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> str: ...

    def wait_for_agent(self, terminal_handle: str) -> None: ...

    def send_prompt(self, terminal_handle: str, prompt: str) -> None: ...

    def close_terminals(
        self, worktree_id: str, terminal_handle: str | None = None
    ) -> dict[str, Any]: ...

    def push_fast_forward(self, worktree_path: str) -> dict[str, str]: ...

    def remove_worktree(self, worktree_id: str) -> dict[str, Any]: ...
