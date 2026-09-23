from __future__ import annotations

import json
from dataclasses import asdict

from flybridge_application import reconcile_external_state
from flybridge_core import ReconcileStore
from flybridge_github import GitHubIssueDevelopment, GitHubPullRequests

from ..runtime import _adapter, _config


def handle_reconcile(args) -> int:
    config = _config(args)
    store = ReconcileStore(config.state_dir, event_retention=config.reconcile.event_retention)
    result = reconcile_external_state(
        _adapter(config),
        store,
        dry_run=args.dry_run,
        include_git=True,
        missing_observations=config.reconcile.missing_observations,
        missing_grace_seconds=config.reconcile.missing_grace_seconds,
        exclude_worktrees=config.reconcile.exclude_worktrees,
    )
    # GitHub collection is deliberately independent; --no-github guarantees it is skipped.
    payload = asdict(result)
    github_skipped = bool(args.no_github or not config.github.enabled)
    payload["github_skipped"] = github_skipped
    github_errors: list[str] = []
    github_observed: list[str] = []
    if not github_skipped and result.orca.success:
        pull_requests = GitHubPullRequests(user=config.github.login)
        for target in store.branch_targets():
            facts, failure = pull_requests.list_by_head(target["repository"], target["branch"])
            if failure is not None:
                if not args.dry_run:
                    store.mark_github_error(target["repository"], failure.message)
                github_errors.append(f"{target['repository']}: {failure.message}")
                continue
            if not args.dry_run:
                store.apply_pull_request_candidates(target["run_id"], facts)
            github_observed.extend(fact.url for fact in facts)
        issues = GitHubIssueDevelopment(user=config.github.login)
        for target in store.explicit_issue_targets():
            try:
                development = issues.fetch(target["url"])
            except RuntimeError as exc:
                github_errors.append(f"{target['url']}: {exc}")
                continue
            github_observed.append(development.issue_url)
            if not args.dry_run:
                for url in development.body_urls:
                    store.add_inferred_reference(
                        target["run_id"], url, relation="related", source="issue_body"
                    )
                for pull in development.pull_requests:
                    store.add_inferred_reference(
                        target["run_id"],
                        pull.url,
                        relation="related",
                        source="linked_issue",
                    )
            for branch in development.linked_branches:
                if not branch.repository:
                    continue
                facts, failure = pull_requests.list_by_head(branch.repository, branch.name)
                if failure is not None:
                    if not args.dry_run:
                        store.mark_github_error(branch.repository, failure.message)
                    github_errors.append(f"{branch.repository}: {failure.message}")
                    continue
                if not args.dry_run:
                    store.apply_pull_request_candidates(
                        target["run_id"], facts, source="linked_branch"
                    )
    payload["github_observed"] = github_observed
    payload["github_errors"] = github_errors
    print(json.dumps(payload, indent=2))
    return 0 if result.orca.success else 2


def handle_worktree(args) -> int:
    config = _config(args)
    store = ReconcileStore(config.state_dir, event_retention=config.reconcile.event_retention)
    if args.repository_command == "add":
        store.add_related_repository(args.orca_id, args.path)
    else:
        store.remove_related_repository(args.orca_id, args.path)
    print(
        json.dumps(
            {
                "orca_id": args.orca_id,
                "path": str(args.path.expanduser().resolve()),
                "relation": "related",
                "action": args.repository_command,
            },
            indent=2,
        )
    )
    return 0
