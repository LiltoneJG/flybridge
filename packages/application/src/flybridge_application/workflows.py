from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from flybridge_core import (
    AdapterReferenceConflict,
    LifecycleOperationConflict,
    OrchestrationRun,
    ResourceQueue,
    RoleReadiness,
    WorkflowArtifact,
    WorkflowArtifactStore,
    WorkflowMode,
    WorkflowRecord,
    WorkflowRole,
    WorkflowStatus,
    WorkflowStore,
    merge_workflow_marker,
    parse_issue_url,
    repositories_match,
)

from .git_probe import GitProbeError, GitWorktreeProbe
from .prompts import render_resume_prompt, render_start_prompt
from .runtime import WorkflowRuntime

ROLE_PLAN = (WorkflowRole.MANAGER, WorkflowRole.WORKER, WorkflowRole.REVIEWER)
ACTIVATION_WAIT_SECONDS = 65
REPORTED_UNCOMMITTED_CHANGES = 5
_TERMINAL_RUN_STATUSES = frozenset({"completed", "blocked", "failed"})


def _progress_role(record: WorkflowRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "role": record.role.value,
        "slot": record.slot,
        "status": record.status.value,
        "error": record.error,
    }


def progress_stage(
    *,
    manager: WorkflowRecord,
    children: list[WorkflowRecord],
    run: OrchestrationRun | None,
    resource_waiting: bool = False,
) -> str:
    """Derive a single progress label from durable role and run state."""
    if run is not None and run.status in _TERMINAL_RUN_STATUSES:
        return run.status
    if resource_waiting:
        return "waiting_resource"
    if manager.status == WorkflowStatus.RUNNING:
        return "planning"
    worker = next((child for child in children if child.role == WorkflowRole.WORKER), None)
    reviewers = [child for child in children if child.role == WorkflowRole.REVIEWER]
    cycle = 1 if run is None else run.current_review_cycle
    if worker is not None and worker.status == WorkflowStatus.RUNNING:
        return "implementing" if cycle <= 1 else "addressing_review"
    if any(child.status == WorkflowStatus.RUNNING for child in reviewers):
        return "reviewing"
    return "waiting"


def _matching_github_issue_number(repository: Path, issue_url: str | None) -> int | None:
    if not issue_url:
        return None
    try:
        ref = parse_issue_url(issue_url)
    except ValueError:
        return None
    try:
        state = GitWorktreeProbe().inspect(str(repository))
    except GitProbeError:
        return None
    if repositories_match(state.github_repository, ref.repository):
        return ref.number
    return None


@dataclass(frozen=True)
class HarvestResult:
    """Git integration of a worker commit into the manager worktree."""

    workflow_id: str
    method: str
    before: str | None
    after: str | None
    worker_sha: str | None
    skip_reason: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class DeliveryCheckResult:
    """Whether the manager HEAD is the exact locally approved orchestration tip."""

    workflow_id: str
    eligible: bool
    approved_sha: str | None
    actual_sha: str | None
    reason: str | None = None


@dataclass(frozen=True)
class RetireResult:
    """Post-outcome resource recovery that does not change workflow lifecycle status."""

    workflow_id: str
    keep: str
    harvested: HarvestResult
    closed_handles: tuple[tuple[str, str], ...]
    removed_worktrees: tuple[str, ...]
    kept: tuple[str, ...]
    dry_run: bool = False


@dataclass(frozen=True)
class ReconcileResult:
    """Per-workflow cleanup outcomes that never abort the remaining IDs."""

    reconciled: list[WorkflowRecord]
    errors: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[WorkflowRecord]:
        return iter(self.reconciled)

    def __len__(self) -> int:
        return len(self.reconciled)

    def __getitem__(self, index: int) -> WorkflowRecord:
        return self.reconciled[index]


class WorkflowService:
    """Own workflow state transitions while adapters own the external launch."""

    def __init__(self, store: WorkflowStore, queue: ResourceQueue | None = None) -> None:
        self.store = store
        self.artifacts = WorkflowArtifactStore(store.path.parent, store)
        self.queue = queue
        self.reconcile_terminal_queue_requests()

    def reconcile_terminal_queue_requests(self) -> None:
        """Idempotently close the workflow/queue cross-database commit gap."""
        if self.queue is not None:
            self.queue.reconcile_terminal_workflows(self.store)

    @staticmethod
    def _discard_unowned_terminal(
        runtime: WorkflowRuntime, adapter_reference: str, terminal_handle: str
    ) -> None:
        """Best-effort compensation for a terminal not recorded in durable ownership."""
        try:
            runtime.close_terminals(adapter_reference, terminal_handle)
        except (OSError, RuntimeError):
            pass

    def _replace_agent_terminal(
        self, workflow: WorkflowRecord, runtime: WorkflowRuntime, replacement: str
    ) -> None:
        """Persist the new agent and close observers dropped by that ownership swap."""
        if not workflow.adapter_reference:
            raise ValueError("running workflow has incomplete persisted worktree ownership")
        observers = self.store.owned_terminal_handles(workflow.id, kind="observer")
        self.store.replace_agent_terminal(workflow.id, workflow.terminal_handle, replacement)
        for handle in observers:
            try:
                runtime.close_terminals(workflow.adapter_reference, handle)
            except (OSError, RuntimeError):
                continue
            self.store.record_observer_termination(workflow.id, handle, "replaced_with_new_agent")

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

    @staticmethod
    def _verify_persisted_identity(workflow: WorkflowRecord, runtime: WorkflowRuntime) -> None:
        verifier = getattr(runtime, "verify_implementation_identity", None)
        if not callable(verifier):
            return
        if (
            not workflow.adapter_reference
            or not workflow.worktree_path
            or not workflow.implementation_repository
            or not workflow.runtime_repository_id
            or not workflow.start_sha
        ):
            raise ValueError("workflow has incomplete persisted implementation identity")
        verifier(
            workflow.adapter_reference,
            workflow.worktree_path,
            workflow.implementation_repository,
            workflow.runtime_repository_id,
            workflow.start_sha,
        )

    def _reconcile_name_ghost(self, workflow: WorkflowRecord, runtime: WorkflowRuntime) -> None:
        """Drop unreconciled name ownership when the prior worktree and terminal are gone."""
        prior = self.store.find_prior_unreconciled_by_name(
            workflow.name, exclude_workflow_id=workflow.id
        )
        if prior is None:
            return
        verify = getattr(runtime, "verify_worktree", None)
        terminal_valid = getattr(runtime, "terminal_is_valid", None)
        if not callable(verify) or not callable(terminal_valid):
            raise AdapterReferenceConflict(
                f"workflow name is still owned by workflow {prior.id}; "
                "reconcile that workflow with `workflow cleanup --apply` before retrying"
            )
        missing_worktree = not prior.adapter_reference or not prior.worktree_path
        if not missing_worktree:
            try:
                verify(prior.adapter_reference, prior.worktree_path)
            except (OSError, RuntimeError, TypeError, ValueError):
                missing_worktree = True
        terminal_ended = True
        if prior.adapter_reference and prior.terminal_handle:
            try:
                terminal_ended = not bool(
                    terminal_valid(prior.adapter_reference, prior.terminal_handle)
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                terminal_ended = True
        if missing_worktree and terminal_ended:
            self.store.mark_external_reconciled(prior.id)
            return
        raise AdapterReferenceConflict(
            f"workflow name is still owned by workflow {prior.id}; "
            "reconcile that workflow with `workflow cleanup --apply` before retrying"
        )

    def start_existing(
        self,
        workflow_id: str,
        create_external: Callable[[], object],
        activate_external: Callable[[str], None],
        cleanup_external: Callable[[str, str | None], None],
        remove_external: Callable[[str], None] | None = None,
        identify_external: Callable[[object], tuple[str, str, str]] | None = None,
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
            implementation_repository = runtime_repository_id = start_sha = None
            if identify_external is not None:
                implementation_repository, runtime_repository_id, start_sha = identify_external(
                    external
                )
                if not implementation_repository or not runtime_repository_id or not start_sha:
                    raise RuntimeError("adapter did not return complete implementation identity")
            self.store.attach_external(
                workflow.id,
                adapter_reference=adapter_reference,
                worktree_path=worktree_path,
                terminal_handle=terminal_handle,
                owns_worktree=bool(getattr(external, "owns_worktree", True)),
                implementation_repository=implementation_repository,
                start_sha=start_sha,
                runtime_repository_id=runtime_repository_id,
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
            cleanup_reference = current.adapter_reference or orphan_reference
            if cleanup_reference:
                handles = (
                    [] if ownership_conflict else self.store.owned_terminal_handles(current.id)
                )
                if orphan_terminal and orphan_terminal not in handles:
                    handles.append(orphan_terminal)
                for handle in handles:
                    try:
                        cleanup_external(cleanup_reference, handle)
                    except (OSError, RuntimeError) as cleanup_exc:
                        cleanup_errors.append(f"terminal {handle}: {cleanup_exc}")
                if (
                    remove_external
                    and not ownership_conflict
                    and bool(getattr(external, "owns_worktree", True))
                    and not external_attached
                    and not cleanup_errors
                ):
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

    def put_artifact(self, workflow_id: str, kind: str, content: str) -> WorkflowArtifact:
        return self.artifacts.put(workflow_id, kind, content)

    def show_artifact(self, workflow_id: str, kind: str) -> tuple[WorkflowArtifact, str]:
        return self.artifacts.read(workflow_id, kind)

    def verify_artifact(self, workflow_id: str, kind: str) -> WorkflowArtifact:
        return self.artifacts.verify(workflow_id, kind)

    def verify_readiness_artifact(self, readiness: RoleReadiness) -> str:
        """Verify the immutable digest snapshot named by a readiness event."""
        content = self.artifacts.read_digest(
            readiness.workflow_id, readiness.artifact_kind, readiness.artifact_sha256
        )
        if content != readiness.artifact_content:
            raise ValueError("readiness artifact snapshot content does not match its event")
        return content

    def role_ready(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        summary: str,
        outcome: str | None = None,
    ) -> RoleReadiness:
        """Verify role output and Git state before recording a terminal-safe readiness event."""
        workflow = self.store.get(workflow_id)
        if workflow.mode != WorkflowMode.ORCHESTRATED or workflow.role == WorkflowRole.SINGLE:
            raise ValueError("role-ready requires an orchestrated role")
        if not workflow.worktree_path or not workflow.start_sha:
            raise ValueError("workflow has incomplete persisted implementation identity")
        self._verify_persisted_identity(workflow, runtime)
        kind = {
            WorkflowRole.MANAGER: "plan",
            WorkflowRole.WORKER: "verification",
            WorkflowRole.REVIEWER: "review",
        }[workflow.role]
        artifact, artifact_content = self.artifacts.read(workflow.id, kind)
        if workflow.role == WorkflowRole.WORKER:
            runtime.verify_worker_ready(workflow.worktree_path, workflow.start_sha)
        else:
            runtime.verify_pristine_start(workflow.worktree_path, workflow.start_sha)
        blocked_reason = summary if outcome == "blocked" else None
        reviewer_outcome = None if outcome == "blocked" else outcome
        return self.store.record_role_readiness(
            workflow.id,
            summary=summary,
            outcome=reviewer_outcome,
            blocked_reason=blocked_reason,
            artifact_kind=kind,
            artifact_sha256=artifact.sha256,
            artifact_content=artifact_content,
        )

    def artifact_metadata(self, workflow_id: str) -> list[WorkflowArtifact]:
        return self.artifacts.list_for_workflow(workflow_id)

    def progress_snapshot(self, workflow_id: str) -> dict[str, object]:
        """Read-only run progress for `workflow status`, without artifact bodies."""
        workflow = self.store.get(workflow_id)
        if workflow.mode != WorkflowMode.ORCHESTRATED:
            return {
                "stage": workflow.status.value,
                "current_review_cycle": None,
                "max_review_cycles": None,
                "roles": [_progress_role(workflow)],
                "readiness": [],
            }
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        if root_id is None:
            raise ValueError("orchestrated role has no root manager")
        manager = self.store.get(root_id)
        children = self.store.children(root_id)
        try:
            run = self.store.orchestration_run(root_id)
        except ValueError:
            run = None
        waiting_owners = set(self.queue.active_owners()) if self.queue is not None else set()
        resource_waiting = any(
            record.status == WorkflowStatus.RUNNING and record.id in waiting_owners
            for record in (manager, *children)
        )
        return {
            "stage": progress_stage(
                manager=manager,
                children=children,
                run=run,
                resource_waiting=resource_waiting,
            ),
            "current_review_cycle": None if run is None else run.current_review_cycle,
            "max_review_cycles": None if run is None else run.max_review_cycles,
            "roles": [_progress_role(manager), *(_progress_role(child) for child in children)],
            "readiness": [asdict(item) for item in self.store.role_readiness_history(root_id)],
        }

    def _artifact_prompt_context(
        self, workflow: WorkflowRecord
    ) -> tuple[str | None, str | None, str | None]:
        if workflow.mode != WorkflowMode.ORCHESTRATED:
            return None, None, None
        if workflow.role == WorkflowRole.WORKER:
            try:
                reviews = []
                for artifact in self.artifacts.list_for_kind(workflow.id, "review"):
                    reviews.append(
                        self.artifacts.read_digest(artifact.workflow_id, "review", artifact.sha256)
                    )
                review = (
                    "\n\n".join(
                        f"Reviewer slot artifact {index}:\n{content}"
                        for index, content in enumerate(reviews)
                    )
                    or None
                )
            except ValueError:
                review = None
            return self.artifacts.read(workflow.id, "plan")[1], None, review
        if workflow.role == WorkflowRole.REVIEWER:
            return (
                self.artifacts.read(workflow.id, "plan")[1],
                self.artifacts.read(workflow.id, "verification")[1],
                None,
            )
        return None, None, None

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
        attach_existing: bool = False,
        model: str | None = None,
    ) -> WorkflowRecord:
        """Launch one durable record through an adapter-neutral runtime."""
        workflow = self.store.get(workflow_id)
        if attach_existing and workflow.parent_id:
            raise ValueError("child roles cannot attach to an existing worktree")
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
            self._verify_persisted_identity(source, runtime)
        manager_plan, worker_verification, review_feedback = self._artifact_prompt_context(workflow)
        review_source_urls = (
            self.store.registered_source_urls(workflow.run_id)
            if workflow.role == WorkflowRole.REVIEWER
            else ()
        )
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
            issue_url=(None if workflow.role == WorkflowRole.REVIEWER else workflow.issue_url),
            review_source_urls=review_source_urls,
            manager_plan=manager_plan,
            worker_verification=worker_verification,
            review_feedback=review_feedback,
        )

        def create_external():
            self._reconcile_name_ghost(workflow, runtime)
            if attach_existing:
                return runtime.attach_new_agent(
                    Path(workflow.repository),
                    workflow.name,
                    workflow.mode,
                    agent,
                    prompt,
                    role=workflow.role,
                    model=model,
                )
            issue_number = _matching_github_issue_number(
                Path(workflow.repository), workflow.issue_url
            )
            return runtime.start(
                Path(workflow.repository),
                workflow.name,
                workflow.mode,
                agent,
                prompt,
                role=workflow.role,
                parent_worktree_id=parent_worktree_id,
                base_branch=base_branch,
                comment=merge_workflow_marker(workflow.issue_url, workflow.run_id, workflow.id),
                github_issue_number=issue_number,
                model=model,
            )

        issue_urls = (workflow.issue_url,) if workflow.issue_url else ()
        identity = getattr(runtime, "implementation_identity", None)
        identify_external = None
        if callable(identity):
            identify_external = lambda external: identity(
                str(getattr(external, "worktree_id", "")),
                str(getattr(external, "worktree", "")),
            )
        started = self.start_existing(
            workflow.id,
            create_external,
            lambda worktree_id: runtime.set_lifecycle(
                worktree_id, WorkflowStatus.RUNNING, issue_urls=issue_urls
            ),
            runtime.close_terminals,
            runtime.remove_worktree,
            identify_external,
            allow_duplicate=allow_duplicate,
            already_starting=already_starting,
        )
        self.store.record_agent_run(started.id, agent, started.terminal_handle, model=model)
        if observer_command is not None:
            self.attach_observer(started.id, runtime, observer_command)
            started = self.store.get(started.id)
        return started

    @staticmethod
    def matching_github_issue_number(repository: Path, issue_url: str | None) -> int | None:
        """Return an Orca-linkable issue only for the implementation repository itself."""
        return _matching_github_issue_number(repository, issue_url)

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
        observer_command: str | None = None,
        observer_enabled: bool | None = None,
        model: str | None = None,
    ) -> WorkflowRecord:
        """Resume a persisted running workflow in its exact owned Orca worktree."""
        workflow = self.store.get(workflow_id)
        if workflow.status != WorkflowStatus.RUNNING:
            raise ValueError("only a running workflow can be resumed")
        if not workflow.adapter_reference or not workflow.worktree_path:
            raise ValueError("running workflow has incomplete persisted worktree ownership")
        manager_plan, worker_verification, review_feedback = self._artifact_prompt_context(workflow)
        review_source_urls = (
            self.store.registered_source_urls(workflow.run_id)
            if workflow.role == WorkflowRole.REVIEWER
            else ()
        )
        prompt = render_resume_prompt(
            objective=workflow.objective,
            role=workflow.role,
            response_language=response_language,
            skill_paths=skill_paths,
            workflow_id=workflow.id,
            resource_names=resource_names,
            config_path=config_path,
            handoff_summary=self._handoff_summary(workflow),
            issue_url=(None if workflow.role == WorkflowRole.REVIEWER else workflow.issue_url),
            review_source_urls=review_source_urls,
            manager_plan=manager_plan,
            worker_verification=worker_verification,
            review_feedback=review_feedback,
        )
        runtime.verify_worktree(workflow.adapter_reference, workflow.worktree_path)
        self._verify_persisted_identity(workflow, runtime)
        terminal_handle = workflow.terminal_handle
        if not runtime.terminal_is_valid(workflow.adapter_reference, terminal_handle):
            replacement = runtime.create_new_agent_terminal(
                workflow.adapter_reference,
                workflow.worktree_path,
                agent,
                prompt,
                model=model,
            )
            try:
                self._replace_agent_terminal(workflow, runtime, replacement)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                self._discard_unowned_terminal(runtime, workflow.adapter_reference, replacement)
                raise
            resumed = self.store.mark_resumed(workflow.id)
            self.store.record_agent_run(resumed.id, agent, replacement, model=model)
            return self._after_resume(resumed.id, runtime, observer_command, observer_enabled)
        if not terminal_handle:
            raise ValueError("running workflow has no resumable agent terminal")
        runtime.wait_for_agent(terminal_handle)
        runtime.send_prompt(terminal_handle, prompt)
        resumed = self.store.mark_resumed(workflow.id)
        self.store.record_agent_run(resumed.id, agent, terminal_handle, model=model)
        return self._after_resume(resumed.id, runtime, observer_command, observer_enabled)

    def _after_resume(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        observer_command: str | None,
        observer_enabled: bool | None,
    ) -> WorkflowRecord:
        resumed = self.store.get(workflow_id)
        if observer_enabled is not None:
            resumed = self.store.set_queue_observer_enabled(resumed.id, observer_enabled)
        self._ensure_observer(resumed.id, runtime, observer_command)
        return self.store.get(resumed.id)

    def restart_with_new_agent(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        agent: str,
        response_language: str,
        skill_paths: tuple[Path, ...],
        resource_names: tuple[str, ...] = (),
        config_path: Path | None = None,
        objective: str | None = None,
        observer_command: str | None = None,
        observer_enabled: bool | None = None,
        model: str | None = None,
    ) -> WorkflowRecord:
        """Continue a workflow with a new agent process, never its old session."""
        if objective is not None:
            self.store.update_objective(workflow_id, objective)
        workflow = self.store.get(workflow_id)
        if workflow.status != WorkflowStatus.RUNNING:
            raise ValueError("only a running workflow can be restarted with a new agent")
        if not workflow.adapter_reference or not workflow.worktree_path:
            raise ValueError("running workflow has incomplete persisted worktree ownership")
        manager_plan, worker_verification, review_feedback = self._artifact_prompt_context(workflow)
        review_source_urls = (
            self.store.registered_source_urls(workflow.run_id)
            if workflow.role == WorkflowRole.REVIEWER
            else ()
        )
        prompt = render_start_prompt(
            objective=workflow.objective,
            mode=workflow.mode,
            role=workflow.role,
            response_language=response_language,
            skill_paths=skill_paths,
            workflow_id=workflow.id,
            resource_names=resource_names,
            config_path=config_path,
            handoff_summary=self._handoff_summary(workflow),
            issue_url=(None if workflow.role == WorkflowRole.REVIEWER else workflow.issue_url),
            review_source_urls=review_source_urls,
            manager_plan=manager_plan,
            worker_verification=worker_verification,
            review_feedback=review_feedback,
        )
        runtime.verify_worktree(workflow.adapter_reference, workflow.worktree_path)
        self._verify_persisted_identity(workflow, runtime)
        replacement = runtime.create_new_agent_terminal(
            workflow.adapter_reference,
            workflow.worktree_path,
            agent,
            prompt,
            model=model,
        )
        try:
            self._replace_agent_terminal(workflow, runtime, replacement)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            self._discard_unowned_terminal(runtime, workflow.adapter_reference, replacement)
            raise
        restarted = self.store.mark_resumed(workflow.id)
        if observer_enabled is not None:
            restarted = self.store.set_queue_observer_enabled(restarted.id, observer_enabled)
        self._ensure_observer(restarted.id, runtime, observer_command)
        return self.store.get(restarted.id)

    def _handoff_summary(self, workflow: WorkflowRecord) -> str | None:
        if workflow.parent_id is None:
            return None
        return self.store.handoff_for(workflow.id).summary

    def _ensure_observer(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        command: str | None,
        *,
        fail_workflow: bool = True,
    ) -> None:
        workflow = self.store.get(workflow_id)
        if command is None or not workflow.queue_observer_enabled:
            return
        handles = self.store.owned_terminal_handles(workflow.id, kind="observer")
        stale = [
            handle
            for handle in handles
            if not workflow.adapter_reference
            or not runtime.terminal_is_valid(workflow.adapter_reference, handle)
        ]
        live = [handle for handle in handles if handle not in stale]
        if stale:
            self.store.forget_owned_terminals(
                workflow.id, stale, kind="observer", reason="observer_terminal_unavailable"
            )
            if workflow.adapter_reference:
                for handle in stale:
                    try:
                        runtime.close_terminals(workflow.adapter_reference, handle)
                    except (OSError, RuntimeError):
                        continue
        if live:
            return
        self.attach_observer(workflow.id, runtime, command, fail_workflow=fail_workflow)

    def attach_observer(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        command: str,
        *,
        fail_workflow: bool = True,
    ) -> str:
        """Create and persist a queue observer or fail the owned workflow cleanly."""
        workflow = self.store.get(workflow_id)
        if workflow.status != WorkflowStatus.RUNNING or not workflow.adapter_reference:
            raise ValueError("queue observer requires a running workflow")
        handle = ""
        try:
            handle = runtime.create_observer(workflow.adapter_reference, command)
            self.store.add_owned_terminal(workflow.id, handle, "observer", require_running=True)
            self.store.set_queue_observer_enabled(workflow.id, True)
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
            if not fail_workflow:
                if handle:
                    try:
                        runtime.close_terminals(workflow.adapter_reference, handle)
                    except (OSError, RuntimeError):
                        pass
                raise RuntimeError(f"queue observer restart failed: {exc}") from exc
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
        children = list(self.store.children(parent_id))
        worker = next((child for child in children if child.role == WorkflowRole.WORKER), None)
        reviewers = [child for child in children if child.role == WorkflowRole.REVIEWER]
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
        if worker and worker.status == WorkflowStatus.COMPLETED:
            for reviewer in reviewers:
                if reviewer.status != WorkflowStatus.REQUESTED:
                    continue
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
        if target in {WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}:
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
        finished = self.store.get(finished.id)
        if (
            target in {WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
            and not finished.owns_worktree
            and finished.adapter_reference
            and finished.external_reconciled_at is None
        ):
            finished = self.store.mark_external_reconciled(finished.id)
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
        observer_handles = set(self.store.owned_terminal_handles(workflow.id, kind="observer"))
        coordinator_handles = set(
            self.store.owned_terminal_handles(workflow.id, kind="coordinator")
        )
        for handle in self.store.owned_terminal_handles(workflow.id):
            if handle in coordinator_handles:
                continue
            try:
                close_external(workflow.adapter_reference, handle)
                if handle in observer_handles:
                    self.store.record_observer_termination(
                        workflow.id, handle, f"workflow_{workflow.status.value}"
                    )
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(f"{handle}: {exc}")
        if cleanup_errors:
            detail = "owned terminal cleanup failed: " + "; ".join(cleanup_errors)
            self.store.set_cleanup_error(workflow.id, detail)
            raise RuntimeError(detail)
        if workflow.status == WorkflowStatus.COMPLETED:
            self.store.set_cleanup_error(workflow.id, None)

    def attach_coordinator(self, manager_id: str, runtime: WorkflowRuntime, command: str) -> str:
        """Create the one durable coordinator terminal for an orchestrated root."""
        return self.attach_watchdog(manager_id, runtime, command, orchestrated=True)

    def attach_watchdog(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        command: str,
        *,
        orchestrated: bool | None = None,
    ) -> str:
        """Create the durable supervisor terminal for a root workflow."""
        manager = self.store.get(workflow_id)
        if manager.parent_id is not None or not manager.adapter_reference:
            raise ValueError("watchdog requires a root workflow with a runtime")
        required_orchestrated = (
            manager.mode == WorkflowMode.ORCHESTRATED if orchestrated is None else orchestrated
        )
        if required_orchestrated:
            if manager.mode != WorkflowMode.ORCHESTRATED or manager.role != WorkflowRole.MANAGER:
                raise ValueError("coordinator requires an orchestrated manager with a runtime")
            if self.store.orchestration_run(manager.id).status != "running":
                raise ValueError("coordinator requires a running orchestration")
        elif manager.mode != WorkflowMode.SINGLE:
            raise ValueError("single watchdog requires a single-mode root")
        existing = self.store.owned_terminal_handles(manager.id, kind="coordinator")
        live = [
            handle
            for handle in existing
            if runtime.terminal_is_valid(manager.adapter_reference, handle)
        ]
        if live:
            return live[0]
        if existing:
            self.store.forget_owned_terminals(
                manager.id,
                existing,
                kind="coordinator",
                reason="coordinator_terminal_unavailable",
            )
        handle = runtime.create_coordinator(manager.adapter_reference, command)
        try:
            self.store.add_owned_terminal(manager.id, handle, "coordinator", require_running=False)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            self._discard_unowned_terminal(runtime, manager.adapter_reference, handle)
            raise
        return handle

    def retry_coordinator(
        self, manager_id: str, runtime: WorkflowRuntime, command: str
    ) -> tuple[OrchestrationRun, str]:
        """Close stale ownership, CAS-reset a recoverable run, and attach current code."""
        manager = self.store.get(manager_id)
        if (
            manager.mode != WorkflowMode.ORCHESTRATED
            or manager.role != WorkflowRole.MANAGER
            or not manager.adapter_reference
        ):
            raise ValueError("coordinator retry requires an orchestrated manager with a runtime")
        run = self.store.orchestration_run(manager.id)
        if run.status != "running":
            handles = set(self.store.owned_terminal_handles(manager.id, kind="coordinator"))
            if run.coordinator_handle:
                handles.add(run.coordinator_handle)
            for handle in sorted(handles):
                if not runtime.terminal_is_valid(manager.adapter_reference, handle):
                    continue
                try:
                    runtime.close_terminals(manager.adapter_reference, handle)
                except (OSError, RuntimeError) as exc:
                    code = str(getattr(exc, "code", ""))
                    if code != "selector_not_found" and "selector_not_found" not in str(exc):
                        raise
            run = self.store.retry_orchestration(manager.id, expected_updated_at=run.updated_at)
        handle = self.attach_coordinator(manager.id, runtime, command)
        return self.store.orchestration_run(manager.id), handle

    def release_coordinator(self, manager_id: str, reason: str) -> tuple[str, ...]:
        """Record coordinator release before the supervisor flushes output and self-closes."""
        return self.store.release_coordinator_ownership(manager_id, reason)

    def close_coordinator(
        self, manager_id: str, runtime: WorkflowRuntime, reason: str
    ) -> tuple[str, ...]:
        """Close the coordinator after output flush, or later as an operator fallback."""
        manager = self.store.get(manager_id)
        owned = self.release_coordinator(manager_id, reason)
        run = self.store.orchestration_run(manager_id)
        recorded = (run.coordinator_handle,) if run.coordinator_handle else ()
        handles = owned or recorded
        if not manager.adapter_reference:
            return ()
        for handle in handles:
            runtime.close_terminals(manager.adapter_reference, handle)
        return tuple(handles)

    def close_watchdog(
        self, workflow_id: str, runtime: WorkflowRuntime, reason: str
    ) -> tuple[str, ...]:
        workflow = self.store.get(workflow_id)
        if workflow.mode == WorkflowMode.ORCHESTRATED:
            return self.close_coordinator(workflow_id, runtime, reason)
        handles = tuple(self.store.owned_terminal_handles(workflow.id, kind="coordinator"))
        if handles:
            self.store.forget_owned_terminals(
                workflow.id, handles, kind="coordinator", reason=reason
            )
        if not workflow.adapter_reference:
            return ()
        for handle in handles:
            try:
                runtime.close_terminals(workflow.adapter_reference, handle)
            except (OSError, RuntimeError) as exc:
                if not self._missing_selector(exc):
                    raise
        return handles

    def _root_workflow(self, workflow_id: str) -> WorkflowRecord:
        workflow = self.store.get_by_run_or_step_id(workflow_id)
        if workflow.parent_id is None:
            return workflow
        return self.store.get(workflow.parent_id)

    def _require_finished_root(self, root: WorkflowRecord, *, allow_approved: bool = False) -> None:
        if root.mode == WorkflowMode.ORCHESTRATED:
            run = self.store.orchestration_run(root.id)
            if run.status in _TERMINAL_RUN_STATUSES:
                return
            if allow_approved:
                try:
                    self._approved_worker_sha(root, require_completed_run=False)
                except ValueError:
                    raise ValueError(
                        "harvest and retire require a completed, blocked, or failed orchestration"
                    ) from None
                return
            raise ValueError(
                "harvest and retire require a completed, blocked, or failed orchestration"
            )
        if root.status not in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }:
            raise ValueError("harvest and retire require a finished workflow")

    @staticmethod
    def _missing_selector(exc: BaseException) -> bool:
        code = str(getattr(exc, "code", ""))
        return code == "selector_not_found" or "selector_not_found" in str(exc)

    @staticmethod
    def _implementation_worktree_present(runtime: WorkflowRuntime, worktree_path: str) -> bool:
        checker = getattr(runtime, "implementation_worktree_present", None)
        if not callable(checker):
            return True
        return bool(checker(worktree_path))

    def _approved_worker_sha(
        self, root: WorkflowRecord, *, require_completed_run: bool = True
    ) -> str:
        run = self.store.orchestration_run(root.id)
        if require_completed_run and run.status != "completed":
            raise ValueError("delivery requires a completed orchestration")
        reviewers = [
            child for child in self.store.children(root.id) if child.role == WorkflowRole.REVIEWER
        ]
        if not reviewers or any(
            reviewer.status != WorkflowStatus.COMPLETED or not reviewer.start_sha
            for reviewer in reviewers
        ):
            raise ValueError("delivery requires every reviewer to complete on a recorded SHA")
        approved_shas = {reviewer.start_sha for reviewer in reviewers}
        if len(approved_shas) != 1:
            raise ValueError("reviewers did not approve the same implementation SHA")
        return next(iter(approved_shas))

    def delivery_check(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        require_completed_run: bool = True,
    ) -> DeliveryCheckResult:
        """Fail closed unless the manager is at the exact SHA approved by all reviewers."""
        root = self._root_workflow(workflow_id)
        if root.mode != WorkflowMode.ORCHESTRATED:
            return DeliveryCheckResult(root.id, False, None, None, "workflow is not orchestrated")
        try:
            approved_sha = self._approved_worker_sha(
                root, require_completed_run=require_completed_run
            )
        except ValueError as exc:
            return DeliveryCheckResult(root.id, False, None, None, str(exc))
        if not root.adapter_reference or not root.worktree_path:
            return DeliveryCheckResult(
                root.id, False, approved_sha, None, "manager worktree is unavailable"
            )
        self._verify_persisted_identity(root, runtime)
        _repository, _runtime_id, actual_sha = runtime.implementation_identity(
            root.adapter_reference, root.worktree_path
        )
        if not actual_sha:
            return DeliveryCheckResult(
                root.id, False, approved_sha, None, "manager worktree has no verifiable HEAD"
            )
        if actual_sha != approved_sha:
            return DeliveryCheckResult(
                root.id,
                False,
                approved_sha,
                actual_sha,
                "manager HEAD is not the locally approved implementation SHA; harvest first",
            )
        return DeliveryCheckResult(root.id, True, approved_sha, actual_sha)

    def harvest(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        dry_run: bool = False,
        allow_approved: bool = False,
    ) -> HarvestResult:
        """Copy worker commits into the manager worktree without closing resources."""
        root = self._root_workflow(workflow_id)
        self._require_finished_root(root, allow_approved=allow_approved)
        if root.mode != WorkflowMode.ORCHESTRATED:
            return HarvestResult(
                root.id,
                "skipped",
                None,
                None,
                None,
                skip_reason="not_orchestrated",
                dry_run=dry_run,
            )
        worker = next(
            (child for child in self.store.children(root.id) if child.role == WorkflowRole.WORKER),
            None,
        )
        if (
            worker is None
            or not worker.adapter_reference
            or not worker.worktree_path
            or not root.adapter_reference
            or not root.worktree_path
        ):
            return HarvestResult(
                root.id,
                "skipped",
                None,
                None,
                None,
                skip_reason="no_worker_worktree",
                dry_run=dry_run,
            )
        if worker.external_reconciled_at is not None:
            return HarvestResult(
                root.id,
                "skipped",
                None,
                None,
                None,
                skip_reason="worker_worktree_gone",
                dry_run=dry_run,
            )
        if not self._implementation_worktree_present(runtime, worker.worktree_path):
            return HarvestResult(
                root.id,
                "skipped",
                None,
                None,
                None,
                skip_reason="worker_worktree_gone",
                dry_run=dry_run,
            )
        self._verify_persisted_identity(root, runtime)
        self._verify_persisted_identity(worker, runtime)
        _repository, _runtime_id, worker_sha = runtime.implementation_identity(
            worker.adapter_reference, worker.worktree_path
        )
        if not worker_sha:
            raise ValueError("worker worktree has no verifiable HEAD")
        run = self.store.orchestration_run(root.id)
        if run.status == "completed" or allow_approved:
            approved_sha = self._approved_worker_sha(
                root, require_completed_run=run.status == "completed"
            )
            if worker_sha != approved_sha:
                raise ValueError(
                    "worker HEAD changed after local approval; "
                    "refusing to harvest unreviewed commits"
                )
            worker_sha = approved_sha
        if root.start_sha and worker_sha == root.start_sha:
            return HarvestResult(
                root.id,
                "skipped",
                root.start_sha,
                root.start_sha,
                worker_sha,
                skip_reason="no_implementation_commits",
                dry_run=dry_run,
            )
        integrated = runtime.integrate_worker_commit(
            root.worktree_path, worker.worktree_path, worker_sha, dry_run=dry_run
        )
        return HarvestResult(
            root.id,
            str(integrated["method"]),
            integrated.get("before"),
            integrated.get("after"),
            worker_sha,
            dry_run=dry_run,
        )

    def deliver_approved(
        self, workflow_id: str, runtime: WorkflowRuntime, *, force_push: bool = False
    ) -> dict[str, object]:
        """Harvest the approved tip into the manager worktree and fast-forward push once."""
        if force_push:
            raise ValueError("force push is not supported")
        harvested = self.harvest(workflow_id, runtime, allow_approved=True)
        check = self.delivery_check(workflow_id, runtime, require_completed_run=False)
        if not check.eligible:
            raise ValueError(check.reason or "delivery-check failed")
        root = self._root_workflow(workflow_id)
        if not root.worktree_path:
            raise ValueError("manager worktree is unavailable")
        pushed = runtime.push_fast_forward(root.worktree_path)
        return {"harvested": harvested, "check": check, "pushed": pushed}

    def try_progress_push(
        self, workflow_id: str, runtime: WorkflowRuntime
    ) -> dict[str, object] | None:
        """Fast-forward push a verified blocked/failed tip once. Never force-push."""
        root = self._root_workflow(workflow_id)
        if root.mode != WorkflowMode.ORCHESTRATED or not root.worktree_path:
            return None
        try:
            harvested = self.harvest(root.id, runtime)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if harvested.skip_reason or harvested.method not in {"ff", "already"}:
            return None
        try:
            pushed = runtime.push_fast_forward(root.worktree_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return {"harvested": harvested, "pushed": pushed}

    def _close_role_resources(
        self,
        workflow: WorkflowRecord,
        runtime: WorkflowRuntime,
        *,
        remove_worktree: bool,
        extra_handles: tuple[str, ...] = (),
        dry_run: bool,
    ) -> tuple[tuple[str, str], ...]:
        if not workflow.adapter_reference:
            return ()
        handles = set(self.store.owned_terminal_handles(workflow.id))
        if workflow.terminal_handle:
            handles.add(workflow.terminal_handle)
        handles.update(handle for handle in extra_handles if handle)
        closed: list[tuple[str, str]] = []
        for handle in sorted(handles):
            closed.append((workflow.adapter_reference, handle))
            if dry_run:
                continue
            try:
                runtime.close_terminals(workflow.adapter_reference, handle)
            except (OSError, RuntimeError) as exc:
                if not self._missing_selector(exc):
                    raise
        if dry_run:
            return tuple(closed)
        if remove_worktree and workflow.owns_worktree:
            try:
                runtime.remove_worktree(workflow.adapter_reference)
            except (OSError, RuntimeError) as exc:
                if not self._missing_selector(exc):
                    raise
        if workflow.external_reconciled_at is None:
            self.store.mark_external_reconciled(workflow.id)
        return tuple(closed)

    def retire(
        self,
        workflow_id: str,
        runtime: WorkflowRuntime,
        *,
        keep: str,
        dry_run: bool = False,
    ) -> RetireResult:
        """Harvest worker commits, then close owned child (and optionally manager) resources."""
        if keep not in {"manager", "none"}:
            raise ValueError("retire --keep must be manager or none")
        root = self._root_workflow(workflow_id)
        harvested = self.harvest(root.id, runtime, dry_run=dry_run)
        children = [child for child in self.store.children(root.id)]
        closed: list[tuple[str, str]] = []
        removed: list[str] = []
        for child in children:
            if not child.adapter_reference:
                continue
            will_remove = child.owns_worktree and child.external_reconciled_at is None
            closed.extend(
                self._close_role_resources(
                    child, runtime, remove_worktree=will_remove, dry_run=dry_run
                )
            )
            if will_remove:
                removed.append(child.adapter_reference)
        extra = ()
        if keep == "none":
            try:
                run = self.store.orchestration_run(root.id)
                extra = (run.coordinator_handle,) if run.coordinator_handle else ()
            except ValueError:
                extra = ()
            will_remove = root.owns_worktree and root.external_reconciled_at is None
            closed.extend(
                self._close_role_resources(
                    root,
                    runtime,
                    remove_worktree=will_remove,
                    extra_handles=extra,
                    dry_run=dry_run,
                )
            )
            if will_remove and root.adapter_reference:
                removed.append(root.adapter_reference)
        kept: list[str] = []
        if root.adapter_reference and (keep == "manager" or not root.owns_worktree):
            kept.append(root.adapter_reference)
        return RetireResult(
            root.id,
            keep,
            harvested,
            tuple(closed),
            tuple(dict.fromkeys(removed)),
            tuple(kept),
            dry_run=dry_run,
        )

    def try_retire_keep_manager(
        self, workflow_id: str, runtime: WorkflowRuntime
    ) -> dict[str, object]:
        """Retire owned children after a terminal run. Never roll back the outcome."""
        root = self._root_workflow(workflow_id)
        try:
            retired = self.retire(root.id, runtime, keep="manager")
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
            self.store.set_cleanup_error(root.id, str(exc))
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": retired}

    def begin_next_review_cycle(
        self, manager_id: str, reviewer_readiness_id: str, runtime: WorkflowRuntime
    ) -> None:
        """Advance the reused worker record to the reviewed commit before its next cycle."""
        children = {child.role: child for child in self.store.children(manager_id)}
        worker = children.get(WorkflowRole.WORKER)
        if (
            worker is None
            or not worker.adapter_reference
            or not worker.worktree_path
            or not worker.implementation_repository
            or not worker.runtime_repository_id
        ):
            raise ValueError("worker has incomplete persisted implementation identity")
        repository, runtime_repository_id, head_sha = runtime.implementation_identity(
            worker.adapter_reference, worker.worktree_path
        )
        if (
            repository != worker.implementation_repository
            or runtime_repository_id != worker.runtime_repository_id
        ):
            raise ValueError("worker repository identity changed before the next review cycle")
        self.store.prepare_next_review_cycle(
            manager_id,
            worker_start_sha=head_sha,
            reviewer_readiness_id=reviewer_readiness_id,
        )

    def reconcile_stale(
        self,
        max_age_seconds: float,
        close_external: Callable[[str, str | None], None],
        remove_external: Callable[[str], None],
        *,
        workflow_ids: tuple[str, ...] | None = None,
    ) -> ReconcileResult:
        reconciled: list[WorkflowRecord] = []
        reconciliation_errors: list[str] = []
        selected = {workflow.id for workflow in self.store.stale_active(max_age_seconds)}
        selected.update(workflow.id for workflow in self.store.stale_reconcilable(max_age_seconds))
        if workflow_ids is not None:
            requested = tuple(
                dict.fromkeys(workflow_id.strip() for workflow_id in workflow_ids if workflow_id)
            )
            if not requested:
                raise ValueError("workflow identifier is required")
            selected = set(requested)
        for workflow_id in sorted(selected):
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
                        if current.owns_worktree:
                            remove_external(current.adapter_reference)
                    except (OSError, RuntimeError) as exc:
                        if "selector_not_found" not in str(exc):
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
        return ReconcileResult(reconciled, tuple(reconciliation_errors))
