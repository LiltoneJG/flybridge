from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from flybridge_core import WorkflowMode, WorkflowStatus


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
        parent_worktree_id: str | None = None,
        base_branch: str | None = None,
    ) -> Any: ...

    def current_branch(self, worktree_path: str) -> str: ...

    def uncommitted_changes(self, worktree_path: str) -> tuple[str, ...]: ...

    def prepare_repository(self, repository: Path) -> None: ...

    def register_repository(self, repository: Path) -> dict[str, Any]: ...

    def set_lifecycle(
        self, worktree_id: str, state: WorkflowStatus, detail: str | None = None
    ) -> dict[str, Any]: ...

    def create_observer(self, worktree_id: str, command: str) -> str: ...

    def verify_worktree(self, worktree_id: str, worktree_path: str) -> None: ...

    def terminal_is_valid(self, worktree_id: str, terminal_handle: str | None) -> bool: ...

    def create_agent_terminal(self, worktree_id: str, agent: str) -> str: ...

    def wait_for_agent(self, terminal_handle: str) -> None: ...

    def send_prompt(self, terminal_handle: str, prompt: str) -> None: ...

    def close_terminals(
        self, worktree_id: str, terminal_handle: str | None = None
    ) -> dict[str, Any]: ...

    def remove_worktree(self, worktree_id: str) -> dict[str, Any]: ...
