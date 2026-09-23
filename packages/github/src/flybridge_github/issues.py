from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from flybridge_core import parse_issue_url, resource_urls_from_text

from .cli import GitHubCli, GitHubCliError
from .pull_requests import GitHubPullRequestError

_UNSAFE_GH_MARKERS = ("{owner}", "{repo}", "{branch}")


@dataclass(frozen=True)
class LinkedBranch:
    name: str
    repository: str | None


@dataclass(frozen=True)
class ConnectedPullRequest:
    repository: str
    number: int
    title: str
    url: str
    state: str
    head_ref_name: str | None


@dataclass(frozen=True)
class IssueDevelopment:
    issue_url: str
    linked_branches: tuple[LinkedBranch, ...]
    pull_requests: tuple[ConnectedPullRequest, ...]
    body_urls: tuple[str, ...]

    @property
    def join_key(self) -> str:
        return parse_issue_url(self.issue_url).join_key

    def as_dict(self) -> dict[str, object]:
        return {
            "linked_branches": [
                {"name": branch.name, "repository": branch.repository}
                for branch in self.linked_branches
            ],
            "pull_requests": [
                {
                    "repository": pull.repository,
                    "number": pull.number,
                    "title": pull.title,
                    "url": pull.url,
                    "state": pull.state,
                    "head_ref_name": pull.head_ref_name,
                }
                for pull in self.pull_requests
            ],
            "body_urls": list(self.body_urls),
        }


class GitHubIssueDevelopment:
    """Read-only GitHub issue development facts; no worktree matching."""

    def __init__(
        self,
        executable: str = "gh",
        *,
        user: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.cli = GitHubCli(executable, user=user, runner=runner)
        self.executable = self.cli.executable
        self.runner = self.cli.runner

    @staticmethod
    def _cli_value(field: str, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise GitHubPullRequestError(f"GitHub {field} is invalid")
        if value.startswith("@") or any(marker in value for marker in _UNSAFE_GH_MARKERS):
            raise GitHubPullRequestError(f"GitHub {field} contains an unsafe gh expansion")
        return value

    _BATCH_SIZE = 20
    _ISSUE_FIELDS = """
              url
              body
              linkedBranches(first:20) {
                nodes { ref { name repository { nameWithOwner } } }
              }
              timelineItems(first:80, itemTypes:[CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
                nodes {
                  __typename
                  ... on CrossReferencedEvent {
                    source {
                      __typename
                      ... on PullRequest {
                        number title url state headRefName
                        repository { nameWithOwner }
                      }
                    }
                  }
                  ... on ConnectedEvent {
                    subject {
                      __typename
                      ... on PullRequest {
                        number title url state headRefName
                        repository { nameWithOwner }
                      }
                    }
                  }
                }
              }
    """

    def fetch_many(self, issue_urls: Sequence[str]) -> dict[str, IssueDevelopment]:
        unique: list[str] = []
        seen: set[str] = set()
        for raw in issue_urls:
            try:
                ref = parse_issue_url(raw)
            except ValueError:
                continue
            if ref.join_key in seen:
                continue
            seen.add(ref.join_key)
            unique.append(ref.url)
        collected: dict[str, IssueDevelopment] = {}
        for start in range(0, len(unique), self._BATCH_SIZE):
            collected.update(self._fetch_batch(unique[start : start + self._BATCH_SIZE]))
        return collected

    def fetch(self, issue_url: str) -> IssueDevelopment:
        ref = parse_issue_url(issue_url)
        fetched = self._fetch_batch([ref.url])
        development = fetched.get(ref.join_key)
        if development is None:
            raise GitHubPullRequestError(f"GitHub issue was not found: {ref.url}")
        return development

    def _fetch_batch(self, issue_urls: Sequence[str]) -> dict[str, IssueDevelopment]:
        if not issue_urls:
            return {}
        declarations: list[str] = []
        selections: list[str] = []
        arguments = [self.executable, "api", "graphql"]
        for index, issue_url in enumerate(issue_urls):
            ref = parse_issue_url(issue_url)
            owner, _, name = ref.repository.partition("/")
            owner = self._cli_value("owner", owner)
            name = self._cli_value("repository name", name)
            declarations.append(f"$o{index}:String!, $n{index}:String!, $num{index}:Int!")
            selections.append(
                f"i{index}: repository(owner:$o{index}, name:$n{index}) "
                f"{{ issue(number:$num{index}) {{ {self._ISSUE_FIELDS} }} }}"
            )
            arguments.extend(
                [
                    "-F",
                    f"o{index}={owner}",
                    "-F",
                    f"n{index}={name}",
                    "-F",
                    f"num{index}={ref.number}",
                ]
            )
        query = "query(" + ", ".join(declarations) + ") { " + " ".join(selections) + " }"
        arguments[3:3] = ["-f", f"query={query}"]
        try:
            result = self.cli.run(arguments, timeout=120)
        except GitHubCliError as exc:
            raise GitHubPullRequestError(str(exc)) from exc
        if result.returncode:
            raise GitHubPullRequestError(result.stderr.strip() or "GitHub CLI failed")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubPullRequestError("unexpected GitHub issue response") from exc
        if not isinstance(response, dict):
            raise GitHubPullRequestError("unexpected GitHub issue response")
        data = response.get("data")
        if not isinstance(data, dict):
            errors = response.get("errors")
            detail = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = errors[0].get("message")
                detail = f": {message}" if isinstance(message, str) and message else ""
            raise GitHubPullRequestError(f"GitHub issue GraphQL query failed{detail}")
        collected: dict[str, IssueDevelopment] = {}
        for index, issue_url in enumerate(issue_urls):
            repository_node = data.get(f"i{index}")
            if not isinstance(repository_node, dict):
                continue
            issue = repository_node.get("issue")
            if not isinstance(issue, dict):
                continue
            development = self._development_from_issue(issue_url, issue)
            collected[development.join_key] = development
        if collected:
            return collected
        errors = response.get("errors")
        if errors:
            detail = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = errors[0].get("message")
                detail = f": {message}" if isinstance(message, str) and message else ""
            raise GitHubPullRequestError(f"GitHub issue GraphQL query failed{detail}")
        return {}

    def _development_from_issue(self, issue_url: str, issue: dict) -> IssueDevelopment:
        ref = parse_issue_url(issue_url)
        body = issue.get("body") if isinstance(issue.get("body"), str) else ""
        branches: list[LinkedBranch] = []
        for node in (issue.get("linkedBranches") or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            git_ref = node.get("ref") or {}
            if not isinstance(git_ref, dict):
                continue
            branch_name = git_ref.get("name")
            if not isinstance(branch_name, str) or not branch_name:
                continue
            repository = None
            repo_node = git_ref.get("repository")
            if isinstance(repo_node, dict) and isinstance(repo_node.get("nameWithOwner"), str):
                repository = repo_node["nameWithOwner"]
            branches.append(LinkedBranch(branch_name, repository))
        pulls: list[ConnectedPullRequest] = []
        seen_pulls: set[tuple[str, int]] = set()
        for node in (issue.get("timelineItems") or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            source = node.get("source") or node.get("subject")
            if not isinstance(source, dict) or source.get("__typename") != "PullRequest":
                continue
            number = source.get("number")
            title = source.get("title") if isinstance(source.get("title"), str) else ""
            url = source.get("url") if isinstance(source.get("url"), str) else ""
            state = source.get("state") if isinstance(source.get("state"), str) else ""
            head = source.get("headRefName")
            head_ref = head if isinstance(head, str) else None
            repo_node = source.get("repository")
            repository = (
                repo_node.get("nameWithOwner")
                if isinstance(repo_node, dict) and isinstance(repo_node.get("nameWithOwner"), str)
                else ref.repository
            )
            if not isinstance(number, int) or isinstance(number, bool) or not url:
                continue
            key = (repository, number)
            if key in seen_pulls:
                continue
            seen_pulls.add(key)
            pulls.append(ConnectedPullRequest(repository, number, title, url, state, head_ref))
        resolved_url = issue.get("url") if isinstance(issue.get("url"), str) else ref.url
        return IssueDevelopment(
            resolved_url,
            tuple(branches),
            tuple(pulls),
            resource_urls_from_text(body),
        )
