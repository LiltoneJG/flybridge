from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict

from flybridge_application import (
    OPERATOR_GUIDANCE,
    GitProbeError,
    GitWorktreeProbe,
    attach_issue_refs,
    attach_matched_review_facts,
    collect_inventory,
    format_pull_request_failures,
    selected_github_repositories,
    supplement_pull_requests,
    validate_skill_paths,
)
from flybridge_application.board_refs import worktree_comment_issue_urls
from flybridge_application.git_probe import GitWorktreeState
from flybridge_application.inventory import _pull_request_payload
from flybridge_core import ConfigError, WorkflowMode, WorkflowRole
from flybridge_github import (
    GitHubCli,
    GitHubCliError,
    GitHubIssueDevelopment,
    GitHubProject,
    GitHubPullRequestError,
    GitHubPullRequests,
)
from flybridge_github.project import GitHubProject as GitHubProjectType
from flybridge_orca import OrcaClient, agent_cli_is_resolvable

from ..runtime import _adapter, _config
from .inventory import select_listed_worktrees


def handle_board(args: argparse.Namespace) -> int:
    if args.assignee and args.all_assignees:
        raise ValueError("pass only one of --assignee and --all-assignees")
    config = _config(args)
    assignee = None if args.all_assignees else (args.assignee or config.github.login)
    project = GitHubProject(config.github)
    candidates = project.screen(
        boards=args.board or None,
        statuses=args.status,
        priorities=args.priority,
        assignee=assignee,
    )
    issues = [asdict(candidate) for candidate in candidates]
    failures: list[str] = []
    unmatched: list[dict] = []
    warnings: list[str] = []
    unmatched_status = GitHubProjectType.unmatched_option_filters(
        args.status or (), project.seen_status_names
    )
    if unmatched_status:
        seen = ", ".join(project.seen_status_names) or "(none)"
        warnings.append(
            "no Project Status matched " + ", ".join(unmatched_status) + f"; seen: {seen}"
        )
    unmatched_priority = GitHubProjectType.unmatched_option_filters(
        args.priority or (), project.seen_priority_names
    )
    if unmatched_priority:
        seen = ", ".join(project.seen_priority_names) or "(none)"
        warnings.append(
            "no Project Priority matched " + ", ".join(unmatched_priority) + f"; seen: {seen}"
        )
    if args.with_refs:
        issues, unmatched, failures = _attach_refs(config, issues, args)
    payload: dict[str, object] = {"count": len(issues), "issues": issues}
    if args.with_refs:
        payload["unmatched_worktrees"] = unmatched
    if warnings:
        payload["warnings"] = warnings
    if failures:
        payload["failures"] = failures
    print(json.dumps(payload, indent=2))
    return 0


def handle_prs(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.github.enabled:
        raise ValueError("GitHub integration is disabled in configuration")
    github = GitHubPullRequests(user=config.github.login)
    if args.prs_command == "refresh-base":
        payload = github.refresh_base(args.repository, args.number)
        print(json.dumps(payload, indent=2))
        return 0
    if args.prs_command != "screen":
        raise ValueError(f"unknown prs command: {args.prs_command}")
    author = args.author or config.github.login
    states = tuple(state.upper() for state in (args.states or ["OPEN"]))
    facts = github.list_authored(author, states)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "author": author,
        "states": list(states),
        "pull_requests": [_pull_request_payload(fact) for fact in facts],
        "failures": [],
    }
    if args.with_review_facts:
        snapshot = attach_matched_review_facts(snapshot, github)
    print(json.dumps(snapshot, indent=2))
    return 0


def handle_operator(args: argparse.Namespace) -> int:
    if args.operator_command != "guide":
        raise ValueError(f"unknown operator command: {args.operator_command}")
    config = _config(args)
    paths = validate_skill_paths(config.operator_skill_paths())
    print(
        json.dumps(
            {
                "response_language": config.response_language,
                "skill_paths": [str(path) for path in paths],
                "instruction": OPERATOR_GUIDANCE,
            },
            indent=2,
        )
    )
    return 0


def _attach_refs(
    config, issues: list[dict], args: argparse.Namespace
) -> tuple[list[dict], list[dict], list[str]]:
    client = _adapter(config)
    listed, truncated = client.list_worktrees()
    selected = select_listed_worktrees(listed, args, config.reconcile.exclude_worktrees)
    probe = GitWorktreeProbe()
    git_states: dict[str, GitWorktreeState | GitProbeError] = {}
    for worktree in selected:
        try:
            git_states[worktree.identity] = probe.inspect(worktree.path)
        except GitProbeError as exc:
            git_states[worktree.identity] = exc
    pull_requests_by_repository = None
    pull_request_errors: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    include_github = config.github.enabled
    if include_github:
        used = selected_github_repositories(git_states)
        github = GitHubPullRequests(user=config.github.login)
        try:
            open_facts, query_failures = github.list_open(used)
            open_facts, extra_failures = supplement_pull_requests(
                github,
                selected,
                git_states,
                open_facts,
                skipped_repositories=tuple(item.repository for item in query_failures),
            )
            pull_requests_by_repository = open_facts
            pull_request_errors, _, unavailable = format_pull_request_failures(
                (*query_failures, *extra_failures),
                used,
            )
        except GitHubPullRequestError as exc:
            pull_requests_by_repository = {}
            pull_request_errors = (str(exc),)
    snapshot = collect_inventory(
        selected,
        probe,
        truncated=truncated,
        git_states=git_states,
        pull_requests_by_repository=pull_requests_by_repository,
        pull_request_errors=pull_request_errors,
        unavailable_github_repositories=unavailable,
        include_github=include_github,
    )
    failures = [str(item) for item in snapshot.get("failures") or []]
    development: dict[str, dict] = {}
    if config.github.enabled:
        urls = [issue["url"] for issue in issues if isinstance(issue.get("url"), str)]
        urls.extend(worktree_comment_issue_urls(snapshot["worktrees"]))
        unique_urls = list(dict.fromkeys(urls))
        try:
            fetched = GitHubIssueDevelopment(user=config.github.login).fetch_many(unique_urls)
            development = {key: value.as_dict() for key, value in fetched.items()}
        except GitHubPullRequestError as exc:
            failures.append(str(exc))
    attached, unmatched = attach_issue_refs(issues, snapshot["worktrees"], development)
    return attached, unmatched, failures


def handle_doctor(args: argparse.Namespace) -> int:
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
        validate_skill_paths(tuple(dict.fromkeys((*paths, *config.operator_skill_paths()))))
    except ConfigError as exc:
        skill_paths_valid = False
        skill_error = str(exc)
    missing_agents = [role.value for role in required_roles if role not in config.orca_agents]
    unresolved_agents = []
    for role in required_roles:
        if role not in config.orca_agents:
            continue
        specs = config.agent_specs(role)
        for index, spec in enumerate(specs):
            if agent_cli_is_resolvable(
                spec.agent,
                model=spec.model,
                presets=config.agent_launch_presets,
                which=shutil.which,
            ):
                continue
            unresolved_agents.append(role.value if len(specs) == 1 else f"{role.value}[{index}]")
    orca_report: dict[str, object] = {"reachable": False}
    if shutil.which(config.orca_executable):
        try:
            client = OrcaClient(config.orca_executable)
            client.launch_presets = config.agent_launch_presets
            orca_report = {
                "reachable": True,
                **client.verify(),
            }
        except RuntimeError as exc:
            orca_report = {"reachable": False, "error": str(exc)}
    report = {
        "config": str(config.path),
        "mode": mode,
        "orca": orca_report,
        "gh": bool(shutil.which("gh")),
        "github_enabled": config.github.enabled,
        "github_login": config.github.login or None,
        "skill_sources_valid": skill_paths_valid,
        "required_agents_configured": not missing_agents,
        "agent_cli_resolvable": not unresolved_agents,
    }
    if skill_error:
        report["skill_error"] = skill_error
    if missing_agents:
        report["missing_agents"] = missing_agents
    if unresolved_agents:
        report["unresolved_agents"] = unresolved_agents
    github_ready = not config.github.enabled or bool(report["gh"])
    if config.github.enabled and report["gh"]:
        try:
            accounts = GitHubCli(user=config.github.login).inspect_accounts()
        except GitHubCliError as exc:
            accounts = {
                "hostname": "github.com",
                "configured_login": config.github.login,
                "active_login": None,
                "authenticated_logins": [],
                "configured_login_authenticated": False,
                "error": str(exc),
            }
            github_ready = False
        else:
            github_ready = bool(accounts.get("configured_login_authenticated"))
        report["github_active_login"] = accounts.get("active_login")
        report["github_login_authenticated"] = accounts.get("configured_login_authenticated")
        if accounts.get("error"):
            report["github_auth_error"] = accounts["error"]
    elif config.github.enabled:
        report["github_active_login"] = None
        report["github_login_authenticated"] = False
    print(json.dumps(report, indent=2))
    return (
        0
        if orca_report.get("reachable")
        and skill_paths_valid
        and not missing_agents
        and not unresolved_agents
        and github_ready
        else 1
    )
