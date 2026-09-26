"""Lifecycle ownership for a display-only queue observer terminal."""

from __future__ import annotations

from pathlib import Path

from flybridge_core import ResourceQueue, WorkflowStatus, WorkflowStore

from .runtime import WorkflowRuntime


class QueueLeaseNotifier:
    """Keep a visible observer tied to its original agent terminal."""

    def __init__(
        self,
        queue: ResourceQueue,
        store: WorkflowStore,
        runtime: WorkflowRuntime,
        workflow_id: str,
        *,
        config_path: Path | None = None,
    ) -> None:
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ValueError("workflow identifier is required")
        self.queue = queue
        self.store = store
        self.runtime = runtime
        self.workflow_id = workflow_id.strip()
        self.config_path = config_path
        workflow = store.get(self.workflow_id)
        self.owner_terminal_handle = workflow.terminal_handle

    def owner_terminal_is_available(self) -> bool:
        """Return false only for a definitively stopped or replaced owner terminal."""
        workflow = self.store.get(self.workflow_id)
        handle = self.owner_terminal_handle
        if (
            workflow.status != WorkflowStatus.RUNNING
            or not handle
            or workflow.terminal_handle != handle
            or not workflow.adapter_reference
        ):
            return False
        return self.runtime.terminal_is_valid(workflow.adapter_reference, handle)

    def detach_if_owner_terminal_unavailable(self) -> list[str]:
        """Remove this observer's durable ownership only while its agent is current."""
        return self.store.detach_observers_for_unavailable_agent(
            self.workflow_id, self.owner_terminal_handle or ""
        )

    def close_observers(self, handles: list[str]) -> list[str]:
        """Close only observer terminals removed by the owner-identity transaction."""
        workflow = self.store.get(self.workflow_id)
        if not workflow.adapter_reference:
            return handles
        errors: list[str] = []
        for handle in handles:
            try:
                self.runtime.close_terminals(workflow.adapter_reference, handle)
            except (OSError, RuntimeError) as exc:
                errors.append(f"{handle}: {exc}")
        return errors
