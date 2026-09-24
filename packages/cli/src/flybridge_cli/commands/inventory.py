from __future__ import annotations

import argparse
import json

from flybridge_application import (
    GitProbeError,
    GitWorktreeProbe,
    InventoryWorktree,
    attach_matched_review_facts,
    collapse_inventory_worktrees,
    collect_inventory,
    filter_github_query_targets,
    format_pull_request_failures,
    path_is_included,
    repository_is_skipped,
    resolved_names,
    resolved_prefixes,
    selected_github_repositories,
    supplement_pull_requests,
)
from flybridge_application.git_probe import GitWorktreeState
from flybridge_github import GitHubPullRequestError, GitHubPullRequests
from flybridge_orca import ListedWorktree

from ..runtime import _adapter, _config


def _identity(worktree: ListedWorktree) -> InventoryWorktree:
    hint: dict[str, object] = {"issues": [], "pull_request": None}
    if worktree.linked_pull_request is not None:
        number, state = worktree.linked_pull_request
        hint["pull_request"] = {"number": number, "state": state}
    return InventoryWorktree(
        worktree.worktree_id,
        worktree.path,
        worktree.name,
        worktree.workspace_status,
        worktree.comment,
        hint,
        worktree.linked_issue,
        worktree.project_id,
    )


def select_listed_worktrees(
    listed: tuple[ListedWorktree, ...],
    args: argparse.Namespace,
    exclude_patterns: tuple[str, ...] = (),
) -> list[InventoryWorktree]:
    include = resolved_prefixes(getattr(args, "path_prefixes", None) or [])
    exclude = resolved_prefixes(getattr(args, "exclude_prefixes", None) or [])
    names = resolved_names(getattr(args, "exclude_names", None) or [])
    selected = [
        _identity(worktree)
        for worktree in listed
        if path_is_included(worktree.path, include, exclude, names, exclude_patterns)
    ]
    return collapse_inventory_worktrees(selected)


def handle_inventory(args: argparse.Namespace) -> int:
    config = _config(args)
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
    include_github = config.github.enabled and not args.no_github
    pull_requests_by_repository = None
    pull_request_errors: tuple[str, ...] = ()
    pull_request_warnings: tuple[str, ...] = ()
    unavailable_github_repositories: tuple[str, ...] = ()
    skipped_github_repositories: tuple[str, ...] = ()
    github = None
    if include_github:
        skip_patterns = config.github.skip_repositories
        used_all = selected_github_repositories(git_states)
        skipped_github_repositories = tuple(
            repository
            for repository in used_all
            if repository_is_skipped(repository, skip_patterns)
        )
        used = filter_github_query_targets(used_all, skip_patterns)
        github = GitHubPullRequests(user=config.github.login)
        try:
            open_facts, query_failures = github.list_open(used)
            open_facts, extra_failures = supplement_pull_requests(
                github,
                selected,
                git_states,
                open_facts,
                skipped_repositories=tuple(
                    dict.fromkeys(
                        (
                            *skipped_github_repositories,
                            *(item.repository for item in query_failures),
                        )
                    )
                ),
            )
        except GitHubPullRequestError as exc:
            pull_requests_by_repository = {}
            if used:
                pull_request_errors = tuple(f"{repository}: {exc}" for repository in sorted(used))
                unavailable_github_repositories = tuple(sorted(used))
            else:
                pull_request_errors = (str(exc),)
        else:
            pull_requests_by_repository = open_facts
            pull_request_errors, pull_request_warnings, unavailable_github_repositories = (
                format_pull_request_failures(
                    (*query_failures, *extra_failures),
                    used,
                    skip_patterns=skip_patterns,
                )
            )
    snapshot = collect_inventory(
        selected,
        probe,
        truncated=truncated,
        git_states=git_states,
        pull_requests_by_repository=pull_requests_by_repository,
        pull_request_errors=pull_request_errors,
        unavailable_github_repositories=unavailable_github_repositories,
        include_github=include_github,
        warnings=pull_request_warnings,
        skipped_github_repositories=skipped_github_repositories,
    )
    if github is not None and getattr(args, "with_review_facts", False):
        snapshot = attach_matched_review_facts(snapshot, github)
    print(json.dumps(snapshot, indent=2))
    if include_github and (snapshot.get("failures") or pull_request_errors):
        return 2
    return 0
