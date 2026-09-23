"""Deterministic lease-grant delivery through a workflow's agent terminal."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flybridge_core import (
    QueueEvent,
    QueueRequestStatus,
    ResourceQueue,
    WorkflowStatus,
    WorkflowStore,
)

from .prompts import render_lease_grant_prompt
from .runtime import WorkflowRuntime


class QueueLeaseNotifier:
    """Wake one parked owner after FIFO promotion. Never reorders the queue."""

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
        self.pending: set[str] = set()
        self.notified: set[str] = set()
        workflow = store.get(self.workflow_id)
        self.owner_terminal_handle = workflow.terminal_handle
        for row in queue.owner_requests(self.workflow_id):
            if row["status"] == QueueRequestStatus.WAITING.value:
                self.pending.add(str(row["request_id"]))

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

    def note_event(self, event: dict[str, object]) -> None:
        """Track queued promotions for this owner only."""
        request_id = str(event.get("request_id") or "")
        name = str(event.get("event") or "")
        if not request_id:
            return
        try:
            details = self.queue.inspect(request_id)
        except ValueError:
            return
        if details["owner"] != self.workflow_id:
            return
        if name == QueueEvent.QUEUED.value:
            self.pending.add(request_id)
            return
        if name in {
            QueueEvent.RELEASED.value,
            QueueEvent.CANCELLED.value,
            QueueEvent.RECOVERED.value,
        }:
            self.pending.discard(request_id)

    def notify_due(self) -> dict[str, Any] | None:
        """Send at most one grant prompt for a pending request that is now leased."""
        for request_id in list(self.pending):
            if request_id in self.notified:
                continue
            try:
                details = self.queue.inspect(request_id, owner=self.workflow_id)
            except ValueError:
                self.pending.discard(request_id)
                continue
            if details["status"] != QueueRequestStatus.LEASED.value:
                continue
            lease_id = details["lease_id"]
            if not isinstance(lease_id, str) or not lease_id:
                continue
            return self._notify(details)
        return None

    def _notify(self, details: dict[str, object]) -> dict[str, Any]:
        request_id = str(details["request_id"])
        lease_id = str(details["lease_id"])
        resource = str(details["resource"])
        try:
            workflow = self.store.get(self.workflow_id)
            handle = workflow.terminal_handle
            if not handle or not workflow.adapter_reference:
                raise ValueError("running workflow has no resumable agent terminal")
            if not self.runtime.terminal_is_valid(workflow.adapter_reference, handle):
                raise ValueError("agent terminal is no longer valid")
            prompt = render_lease_grant_prompt(
                resource=resource,
                request_id=request_id,
                lease_id=lease_id,
                workflow_id=self.workflow_id,
                config_path=self.config_path,
            )
            self.runtime.wait_for_agent(handle)
            self.runtime.send_prompt(handle, prompt)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            return {
                "event": "notify_failed",
                "request_id": request_id,
                "lease_id": lease_id,
                "error": str(exc),
            }
        self.notified.add(request_id)
        self.pending.discard(request_id)
        return {
            "event": "notify_sent",
            "request_id": request_id,
            "lease_id": lease_id,
            "resource": resource,
        }
