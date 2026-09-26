from __future__ import annotations

import argparse
from pathlib import Path

from flybridge_core import ArgumentParser, WorkflowStatus


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(prog="flybridge")
    parser.add_argument(
        "-c", "--config", default="config/flybridge.jsonc", help="JSONC user configuration"
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=ArgumentParser)
    workflow = sub.add_parser("workflow", help="manage Flybridge workflows")
    workflow_sub = workflow.add_subparsers(
        dest="workflow_command", required=True, parser_class=ArgumentParser
    )
    start = workflow_sub.add_parser("start", help="open a single or role-separated Orca workflow")
    start.add_argument("repository", type=Path, nargs="?", help="repository directory")
    start.add_argument("-m", "--mode", choices=["single", "orchestrated"], help="workflow mode")
    start.add_argument("-n", "--name", help="workflow name")
    start.add_argument("-o", "--objective", help="work objective, or @path to read a file")
    start.add_argument(
        "--objective-file",
        type=Path,
        help="read the work objective from a UTF-8 file",
    )
    start.add_argument(
        "--batch",
        type=Path,
        help=(
            "JSON array of attach-existing starts; exclusive with the repository argument. "
            "Each item needs path (or repository), objective or objective_file, "
            "and optional mode, name, and issue. Prints one JSONL row per item, then a "
            "summary object"
        ),
    )
    start.add_argument(
        "--notify-terminal",
        help="Orca terminal handle of the parent agent to notify when a batch is ready",
    )
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
    start.add_argument(
        "--attach-existing",
        action="store_true",
        help="use the repository path as an existing Orca worktree without creating or deleting one",
    )
    start.add_argument(
        "--issue",
        help="canonical GitHub issue URL; required for new worktrees",
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
    status.add_argument(
        "--details",
        action="store_true",
        help="include active requests and lease-age attention hints",
    )
    inspect = queue_sub.add_parser("inspect", help="inspect one queue request")
    inspect.add_argument("request_id", help="queue request identifier")
    inspect.add_argument("-o", "--owner", required=True, help="recorded request owner")
    watch = queue_sub.add_parser("watch", help="stream queue snapshots and events")
    watch.add_argument("-i", "--interval", type=float, default=1.0, help="poll interval in seconds")
    watch.add_argument("--once", action="store_true", help="print one snapshot and exit")
    watch.add_argument(
        "--notify-workflow",
        help="deliver FIFO lease grants to this workflow's agent terminal",
    )
    board = sub.add_parser("board", help="optional GitHub Project candidate screening")
    board_sub = board.add_subparsers(
        dest="board_command", required=True, parser_class=ArgumentParser
    )
    screen = board_sub.add_parser("screen", help="list configured Project issues")
    screen.add_argument(
        "--board",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="project number or owner/number; repeatable; default: all configured boards",
    )
    screen.add_argument(
        "--status",
        action="append",
        default=[],
        help=(
            "Project Status option name; spaces and case are ignored so InProgress matches "
            "In progress; repeatable as OR; default: no status filter"
        ),
    )
    screen.add_argument(
        "--priority",
        action="append",
        default=[],
        help=(
            "Project Priority option name; spaces and case are ignored; repeatable as OR; "
            "default: no priority filter"
        ),
    )
    screen.add_argument(
        "--assignee",
        help="GitHub login; default: github.login from configuration",
    )
    screen.add_argument(
        "--all-assignees",
        action="store_true",
        help="do not filter by assignee",
    )
    screen.add_argument(
        "--with-refs",
        action="store_true",
        help="attach matching Orca worktrees and GitHub issue development facts",
    )
    screen.add_argument(
        "--path-prefix",
        action="append",
        default=[],
        dest="path_prefixes",
        metavar="PREFIX",
        help="with --with-refs, include worktrees under this resolved path; repeatable",
    )
    screen.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        dest="exclude_prefixes",
        metavar="PREFIX",
        help="with --with-refs, exclude worktrees under this resolved path; repeatable",
    )
    screen.add_argument(
        "--exclude-name",
        action="append",
        default=[],
        dest="exclude_names",
        metavar="NAME",
        help="with --with-refs, exclude worktrees whose path contains this directory name; repeatable",
    )
    prs = sub.add_parser("prs", help="optional GitHub authored pull-request screening")
    prs_sub = prs.add_subparsers(dest="prs_command", required=True, parser_class=ArgumentParser)
    prs_screen = prs_sub.add_parser("screen", help="list pull requests by author")
    prs_screen.add_argument(
        "--author",
        help="GitHub login; default: github.login from configuration",
    )
    prs_screen.add_argument(
        "--state",
        action="append",
        default=[],
        dest="states",
        choices=["OPEN", "MERGED", "CLOSED"],
        help="pull-request state; repeatable as OR; default: OPEN",
    )
    prs_screen.add_argument(
        "--with-review-facts",
        action="store_true",
        help=(
            "fetch review, comment, and thread facts for matched open pull requests "
            "that are not drafts and do not have pending checks"
        ),
    )
    prs_refresh = prs_sub.add_parser(
        "refresh-base",
        help="recompute a pull request base onto the current target-branch tip",
    )
    prs_refresh.add_argument("repository", help="GitHub repository in owner/name form")
    prs_refresh.add_argument("number", type=int, help="pull request number")
    operator = sub.add_parser("operator", help="show private guidance for the workflow operator")
    operator_sub = operator.add_subparsers(
        dest="operator_command", required=True, parser_class=ArgumentParser
    )
    operator_sub.add_parser(
        "guide", help="print the configured operator skill document index as JSON"
    )
    workflow_list = workflow_sub.add_parser("list", help="list Flybridge workflows")
    workflow_list.add_argument(
        "--status",
        action="append",
        choices=[status.value for status in WorkflowStatus],
        help="repeatable status filter",
    )
    workflow_list.add_argument("--json", action="store_true", help="print JSON (default)")
    workflow_status = workflow_sub.add_parser("status", help="show one workflow and its children")
    workflow_status.add_argument("workflow_id", help="workflow identifier")
    artifact = workflow_sub.add_parser("artifact", help="manage durable workflow artifacts")
    artifact_sub = artifact.add_subparsers(
        dest="artifact_command", required=True, parser_class=ArgumentParser
    )
    artifact_put = artifact_sub.add_parser("put", help="atomically store a workflow artifact")
    artifact_put.add_argument("workflow_id", help="authoring role workflow identifier")
    artifact_put.add_argument("--kind", required=True, choices=["plan", "verification", "review"])
    artifact_input = artifact_put.add_mutually_exclusive_group(required=True)
    artifact_input.add_argument("--file", type=Path, help="read artifact from a UTF-8 file")
    artifact_input.add_argument("--stdin", action="store_true", help="read artifact from stdin")
    for artifact_command in ("show", "verify"):
        artifact_read = artifact_sub.add_parser(
            artifact_command, help=f"{artifact_command} a durable workflow artifact"
        )
        artifact_read.add_argument("workflow_id", help="workflow or root manager identifier")
        artifact_read.add_argument(
            "--kind", required=True, choices=["plan", "verification", "review"]
        )
    observe = workflow_sub.add_parser("observe", help="open a visible queue observer terminal")
    observe.add_argument("workflow_id", help="running workflow identifier")
    role_ready = workflow_sub.add_parser(
        "role-ready", help="record verified orchestrated role readiness without closing terminals"
    )
    role_ready.add_argument("workflow_id", help="running role workflow identifier")
    role_ready.add_argument(
        "--summary", required=True, help="single-line successor or review summary"
    )
    role_ready.add_argument(
        "--outcome",
        choices=["approved", "changes-requested", "blocked"],
        help="reviewer outcome, or blocked for any orchestrated role",
    )
    single_report = workflow_sub.add_parser(
        "single-report", help="record a single agent's result for its parent batch"
    )
    single_report.add_argument("workflow_id")
    single_report.add_argument("--outcome", required=True, choices=["done", "blocked"])
    single_report.add_argument("--summary", required=True)
    batch = workflow_sub.add_parser("batch", help="inspect or watch a parent batch")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    batch_status = batch_sub.add_parser("status")
    batch_status.add_argument("batch_id")
    batch_watch = batch_sub.add_parser("watch")
    batch_watch.add_argument("batch_id")
    batch_watch.add_argument("--once", action="store_true")
    supervise = workflow_sub.add_parser(
        "supervise", help="run the durable workflow coordinator or single-role watchdog"
    )
    supervise.add_argument("workflow_id", help="orchestrated manager or single workflow identifier")
    supervise.add_argument("--once", action="store_true", help="perform at most one transition")
    coordinator_retry = workflow_sub.add_parser(
        "coordinator-retry",
        help="recover an eligible blocked or failed orchestration with current coordinator code",
    )
    coordinator_retry.add_argument("workflow_id", help="orchestrated manager workflow identifier")
    coordinator_close = workflow_sub.add_parser(
        "coordinator-close",
        help="close a released coordinator terminal as an operator fallback",
    )
    coordinator_close.add_argument("workflow_id", help="orchestrated manager workflow identifier")
    advance = workflow_sub.add_parser(
        "advance",
        help="start the next deterministic child role (operator-only)",
    )
    advance.add_argument("workflow_id", help="orchestrated manager workflow ID")
    handoff = workflow_sub.add_parser(
        "handoff",
        help="record the required deterministic role handoff (operator-only)",
    )
    handoff.add_argument(
        "source_workflow_id",
        help="running or completed source workflow identifier",
    )
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
    proceed = workflow_sub.add_parser(
        "proceed",
        help="record a handoff, complete the running source role, and advance (operator-only)",
    )
    proceed.add_argument("workflow_id", help="orchestrated manager workflow ID")
    proceed.add_argument("-s", "--summary", required=True, help="handoff summary")
    resume = workflow_sub.add_parser(
        "resume", help="resume a persisted running workflow in its existing Orca worktree"
    )
    resume.add_argument("workflow_id", help="running workflow identifier")
    for command in ("complete", "cancel", "fail"):
        descriptions = {
            "complete": (
                "mark a running workflow completed (operator-only; closes owned terminals)"
            ),
            "cancel": "cancel a requested, starting, or running workflow",
            "fail": "mark a running workflow failed",
        }
        finish = workflow_sub.add_parser(command, help=descriptions[command])
        finish.add_argument("workflow_id", help="workflow identifier")
        if command == "fail":
            finish.add_argument("-e", "--error", required=True, help="failure description")
    inventory = sub.add_parser(
        "inventory", help="snapshot Orca worktree git and pull-request facts"
    )
    inventory.add_argument(
        "--path-prefix",
        action="append",
        default=[],
        dest="path_prefixes",
        metavar="PREFIX",
        help="include worktrees under this resolved path; repeatable",
    )
    inventory.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        dest="exclude_prefixes",
        metavar="PREFIX",
        help=(
            "exclude worktrees under this resolved path; if the path does not exist, "
            "also exclude worktrees whose directory name matches the final path component; "
            "repeatable"
        ),
    )
    inventory.add_argument(
        "--exclude-name",
        action="append",
        default=[],
        dest="exclude_names",
        metavar="NAME",
        help="exclude worktrees whose resolved path contains this directory name; repeatable",
    )
    github_mode = inventory.add_mutually_exclusive_group()
    github_mode.add_argument(
        "--no-github",
        action="store_true",
        help="inspect Orca and git only",
    )
    github_mode.add_argument(
        "--with-review-facts",
        action="store_true",
        help=(
            "fetch review, comment, and thread facts for matched open pull requests "
            "that are not drafts and do not have pending checks"
        ),
    )
    reconcile = sub.add_parser("reconcile", help="observe and reconcile external state")
    reconcile.add_argument("--dry-run", action="store_true", help="report planned changes only")
    reconcile.add_argument("--no-github", action="store_true", help="skip GitHub API observations")
    worktree = sub.add_parser("worktree", help="manage observed worktree metadata")
    worktree_sub = worktree.add_subparsers(
        dest="worktree_command", required=True, parser_class=ArgumentParser
    )
    repository = worktree_sub.add_parser("repository", help="manage related repositories")
    repository_sub = repository.add_subparsers(
        dest="repository_command", required=True, parser_class=ArgumentParser
    )
    for command in ("add", "remove"):
        relation = repository_sub.add_parser(command)
        relation.add_argument("orca_id")
        relation.add_argument("path", type=Path)
    doctor = sub.add_parser("doctor", help="check local dependencies and configuration")
    doctor.add_argument(
        "-m", "--mode", choices=["single", "orchestrated"], help="workflow mode to validate"
    )
    cleanup = workflow_sub.add_parser("cleanup", help="report recoverable local workflow state")
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument("-n", "--dry-run", action="store_true", help="report candidates only")
    cleanup_mode.add_argument(
        "-a", "--apply", action="store_true", help="close eligible owned worktrees"
    )
    cleanup.add_argument("-t", "--older-than-seconds", type=float, help="minimum stale age")
    cleanup.add_argument("-f", "--force-age", action="store_true", help="treat record age as stale")
    cleanup.add_argument(
        "-w",
        "--workflow",
        action="append",
        dest="workflow_ids",
        help="limit cleanup to one or more workflow identifiers",
    )
    harvest = workflow_sub.add_parser(
        "harvest",
        help="copy worker commits into the manager worktree without closing resources",
    )
    harvest.add_argument(
        "workflow_ids",
        nargs="+",
        help="root manager, child, or run identifiers",
    )
    harvest.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report the planned integration without changing git state",
    )
    delivery_check = workflow_sub.add_parser(
        "delivery-check",
        help="verify that manager HEAD is the exact locally approved orchestration tip",
    )
    delivery_check.add_argument(
        "workflow_ids",
        nargs="+",
        help="root manager, child, or run identifiers",
    )
    retire = workflow_sub.add_parser(
        "retire",
        help="harvest worker commits, then close owned child or manager resources",
    )
    retire.add_argument(
        "workflow_ids",
        nargs="+",
        help="root manager, child, or run identifiers",
    )
    retire.add_argument(
        "--keep",
        required=True,
        choices=["manager", "none"],
        help="retain the manager terminal and worktree, or close the manager too",
    )
    retire.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report harvest and closes without changing git or Orca resources",
    )
    link = workflow_sub.add_parser("link", help="link an explicit GitHub issue or pull request")
    link.add_argument("run_id", help="workflow run id or step id")
    link.add_argument("canonical_url")
    link.add_argument("--relation", required=True, choices=["primary", "related"])
    unlink = workflow_sub.add_parser("unlink", help="remove an explicit GitHub reference")
    unlink.add_argument("run_id", help="workflow run id or step id")
    unlink.add_argument("canonical_url")
    return parser
