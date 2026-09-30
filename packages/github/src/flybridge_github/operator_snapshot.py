"""Bounded, read-only GitHub detail queries for the operator snapshot."""

from __future__ import annotations

import json
import threading
from typing import Any

from .cli import GitHubCli, GitHubCliError


_PR_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      url body state isDraft
      assignees(first:100) { pageInfo { hasNextPage } nodes { login } }
      reviews(first:100) { pageInfo { hasNextPage } nodes {
        url state body submittedAt author { login }
      } }
      comments(first:100) { pageInfo { hasNextPage } nodes {
        url body createdAt author { login }
      } }
      reviewThreads(first:100) { pageInfo { hasNextPage } nodes {
        isResolved isOutdated comments(first:100) {
          pageInfo { hasNextPage } nodes { url body author { login } }
        }
      } }
      statusCheckRollup { contexts(first:100) { pageInfo { hasNextPage } nodes {
        __typename
        ... on CheckRun { name status conclusion detailsUrl summary }
        ... on StatusContext { context state targetUrl description }
      } } }
    }
  }
}
"""

_ISSUE_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    issue(number:$number) {
      url title body state
      assignees(first:100) { pageInfo { hasNextPage } nodes { login } }
      comments(first:100) { pageInfo { hasNextPage } nodes {
        url body createdAt author { login }
      } }
      timelineItems(first:100, itemTypes:[CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
        pageInfo { hasNextPage }
        nodes {
          __typename
          ... on CrossReferencedEvent { source {
            __typename ... on PullRequest { number url repository { nameWithOwner } }
          } }
          ... on ConnectedEvent { subject {
            __typename ... on PullRequest { number url repository { nameWithOwner } }
          } }
        }
      }
    }
  }
}
"""

_PROJECT_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    RESOURCE(number:$number) {
      projectItems(first:50) { pageInfo { hasNextPage } nodes {
        project { title url }
      } }
    }
  }
}
"""


class OperatorSnapshotError(RuntimeError):
    """One GitHub detail query failed without invalidating other facts."""


def _nodes(connection: Any, label: str, warnings: list[str], source: str) -> list[dict]:
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        warnings.append(f"{source}: {label} unavailable")
        return []
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
        warnings.append(f"{source}: {label} pagination state unavailable")
    elif page_info["hasNextPage"] is True:
        warnings.append(f"{source}: {label} truncated")
    if any(not isinstance(node, dict) for node in connection["nodes"]):
        warnings.append(f"{source}: {label} contains invalid entries")
    return [node for node in connection["nodes"] if isinstance(node, dict)]


def _login(node: Any) -> str | None:
    return node.get("login") if isinstance(node, dict) else None


class GitHubOperatorFacts:
    def __init__(self, *, user: str, cli: GitHubCli | None = None) -> None:
        self.cli = cli or GitHubCli(user=user)
        self._project_lock = threading.Lock()
        self._project_access_error: str | None = None
        self._auth_error: str | None = None

    def prime_auth(self) -> None:
        try:
            self.cli.token()
        except GitHubCliError as exc:
            self._auth_error = str(exc)

    def _query(self, repository: str, number: int, query: str, kind: str) -> dict:
        if self._auth_error is not None:
            raise OperatorSnapshotError(self._auth_error)
        owner, sep, name = repository.partition("/")
        if not sep or not owner or not name or "/" in name or number < 1:
            raise OperatorSnapshotError(f"invalid GitHub reference: {repository}#{number}")
        for value in (owner, name):
            self.cli.cli_value("repository", value)
        try:
            result = self.cli.run(
                [
                    self.cli.executable, "api", "graphql", "-f", f"query={query}",
                    "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}",
                ],
                timeout=60,
            )
        except GitHubCliError as exc:
            raise OperatorSnapshotError(str(exc)) from exc
        if result.returncode:
            raise OperatorSnapshotError(result.stderr.strip() or "GitHub API failed")
        try:
            payload = json.loads(result.stdout)
            if payload.get("errors"):
                raise OperatorSnapshotError(str(payload["errors"][0].get("message", "GraphQL error")))
            node = payload["data"]["repository"][kind]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise OperatorSnapshotError("invalid GitHub GraphQL response") from exc
        if not isinstance(node, dict):
            raise OperatorSnapshotError(f"GitHub {kind} was not found: {repository}#{number}")
        return node

    def _projects(self, repository: str, number: int, kind: str, warnings: list[str], source: str) -> list[dict]:
        with self._project_lock:
            if self._project_access_error is not None:
                warnings.append(f"{source}: Project facts unavailable: {self._project_access_error}")
                return []
            try:
                node = self._query(repository, number, _PROJECT_QUERY.replace("RESOURCE", kind), kind)
            except OperatorSnapshotError as exc:
                reason = str(exc).splitlines()[0]
                if "required scopes" in reason:
                    self._project_access_error = reason
                warnings.append(f"{source}: Project facts unavailable: {reason}")
                return []
        return [
            item["project"] for item in _nodes(node.get("projectItems"), "projects", warnings, source)
            if isinstance(item.get("project"), dict)
        ]

    def pull_request(self, repository: str, number: int) -> dict:
        node = self._query(repository, number, _PR_QUERY, "pullRequest")
        source = f"https://github.com/{repository}/pull/{number}"
        warnings: list[str] = []
        assignees = [_login(item) for item in _nodes(node.get("assignees"), "assignees", warnings, source)]
        reviews = [
            {"url": item.get("url"), "state": item.get("state"), "body": item.get("body"),
             "author": _login(item.get("author")), "submitted_at": item.get("submittedAt")}
            for item in _nodes(node.get("reviews"), "reviews", warnings, source)
        ]
        comments = [
            {"url": item.get("url"), "body": item.get("body"), "author": _login(item.get("author")),
             "created_at": item.get("createdAt")}
            for item in _nodes(node.get("comments"), "comments", warnings, source)
        ]
        threads = []
        for item in _nodes(node.get("reviewThreads"), "review threads", warnings, source):
            if item.get("isResolved") is True:
                continue
            threads.append({
                "is_outdated": item.get("isOutdated"),
                "comments": [
                    {"url": comment.get("url"), "body": comment.get("body"),
                     "author": _login(comment.get("author"))}
                    for comment in _nodes(item.get("comments"), "thread comments", warnings, source)
                ],
            })
        rollup = node.get("statusCheckRollup")
        checks = _nodes(rollup.get("contexts") if isinstance(rollup, dict) else None,
                        "checks", warnings, source)
        normalized_checks = []
        failed_checks = []
        for check in checks:
            is_run = check.get("__typename") == "CheckRun"
            normalized = {
                "name": check.get("name") if is_run else check.get("context"),
                "status": check.get("status") if is_run else check.get("state"),
                "conclusion": check.get("conclusion") if is_run else None,
                "url": check.get("detailsUrl") if is_run else check.get("targetUrl"),
                "summary": check.get("summary") if is_run else check.get("description"),
            }
            normalized_checks.append(normalized)
            if str(normalized["conclusion"] or normalized["status"]).upper() in {
                "FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED"
            }:
                failed_checks.append(normalized)
                if not normalized["summary"] or not normalized["url"]:
                    warnings.append(
                        f"{source}: failed check summary or URL unavailable: {normalized['name']}"
                    )
        projects = self._projects(repository, number, "pullRequest", warnings, source)
        return {
            "repository": repository, "number": number, "url": node.get("url") or source,
            "body": node.get("body"), "state": node.get("state"), "is_draft": node.get("isDraft"),
            "assignees": [login for login in assignees if login], "projects": projects,
            "reviews": reviews, "comments": comments, "unresolved_threads": threads,
            "checks": normalized_checks, "failed_checks": failed_checks,
            "warnings": warnings, "provenance": {"source": source, "api": "GitHub GraphQL"},
        }

    def issue(self, repository: str, number: int) -> dict:
        node = self._query(repository, number, _ISSUE_QUERY, "issue")
        source = f"https://github.com/{repository}/issues/{number}"
        warnings: list[str] = []
        assignees = [_login(item) for item in _nodes(node.get("assignees"), "assignees", warnings, source)]
        comments = [
            {"url": item.get("url"), "body": item.get("body"), "author": _login(item.get("author")),
             "created_at": item.get("createdAt")}
            for item in _nodes(node.get("comments"), "comments", warnings, source)
        ]
        projects = self._projects(repository, number, "issue", warnings, source)
        linked = []
        for item in _nodes(node.get("timelineItems"), "issue links", warnings, source):
            pr = item.get("source") or item.get("subject")
            if isinstance(pr, dict) and pr.get("__typename") == "PullRequest":
                repo = pr.get("repository") or {}
                if isinstance(repo, dict) and isinstance(repo.get("nameWithOwner"), str):
                    linked.append({"repository": repo["nameWithOwner"], "number": pr.get("number"),
                                   "url": pr.get("url")})
        return {
            "repository": repository, "number": number, "url": node.get("url") or source,
            "title": node.get("title"), "body": node.get("body"), "state": node.get("state"),
            "assignees": [login for login in assignees if login], "comments": comments,
            "projects": projects, "linked_pull_requests": linked, "warnings": warnings,
            "provenance": {"source": source, "api": "GitHub GraphQL"},
        }
