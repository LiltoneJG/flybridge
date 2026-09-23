from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from flybridge_core import issue_urls_from_text, parse_issue_url

from .inventory import normalize_ref_name


def _join_key(url: str) -> str | None:
    try:
        return parse_issue_url(url).join_key
    except ValueError:
        return None


def _ref_key(repository: str, ref: str) -> str | None:
    if not repository or not ref:
        return None
    head = normalize_ref_name(ref)
    if not head:
        return None
    return f"ref:{repository.lower()}#{head}"


def _urls_from_worktree(row: Mapping[str, Any]) -> list[str]:
    orca = row.get("orca") if isinstance(row.get("orca"), dict) else {}
    hint = orca.get("github_hint") if isinstance(orca.get("github_hint"), dict) else {}
    hint_issues = hint.get("issues") if isinstance(hint.get("issues"), list) else []
    urls: list[str] = []
    for item in hint_issues:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.append(item["url"])
    comment = orca.get("comment") if isinstance(orca.get("comment"), str) else ""
    urls.extend(ref.url for ref in issue_urls_from_text(comment))
    return urls


def _worktree_issue_keys(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for url in _urls_from_worktree(row):
        key = _join_key(url)
        if key is not None:
            keys.add(key)
    return keys


def _worktree_ref_keys(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    git = row.get("git") if isinstance(row.get("git"), dict) else {}
    parent_repo = (
        git.get("github_repository") if isinstance(git.get("github_repository"), str) else ""
    )
    parent_branch = git.get("branch") if isinstance(git.get("branch"), str) else ""
    key = _ref_key(parent_repo, parent_branch)
    if key is not None:
        keys.add(key)
    submodules = git.get("submodules") if isinstance(git.get("submodules"), list) else []
    for item in submodules:
        if not isinstance(item, dict):
            continue
        repository = (
            item.get("github_repository") if isinstance(item.get("github_repository"), str) else ""
        )
        branch = item.get("branch") if isinstance(item.get("branch"), str) else ""
        key = _ref_key(repository, branch)
        if key is not None:
            keys.add(key)
    pulls = row.get("pull_requests") if isinstance(row.get("pull_requests"), list) else []
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        repository = pull.get("repository") if isinstance(pull.get("repository"), str) else ""
        head = pull.get("head_ref_name") if isinstance(pull.get("head_ref_name"), str) else ""
        key = _ref_key(repository, head)
        if key is not None:
            keys.add(key)
    return keys


def _worktree_payload(row: Mapping[str, Any], reasons: Sequence[str]) -> dict[str, Any]:
    orca = row.get("orca") if isinstance(row.get("orca"), dict) else {}
    git = row.get("git") if isinstance(row.get("git"), dict) else {}
    return {
        "path": orca.get("path"),
        "name": orca.get("name"),
        "checkout_repository": git.get("github_repository"),
        "branch": git.get("branch") or orca.get("branch") or "",
        "match": list(reasons),
    }


def _issue_url_keys(issue: Mapping[str, Any]) -> set[str]:
    url = issue.get("url") if isinstance(issue.get("url"), str) else ""
    key = _join_key(url) if url else None
    return {key} if key else set()


def _body_url_keys(development: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    urls = development.get("body_urls") if isinstance(development.get("body_urls"), list) else []
    for url in urls:
        if not isinstance(url, str):
            continue
        key = _join_key(url)
        if key is not None:
            keys.add(key)
    return keys


def _development_pr_keys(development: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    pulls = (
        development.get("pull_requests")
        if isinstance(development.get("pull_requests"), list)
        else []
    )
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        repository = pull.get("repository") if isinstance(pull.get("repository"), str) else ""
        head = pull.get("head_ref_name") if isinstance(pull.get("head_ref_name"), str) else ""
        key = _ref_key(repository, head)
        if key is not None:
            keys.add(key)
    return keys


def _development_branch_keys(development: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    branches = (
        development.get("linked_branches")
        if isinstance(development.get("linked_branches"), list)
        else []
    )
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        repository = branch.get("repository") if isinstance(branch.get("repository"), str) else ""
        name = branch.get("name") if isinstance(branch.get("name"), str) else ""
        key = _ref_key(repository, name)
        if key is not None:
            keys.add(key)
    return keys


def worktree_comment_issue_urls(worktrees: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    urls: list[str] = []
    seen: set[str] = set()
    for row in worktrees:
        for url in _urls_from_worktree(row):
            if _join_key(url) is None or url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return tuple(urls)


def attach_issue_refs(
    issues: Sequence[Mapping[str, Any]],
    worktrees: Sequence[Mapping[str, Any]],
    development: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join board issues to parent worktrees by issue URLs and development refs."""
    prepared: list[tuple[Mapping[str, Any], set[str], set[str]]] = []
    for row in worktrees:
        prepared.append((row, _worktree_issue_keys(row), _worktree_ref_keys(row)))
    attached: list[dict[str, Any]] = []
    matched_paths: set[str] = set()
    for issue in issues:
        row = dict(issue)
        url = row.get("url") if isinstance(row.get("url"), str) else ""
        key = _join_key(url) if url else None
        facts = dict(development.get(key, {})) if key else {}
        issue_keys = _issue_url_keys(row)
        body_keys = _body_url_keys(facts)
        pr_keys = _development_pr_keys(facts)
        branch_keys = _development_branch_keys(facts)
        matches: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for worktree, worktree_issues, worktree_refs in prepared:
            reasons: list[str] = []
            if issue_keys & worktree_issues:
                reasons.append("issue_url")
            if body_keys & worktree_issues:
                reasons.append("body_url")
            if pr_keys & worktree_refs:
                reasons.append("pull_request")
            if branch_keys & worktree_refs:
                reasons.append("linked_branch")
            if not reasons:
                continue
            payload = _worktree_payload(worktree, reasons)
            path = payload.get("path")
            path_key = path if isinstance(path, str) else id(worktree)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            if isinstance(path, str):
                matched_paths.add(path)
            matches.append(payload)
        row["worktrees"] = matches
        row["development"] = facts
        attached.append(row)
    unmatched: list[dict[str, Any]] = []
    for worktree, _issues, _refs in prepared:
        payload = _worktree_payload(worktree, ())
        path = payload.get("path")
        if isinstance(path, str) and path in matched_paths:
            continue
        unmatched.append({key: value for key, value in payload.items() if key != "match"})
    return attached, unmatched
