from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from flybridge_core import (
    AdapterReferenceConflict,
    LifecycleOperationConflict,
    ResourceQueue,
    WorkflowMode,
    WorkflowRecord,
    WorkflowRole,
    WorkflowStatus,
    WorkflowStore,
)

from .prompts import render_resume_prompt, render_start_prompt
from .runtime import WorkflowRuntime

ROLE_PLAN = (WorkflowRole.MANAGER, WorkflowRole.WORKER, WorkflowRole.REVIEWER)
ACTIVATION_WAIT_SECONDS = 65
REPORTED_UNCOMMITTED_CHANGES = 5


class WorkflowService:
    """Own workflow state transitions while adapters own the external launch."""

    def __init__(self, store: WorkflowStore, queue: ResourceQueue | None = None) -> None:
        self.store = store
        self.queue = queue
        self.reconcile_terminal_queue_requests()

    def reconcile_terminal_queue_requests(self) -> None:
        """Idempotently close the workflow/queue cross-database commit gap."""
        if self.queue is not None:
            self.queue.reconcile_terminal_workflows(self.store)

    def _cancel_owned_requests(self, workflows: list[WorkflowRecord]) -> None:
        if self.queue is None:
            return
        for workflow in workflows:
            self.queue.cancel_owner(workflow.id)

    @staticmethod
    def prepare_repository(repository: Path, runtime: WorkflowRuntime) -> None:
        """Complete adapter preflight before any durable workflow record exists."""
        runtime.prepare_repository(repository)
        runtime.register_repository(repository)

    def start_existing(
        self,
        workflow_id: str,
        create_external: Callable[[], object],
        activate_external: Callable[[str], None],
        cleanup_external: Callable[[str, str | None], None],
        remove_external: Callable[[str], None] | None = None,
        *,
        allow_duplicate: bool = False,
        already_starting: bool = False,
    ) -> WorkflowRecord:
        workflow = self.store.get(workflow_id)
        if already_starting:
            if workflow.status != WorkflowStatus.STARTING:
                raise ValueError("reserved workflow must be starting")
        else:
            if workflow.status != WorkflowStatus.REQUESTED:
                raise ValueError("only a requested workflow can be started")
            self.store.begin_start(workflow.id, allow_duplicate=allow_duplicate)
        external = None
        external_attached = False
        try:
            external = create_external()
            adapter_reference = str(getattr(external, "worktree_id", ""))
            worktree_path = str(getattr(external, "worktree", ""))
            terminal_handle = str(getattr(external, "terminal", ""))
            if not adapter_reference or not worktree_path:
                raise RuntimeError("adapter did not return a workflow reference")
            self.store.attach_external(
                workflow.id,
                adapter_reference=adapter_reference,
                worktree_path=worktree_path,
                terminal_handle=terminal_handle,
            )
            external_attached = True
            activation_reference = self.store.claim_activation(workflow.id)
            try:
                activate_external(activation_reference)
            except BaseException:
                self.store.release_activation(workflow.id)
                raise
            return self.store.complete_activation(workflow.id)
        except BaseException as exc:
            if isinstance(exc, GeneratorExit):
                raise
            current = self.store.get(workflow.id)
            cleanup_errors: list[str] = []
            ownership_conflict = isinstance(exc, AdapterReferenceConflict)
            orphan_reference = current.adapter_reference or str(
                getattr(external, "worktree_id", "") or getattr(exc, "worktree_id", "")
            )
            orphan_path = str(
                getattr(external, "worktree", "") or getattr(exc, "worktree_path", "")
            )
            orphan_terminal = str(
                getattr(external, "terminal", "") or getattr(exc, "terminal_handle", "")
            )
            if not ownership_conflict and not current.adapter_reference and orphan_reference:
                try:
                    self.store.attach_partial_external(
                        workflow.id,
                        adapter_reference=orphan_reference,
                        worktree_path=orphan_path,
                    )
                    current = self.store.get(workflow.id)
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as partial_exc:
                    current = self.store.get(workflow.id)
                    if current.status == WorkflowStatus.STARTING:
                        cleanup_errors.append(
                            f"partial ownership could not be recorded: {partial_exc}"
                        )
            cleanup_reference = (
                None if ownership_conflict else current.adapter_reference or orphan_reference
            )
            if cleanup_reference:
                handles = self.store.owned_terminal_handles(current.id)
                if orphan_terminal and orphan_terminal not in handles:
                    handles.append(orphan_terminal)
                for handle in handles:
                    try:
                        cleanup_external(cleanup_reference, handle)
                    except (OSError, RuntimeError) as cleanup_exc:
                        cleanup_errors.append(f"terminal {handle}: {cleanup_exc}")
                if remove_external and not external_attached and not cleanup_errors:
                    try:
                        remove_external(cleanup_reference)
                        if current.adapter_reference:
                            self.store.mark_external_reconciled(current.id)
                    except (OSError, RuntimeError) as cleanup_exc:
                        cleanup_errors.append(f"worktree removal: {cleanup_exc}")
            cleanup_error = "".join(f"; {error}" for error in cleanup_errors)
            current = self.store.get(workflow.id)
            if current.status == WorkflowStatus.STARTING:
                failed = self.store.transition(
                    workflow.id,
                    WorkflowStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}{cleanup_error}",
                )
                affected = [failed, *self.store.cancel_requested_successors(workflow.id)]
                self._cancel_owned_requests(affected)
            raise
        raise RuntimeError("workflow start ended without an activation result")

    @staticmethod
    def _reject_unreachable_changes(source: WorkflowRecord, runtime: WorkflowRuntime) -> None:
        """Refuse to base a child worktree on a branch that omits the source role's work."""
        changes = runtime.uncommitted_changes(source.worktree_path)
        if not changes:
            return
        reported = ", ".join(changes[:REPORTED_UNCOMMITTED_CHANGES])
        remaining = len(changes) - REPORTED_UNCOMMITTED_CHANGES
        if remaining > 0:
            reported = f"{reported}, and {remaining} more"
        raise ValueError(
            f"{source.role} worktree has uncommitted changes that a branch-based child cannot "
            f"see; commit or discard them before advancing: {reported}"
        )

    def launch_existing(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        agent: str,
        response_language: str,
        skill_paths: tuple[Path, ...],
        resource_names: tuple[str, ...] = (),
        config_path: Path | None = None,
        observer_command: str | None = None,
        allow_duplicate: bool = False,
        already_starting: bool = False,
    ) -> WorkflowRecord:
        """Launch one durable record through an adapter-neutral runtime."""
        workflow = self.store.get(workflow_id)
        handoff_summary = None
        parent_worktree_id = None
        base_branch = None
        if workflow.parent_id:
            source = self.store.get(workflow.parent_id)
            if workflow.role == WorkflowRole.REVIEWER:
                source = next(
                    (
                        child
                        for child in self.store.children(workflow.parent_id)
                        if child.role == WorkflowRole.WORKER
                    ),
                    None,
                )
                if source is None:
                    raise ValueError("reviewer requires a worker role")
            if (
                source.status != WorkflowStatus.COMPLETED
                or not source.adapter_reference
                or not source.worktree_path
            ):
                raise ValueError("parent role must complete before its child can start")
            handoff_summary = self.store.handoff_for(workflow.id).summary
            parent_worktree_id = source.adapter_reference
            base_branch = runtime.current_branch(source.worktree_path)
            self._reject_unreachable_changes(source, runtime)
        prompt = render_start_prompt(
            objective=workflow.objective,
            mode=workflow.mode,
            role=workflow.role,
            response_language=response_language,
            skill_paths=skill_paths,
            workflow_id=workflow.id,
            resource_names=resource_names,
            config_path=config_path,
            handoff_summary=handoff_summary,
        )

        def create_external():
            return runtime.start(
                Path(workflow.repository),
                workflow.name,
                workflow.mode,
                agent,
                prompt,
                parent_worktree_id=parent_worktree_id,
                base_branch=base_branch,
            )

        started = self.start_existing(
            workflow.id,
            create_external,
            lambda worktree_id: runtime.set_lifecycle(worktree_id, WorkflowStatus.RUNNING),
            runtime.close_terminals,
            runtime.remove_worktree,
            allow_duplicate=allow_duplicate,
            already_starting=already_starting,
        )
        if observer_command is not None:
            self.attach_observer(started.id, runtime, observer_command)
            started = self.store.get(started.id)
        return started

    def resume(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        agent: str,
        response_language: str,
        skill_paths: tuple[Path, ...],
        resource_names: tuple[str, ...] = (),
        config_path: Path | None = None,
    ) -> WorkflowRecord:
        """Resume a persisted running workflow in its exact owned Orca worktree."""
        workflow = self.store.get(workflow_id)
        if workflow.status != WorkflowStatus.RUNNING:
            raise ValueError("only a running workflow can be resumed")
        if not workflow.adapter_reference or not workflow.worktree_path:
            raise ValueError("running workflow has incomplete persisted worktree ownership")
        prompt = render_resume_prompt(
            objective=workflow.objective,
            role=workflow.role,
            response_language=response_language,
            skill_paths=skill_paths,
            workflow_id=workflow.id,
            resource_names=resource_names,
            config_path=config_path,
        )
        runtime.verify_worktree(workflow.adapter_reference, workflow.worktree_path)
        terminal_handle = workflow.terminal_handle
        if not runtime.terminal_is_valid(workflow.adapter_reference, terminal_handle):
            replacement = runtime.create_agent_terminal(workflow.adapter_reference, agent)
            try:
                self.store.replace_agent_terminal(workflow.id, terminal_handle, replacement)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                try:
                    runtime.close_terminals(workflow.adapter_reference, replacement)
                except (OSError, RuntimeError):
                    pass
                raise
            terminal_handle = replacement
        if not terminal_handle:
            raise ValueError("running workflow has no resumable agent terminal")
        runtime.wait_for_agent(terminal_handle)
        runtime.send_prompt(terminal_handle, prompt)
        return self.store.mark_resumed(workflow.id)

    def attach_observer(self, workflow_id: str, runtime: WorkflowRuntime, command: str) -> str:
        """Create and persist a queue observer or fail the owned workflow cleanly."""
        workflow = self.store.get(workflow_id)
        if workflow.status != WorkflowStatus.RUNNING or not workflow.adapter_reference:
            raise ValueError("queue observer requires a running workflow")
        handle = ""
        try:
            handle = runtime.create_observer(workflow.adapter_reference, command)
            self.store.add_owned_terminal(workflow.id, handle, "observer", require_running=True)
            return handle
        except LifecycleOperationConflict as exc:
            cleanup_error = ""
            if handle:
                try:
                    runtime.close_terminals(workflow.adapter_reference, handle)
                except (OSError, RuntimeError) as cleanup_exc:
                    cleanup_error = f"; terminal {handle}: {cleanup_exc}"
            raise RuntimeError(
                f"queue observer ownership changed during startup: {exc}{cleanup_error}"
            ) from exc
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            error_text = str(exc)
            detail = f"queue observer startup failed: {error_text}"
            try:
                self.finish(
                    workflow.id,
                    WorkflowStatus.FAILED,
                    lambda reference: runtime.set_lifecycle(
                        reference, WorkflowStatus.FAILED, error_text
                    ),
                    error=detail,
                    close_external=runtime.close_terminals,
                )
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as finish_exc:
                detail += f"; workflow finalization: {finish_exc}"
            if handle:
                try:
                    runtime.close_terminals(workflow.adapter_reference, handle)
                except (OSError, RuntimeError) as cleanup_exc:
                    detail += f"; terminal {handle}: {cleanup_exc}"
            raise RuntimeError(detail) from exc

    def plan_children(self, parent_id: str) -> list[WorkflowRecord]:
        parent = self.store.get(parent_id)
        if parent.mode != WorkflowMode.ORCHESTRATED or parent.role != WorkflowRole.MANAGER:
            raise ValueError("only an orchestrated manager can own a role plan")
        children = self.store.children(parent_id)
        if [child.role for child in children] != list(ROLE_PLAN[1:]):
            raise ValueError("complete role plan was not found")
        return children

    def next_ready_child(self, parent_id: str) -> WorkflowRecord | None:
        parent = self.store.get(parent_id)
        if parent.mode != WorkflowMode.ORCHESTRATED or parent.role != WorkflowRole.MANAGER:
            raise ValueError("workflow advance requires an orchestrated manager")
        children = {child.role: child for child in self.store.children(parent_id)}
        worker = children.get(WorkflowRole.WORKER)
        reviewer = children.get(WorkflowRole.REVIEWER)
        if (
            parent.status == WorkflowStatus.COMPLETED
            and worker
            and worker.status == WorkflowStatus.REQUESTED
        ):
            try:
                self.store.handoff_for(worker.id)
            except ValueError:
                return None
            return worker
        if (
            worker
            and worker.status == WorkflowStatus.COMPLETED
            and reviewer
            and reviewer.status == WorkflowStatus.REQUESTED
        ):
            try:
                self.store.handoff_for(reviewer.id)
            except ValueError:
                return None
            return reviewer
        return None

    def finish(
        self,
        workflow_id: str,
        target: WorkflowStatus | str,
        update_external: Callable[[str], None],
        *,
        error: str | None = None,
        close_external: Callable[[str, str | None], None] | None = None,
    ) -> WorkflowRecord:
        try:
            target = WorkflowStatus(target)
        except ValueError as exc:
            raise ValueError("workflow target must be completed, failed, or cancelled") from exc
        if target not in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }:
            raise ValueError("workflow target must be completed, failed, or cancelled")
        deadline = time.monotonic() + ACTIVATION_WAIT_SECONDS
        while True:
            try:
                claim = self.store.claim_terminal_transition(workflow_id, target, error=error)
                break
            except LifecycleOperationConflict as exc:
                if exc.kind != "activation" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        if claim.workflow.status == WorkflowStatus.CANCELLED:
            self._cancel_owned_requests(
                [self.store.get(affected_id) for affected_id in claim.affected_workflow_ids]
            )
        if claim.adapter_reference:
            try:
                update_external(claim.adapter_reference)
            except BaseException as exc:
                if isinstance(exc, GeneratorExit):
                    raise
                self.store.fail_terminal_update(
                    workflow_id,
                    str(exc) if isinstance(exc, Exception) else f"{type(exc).__name__}: {exc}",
                )
                raise
            finished = self.store.complete_terminal_transition(workflow_id, target, error=error)
        else:
            finished = claim.workflow
        affected = [self.store.get(affected_id) for affected_id in claim.affected_workflow_ids]
        if target in {WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}:
            affected.extend(self.store.cancel_requested_successors(workflow_id))
        self._cancel_owned_requests(affected)
        self._close_finished_terminals(finished, close_external)
        return self.store.get(finished.id)

    def retry_failed(self, workflow_id: str) -> WorkflowRecord:
        """Discard any pre-failure queue ownership before making a retry runnable."""
        if self.queue is not None:
            self.queue.cancel_owner(workflow_id)
        return self.store.retry_failed(workflow_id)

    def _close_finished_terminals(
        self,
        workflow: WorkflowRecord,
        close_external: Callable[[str, str | None], None] | None,
    ) -> None:
        if close_external is None or not workflow.adapter_reference:
            return
        cleanup_errors: list[str] = []
        for handle in self.store.owned_terminal_handles(workflow.id):
            try:
                close_external(workflow.adapter_reference, handle)
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(f"{handle}: {exc}")
        if cleanup_errors:
            detail = "owned terminal cleanup failed: " + "; ".join(cleanup_errors)
            self.store.set_cleanup_error(workflow.id, detail)
            raise RuntimeError(detail)
        if workflow.status == WorkflowStatus.COMPLETED:
            self.store.set_cleanup_error(workflow.id, None)

    def reconcile_stale(
        self,
        max_age_seconds: float,
        close_external: Callable[[str, str | None], None],
        remove_external: Callable[[str], None],
    ) -> list[WorkflowRecord]:
        reconciled: list[WorkflowRecord] = []
        reconciliation_errors: list[str] = []
        workflow_ids = {workflow.id for workflow in self.store.stale_active(max_age_seconds)}
        workflow_ids.update(
            workflow.id for workflow in self.store.stale_reconcilable(max_age_seconds)
        )
        for workflow_id in sorted(workflow_ids):
            try:
                current = self.store.get(workflow_id)
                operation = self.store.lifecycle_operation(workflow_id)
                if (
                    current.status == WorkflowStatus.COMPLETED
                    and operation is not None
                    and operation[1] is None
                ):
                    reconciliation_errors.append(
                        f"{workflow_id}: {operation[0]} lifecycle operation is in progress"
                    )
                    continue
                if current.status == WorkflowStatus.COMPLETED:
                    if not current.cleanup_error:
                        continue
                    cleanup_errors: list[str] = []
                    if current.adapter_reference:
                        for handle in self.store.owned_terminal_handles(current.id):
                            try:
                                close_external(current.adapter_reference, handle)
                            except (OSError, RuntimeError) as exc:
                                cleanup_errors.append(f"{handle}: {exc}")
                    if cleanup_errors:
                        reconciliation_errors.append(
                            f"{workflow_id}: terminal cleanup failed: {'; '.join(cleanup_errors)}"
                        )
                        continue
                    self.store.set_cleanup_error(workflow_id, None)
                    self._cancel_owned_requests([current])
                    reconciled.append(self.store.get(workflow_id))
                    continue
                if current.adapter_reference and current.external_reconciled_at is None:
                    try:
                        current = self.store.claim_stale_reconciliation(
                            workflow_id, max_age_seconds
                        )
                    except LifecycleOperationConflict as exc:
                        reconciliation_errors.append(f"{workflow_id}: {exc}")
                        continue
                    cleanup_errors = []
                    for handle in self.store.owned_terminal_handles(current.id):
                        try:
                            close_external(current.adapter_reference, handle)
                        except (OSError, RuntimeError) as exc:
                            cleanup_errors.append(f"{handle}: {exc}")
                    try:
                        remove_external(current.adapter_reference)
                    except (OSError, RuntimeError) as exc:
                        detail = (
                            f"terminal cleanup failed: {'; '.join(cleanup_errors)}; "
                            if cleanup_errors
                            else ""
                        )
                        self.store.fail_terminal_update(
                            workflow_id, f"{detail}worktree removal failed: {exc}"
                        )
                        detail = (
                            f"; terminal cleanup failed: {'; '.join(cleanup_errors)}"
                            if cleanup_errors
                            else ""
                        )
                        reconciliation_errors.append(
                            f"{workflow_id}: worktree removal failed: {exc}{detail}"
                        )
                        continue
                    finished, affected_ids = self.store.complete_stale_reconciliation(workflow_id)
                    affected = [self.store.get(affected_id) for affected_id in affected_ids]
                    self._cancel_owned_requests(affected)
                    reconciled.append(finished)
                    continue
                current = self.store.get(workflow_id)
                if current.status in {
                    WorkflowStatus.REQUESTED,
                    WorkflowStatus.STARTING,
                    WorkflowStatus.RUNNING,
                }:
                    try:
                        finished = self.store.transition(
                            workflow_id,
                            WorkflowStatus.CANCELLED,
                            error="stale workflow reconciled by explicit cleanup",
                        )
                    except ValueError as exc:
                        reconciliation_errors.append(f"{workflow_id}: {exc}")
                        continue
                    affected = [finished, *self.store.cancel_requested_successors(workflow_id)]
                    reconciled.append(finished)
                else:
                    finished = current
                    affected = [finished]
                    reconciled.append(finished)
                self._cancel_owned_requests(affected)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                reconciliation_errors.append(f"{workflow_id}: {exc}")
        if reconciliation_errors:
            raise RuntimeError("; ".join(reconciliation_errors))
        return reconciled
