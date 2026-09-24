from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flybridge_github.pull_requests import (
    GitHubPullRequests,
    PullRequestFact,
    PullRequestQueryFailure,
)

from .git_probe import GitProbeError, GitWorktreeProbe, GitWorktreeState, resolve_path_prefix
from .issue_hints import github_hint_issues

INVENTORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InventoryWorktree:
    identity: str
    path: str
    name: str
    workspace_status: str
    comment: str
    github_hint: dict[str, Any]
    linked_issue: int | None = None
    project_id: str = ""
    aliases: tuple[dict[str, str], ...] = ()


def normalize_ref_name(value: str) -> str:
    return value.strip().removeprefix("refs/heads/")


def worktree_is_excluded(worktree_path: str, patterns: Sequence[str]) -> bool:
    """Return True when a configured glob or directory name matches the checkout."""
    if not patterns:
        return False
    resolved = Path(worktree_path).expanduser().resolve()
    posix = resolved.as_posix()
    for pattern in patterns:
        text = pattern.strip()
        if not text:
            continue
        if any(marker in text for marker in "*?["):
            if fnmatch.fnmatch(posix, text) or any(
                fnmatch.fnmatch(part, text) for part in resolved.parts
            ):
                return True
            continue
        if text in resolved.parts:
            return True
    return False


def path_is_included(
    worktree_path: str,
    include_prefixes: Sequence[Path],
    exclude_prefixes: Sequence[Path],
    exclude_names: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> bool:
    resolved = Path(worktree_path).expanduser().resolve()
    if include_prefixes and not any(_under(resolved, prefix) for prefix in include_prefixes):
        return False
    if any(_under(resolved, prefix) for prefix in exclude_prefixes):
        return False
    names = [name.strip() for name in exclude_names if name and name.strip()]
    if any(name in resolved.parts for name in names):
        return False
    return not worktree_is_excluded(worktree_path, exclude_patterns)


def collapse_inventory_worktrees(
    worktrees: Sequence[InventoryWorktree],
) -> list[InventoryWorktree]:
    """Keep one row per resolved checkout; extra Orca cards become aliases."""
    groups: dict[str, list[InventoryWorktree]] = {}
    order: list[str] = []
    for worktree in worktrees:
        key = str(Path(worktree.path).expanduser().resolve())
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(worktree)
    collapsed: list[InventoryWorktree] = []
    for key in order:
        items = groups[key]
        if len(items) == 1 and not items[0].aliases:
            collapsed.append(items[0])
            continue
        primary = max(
            items,
            key=lambda item: (
                1 if item.linked_issue is not None else 0,
                len((item.comment or "").strip()),
            ),
        )
        aliases = [
            *primary.aliases,
            *(
                {
                    "id": item.identity,
                    "name": item.name,
                    "workspace_status": item.workspace_status,
                }
                for item in items
                if item.identity != primary.identity
            ),
        ]
        collapsed.append(
            InventoryWorktree(
                primary.identity,
                primary.path,
                primary.name,
                primary.workspace_status,
                primary.comment,
                primary.github_hint,
                primary.linked_issue,
                primary.project_id,
                tuple(aliases),
            )
        )
    return collapsed


def _under(path: Path, prefix: Path) -> bool:
    if path == prefix or path.is_relative_to(prefix):
        return True
    if prefix.exists():
        return False
    name = prefix.name
    return bool(name) and name in path.parts


def _hint(worktree: InventoryWorktree, git_repository: str | None) -> dict[str, Any]:
    pull_request = None
    if isinstance(worktree.github_hint, dict):
        pull_request = worktree.github_hint.get("pull_request")
    return {
        "issues": github_hint_issues(
            comment=worktree.comment,
            project_id=worktree.project_id,
            linked_issue=worktree.linked_issue,
            git_repository=git_repository,
        ),
        "pull_request": pull_request,
    }


def checkout_heads(state: GitWorktreeState) -> tuple[tuple[str, str], ...]:
    heads: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if state.github_repository and state.branch:
        key = (state.github_repository, normalize_ref_name(state.branch))
        heads.append(key)
        seen.add(key)
    for item in state.submodules:
        if not item.github_repository or not item.branch:
            continue
        key = (item.github_repository, normalize_ref_name(item.branch))
        if key in seen:
            continue
        seen.add(key)
        heads.append(key)
    return tuple(heads)


def selected_github_repositories(
    git_states: Mapping[str, GitWorktreeState | GitProbeError],
) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for state in git_states.values():
        if not isinstance(state, GitWorktreeState):
            continue
        for repository in (
            *state.github_repositories,
            *(item.github_repository for item in state.submodules if item.github_repository),
        ):
            if repository in seen:
                continue
            seen.add(repository)
            found.append(repository)
    return tuple(found)


def repository_is_skipped(repository: str, patterns: Sequence[str]) -> bool:
    """Return True when a GitHub nameWithOwner matches a skip prefix or exact name."""
    name = repository.strip().lower()
    if not name:
        return False
    for pattern in patterns:
        text = pattern.strip().lower()
        if not text:
            continue
        if text.endswith("/"):
            if name.startswith(text):
                return True
            continue
        if name == text or name.startswith(f"{text}/"):
            return True
    return False


def filter_github_query_targets(
    repositories: Sequence[str], skip_patterns: Sequence[str]
) -> tuple[str, ...]:
    return tuple(
        repository
        for repository in repositories
        if not repository_is_skipped(repository, skip_patterns)
    )


def classify_pull_request_failure(failure: PullRequestQueryFailure) -> str:
    message = failure.message
    if GitHubPullRequests.is_transient_message(message):
        return "transient"
    lowered = message.lower()
    if "could not resolve to a repository" in lowered or "repository was not found" in lowered:
        return "missing"
    return "fatal"


def collect_inventory(
    worktrees: Sequence[InventoryWorktree],
    probe: GitWorktreeProbe,
    *,
    truncated: bool = False,
    git_states: Mapping[str, GitWorktreeState | GitProbeError] | None = None,
    pull_requests_by_repository: Mapping[str, Sequence[Any]] | None = None,
    pull_request_errors: Sequence[str] = (),
    unavailable_github_repositories: Sequence[str] = (),
    include_github: bool = True,
    warnings: Sequence[str] = (),
    skipped_github_repositories: Sequence[str] = (),
) -> dict[str, Any]:
    selected_states: dict[str, GitWorktreeState | GitProbeError] = {}
    rows_meta: list[tuple[InventoryWorktree, GitWorktreeState | None, list[str]]] = []
    for worktree in sorted(worktrees, key=lambda item: (item.path, item.identity)):
        errors: list[str] = []
        cached = None if git_states is None else git_states.get(worktree.identity)
        try:
            if isinstance(cached, GitProbeError):
                raise cached
            state = cached if isinstance(cached, GitWorktreeState) else probe.inspect(worktree.path)
            selected_states[worktree.identity] = state
            rows_meta.append((worktree, state, errors))
        except GitProbeError as exc:
            errors.append(str(exc))
            selected_states[worktree.identity] = exc
            rows_meta.append((worktree, None, errors))
    used_repositories = set(selected_github_repositories(selected_states))
    skipped = frozenset(
        repository for repository in skipped_github_repositories if repository in used_repositories
    )
    failures: list[str] = []
    if truncated:
        failures.append("Orca worktree list was truncated")
    if include_github:
        failures.extend(
            error
            for error in pull_request_errors
            if any(
                repository in error for repository in used_repositories if repository not in skipped
            )
        )
    unavailable = frozenset(
        repository
        for repository in unavailable_github_repositories
        if repository in used_repositories and repository not in skipped
    )
    rows: list[dict[str, Any]] = []
    for worktree, state, errors in rows_meta:
        git_payload = _git_payload(state) if state is not None else None
        repository = state.github_repository if state is not None else None
        if include_github:
            for repo in _state_repositories(state):
                if repo in unavailable:
                    errors.append(f"GitHub pull requests were unavailable for {repo}")
        orca: dict[str, Any] = {
            "id": worktree.identity,
            "path": worktree.path,
            "name": worktree.name,
            "workspace_status": worktree.workspace_status,
            "comment": worktree.comment,
            "github_hint": _hint(worktree, repository),
        }
        if worktree.aliases:
            orca["aliases"] = list(worktree.aliases)
        row: dict[str, Any] = {
            "orca": orca,
            "git": git_payload,
            "errors": errors,
        }
        if include_github:
            row["pull_requests"] = _match_worktree_pull_requests(
                pull_requests_by_repository or {},
                state,
            )
        rows.append(row)
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worktrees": rows,
        "failures": failures,
        "warnings": list(warnings),
        "skipped_github_repositories": list(skipped_github_repositories),
    }


def _state_repositories(state: GitWorktreeState | None) -> tuple[str, ...]:
    if state is None:
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for repository in (
        *state.github_repositories,
        *(item.github_repository for item in state.submodules if item.github_repository),
    ):
        if repository in seen:
            continue
        seen.add(repository)
        found.append(repository)
    return tuple(found)


def _git_payload(state: GitWorktreeState) -> dict[str, Any]:
    return {
        "branch": state.branch,
        "dirty": state.dirty,
        "unpushed_commits": state.unpushed_commits,
        "ahead": state.ahead,
        "behind": state.behind,
        "github_repository": state.github_repository,
        "github_repositories": list(state.github_repositories),
        "submodules": [
            {
                "path": item.path,
                "sha": item.sha,
                "dirty": item.dirty,
                "unpushed_commits": item.unpushed_commits,
                "github_repository": item.github_repository,
                "branch": item.branch,
            }
            for item in state.submodules
        ],
    }


def _match_worktree_pull_requests(
    by_repository: Mapping[str, Sequence[Any]],
    state: GitWorktreeState | None,
) -> list[dict[str, Any]]:
    if state is None:
        return []
    matched: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add(repository: str | None, branch: str, source: str) -> None:
        if not repository or not branch:
            return
        for payload in _match_pull_requests(by_repository, repository, branch):
            key = (payload["repository"], payload["number"])
            if key in seen:
                continue
            seen.add(key)
            item = dict(payload)
            item["matched_from"] = source
            matched.append(item)

    add(state.github_repository, state.branch, "parent")
    for item in state.submodules:
        add(item.github_repository, item.branch, "submodule")
    return matched


def _match_pull_requests(
    by_repository: Mapping[str, Sequence[Any]],
    repository: str | None,
    branch: str,
) -> list[dict[str, Any]]:
    if not repository:
        return []
    head = normalize_ref_name(branch)
    matched: list[dict[str, Any]] = []
    for fact in by_repository.get(repository, ()):
        if normalize_ref_name(getattr(fact, "head_ref_name", "")) != head:
            continue
        matched.append(_pull_request_payload(fact))
    return matched


def _pull_request_payload(fact: Any) -> dict[str, Any]:
    return {
        "repository": fact.repository,
        "number": fact.number,
        "title": fact.title,
        "url": fact.url,
        "state": fact.state,
        "is_draft": fact.is_draft,
        "mergeable": fact.mergeable,
        "merge_state_status": fact.merge_state_status,
        "review_decision": fact.review_decision,
        "head_ref_name": fact.head_ref_name,
        "base_ref_name": getattr(fact, "base_ref_name", None),
        "base_ref_oid": getattr(fact, "base_ref_oid", None),
        "base_ref_tip_oid": getattr(fact, "base_ref_tip_oid", None),
        "base_ref_stale": getattr(fact, "base_ref_stale", None),
        "assignees": list(fact.assignees),
        "checks": [
            {
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
                "is_required": getattr(check, "is_required", None),
            }
            for check in fact.checks
        ],
        "unresolved_review_threads": fact.unresolved_review_threads,
        "issue_comment_count": fact.issue_comment_count,
        "author": getattr(fact, "author", None),
    }


def merge_pull_request_facts(
    existing: Mapping[str, Sequence[PullRequestFact]],
    extra: Sequence[PullRequestFact],
) -> dict[str, tuple[PullRequestFact, ...]]:
    merged: dict[str, list[PullRequestFact]] = {
        repository: list(facts) for repository, facts in existing.items()
    }
    seen = {(fact.repository, fact.number): None for facts in merged.values() for fact in facts}
    for fact in extra:
        key = (fact.repository, fact.number)
        if key in seen:
            continue
        seen[key] = None
        merged.setdefault(fact.repository, []).append(fact)
    return {repository: tuple(facts) for repository, facts in merged.items()}


def supplement_pull_requests(
    client: GitHubPullRequests,
    worktrees: Sequence[InventoryWorktree],
    git_states: Mapping[str, GitWorktreeState | GitProbeError],
    open_facts: Mapping[str, Sequence[PullRequestFact]],
    skipped_repositories: Sequence[str] = (),
) -> tuple[dict[str, tuple[PullRequestFact, ...]], tuple[PullRequestQueryFailure, ...]]:
    extras: list[PullRequestFact] = []
    failures: list[PullRequestQueryFailure] = []
    skipped = {name.lower() for name in skipped_repositories if name}
    for worktree in worktrees:
        cached = git_states.get(worktree.identity)
        if not isinstance(cached, GitWorktreeState):
            continue
        for repository, branch in checkout_heads(cached):
            if repository.lower() in skipped:
                continue
            if _head_has_fact(open_facts, repository, branch):
                continue
            facts, failure = client.list_by_head(repository, branch)
            extras.extend(facts)
            if failure is not None:
                failures.append(failure)
        hinted = _hinted_pull_request_number(worktree)
        repository = cached.github_repository
        if hinted is None or repository is None or repository.lower() in skipped:
            continue
        if _number_has_fact(open_facts, extras, repository, hinted):
            continue
        fact, failure = client.get(repository, hinted)
        if fact is not None:
            extras.append(fact)
        if failure is not None:
            failures.append(failure)
    return merge_pull_request_facts(open_facts, extras), tuple(failures)


def _head_has_fact(
    by_repository: Mapping[str, Sequence[PullRequestFact]],
    repository: str,
    branch: str,
) -> bool:
    head = normalize_ref_name(branch)
    return any(
        normalize_ref_name(fact.head_ref_name) == head for fact in by_repository.get(repository, ())
    )


def _number_has_fact(
    open_facts: Mapping[str, Sequence[PullRequestFact]],
    extras: Sequence[PullRequestFact],
    repository: str,
    number: int,
) -> bool:
    for fact in (*open_facts.get(repository, ()), *extras):
        if fact.repository == repository and fact.number == number:
            return True
    return False


def _hinted_pull_request_number(worktree: InventoryWorktree) -> int | None:
    if not isinstance(worktree.github_hint, dict):
        return None
    hinted = worktree.github_hint.get("pull_request")
    if not isinstance(hinted, dict):
        return None
    number = hinted.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    return number


def format_pull_request_failures(
    failures: Sequence[PullRequestQueryFailure],
    used_repositories: Sequence[str],
    *,
    skip_patterns: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return fatal messages, transient warnings, and unavailable repositories."""
    used = frozenset(used_repositories)
    fatal_messages: list[str] = []
    warning_messages: list[str] = []
    unavailable: list[str] = []
    for failure in failures:
        if failure.repository not in used:
            continue
        if repository_is_skipped(failure.repository, skip_patterns):
            continue
        kind = classify_pull_request_failure(failure)
        text = (
            failure.message
            if failure.repository in failure.message
            else f"{failure.repository}: {failure.message}"
        )
        if kind == "transient":
            warning_messages.append(text)
            continue
        unavailable.append(failure.repository)
        fatal_messages.append(text)
    return (
        tuple(fatal_messages),
        tuple(warning_messages),
        tuple(dict.fromkeys(unavailable)),
    )


def resolved_prefixes(values: Sequence[str]) -> tuple[Path, ...]:
    return tuple(resolve_path_prefix(value) for value in values)


def resolved_names(values: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for value in values:
        name = value.strip()
        if not name:
            raise ValueError("exclude name must be a non-empty string")
        if "/" in name or name in {".", ".."}:
            raise ValueError(f"exclude name is invalid: {value}")
        names.append(name)
    return tuple(names)


_PENDING_CHECK_STATUSES = frozenset(
    {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
)


def pull_request_needs_review_facts(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("state") or "").upper() != "OPEN":
        return False
    if payload.get("is_draft") is True:
        return False
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return True
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").upper()
        if status in _PENDING_CHECK_STATUSES:
            return False
    return True


def unique_review_fact_keys(snapshot: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    keys: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for payload in _iter_pull_request_payloads(snapshot):
        if not pull_request_needs_review_facts(payload):
            continue
        repository = payload.get("repository")
        number = payload.get("number")
        if not isinstance(repository, str) or not isinstance(number, int):
            continue
        key = (repository, number)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return tuple(keys)


def attach_matched_review_facts(
    snapshot: dict[str, Any],
    client: GitHubPullRequests,
) -> dict[str, Any]:
    facts: dict[tuple[str, int], dict[str, Any]] = {}
    extra_failures: list[str] = []
    heads = _review_fact_heads(snapshot)
    for repository, number in unique_review_fact_keys(snapshot):
        fact, failure = client.get_review_facts(
            repository,
            number,
            head_ref_name=heads.get((repository, number)),
        )
        if fact is not None:
            facts[(repository, number)] = fact
        if failure is not None:
            extra_failures.append(f"{repository}#{number}: {failure.message}")
    return apply_review_facts(snapshot, facts, extra_failures)


def apply_review_facts(
    snapshot: dict[str, Any],
    facts: Mapping[tuple[str, int], Mapping[str, Any]],
    extra_failures: Sequence[str] = (),
) -> dict[str, Any]:
    failures = [str(item) for item in snapshot.get("failures") or []]
    failures.extend(extra_failures)
    snapshot["failures"] = failures
    for payload in _iter_pull_request_payloads(snapshot):
        repository = payload.get("repository")
        number = payload.get("number")
        if not isinstance(repository, str) or not isinstance(number, int):
            continue
        detail = facts.get((repository, number))
        if detail is not None:
            payload["review_facts"] = dict(detail)
            if isinstance(detail.get("checks"), list):
                payload["checks"] = list(detail["checks"])
    return snapshot


def _iter_pull_request_payloads(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    worktrees = snapshot.get("worktrees")
    if isinstance(worktrees, list):
        for worktree in worktrees:
            if not isinstance(worktree, dict):
                continue
            pulls = worktree.get("pull_requests")
            if not isinstance(pulls, list):
                continue
            for payload in pulls:
                if isinstance(payload, dict):
                    payloads.append(payload)
    pulls = snapshot.get("pull_requests")
    if isinstance(pulls, list):
        for payload in pulls:
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


def _review_fact_heads(snapshot: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    heads: dict[tuple[str, int], str] = {}
    for payload in _iter_pull_request_payloads(snapshot):
        repository = payload.get("repository")
        number = payload.get("number")
        head = payload.get("head_ref_name")
        if (
            isinstance(repository, str)
            and isinstance(number, int)
            and isinstance(head, str)
            and head
            and (repository, number) not in heads
        ):
            heads[(repository, number)] = head
    return heads
