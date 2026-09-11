from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

from flybridge_application import (
    WorkflowService,
    validate_skill_paths,
)
from flybridge_core import (
    ArgumentParser,
    ConfigError,
    ResourceQueue,
    WorkflowMode,
    WorkflowRole,
    WorkflowStatus,
    WorkflowStore,
    load_config,
)
from flybridge_github import GitHubProject
from flybridge_orca import OrcaClient


def _config(args: argparse.Namespace):
    return load_config(Path(args.config))


def _adapter(config) -> OrcaClient:
    client = OrcaClient(config.orca_executable)
    client.verify()
    return client


def _running_owner(store: WorkflowStore, owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("owner must identify an existing running workflow")
    try:
        workflow = store.get(owner.strip())
    except ValueError as exc:
        raise ValueError("owner must identify an existing running workflow") from exc
    if workflow.status != WorkflowStatus.RUNNING:
        raise ValueError("owner must identify an existing running workflow")
    return workflow.id


def _observer_command(config_path: Path) -> str:
    """Use the current installed interpreter instead of relying on a shell PATH."""
    arguments = [
        sys.executable,
        "-m",
        "flybridge_cli.main",
        "--config",
        str(config_path),
        "queue",
        "watch",
    ]
    return shlex.join(arguments)


def _validate_mode_skill_paths(config, mode: WorkflowMode) -> None:
    roles = (
        (WorkflowRole.SINGLE,)
        if mode == WorkflowMode.SINGLE
        else (WorkflowRole.MANAGER, WorkflowRole.WORKER, WorkflowRole.REVIEWER)
    )
    paths = tuple(path for role in roles for path in config.skill_paths_for(role))
    validate_skill_paths(tuple(dict.fromkeys(paths)))
    missing_agents = [role.value for role in roles if role not in config.orca_agents]
    if missing_agents:
        raise ConfigError(
            "orca.agents must configure the required roles: " + ", ".join(missing_agents)
        )


def _generated_workflow_name() -> str:
    return f"flybridge-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}"


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(prog="flybridge")
    parser.add_argument(
        "-c", "--config", default="config/flybridge.jsonc", help="JSONC user configuration"
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=ArgumentParser)
    start = sub.add_parser("start", help="open a single or role-separated Orca workflow")
    start.add_argument("repository", type=Path, help="repository directory")
    start.add_argument("-m", "--mode", choices=["single", "orchestrated"], help="workflow mode")
    start.add_argument("-n", "--name", help="workflow name")
    start.add_argument("-o", "--objective", required=True, help="work objective")
    start.add_argument(
        "-d",
        "--allow-duplicate",
        action="store_true",
        help="allow the same repository and active root objective",
    )
    start.add_argument(
        "--queue-observer",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="open a visible queue observer for the root workflow",
    )
    queue = sub.add_parser("queue", help="manage deterministic resource queues")
    queue_sub = queue.add_subparsers(
        dest="queue_command", required=True, parser_class=ArgumentParser
    )
    acquire = queue_sub.add_parser("acquire", help="enqueue and acquire a named resource")
    acquire.add_argument("resource", help="interfering resource name")
    acquire.add_argument("-o", "--owner", required=True, help="running workflow owner")
    release = queue_sub.add_parser("release", help="release an active resource lease")
    release.add_argument("resource", help="interfering resource name")
    release.add_argument("-l", "--lease", required=True, help="active lease identifier")
    release.add_argument("-o", "--owner", required=True, help="recorded request owner")
    cancel = queue_sub.add_parser("cancel", help="cancel a waiting or active request")
    cancel.add_argument("-r", "--request", required=True, help="request identifier")
    recover = queue_sub.add_parser("recover", help="recover explicitly stale leases")
    recover.add_argument(
        "-t", "--older-than-seconds", type=float, required=True, help="stale lease age"
    )
    status = queue_sub.add_parser("status", help="summarize resource queue states")
    status.add_argument("resource", nargs="?", help="optional resource name")
    inspect = queue_sub.add_parser("inspect", help="inspect one queue request")
    inspect.add_argument("request_id", help="queue request identifier")
    inspect.add_argument("-o", "--owner", required=True, help="recorded request owner")
    watch = queue_sub.add_parser("watch", help="stream queue snapshots and events")
    watch.add_argument("-i", "--interval", type=float, default=1.0, help="poll interval in seconds")
    watch.add_argument("--once", action="store_true", help="print one snapshot and exit")
    observer = queue_sub.add_parser("observer", help="open a visible Orca queue observer terminal")
    observer.add_argument(
        "-w", "--worktree-id", required=True, help="owning Orca worktree identifier"
    )
    board = sub.add_parser("board", help="optional GitHub Project candidate screening")
    board_sub = board.add_subparsers(
        dest="board_command", required=True, parser_class=ArgumentParser
    )
    board_sub.add_parser("screen", help="list eligible configured Project issues")
    workflow = sub.add_parser("workflow", help="inspect or finish a Flybridge workflow")
    workflow_sub = workflow.add_subparsers(
        dest="workflow_command", required=True, parser_class=ArgumentParser
    )
    workflow_status = workflow_sub.add_parser("status", help="show one workflow and its children")
    workflow_status.add_argument("workflow_id", help="workflow identifier")
    advance = workflow_sub.add_parser("advance", help="start the next deterministic child role")
    advance.add_argument("workflow_id", help="orchestrated manager workflow ID")
    handoff = workflow_sub.add_parser(
        "handoff", help="record the required deterministic role handoff"
    )
    handoff.add_argument("source_workflow_id", help="completed source workflow identifier")
    handoff.add_argument("target_workflow_id", help="next workflow identifier")
    handoff.add_argument("-s", "--summary", required=True, help="handoff summary")
    retry = workflow_sub.add_parser(
        "retry", help="return an externally reconciled failed role to requested"
    )
    retry.add_argument("workflow_id", help="failed workflow identifier")
    launch = workflow_sub.add_parser(
        "launch", help="start a requested root workflow after retry or planning"
    )
    launch.add_argument("workflow_id", help="requested root workflow identifier")
    resume = workflow_sub.add_parser(
        "resume", help="resume a persisted running workflow in its existing Orca worktree"
    )
    resume.add_argument("workflow_id", help="running workflow identifier")
    for command in ("complete", "cancel", "fail"):
        descriptions = {
            "complete": "mark a running workflow completed",
            "cancel": "cancel a requested, starting, or running workflow",
            "fail": "mark a running workflow failed",
        }
        finish = workflow_sub.add_parser(command, help=descriptions[command])
        finish.add_argument("workflow_id", help="workflow identifier")
        if command == "fail":
            finish.add_argument("-e", "--error", required=True, help="failure description")
    doctor = sub.add_parser("doctor", help="check local dependencies and configuration")
    doctor.add_argument(
        "-m", "--mode", choices=["single", "orchestrated"], help="workflow mode to validate"
    )
    cleanup = sub.add_parser("cleanup", help="report recoverable local workflow state")
    cleanup_mode = cleanup.add_mutually_exclusive_group(required=True)
    cleanup_mode.add_argument("-n", "--dry-run", action="store_true", help="report candidates only")
    cleanup_mode.add_argument(
        "-a", "--apply", action="store_true", help="close eligible owned worktrees"
    )
    cleanup.add_argument("-t", "--older-than-seconds", type=float, help="minimum stale age")
    cleanup.add_argument("-f", "--force-age", action="store_true", help="treat record age as stale")
    return parser


def _launch_record(
    service: WorkflowService,
    config,
    workflow_id: str,
    *,
    observer_command: str | None = None,
    allow_duplicate: bool = False,
    already_starting: bool = False,
) -> object:
    workflow = service.store.get(workflow_id)
    role = workflow.role
    try:
        agent = config.orca_agents[role]
    except KeyError as exc:
        raise ConfigError(f"orca.agents.{role} is required") from exc
    skill_paths = validate_skill_paths(config.skill_paths_for(role))
    client = _adapter(config)
    return service.launch_existing(
        workflow.id,
        client,
        agent=agent,
        response_language=config.response_language,
        skill_paths=skill_paths,
        resource_names=config.queue_resources,
        config_path=config.path,
        observer_command=observer_command,
        allow_duplicate=allow_duplicate,
        already_starting=already_starting,
    )


def _cmd_start(args: argparse.Namespace) -> int:
    config = _config(args)
    repository = args.repository.resolve()
    if not repository.is_dir():
        raise ValueError(f"repository does not exist: {repository}")
    if not args.objective.strip():
        raise ValueError("workflow objective is required")
    mode = WorkflowMode(args.mode) if args.mode else config.default_mode
    name = args.name or _generated_workflow_name()
    _validate_mode_skill_paths(config, mode)
    client = _adapter(config)
    WorkflowService.prepare_repository(repository, client)
    service = WorkflowService(WorkflowStore(config.state_dir), ResourceQueue(config.state_dir))
    plan = service.store.reserve_root_plan(
        repository,
        mode,
        name,
        args.objective,
        allow_duplicate=args.allow_duplicate,
    )
    requested = plan[0]
    observer_enabled = config.queue_observer if args.queue_observer is None else args.queue_observer
    workflow = _launch_record(
        service,
        config,
        requested.id,
        observer_command=_observer_command(config.path) if observer_enabled else None,
        allow_duplicate=args.allow_duplicate,
        already_starting=True,
    )
    children = plan[1:]
    print(
        json.dumps(
            {
                "workflow": asdict(workflow),
                "planned_children": [asdict(child) for child in children],
            },
            indent=2,
        )
    )
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    config = _config(args)
    queue = ResourceQueue(config.state_dir)
    store = WorkflowStore(config.state_dir)
    WorkflowService(store, queue)
    if args.queue_command == "acquire":
        owner = _running_owner(store, args.owner)
        result = queue.acquire(args.resource, owner)
        try:
            _running_owner(store, owner)
        except ValueError as exc:
            queue.cancel_owner(owner)
            raise ValueError("owner workflow stopped during resource acquisition") from exc
        print(json.dumps(asdict(result)))
    elif args.queue_command == "release":
        print(
            json.dumps(
                {
                    "next_lease_id": queue.release(
                        args.lease, resource=args.resource, owner=args.owner
                    )
                }
            )
        )
    elif args.queue_command == "cancel":
        print(json.dumps({"next_lease_id": queue.cancel(args.request)}))
    elif args.queue_command == "recover":
        print(json.dumps({"promoted_lease_ids": queue.recover_stale(args.older_than_seconds)}))
    elif args.queue_command == "status":
        print(json.dumps(queue.status(args.resource), indent=2))
    elif args.queue_command == "inspect":
        print(json.dumps(queue.inspect(args.request_id, owner=args.owner), indent=2))
    elif args.queue_command == "observer":
        workflow = store.find_by_adapter_reference(args.worktree_id)
        command = _observer_command(config.path)
        terminal_handle = WorkflowService(store, queue).attach_observer(
            workflow.id, _adapter(config), command
        )
        print(
            json.dumps({"workflow_id": workflow.id, "terminal_handle": terminal_handle}, indent=2)
        )
    else:
        if (
            isinstance(args.interval, bool)
            or not isinstance(args.interval, (int, float))
            or not isfinite(args.interval)
            or args.interval <= 0
        ):
            raise ValueError("queue watch interval must be a finite positive number")
        cursor = 0
        print(json.dumps({"event": "snapshot", "queues": queue.status()}), flush=True)
        while True:
            events = queue.events(cursor)
            for event in events:
                cursor = int(event["sequence"])
                print(json.dumps(event), flush=True)
            if len(events) == 1000:
                continue
            if args.once:
                return 0
            time.sleep(args.interval)
    return 0


def _cmd_workflow(args: argparse.Namespace) -> int:
    config = _config(args)
    store = WorkflowStore(config.state_dir)
    if args.workflow_command == "status":
        workflow = store.get(args.workflow_id)
        print(
            json.dumps(
                {
                    "workflow": asdict(workflow),
                    "children": [asdict(child) for child in store.children(workflow.id)],
                },
                indent=2,
            )
        )
        return 0
    if args.workflow_command == "advance":
        service = WorkflowService(store, ResourceQueue(config.state_dir))
        child = service.next_ready_child(args.workflow_id)
        if child is None:
            raise ValueError("no child role is ready to start")
        started = _launch_record(service, config, child.id)
        print(json.dumps({"workflow": asdict(started)}, indent=2))
        return 0
    if args.workflow_command == "handoff":
        handoff = store.record_handoff(
            args.source_workflow_id, args.target_workflow_id, args.summary
        )
        print(json.dumps(asdict(handoff), indent=2))
        return 0
    if args.workflow_command == "retry":
        service = WorkflowService(store, ResourceQueue(config.state_dir))
        workflow = service.retry_failed(args.workflow_id)
        print(json.dumps({"workflow": asdict(workflow)}, indent=2))
        return 0
    if args.workflow_command == "launch":
        workflow = store.get(args.workflow_id)
        if workflow.status != WorkflowStatus.REQUESTED:
            raise ValueError("only a requested workflow can be launched")
        if workflow.parent_id is not None:
            raise ValueError("child roles start through workflow advance")
        service = WorkflowService(store, ResourceQueue(config.state_dir))
        started = _launch_record(service, config, workflow.id)
        print(json.dumps({"workflow": asdict(started)}, indent=2))
        return 0
    service = WorkflowService(store, ResourceQueue(config.state_dir))
    if args.workflow_command == "resume":
        workflow = store.get(args.workflow_id)
        try:
            agent = config.orca_agents[workflow.role]
        except KeyError as exc:
            raise ConfigError(f"orca.agents.{workflow.role} is required") from exc
        skill_paths = validate_skill_paths(config.skill_paths_for(workflow.role))
        resumed = service.resume(
            workflow.id,
            _adapter(config),
            agent=agent,
            response_language=config.response_language,
            skill_paths=skill_paths,
            resource_names=config.queue_resources,
            config_path=config.path,
        )
        print(json.dumps({"workflow": asdict(resumed)}, indent=2))
        return 0
    target = {
        "complete": WorkflowStatus.COMPLETED,
        "cancel": WorkflowStatus.CANCELLED,
        "fail": WorkflowStatus.FAILED,
    }[args.workflow_command]
    client = _adapter(config)

    def update_external(worktree_id: str) -> None:
        client.set_lifecycle(worktree_id, target, getattr(args, "error", None))

    workflow = service.finish(
        args.workflow_id,
        target,
        update_external,
        error=getattr(args, "error", None),
        close_external=client.close_terminals,
    )
    print(json.dumps(asdict(workflow), indent=2))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    mode = WorkflowMode(args.mode) if args.mode else config.default_mode
    required_roles = (
        (WorkflowRole.SINGLE,)
        if mode == WorkflowMode.SINGLE
        else (WorkflowRole.MANAGER, WorkflowRole.WORKER, WorkflowRole.REVIEWER)
    )
    skill_paths_valid = True
    skill_error = ""
    try:
        paths = tuple(path for role in required_roles for path in config.skill_paths_for(role))
        validate_skill_paths(tuple(dict.fromkeys(paths)))
    except ConfigError as exc:
        skill_paths_valid = False
        skill_error = str(exc)
    missing_agents = [role.value for role in required_roles if role not in config.orca_agents]
    orca_report: dict[str, object] = {"reachable": False}
    if shutil.which(config.orca_executable):
        try:
            orca_report = {"reachable": True, **OrcaClient(config.orca_executable).verify()}
        except RuntimeError as exc:
            orca_report = {"reachable": False, "error": str(exc)}
    report = {
        "config": str(config.path),
        "mode": mode,
        "orca": orca_report,
        "gh": bool(shutil.which("gh")),
        "github_enabled": config.github.enabled,
        "skill_sources_valid": skill_paths_valid,
        "required_agents_configured": not missing_agents,
    }
    if skill_error:
        report["skill_error"] = skill_error
    if missing_agents:
        report["missing_agents"] = missing_agents
    print(json.dumps(report, indent=2))
    github_ready = not config.github.enabled or report["gh"]
    return (
        0
        if orca_report.get("reachable")
        and skill_paths_valid
        and not missing_agents
        and github_ready
        else 1
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            return _cmd_start(args)
        if args.command == "queue":
            return _cmd_queue(args)
        if args.command == "workflow":
            return _cmd_workflow(args)
        if args.command == "board":
            candidates = GitHubProject(_config(args).github).screen()
            print(json.dumps([candidate.__dict__ for candidate in candidates], indent=2))
            return 0
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "cleanup":
            config = _config(args)
            service = WorkflowService(
                WorkflowStore(config.state_dir), ResourceQueue(config.state_dir)
            )
            if args.older_than_seconds is None:
                candidates = service.store.active()
                candidates.extend(service.store.unreconciled_terminal())
            else:
                candidates = {
                    workflow.id: workflow
                    for workflow in service.store.stale_active(args.older_than_seconds)
                }
                candidates.update(
                    {
                        workflow.id: workflow
                        for workflow in service.store.stale_reconcilable(args.older_than_seconds)
                    }
                )
                candidates = list(candidates.values())
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
                                }
                                for workflow in candidates
                            ],
                        }
                    )
                )
                return 0
            if args.older_than_seconds is None:
                raise ValueError("cleanup --apply requires --older-than-seconds")
            if not args.force_age:
                raise ValueError("cleanup --apply requires --force-age because age is not liveness")
            client = _adapter(config)
            reconciled = service.reconcile_stale(
                args.older_than_seconds, client.close_terminals, client.remove_worktree
            )
            print(
                json.dumps(
                    {
                        "dry_run": False,
                        "reconciled_count": len(reconciled),
                        "workflow_ids": [workflow.id for workflow in reconciled],
                    }
                )
            )
            return 0
    except (ConfigError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"flybridge: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
