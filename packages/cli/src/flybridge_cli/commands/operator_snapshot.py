"""One read-only view of worktrees and their related GitHub evidence."""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from flybridge_core import issue_urls_from_text, parse_issue_url
from flybridge_github.operator_snapshot import GitHubOperatorFacts

from ..runtime import _config
from .inventory import build_inventory

_MAX_WORKERS = 4
_LOCAL_ISSUE = re.compile(
    r"(?i)\b(?:closes?|closed|fixes?|fixed|resolves?|resolved|related(?:\s+to)?)\s*:?\s*#([1-9][0-9]*)\b"
)


def _key(repository: str, number: int) -> tuple[str, int]:
    return repository, number


def _pr_issue_urls(pr: dict) -> list[str]:
    body = pr.get("body") or ""
    comments = [item.get("body") or "" for item in pr.get("comments") or []]
    urls: list[str] = []
    for text in (body, *comments):
        urls.extend(ref.url for ref in issue_urls_from_text(text))
    urls.extend(
        f"https://github.com/{pr['repository']}/issues/{match.group(1)}"
        for match in _LOCAL_ISSUE.finditer(body)
    )
    return list(dict.fromkeys(urls))


def _collect(
    keys: set[tuple[str, int]],
    fetch,
    kind: str,
) -> tuple[dict[tuple[str, int], dict], list[str]]:
    ordered = sorted(keys, key=lambda item: (item[0].casefold(), item[1]))
    results: dict[tuple[str, int], dict] = {}
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(fetch, *key): key for key in ordered}
        completed: dict[tuple[str, int], tuple[dict | None, str | None]] = {}
        for future in as_completed(futures):
            key = futures[future]
            try:
                completed[key] = future.result(), None
            except Exception as exc:  # each remote failure stays local to its reference
                completed[key] = None, str(exc)
    for key in ordered:
        value, error = completed[key]
        if value is None:
            path = "pull" if kind == "pull request" else "issues"
            source = f"https://github.com/{key[0]}/{path}/{key[1]}"
            warnings.append(f"{kind} {key[0]}#{key[1]} unavailable ({source}): {error}")
        else:
            results[key] = value
            warnings.extend(value.get("warnings") or [])
    return results, warnings


def enrich_snapshot(
    inventory: dict[str, Any],
    facts: GitHubOperatorFacts,
    *,
    assignee: str | None,
) -> dict[str, Any]:
    """Fetch each related issue or PR once, retaining input and result order."""
    rows = inventory.get("worktrees") or []
    pr_keys: set[tuple[str, int]] = set()
    issue_keys: set[tuple[str, int]] = set()
    for row in rows:
        pr_urls: list[str] = []
        for pr in row.get("pull_requests") or []:
            repository, number = pr.get("repository"), pr.get("number")
            if isinstance(repository, str) and isinstance(number, int) and number > 0:
                pr_keys.add(_key(repository, number))
                pr_urls.append(f"https://github.com/{repository}/pull/{number}")
        hint = (row.get("orca") or {}).get("github_hint") or {}
        issue_urls: list[str] = []
        for entry in hint.get("issues") or []:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                issue_urls.append(entry["url"])
        comment = (row.get("orca") or {}).get("comment") or ""
        issue_urls.extend(ref.url for ref in issue_urls_from_text(comment))
        row["related_pull_request_urls"] = list(dict.fromkeys(pr_urls))
        row["related_issue_urls"] = list(dict.fromkeys(issue_urls))
        for url in row["related_issue_urls"]:
            try:
                ref = parse_issue_url(url)
            except ValueError:
                inventory.setdefault("warnings", []).append(f"invalid issue hint: {url}")
                continue
            issue_keys.add(_key(ref.repository, ref.number))

    prs, pr_warnings = _collect(pr_keys, facts.pull_request, "pull request")
    for pr in prs.values():
        for url in _pr_issue_urls(pr):
            ref = parse_issue_url(url)
            issue_keys.add(_key(ref.repository, ref.number))
    issues, issue_warnings = _collect(issue_keys, facts.issue, "issue")
    linked_keys: set[tuple[str, int]] = set()
    for issue in issues.values():
        for link in issue.get("linked_pull_requests") or []:
            repository, number = link.get("repository"), link.get("number")
            if isinstance(repository, str) and isinstance(number, int) and number > 0:
                linked_keys.add(_key(repository, number))
    extra, extra_warnings = _collect(linked_keys - pr_keys, facts.pull_request, "pull request")
    prs.update(extra)

    for row in rows:
        issue_urls = set(row["related_issue_urls"])
        pr_urls = set(row["related_pull_request_urls"])
        for pr in prs.values():
            if pr["url"] in pr_urls:
                issue_urls.update(_pr_issue_urls(pr))
        for issue in issues.values():
            if issue["url"] in issue_urls:
                pr_urls.update(
                    link["url"] for link in issue.get("linked_pull_requests") or []
                    if isinstance(link.get("url"), str)
                )
        row["related_issue_urls"] = sorted(issue_urls)
        row["related_pull_request_urls"] = sorted(pr_urls)

    for collection in (issues, prs):
        for item in collection.values():
            item["matches_assignee"] = assignee is None or assignee.casefold() in {
                login.casefold() for login in item.get("assignees") or [] if isinstance(login, str)
            }
    # Retain all related facts for provenance. `matches_assignee` identifies the focus set.
    focused_issues = sorted(item["url"] for item in issues.values() if item["matches_assignee"])
    focused_prs = sorted(item["url"] for item in prs.values() if item["matches_assignee"])
    focused_urls = set((*focused_issues, *focused_prs))
    focused_worktrees = [
        row["orca"]["path"] for row in rows
        if assignee is None or focused_urls.intersection(
            (*row["related_issue_urls"], *row["related_pull_request_urls"])
        )
    ]
    return {
        "schema_version": 1,
        "generated_at": inventory.get("generated_at"),
        "assignee": assignee,
        "focused_refs": {"issues": focused_issues, "pull_requests": focused_prs},
        "focused_worktrees": focused_worktrees,
        "worktrees": rows,
        "issues": [issues[key] for key in sorted(issues)],
        "pull_requests": [prs[key] for key in sorted(prs)],
        "warnings": [*(inventory.get("warnings") or []), *pr_warnings, *issue_warnings,
                     *extra_warnings],
        "failures": inventory.get("failures") or [],
        "provenance": {
            "worktrees": "Orca worktree ps and local git",
            "pull_request_matches": "GitHub branch head and Orca PR hint",
            "details": "GitHub GraphQL; collection capped at four concurrent requests",
        },
    }


def handle_snapshot(args: argparse.Namespace) -> int:
    if args.assignee and args.all_assignees:
        raise ValueError("pass only one of --assignee and --all-assignees")
    config = _config(args)
    if not config.github.enabled:
        raise ValueError("operator snapshot requires enabled GitHub integration")
    assignee = None if args.all_assignees else (args.assignee or config.github.login)
    inventory, failed = build_inventory(args, config)
    facts = GitHubOperatorFacts(user=config.github.login)
    facts.prime_auth()
    snapshot = enrich_snapshot(inventory, facts, assignee=assignee)
    print(json.dumps(snapshot, indent=2))
    return 2 if failed or snapshot["failures"] or snapshot["warnings"] else 0
