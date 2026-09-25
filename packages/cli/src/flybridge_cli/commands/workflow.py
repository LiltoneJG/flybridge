from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from flybridge_application import (
    WorkflowService,
    reconcile_external_state,
    validate_skill_paths,
    worktree_is_excluded,
)
from flybridge_core import (
    MAX_ARTIFACT_BYTES,
    ConfigError,
    ReconcileStore,
    ResourceQueue,
    WorkflowMode,
    WorkflowRole,
    WorkflowStatus,
    WorkflowStore,
    issue_urls_from_text,
    parse_issue_url,
)

from ..runtime import (
    _adapter,
    _config,
    _coordinator_command,
    _generated_workflow_name,
    _observer_command,
    _optional_observer_command,
    _validate_mode_skill_paths,
)


def _read_objective_file(path: Path) -> str:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise ValueError(f"objective file does not exist: {resolved}")
    text = resolved.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("workflow objective is required")
    return text


def _read_artifact_file(path: Path) -> str:
    resolved = path.expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("artifact input must be a regular file")
        if details.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            payload = stream.read(MAX_ARTIFACT_BYTES + 1)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("artifact input is not valid UTF-8") from exc


def _read_artifact_stdin() -> str:
    text = sys.stdin.read(MAX_ARTIFACT_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit")
    return text


def _resolve_objective(objective: str | None, objective_file: Path | None) -> str:
    if objective_file is not None and objective:
        raise ValueError("pass only one of --objective and --objective-file")
    if objective_file is not None:
        return _read_objective_file(objective_file)
    if objective is None or not str(objective).strip():
        raise ValueError("workflow objective is required")
    text = str(objective).strip()
    if text.startswith("@"):
        referenced = text[1:]
        if not referenced:
            raise ValueError("objective file path is required after @")
        return _read_objective_file(Path(referenced))
    return text


def _objective_source_path(objective: str | None, objective_file: Path | None) -> Path | None:
    if objective_file is not None:
        return objective_file.expanduser().resolve()
    if objective is not None and str(objective).strip().startswith("@"):
        return Path(str(objective).strip()[1:]).expanduser().resolve()
    return None


def _observer_wanted(config, queue_observer: bool | None) -> bool:
    if queue_observer is not None:
        return queue_observer
    return bool(config.queue_observer)


def _start_mode(config, mode: str | None, *, attach_existing: bool) -> WorkflowMode:
    if mode:
        return WorkflowMode(mode)
    if attach_existing:
        return WorkflowMode.SINGLE
    return config.default_mode


def _optional_adapter(config):
    try:
        return _adapter(config)
    except (OSError, RuntimeError, ValueError):
        return None


def _sync_orca(config, *, fatal: bool) -> dict[str, object]:
    if not config.reconcile.auto_before_workflow_commands:
        return {"observed_at": None, "fresh": False, "error": None, "truncated": False}
    client = _optional_adapter(config) if not fatal else _adapter(config)
    if client is not None and not hasattr(client, "list_worktrees"):
        return {"observed_at": None, "fresh": False, "error": None, "truncated": False}
    store = ReconcileStore(config.state_dir, event_retention=config.reconcile.event_retention)
    if client is None:
        summary = store.record_failure("Orca synchronization is unavailable")
    else:
        summary = reconcile_external_state(
            client,
            store,
            include_git=False,
            missing_observations=config.reconcile.missing_observations,
            missing_grace_seconds=config.reconcile.missing_grace_seconds,
            exclude_worktrees=config.reconcile.exclude_worktrees,
        ).orca
    if fatal and not summary.success:
        raise RuntimeError(summary.errors[0] if summary.errors else "Orca synchronization failed")
    return store.freshness()


def _owner_terminal_valid(workflow, client) -> bool:
    if client is None:
        return False
    if (
        workflow.status != WorkflowStatus.RUNNING
        or not workflow.adapter_reference
        or not workflow.terminal_handle
    ):
        return False
    try:
        return bool(client.terminal_is_valid(workflow.adapter_reference, workflow.terminal_handle))
    except (OSError, RuntimeError, ValueError, AttributeError):
        return False


def _workflow_list_item(workflow, *, owner_terminal_valid: bool) -> dict[str, object]:
    return {
        "id": workflow.id,
        "run_id": workflow.run_id,
        "name": workflow.name,
        "status": workflow.status,
        "role": workflow.role,
        "mode": workflow.mode,
        "repository": workflow.repository,
        "worktree_path": workflow.worktree_path,
        "adapter_reference": workflow.adapter_reference,
        "updated_at": workflow.updated_at,
        "owns_worktree": workflow.owns_worktree,
        "queue_observer_enabled": workflow.queue_observer_enabled,
        "owner_terminal_valid": owner_terminal_valid,
        "observer_stop_reason": workflow.observer_stop_reason,
    }


def _agent_spec(config, workflow):
    try:
        return config.agent_spec(workflow.role, workflow.slot)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"orca.agents.{workflow.role} is required") from exc


def _launch_record(
    service: WorkflowService,
    config,
    workflow_id: str,
    *,
    observer_command: str | None = None,
    allow_duplicate: bool = False,
    already_starting: bool = False,
    attach_existing: bool = False,
) -> object:
    workflow = service.store.get(workflow_id)
    spec = _agent_spec(config, workflow)
    skill_paths = validate_skill_paths(config.skill_paths_for(workflow.role))
    client = _adapter(config)
    return service.launch_existing(
        workflow.id,
        client,
        agent=spec.agent,
        model=spec.model,
        response_language=config.response_language,
        skill_paths=skill_paths,
        resource_names=config.queue_resources,
        config_path=config.path,
        observer_command=observer_command,
        allow_duplicate=allow_duplicate,
        already_starting=already_starting,
        attach_existing=attach_existing,
    )


def _cmd_start(args: argparse.Namespace) -> int:
    if args.batch is not None:
        if args.repository is not None:
            raise ValueError("start --batch cannot be combined with a repository argument")
        if args.attach_existing:
            raise ValueError("start --batch always attaches existing worktrees")
        return _cmd_start_batch(args)
    if args.repository is None:
        raise ValueError("repository directory is required")
    config = _config(args)
    objective = _resolve_objective(args.objective, args.objective_file)
    objective_source = _objective_source_path(args.objective, args.objective_file)
    code, _extras = _start_one(
        config,
        repository=args.repository,
        mode=args.mode,
        name=args.name,
        objective=objective,
        objective_source=objective_source,
        allow_duplicate=args.allow_duplicate,
        queue_observer=args.queue_observer,
        attach_existing=args.attach_existing,
        issue=args.issue,
        print_result=True,
    )
    return code


def _cmd_start_batch(args: argparse.Namespace) -> int:
    config = _config(args)
    batch_path = Path(args.batch).expanduser()
    if not batch_path.is_file():
        raise ValueError(f"batch file does not exist: {batch_path}")
    try:
        payload = json.loads(batch_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"batch file is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise TypeError("batch file must contain a JSON array")
    results: list[dict[str, object]] = []
    ok = 0
    failed = 0
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            failed += 1
            row = {"index": index, "ok": False, "error": "batch item must be an object"}
            results.append(row)
            print(json.dumps(row, separators=(",", ":")), flush=True)
            continue
        try:
            path_value = item.get("path")
            repository_value = item.get("repository")
            if (
                isinstance(path_value, str)
                and path_value.strip()
                and isinstance(repository_value, str)
                and repository_value.strip()
                and path_value.strip() != repository_value.strip()
            ):
                raise ValueError("batch item cannot specify both path and repository")
            if not isinstance(path_value, str) or not path_value.strip():
                path_value = repository_value
            if not isinstance(path_value, str) or not path_value.strip():
                raise ValueError("batch item path is required")
            name = item.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError("batch item name must be a string")
            mode = item.get("mode")
            if mode is not None and mode not in {"single", "orchestrated"}:
                raise ValueError("batch item mode must be single or orchestrated")
            if "objective_file" in item and "objective" in item:
                raise ValueError("batch item cannot include both objective and objective_file")
            if "objective_file" in item:
                file_value = item["objective_file"]
                if not isinstance(file_value, str) or not file_value.strip():
                    raise ValueError("batch item objective_file is required")
                objective_path = Path(file_value).expanduser()
                if not objective_path.is_absolute():
                    objective_path = (batch_path.parent / objective_path).resolve()
                objective = _read_objective_file(objective_path)
                objective_source = objective_path
            else:
                objective = _resolve_objective(item.get("objective"), None)
                objective_source = _objective_source_path(item.get("objective"), None)
            issue = item.get("issue")
            if issue is not None and not isinstance(issue, str):
                raise ValueError("batch item issue must be a string")
            code, extras = _start_one(
                config,
                repository=Path(path_value),
                mode=mode,
                name=name,
                objective=objective,
                objective_source=objective_source,
                allow_duplicate=args.allow_duplicate,
                queue_observer=args.queue_observer,
                attach_existing=True,
                issue=issue,
                print_result=False,
            )
            if code != 0:
                raise RuntimeError(f"start exited with status {code}")
            ok += 1
            row = {
                "index": index,
                "ok": True,
                "path": str(Path(path_value).resolve()),
                **extras,
            }
            results.append(row)
            print(json.dumps(row, separators=(",", ":")), flush=True)
        except (ConfigError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
            failed += 1
            row = {
                "index": index,
                "ok": False,
                "path": item.get("path"),
                "error": str(exc),
            }
            results.append(row)
            print(json.dumps(row, separators=(",", ":")), flush=True)
    print(json.dumps({"ok": ok, "failed": failed, "results": results}, indent=2), flush=True)
    return 2 if failed else 0


def _resolve_issue_url(value: str | None, *, comment: str = "", attach_existing: bool) -> str:
    if value is not None and str(value).strip():
        return parse_issue_url(str(value).strip()).url
    if attach_existing:
        refs = issue_urls_from_text(comment)
        if refs:
            return refs[0].url
        raise ValueError(
            "GitHub issue URL is required; pass --issue or set it on the worktree comment"
        )
    raise ValueError("GitHub issue URL is required; pass --issue")


def _start_one(
    config,
    *,
    repository: Path,
    mode: str | None,
    name: str | None,
    objective: str,
    objective_source: Path | None = None,
    allow_duplicate: bool,
    queue_observer: bool | None,
    attach_existing: bool,
    issue: str | None,
    print_result: bool,
) -> tuple[int, dict[str, object]]:
    repository = repository.expanduser().resolve()
    extras: dict[str, object] = {"exclude_warning": False}
    if not repository.is_dir():
        raise ValueError(f"repository does not exist: {repository}")
    if worktree_is_excluded(str(repository), config.reconcile.exclude_worktrees):
        extras["exclude_warning"] = True
        print(
            f"warning: {repository} matches reconcile.exclude_worktrees; continuing",
            file=sys.stderr,
        )
    if not objective.strip():
        raise ValueError("workflow objective is required")
    resolved_mode = _start_mode(config, mode, attach_existing=attach_existing)
    workflow_name = name or _generated_workflow_name()
    issue_url = None
    if not attach_existing:
        issue_url = _resolve_issue_url(issue, attach_existing=False)
        _validate_mode_skill_paths(config, resolved_mode)
    client = _adapter(config)
    if hasattr(client, "list_worktrees"):
        _sync_orca(config, fatal=True)
    if not attach_existing:
        WorkflowService.prepare_repository(repository, client)
    service = WorkflowService(WorkflowStore(config.state_dir), ResourceQueue(config.state_dir))
    comment = ""
    worktree_id = None
    if attach_existing:
        worktree_id, _worktree_path = client.existing_worktree(repository)
        comment = client.worktree_comment(worktree_id)
        try:
            existing = service.store.find_by_adapter_reference(worktree_id)
        except ValueError:
            existing = None
        if existing is not None:
            if existing.status != WorkflowStatus.RUNNING:
                raise ValueError("active Flybridge workflow for this worktree is still starting")
            observer_enabled = _observer_wanted(config, queue_observer)
            spec = _agent_spec(config, existing)
            workflow = service.restart_with_new_agent(
                existing.id,
                client,
                agent=spec.agent,
                model=spec.model,
                response_language=config.response_language,
                skill_paths=validate_skill_paths(config.skill_paths_for(existing.role)),
                resource_names=config.queue_resources,
                config_path=config.path,
                objective=objective,
                observer_command=(
                    _observer_command(config.path, existing.id) if observer_enabled else None
                ),
                observer_enabled=observer_enabled,
            )
            service.store.set_objective_source(
                workflow.run_id,
                objective_source,
                hashlib.sha256(objective.encode("utf-8")).hexdigest()
                if objective_source is not None
                else None,
            )
            if print_result:
                print(
                    json.dumps(
                        {"workflow": asdict(workflow), "new_agent": True, **extras},
                        indent=2,
                    )
                )
            return 0, extras
        stale = service.store.find_unreconciled_by_adapter_reference(worktree_id)
        if stale is not None and stale.status in {
            WorkflowStatus.CANCELLED,
            WorkflowStatus.FAILED,
            WorkflowStatus.COMPLETED,
        }:
            service.store.mark_external_reconciled(stale.id)
        _validate_mode_skill_paths(config, resolved_mode)
        issue_url = _resolve_issue_url(issue, comment=comment, attach_existing=True)
        client.set_issue_comment(
            worktree_id,
            issue_url,
            github_issue_number=WorkflowService.matching_github_issue_number(repository, issue_url),
        )
    plan = service.store.reserve_root_plan(
        repository,
        resolved_mode,
        workflow_name,
        objective,
        allow_duplicate=allow_duplicate,
        issue_url=issue_url,
        max_review_cycles=config.max_review_cycles,
        reviewer_count=max(1, len(config.agent_specs(WorkflowRole.REVIEWER))),
    )
    service.store.set_objective_source(
        plan[0].run_id,
        objective_source,
        hashlib.sha256(objective.encode("utf-8")).hexdigest()
        if objective_source is not None
        else None,
    )
    if issue_url is not None:
        ReconcileStore(
            config.state_dir, event_retention=config.reconcile.event_retention
        ).link_reference(plan[0].run_id, issue_url, "primary")
    requested = plan[0]
    observer_enabled = _observer_wanted(config, queue_observer)
    workflow = _launch_record(
        service,
        config,
        requested.id,
        observer_command=(
            _observer_command(config.path, requested.id) if observer_enabled else None
        ),
        allow_duplicate=allow_duplicate,
        already_starting=True,
        attach_existing=attach_existing,
    )
    coordinator_handle = None
    watchdog_command = _coordinator_command(config.path, workflow.id)
    try:
        if resolved_mode == WorkflowMode.ORCHESTRATED:
            coordinator_handle = service.attach_coordinator(workflow.id, client, watchdog_command)
        else:
            coordinator_handle = service.attach_watchdog(
                workflow.id, client, watchdog_command, orchestrated=False
            )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        if resolved_mode == WorkflowMode.ORCHESTRATED:
            service.store.set_orchestration_outcome(
                workflow.id, "blocked", error=f"coordinator startup failed: {exc}"
            )
        raise
    children = plan[1:]
    if print_result:
        print(
            json.dumps(
                {
                    "run": service.store.get_run(workflow.run_id),
                    "workflow": asdict(workflow),
                    "planned_children": [asdict(child) for child in children],
                    "coordinator_terminal": coordinator_handle,
                    **extras,
                },
                indent=2,
            )
        )
    return 0, extras


def _finish_role(service: WorkflowService, client, workflow_id: str) -> None:
    """Complete an autonomous role while preserving its agent terminal for inspection."""
    service.finish(
        workflow_id,
        WorkflowStatus.COMPLETED,
        lambda reference: client.set_lifecycle(reference, WorkflowStatus.COMPLETED),
    )


def _verify_ready_artifact(service: WorkflowService, readiness) -> None:
    service.verify_readiness_artifact(readiness)


def _ensure_child_started(service: WorkflowService, config, child_id: str) -> None:
    """Launch a successor at most once, re-reading its state to survive a replay."""
    child = service.store.get(child_id)
    if child.status in {WorkflowStatus.RUNNING, WorkflowStatus.STARTING}:
        return
    if child.status != WorkflowStatus.REQUESTED:
        raise ValueError(f"{child.role} cannot resume autonomous launch from {child.status}")
    try:
        _launch_record(
            service, config, child.id, observer_command=_optional_observer_command(config, child.id)
        )
    except ValueError as exc:
        current = service.store.get(child_id)
        if current.status in {WorkflowStatus.RUNNING, WorkflowStatus.STARTING}:
            return
        raise ValueError(
            f"{child.role} cannot resume autonomous launch from {current.status}"
        ) from exc


def _dead_role(service: WorkflowService, manager_id: str) -> str | None:
    """Report a role that can no longer progress so the run never polls forever."""
    records = [service.store.get(manager_id), *service.store.children(manager_id)]
    dead = [
        record
        for record in records
        if record.status in {WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ]
    if not dead:
        return None
    return "; ".join(f"{record.role} is {record.status.value}" for record in dead)


def _remove_review_worktree(client, reviewer) -> None:
    """Treat an already-removed reviewer worktree as successful crash replay."""
    if not reviewer.adapter_reference or not reviewer.owns_worktree:
        return
    try:
        client.remove_worktree(reviewer.adapter_reference)
    except (OSError, RuntimeError) as exc:
        code = str(getattr(exc, "code", ""))
        if code != "selector_not_found" and "selector_not_found" not in str(exc):
            raise


def _ensure_rework_agent(service: WorkflowService, config, client, worker_id: str) -> None:
    """Replay the cycle transition until its worker has one live agent terminal."""
    worker = service.store.get(worker_id)
    if _owner_terminal_valid(worker, client):
        return
    try:
        spec = _agent_spec(config, service.store.get(worker_id))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError("orca.agents.worker is required") from exc
    service.restart_with_new_agent(
        worker.id,
        client,
        agent=spec.agent,
        model=spec.model,
        response_language=config.response_language,
        skill_paths=validate_skill_paths(config.skill_paths_for(WorkflowRole.WORKER)),
        resource_names=config.queue_resources,
        config_path=config.path,
        observer_command=_optional_observer_command(config, worker.id),
        observer_enabled=bool(config.queue_observer),
    )


def _age_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()


def _timeout_block(service: WorkflowService, client, workflow, reason: str) -> dict[str, object]:
    """Stop a running role, keep commits, and block the aggregate run when orchestrated."""
    if workflow.status == WorkflowStatus.RUNNING:
        service.finish(
            workflow.id,
            WorkflowStatus.CANCELLED,
            lambda reference: client.set_lifecycle(reference, WorkflowStatus.CANCELLED, reason),
            error=reason,
            close_external=client.close_terminals,
        )
    if workflow.mode == WorkflowMode.ORCHESTRATED:
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        if root_id is None:
            raise ValueError("orchestrated role has no root manager")
        service.store.set_orchestration_outcome(root_id, "blocked", error=reason)
        service.release_coordinator(root_id, "orchestration_blocked")
        run = service.store.orchestration_run(root_id)
        result = {
            "action": "blocked",
            "workflow_id": workflow.id,
            "reason": reason,
            "run": asdict(run),
        }
        progress = service.try_progress_push(root_id, client)
        if progress is not None:
            result["progress_push"] = {"pushed": progress["pushed"]}
        _attach_keep_manager_retire(service, client, root_id, result)
        return result
    return {"action": "blocked", "workflow_id": workflow.id, "reason": reason}


def _refresh_activation(store, workflow_id: str | None) -> None:
    if not workflow_id:
        return
    try:
        store.mark_resumed(str(workflow_id))
    except ValueError:
        return


def _promoted_owner(queue: ResourceQueue, lease_id: str | None) -> str | None:
    if not lease_id:
        return None
    try:
        return str(queue.inspect(lease_id)["owner"])
    except ValueError:
        return None


def _apply_watchdog(
    service: WorkflowService, config, client, root_id: str
) -> dict[str, object] | None:
    queue = service.queue if service.queue is not None else ResourceQueue(config.state_dir)
    root = service.store.get(root_id)
    records = (root, *service.store.children(root.id))
    owners = {record.id for record in records}
    for item in queue.leased_requests(owners=owners):
        request_id = str(item["request_id"])
        try:
            owner = service.store.get(str(item["owner"]))
        except ValueError:
            try:
                promoted = queue.cancel(request_id)
            except ValueError:
                continue
            _refresh_activation(service.store, _promoted_owner(queue, promoted))
            continue
        if owner.status == WorkflowStatus.RUNNING and _owner_terminal_valid(owner, client):
            continue
        try:
            promoted = queue.cancel(request_id)
        except ValueError:
            continue
        _refresh_activation(service.store, _promoted_owner(queue, promoted))
        if owner.status == WorkflowStatus.RUNNING:
            return _timeout_block(
                service, client, owner, f"resource-timeout: lease {item['resource']}"
            )
    waiting = set(queue.active_owners())
    for record in records:
        if record.status != WorkflowStatus.RUNNING or record.id in waiting:
            continue
        if service.store.has_unconsumed_readiness(record.id):
            continue
        age = _age_seconds(record.activated_at)
        if age is None:
            age = _age_seconds(record.updated_at)
        if age is not None and age > config.role_timeout_seconds:
            return _timeout_block(service, client, record, "role-timeout")
    return None


def _consume_declared_blocker(service: WorkflowService, client, readiness) -> dict[str, object]:
    """Close a blocked role, cancel successors, and atomically terminate the run."""
    service.verify_readiness_artifact(readiness)
    workflow = service.store.get(readiness.workflow_id)
    detail = f"{workflow.role.value} blocked: {readiness.blocked_reason}"
    if workflow.status == WorkflowStatus.RUNNING:
        service.finish(
            workflow.id,
            WorkflowStatus.CANCELLED,
            lambda reference: client.set_lifecycle(reference, WorkflowStatus.CANCELLED, detail),
            error=detail,
            close_external=client.close_terminals,
        )
        workflow = service.store.get(workflow.id)
    if workflow.status != WorkflowStatus.CANCELLED:
        raise ValueError("declared blocker role could not reach cancelled state")
    blocked = service.store.finalize_blocked_readiness(readiness.id)
    result = {
        "action": "blocked",
        "workflow_id": workflow.id,
        "blocker": asdict(readiness),
        "run": asdict(blocked),
    }
    progress = service.try_progress_push(blocked.root_manager_id, client)
    if progress is not None:
        result["progress_push"] = {"pushed": progress["pushed"]}
    _attach_keep_manager_retire(service, client, blocked.root_manager_id, result)
    return result


def _supervise_once(service: WorkflowService, config, manager_id: str) -> dict[str, object]:
    """Replay one durable orchestration edge without duplicating a child launch."""
    manager = service.store.get(manager_id)
    if manager.mode == WorkflowMode.SINGLE:
        client = _adapter(config)
        timed_out = _apply_watchdog(service, config, client, manager.id)
        if timed_out is not None:
            return timed_out
        if manager.status != WorkflowStatus.RUNNING:
            return {"action": "terminal", "workflow_id": manager.id, "status": manager.status.value}
        waiting = set() if service.queue is None else set(service.queue.active_owners())
        if manager.id in waiting:
            return {"action": "waiting_resource", "workflow_id": manager.id}
        return {"action": "waiting", "workflow_id": manager.id}
    if manager.mode != WorkflowMode.ORCHESTRATED or manager.role != WorkflowRole.MANAGER:
        raise ValueError("workflow supervise requires an orchestrated manager or single root")
    run = service.store.orchestration_run(manager.id)
    if run.status != "running":
        result = {"action": "terminal", "run": asdict(run)}
        _attach_keep_manager_retire(service, _adapter(config), manager.id, result)
        return result
    if run.coordinator_retry_at is not None:
        retry_at = datetime.fromisoformat(run.coordinator_retry_at)
        remaining = (retry_at - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            return {"action": "retry-wait", "retry_after_seconds": remaining, "run": asdict(run)}
    children = list(service.store.children(manager.id))
    worker = next((item for item in children if item.role == WorkflowRole.WORKER), None)
    reviewers = [item for item in children if item.role == WorkflowRole.REVIEWER]
    if worker is None or not reviewers:
        raise ValueError("complete role plan was not found")
    client = _adapter(config)
    timed_out = _apply_watchdog(service, config, client, manager.id)
    if timed_out is not None:
        return timed_out
    waiting_owners = set() if service.queue is None else set(service.queue.active_owners())
    parked = [
        record
        for record in (manager, worker, *reviewers)
        if record.status == WorkflowStatus.RUNNING and record.id in waiting_owners
    ]
    if parked:
        return {
            "action": "waiting_resource",
            "workflow_id": parked[0].id,
            "run": asdict(run),
        }
    blocker = service.store.pending_blocker(manager.id)
    if blocker is not None:
        return _consume_declared_blocker(service, client, blocker)

    try:
        manager_ready = service.store.role_readiness(manager.id, WorkflowRole.MANAGER, 1)
    except ValueError:
        manager_ready = None
    if manager_ready is not None and manager_ready.consumed_at is None:
        _verify_ready_artifact(service, manager_ready)
        if manager.status == WorkflowStatus.RUNNING:
            service.store.record_or_update_handoff(manager.id, worker.id, manager_ready.summary)
            _finish_role(service, client, manager.id)
            manager = service.store.get(manager.id)
        _ensure_child_started(service, config, worker.id)
        # Consume last: a crash before this point replays onto an already-running child.
        service.store.consume_role_readiness(manager_ready.id)
        return {"action": "launched-worker", "workflow_id": worker.id}

    run = service.store.orchestration_run(manager.id)
    try:
        worker_ready = service.store.role_readiness(
            manager.id, WorkflowRole.WORKER, run.current_review_cycle
        )
    except ValueError:
        worker_ready = None
    worker = service.store.get(worker.id)
    reviewers = [service.store.get(item.id) for item in reviewers]
    if (
        worker_ready is None
        and run.current_review_cycle > 1
        and worker.status == WorkflowStatus.RUNNING
        and all(item.status == WorkflowStatus.REQUESTED for item in reviewers)
    ):
        _ensure_rework_agent(service, config, client, worker.id)
        worker = service.store.get(worker.id)
    if worker_ready is not None and worker_ready.consumed_at is None:
        _verify_ready_artifact(service, worker_ready)
        if worker.status == WorkflowStatus.RUNNING:
            for reviewer in reviewers:
                service.store.record_or_update_handoff(worker.id, reviewer.id, worker_ready.summary)
            _finish_role(service, client, worker.id)
            worker = service.store.get(worker.id)
        launched = []
        for reviewer in reviewers:
            _ensure_child_started(service, config, reviewer.id)
            launched.append(service.store.get(reviewer.id).id)
        service.store.consume_role_readiness(worker_ready.id)
        return {"action": "launched-reviewer", "workflow_id": launched[0], "workflow_ids": launched}

    reviewer_ready_list = service.store.role_readiness_list(
        manager.id, WorkflowRole.REVIEWER, run.current_review_cycle
    )
    reviewers = [service.store.get(item.id) for item in reviewers]
    ready_by_workflow = {item.workflow_id: item for item in reviewer_ready_list}
    for reviewer in reviewers:
        ready = ready_by_workflow.get(reviewer.id)
        if ready is None or ready.consumed_at is not None:
            continue
        _verify_ready_artifact(service, ready)
        if reviewer.status == WorkflowStatus.RUNNING:
            _finish_role(service, client, reviewer.id)
    reviewers = [service.store.get(item.id) for item in reviewers]
    unconsumed = [item for item in reviewer_ready_list if item.consumed_at is None]
    if len(reviewer_ready_list) < len(reviewers) or not unconsumed:
        dead = _dead_role(service, manager.id)
        if dead is None:
            return {"action": "waiting", "run": asdict(run)}
        service.store.set_orchestration_outcome(
            manager.id, "failed", error=f"orchestration cannot continue: {dead}"
        )
        service.release_coordinator(manager.id, "orchestration_failed")
        failed = service.store.orchestration_run(manager.id)
        progress = service.try_progress_push(manager.id, client)
        result = {"action": "failed", "run": asdict(failed)}
        if progress is not None:
            result["progress_push"] = {"pushed": progress["pushed"]}
        _attach_keep_manager_retire(service, client, manager.id, result)
        return result
    if any(item.status == WorkflowStatus.RUNNING for item in reviewers):
        return {"action": "waiting", "run": asdict(run)}
    extra_ids = tuple(item.id for item in unconsumed[1:])
    if all(item.outcome == "approved" for item in unconsumed):
        try:
            delivery = service.deliver_approved(
                manager.id, client, force_push=config.delivery_force_push
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            service.store.set_orchestration_outcome(
                manager.id, "failed", error=f"delivery_failed: {exc}"
            )
            service.release_coordinator(manager.id, "orchestration_failed")
            failed = service.store.orchestration_run(manager.id)
            result = {
                "action": "delivery_failed",
                "run": asdict(failed),
                "error": str(exc),
            }
            _attach_keep_manager_retire(service, client, manager.id, result)
            return result
        completed = service.store.finalize_reviewer_outcome(
            unconsumed[0].id,
            status="completed",
            error=None,
            coordinator_reason="orchestration_completed",
            extra_readiness_ids=extra_ids,
        )
        result = {
            "action": "completed",
            "run": asdict(completed),
            "delivery": {
                "harvest_method": delivery["harvested"].method,
                "approved_sha": delivery["check"].approved_sha,
                "pushed": delivery["pushed"],
            },
        }
        _attach_keep_manager_retire(service, client, manager.id, result)
        return result

    if run.current_review_cycle >= run.max_review_cycles:
        blocked = service.store.finalize_reviewer_outcome(
            unconsumed[0].id,
            status="blocked",
            error=f"review changes requested at maximum cycle {run.max_review_cycles}",
            coordinator_reason="orchestration_blocked",
            extra_readiness_ids=extra_ids,
        )
        progress = service.try_progress_push(manager.id, client)
        result = {"action": "blocked", "run": asdict(blocked)}
        if progress is not None:
            result["progress_push"] = {"pushed": progress["pushed"]}
        _attach_keep_manager_retire(service, client, manager.id, result)
        return result
    for reviewer in reviewers:
        if reviewer.adapter_reference and reviewer.external_reconciled_at is None:
            _remove_review_worktree(client, reviewer)
            service.store.mark_external_reconciled(reviewer.id)
    trigger = next(item for item in unconsumed if item.outcome == "changes-requested")
    service.begin_next_review_cycle(manager.id, trigger.id, client)
    worker = service.store.get(worker.id)
    _ensure_rework_agent(service, config, client, worker.id)
    return {
        "action": "returned-to-worker",
        "workflow_id": worker.id,
        "review_cycle": run.current_review_cycle + 1,
    }


def _cmd_supervise(args: argparse.Namespace, config, service: WorkflowService) -> int:
    root = service.store.get(args.workflow_id)
    is_orchestrated = root.mode == WorkflowMode.ORCHESTRATED
    while True:
        try:
            result = _supervise_once(service, config, args.workflow_id)
        except (OSError, RuntimeError, sqlite3.OperationalError) as exc:
            run = service.store.record_coordinator_error(
                args.workflow_id,
                f"{type(exc).__name__}: {exc}",
                max_errors=config.max_coordinator_errors,
                initial_delay_seconds=config.coordinator_retry_initial_seconds,
                max_delay_seconds=config.coordinator_retry_max_seconds,
            )
            if run.status == "blocked":
                service.release_coordinator(args.workflow_id, "orchestration_blocked")
                run = service.store.orchestration_run(args.workflow_id)
                result = {"action": "blocked", "run": asdict(run)}
                client = _adapter(config)
                _attach_keep_manager_retire(service, client, args.workflow_id, result)
                print(json.dumps(result, indent=2), flush=True)
                service.close_watchdog(args.workflow_id, client, "orchestration_blocked")
                return 2
            result = {
                "action": "retry-wait",
                "retry_after_seconds": max(
                    0.0,
                    (
                        datetime.fromisoformat(run.coordinator_retry_at) - datetime.now(UTC)
                    ).total_seconds(),
                ),
                "run": asdict(run),
            }
            if args.once:
                print(json.dumps(result, indent=2), flush=True)
                return 0
        except (ConfigError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
            run = service.store.set_orchestration_outcome(
                args.workflow_id, "blocked", error=f"{type(exc).__name__}: {exc}"
            )
            service.release_coordinator(args.workflow_id, "orchestration_blocked")
            run = service.store.orchestration_run(args.workflow_id)
            result = {"action": "blocked", "run": asdict(run)}
            client = _adapter(config)
            _attach_keep_manager_retire(service, client, args.workflow_id, result)
            print(json.dumps(result, indent=2), flush=True)
            service.close_watchdog(args.workflow_id, client, "orchestration_blocked")
            return 2
        else:
            if is_orchestrated and result["action"] != "retry-wait":
                service.store.clear_coordinator_errors(args.workflow_id)
        if args.once or result["action"] in {
            "completed",
            "blocked",
            "failed",
            "terminal",
            "delivery_failed",
        }:
            print(json.dumps(result, indent=2), flush=True)
            if result["action"] in {
                "completed",
                "blocked",
                "failed",
                "terminal",
                "delivery_failed",
            }:
                service.close_watchdog(
                    args.workflow_id,
                    _adapter(config),
                    f"orchestration_{result['action']}",
                )
            return 0
        delay = float(result["retry_after_seconds"]) if result["action"] == "retry-wait" else 1.0
        time.sleep(max(delay, 0.01))


def _cmd_cleanup(args: argparse.Namespace, config, store: WorkflowStore) -> int:
    """Report or reconcile durable workflow ownership without deleting history."""
    service = WorkflowService(store, ResourceQueue(config.state_dir))
    leftover_roots = store.roots_needing_child_retirement()
    leftover_ids = {workflow.id for workflow in leftover_roots}
    if args.older_than_seconds is None:
        candidates = service.store.active()
        candidates.extend(service.store.unreconciled_terminal())
    else:
        candidates_by_id = {
            workflow.id: workflow
            for workflow in service.store.stale_active(args.older_than_seconds)
        }
        candidates_by_id.update(
            {
                workflow.id: workflow
                for workflow in service.store.stale_reconcilable(args.older_than_seconds)
            }
        )
        candidates = list(candidates_by_id.values())
    if args.workflow_ids:
        wanted = set(args.workflow_ids)
        candidates = [workflow for workflow in candidates if workflow.id in wanted]
        leftover_roots = [workflow for workflow in leftover_roots if workflow.id in wanted]
        leftover_ids = {workflow.id for workflow in leftover_roots}
    seen = {workflow.id for workflow in candidates}
    for leftover in leftover_roots:
        if leftover.id not in seen:
            candidates.append(leftover)
            seen.add(leftover.id)
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "candidate_count": len(candidates),
                    "candidates": [
                        {
                            "workflow_id": workflow.id,
                            "status": workflow.status,
                            "updated_at": workflow.updated_at,
                            "adapter_reference": workflow.adapter_reference,
                            "retire_recommended": workflow.id in leftover_ids,
                            "root_id": workflow.id if workflow.id in leftover_ids else None,
                        }
                        for workflow in candidates
                    ],
                }
            )
        )
        return 0
    if args.older_than_seconds is None:
        raise ValueError("cleanup --apply requires --older-than-seconds")
    client = _adapter(config)
    result = service.reconcile_stale(
        args.older_than_seconds,
        client.close_terminals,
        client.remove_worktree,
        workflow_ids=tuple(args.workflow_ids) if args.workflow_ids else None,
    )
    print(
        json.dumps(
            {
                "dry_run": False,
                "reconciled_count": len(result.reconciled),
                "workflow_ids": [workflow.id for workflow in result.reconciled],
                "errors": list(result.errors),
            }
        )
    )
    return 2 if result.errors else 0


def _unique_root_ids(store: WorkflowStore, workflow_ids: list[str]) -> tuple[list[str], list[str]]:
    """Preserve caller order while collapsing child/run ids onto one root."""
    roots: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for workflow_id in dict.fromkeys(workflow_ids):
        try:
            root = store.get_by_run_or_step_id(workflow_id)
        except ValueError as exc:
            errors.append(f"{workflow_id}: {exc}")
            continue
        root_id = root.id if root.parent_id is None else root.parent_id
        if root_id in seen:
            continue
        seen.add(root_id)
        roots.append(root_id)
    return roots, errors


def _harvest_payload(result) -> dict[str, object]:
    return asdict(result)


def _retire_payload(result) -> dict[str, object]:
    payload = asdict(result)
    payload["closed_handles"] = [
        {"adapter_reference": reference, "handle": handle}
        for reference, handle in result.closed_handles
    ]
    return payload


def _attach_keep_manager_retire(
    service: WorkflowService, client, root_id: str, result: dict[str, object]
) -> None:
    """Close owned children after a terminal run without rolling the outcome back."""
    retired = service.try_retire_keep_manager(root_id, client)
    if retired.get("ok") is True:
        result["retire"] = _retire_payload(retired["result"])
        return
    result["retire_error"] = retired.get("error")


def _cmd_harvest(args: argparse.Namespace, config, store: WorkflowStore) -> int:
    """Integrate worker commits into each finished manager worktree."""
    service = WorkflowService(store, ResourceQueue(config.state_dir))
    client = _adapter(config)
    results: list[dict[str, object]] = []
    roots, errors = _unique_root_ids(store, args.workflow_ids)
    for workflow_id in roots:
        try:
            harvested = service.harvest(workflow_id, client, dry_run=bool(args.dry_run))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(f"{workflow_id}: {exc}")
            continue
        results.append(_harvest_payload(harvested))
    print(
        json.dumps(
            {
                "dry_run": bool(args.dry_run),
                "results": results,
                "errors": errors,
            },
            indent=2,
        )
    )
    return 2 if errors else 0


def _cmd_delivery_check(args: argparse.Namespace, config, store: WorkflowStore) -> int:
    """Verify that every requested manager is at its exact locally approved SHA."""
    service = WorkflowService(store, ResourceQueue(config.state_dir))
    client = _adapter(config)
    results: list[dict[str, object]] = []
    roots, errors = _unique_root_ids(store, args.workflow_ids)
    for workflow_id in roots:
        try:
            checked = service.delivery_check(workflow_id, client)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(f"{workflow_id}: {exc}")
            continue
        results.append(asdict(checked))
    eligible = (
        not errors
        and len(results) == len(roots)
        and all(bool(result["eligible"]) for result in results)
    )
    print(json.dumps({"eligible": eligible, "results": results, "errors": errors}, indent=2))
    return 0 if eligible else 2


def _cmd_retire(args: argparse.Namespace, config, store: WorkflowStore) -> int:
    """Harvest, then close owned resources for each finished root."""
    service = WorkflowService(store, ResourceQueue(config.state_dir))
    client = _adapter(config)
    results: list[dict[str, object]] = []
    roots, errors = _unique_root_ids(store, args.workflow_ids)
    for workflow_id in roots:
        try:
            retired = service.retire(
                workflow_id, client, keep=args.keep, dry_run=bool(args.dry_run)
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            errors.append(f"{workflow_id}: {exc}")
            continue
        results.append(_retire_payload(retired))
    print(
        json.dumps(
            {
                "dry_run": bool(args.dry_run),
                "keep": args.keep,
                "results": results,
                "errors": errors,
            },
            indent=2,
        )
    )
    return 2 if errors else 0


def handle(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.workflow_command == "start":
        return _cmd_start(args)
    read_only = args.workflow_command in {"list", "status"}
    # The durable supervisor owns its own retry policy. Let it observe and
    # persist transient Orca failures instead of bypassing that policy here.
    freshness = _sync_orca(config, fatal=not read_only and args.workflow_command != "supervise")
    store = WorkflowStore(config.state_dir)
    if args.workflow_command == "list":
        statuses = tuple(WorkflowStatus(status) for status in args.status) if args.status else None
        workflows = store.list_workflows(statuses=statuses)
        client = _optional_adapter(config)
        print(
            json.dumps(
                {
                    "workflows": [
                        _workflow_list_item(
                            item, owner_terminal_valid=_owner_terminal_valid(item, client)
                        )
                        for item in workflows
                    ],
                    "freshness": freshness,
                },
                indent=2,
            )
        )
        return 0
    if args.workflow_command == "status":
        workflow = store.get_by_run_or_step_id(args.workflow_id)
        queue = ResourceQueue(config.state_dir)
        service = WorkflowService(store, queue)
        artifacts = service.artifact_metadata(workflow.id)
        client = _optional_adapter(config)
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        run = None
        blocker = None
        if workflow.mode == WorkflowMode.ORCHESTRATED and root_id is not None:
            try:
                run = asdict(store.orchestration_run(root_id))
                recorded_blocker = store.blocker_for_run(root_id)
                blocker = None if recorded_blocker is None else asdict(recorded_blocker)
            except ValueError:
                # Role records predating the run aggregate stay reportable.
                run = None
        queue_owners = [workflow.id]
        if workflow.parent_id is None:
            queue_owners.extend(child.id for child in store.children(workflow.id))
        active_requests = queue.active_requests(
            owners=queue_owners,
            attention_after_seconds=config.queue_lease_timeout_seconds,
        )
        print(
            json.dumps(
                {
                    "workflow_run": store.get_run(workflow.run_id),
                    "workflow": asdict(workflow),
                    "run": run,
                    "blocker": blocker,
                    "children": [asdict(child) for child in store.children(workflow.id)],
                    "artifacts": [asdict(artifact) for artifact in artifacts],
                    "progress": service.progress_snapshot(workflow.id),
                    "resource_queue": {
                        "requests": active_requests,
                        "attention_after_seconds": config.queue_lease_timeout_seconds,
                        "attention_required": any(
                            item["attention_required"] for item in active_requests
                        ),
                    },
                    "observer": {
                        "enabled": workflow.queue_observer_enabled,
                        "owned_handles": store.owned_terminal_handles(workflow.id, kind="observer"),
                        "owner_terminal_valid": _owner_terminal_valid(workflow, client),
                        "last_termination": None
                        if workflow.observer_stopped_at is None
                        else {
                            "handle": workflow.observer_last_handle,
                            "reason": workflow.observer_stop_reason,
                            "at": workflow.observer_stopped_at,
                        },
                    },
                    "freshness": freshness,
                },
                indent=2,
            )
        )
        return 0
    if args.workflow_command == "artifact":
        service = WorkflowService(store)
        if args.artifact_command == "put":
            content = (
                _read_artifact_file(args.file) if args.file is not None else _read_artifact_stdin()
            )
            artifact = service.put_artifact(args.workflow_id, args.kind, content)
            print(json.dumps({"artifact": asdict(artifact)}, indent=2))
            return 0
        if args.artifact_command == "show":
            artifact, content = service.show_artifact(args.workflow_id, args.kind)
            print(json.dumps({"artifact": asdict(artifact), "content": content}, indent=2))
            return 0
        artifact = service.verify_artifact(args.workflow_id, args.kind)
        print(json.dumps({"artifact": asdict(artifact), "verified": True}, indent=2))
        return 0
    if args.workflow_command in {"link", "unlink"}:
        refs = ReconcileStore(config.state_dir, event_retention=config.reconcile.event_retention)
        run_id = refs.resolve_run_id(args.run_id)
        if args.workflow_command == "link":
            canonical = refs.link_reference(run_id, args.canonical_url, args.relation)
            print(
                json.dumps(
                    {"run_id": run_id, "url": canonical, "relation": args.relation},
                    indent=2,
                )
            )
        else:
            refs.unlink_reference(run_id, args.canonical_url)
            print(json.dumps({"run_id": run_id, "url": args.canonical_url}, indent=2))
        return 0
    if args.workflow_command == "observe":
        workflow = store.get(args.workflow_id)
        client = _adapter(config)
        if not _owner_terminal_valid(workflow, client):
            raise ValueError(
                "queue observer requires a live owner agent terminal; "
                f"resume with `flybridge workflow resume {args.workflow_id}` first"
            )
        terminal_handle = WorkflowService(store, ResourceQueue(config.state_dir)).attach_observer(
            args.workflow_id, client, _observer_command(config.path, args.workflow_id)
        )
        print(
            json.dumps(
                {"workflow_id": args.workflow_id, "terminal_handle": terminal_handle}, indent=2
            )
        )
        return 0
    if args.workflow_command == "cleanup":
        return _cmd_cleanup(args, config, store)
    if args.workflow_command == "harvest":
        return _cmd_harvest(args, config, store)
    if args.workflow_command == "delivery-check":
        return _cmd_delivery_check(args, config, store)
    if args.workflow_command == "retire":
        return _cmd_retire(args, config, store)
    service = WorkflowService(store, ResourceQueue(config.state_dir))
    if args.workflow_command == "role-ready":
        readiness = service.role_ready(
            args.workflow_id,
            _adapter(config),
            summary=args.summary,
            outcome=args.outcome,
        )
        print(json.dumps({"readiness": asdict(readiness)}, indent=2))
        return 0
    if args.workflow_command == "supervise":
        return _cmd_supervise(args, config, service)
    if args.workflow_command == "coordinator-retry":
        run, handle = service.retry_coordinator(
            args.workflow_id,
            _adapter(config),
            _coordinator_command(config.path, args.workflow_id),
        )
        print(
            json.dumps(
                {"run": asdict(run), "coordinator_handle": handle},
                indent=2,
            )
        )
        return 0
    if args.workflow_command == "coordinator-close":
        handles = service.close_coordinator(args.workflow_id, _adapter(config), "operator_cleanup")
        print(
            json.dumps(
                {"workflow_id": args.workflow_id, "closed_handles": list(handles)},
                indent=2,
            )
        )
        return 0
    if args.workflow_command == "advance":
        child = service.next_ready_child(args.workflow_id)
        if child is None:
            raise ValueError("no child role is ready to start")
        started = _launch_record(
            service, config, child.id, observer_command=_optional_observer_command(config, child.id)
        )
        print(json.dumps({"workflow": asdict(started)}, indent=2))
        return 0
    if args.workflow_command == "proceed":
        manager = store.get(args.workflow_id)
        if manager.mode != WorkflowMode.ORCHESTRATED or manager.role != WorkflowRole.MANAGER:
            raise ValueError("workflow proceed requires an orchestrated manager")
        children = list(store.children(manager.id))
        worker = next((child for child in children if child.role == WorkflowRole.WORKER), None)
        reviewers = [child for child in children if child.role == WorkflowRole.REVIEWER]
        if worker is None or not reviewers:
            raise ValueError("complete role plan was not found")
        if manager.status == WorkflowStatus.RUNNING:
            store.record_handoff(manager.id, worker.id, args.summary)
            source = manager
        elif worker.status == WorkflowStatus.RUNNING:
            for reviewer in reviewers:
                store.record_handoff(worker.id, reviewer.id, args.summary)
            source = worker
        else:
            raise ValueError("no running predecessor is ready to proceed")
        client = _adapter(config)
        service.finish(
            source.id,
            WorkflowStatus.COMPLETED,
            lambda worktree_id: client.set_lifecycle(worktree_id, WorkflowStatus.COMPLETED),
            close_external=client.close_terminals,
        )
        started = None
        while True:
            child = service.next_ready_child(manager.id)
            if child is None:
                break
            started = _launch_record(
                service,
                config,
                child.id,
                observer_command=_optional_observer_command(config, child.id),
            )
        if started is None:
            raise ValueError("no child role is ready to start")
        print(json.dumps({"workflow": asdict(started)}, indent=2))
        return 0
    if args.workflow_command == "handoff":
        handoff = store.record_handoff(
            args.source_workflow_id, args.target_workflow_id, args.summary
        )
        print(json.dumps(asdict(handoff), indent=2))
        return 0
    if args.workflow_command == "retry":
        workflow = service.retry_failed(args.workflow_id)
        print(json.dumps({"workflow": asdict(workflow)}, indent=2))
        return 0
    if args.workflow_command == "launch":
        workflow = store.get(args.workflow_id)
        if workflow.status != WorkflowStatus.REQUESTED:
            raise ValueError("only a requested workflow can be launched")
        if workflow.parent_id is not None:
            raise ValueError("child roles start through workflow advance")
        started = _launch_record(
            service,
            config,
            workflow.id,
            observer_command=_optional_observer_command(config, workflow.id),
        )
        print(json.dumps({"workflow": asdict(started)}, indent=2))
        return 0
    if args.workflow_command == "resume":
        workflow = store.get(args.workflow_id)
        spec = _agent_spec(config, workflow)
        resumed = service.resume(
            workflow.id,
            _adapter(config),
            agent=spec.agent,
            model=spec.model,
            response_language=config.response_language,
            skill_paths=validate_skill_paths(config.skill_paths_for(workflow.role)),
            resource_names=config.queue_resources,
            config_path=config.path,
            observer_command=(
                _observer_command(config.path, workflow.id) if config.queue_observer else None
            ),
            observer_enabled=bool(config.queue_observer),
        )
        print(json.dumps({"workflow": asdict(resumed)}, indent=2))
        return 0
    target = {
        "complete": WorkflowStatus.COMPLETED,
        "cancel": WorkflowStatus.CANCELLED,
        "fail": WorkflowStatus.FAILED,
    }[args.workflow_command]
    client = _adapter(config)
    workflow = service.finish(
        args.workflow_id,
        target,
        lambda worktree_id: client.set_lifecycle(worktree_id, target, getattr(args, "error", None)),
        error=getattr(args, "error", None),
        close_external=client.close_terminals,
    )
    print(json.dumps(asdict(workflow), indent=2))
    return 0
