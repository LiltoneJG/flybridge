from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .cli import GitHubCli, GitHubCliError

_UNSAFE_GH_MARKERS = ("{owner}", "{repo}", "{branch}")
_PR_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
_TRANSIENT_GITHUB_TOKENS = ("HTTP 502", "HTTP 503", "HTTP 504")


class GitHubPullRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestCheck:
    name: str
    status: str
    conclusion: str | None
    is_required: bool | None = None


@dataclass(frozen=True)
class PullRequestFact:
    repository: str
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    mergeable: str | None
    merge_state_status: str | None
    review_decision: str | None
    head_ref_name: str
    assignees: tuple[str, ...]
    checks: tuple[PullRequestCheck, ...]
    unresolved_review_threads: int
    issue_comment_count: int
    base_ref_name: str | None = None
    author: str | None = None
    base_ref_oid: str | None = None
    base_ref_tip_oid: str | None = None
    base_ref_stale: bool | None = None


@dataclass(frozen=True)
class PullRequestQueryFailure:
    repository: str
    message: str


class GitHubPullRequests:
    """Pull-request facts keyed by repository; inventory commands stay read-only."""

    _PAGE_SIZE = 100
    _MAX_PAGES = 100
    _HEAD_PAGE_SIZE = 5
    _NODE_FIELDS = """
                number
                title
                url
                state
                isDraft
                mergeable
                mergeStateStatus
                reviewDecision
                headRefName
                baseRefName
                baseRefOid
                baseRef {
                  target { oid }
                }
                author { login }
                assignees(first:20) { nodes { login } }
                comments { totalCount }
                reviewThreads(first:100) { nodes { isResolved } }
                statusCheckRollup {
                  state
                  contexts(first:100) {
                    nodes {
                      __typename
                      ... on CheckRun { name status conclusion }
                      ... on StatusContext { context state }
                    }
                  }
                }
    """

    def __init__(
        self,
        executable: str = "gh",
        *,
        user: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        cli: GitHubCli | None = None,
    ) -> None:
        self.cli = cli or GitHubCli(executable, user=user, runner=runner)
        self.executable = self.cli.executable
        self.runner = self.cli.runner

    @staticmethod
    def _cli_value(field: str, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise GitHubPullRequestError(f"GitHub {field} is invalid")
        if value.startswith("@") or any(marker in value for marker in _UNSAFE_GH_MARKERS):
            raise GitHubPullRequestError(f"GitHub {field} contains an unsafe gh expansion")
        return value

    def list_open(
        self, repositories: Sequence[str]
    ) -> tuple[dict[str, tuple[PullRequestFact, ...]], tuple[PullRequestQueryFailure, ...]]:
        unique: list[str] = []
        seen: set[str] = set()
        for repository in repositories:
            name = self._cli_value("repository", repository)
            if name in seen:
                continue
            seen.add(name)
            unique.append(name)
        facts: dict[str, tuple[PullRequestFact, ...]] = {}
        failures: list[PullRequestQueryFailure] = []
        for repository in unique:
            try:
                facts[repository] = self._list_repository_with_retry(repository)
            except (GitHubPullRequestError, GitHubCliError) as exc:
                failures.append(PullRequestQueryFailure(repository, str(exc)))
        return facts, tuple(failures)

    def list_authored(
        self,
        author: str,
        states: Sequence[str] = ("OPEN",),
    ) -> tuple[PullRequestFact, ...]:
        login = self._cli_value("author", author)
        query_text = self._authored_search_query(login, states)
        search_query = f"""
        query($q:String!, $cursor:String) {{
          search(query:$q, type:ISSUE, first:{self._PAGE_SIZE}, after:$cursor) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{
              __typename
              ... on PullRequest {{
                repository {{ nameWithOwner }}
{self._NODE_FIELDS}
              }}
            }}
          }}
        }}
        """
        facts: list[PullRequestFact] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(self._MAX_PAGES):
            arguments = [
                self.executable,
                "api",
                "graphql",
                "-f",
                f"query={search_query}",
                "-f",
                f"q={query_text}",
            ]
            if cursor:
                arguments.extend(["-F", f"cursor={self._cli_value('pagination cursor', cursor)}"])
            payload = self._graphql(arguments)
            try:
                search = payload["data"]["search"]
                nodes = search["nodes"]
                page_info = search["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise GitHubPullRequestError(
                    "unexpected GitHub pull request search response"
                ) from exc
            if (
                not isinstance(nodes, list)
                or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)
            ):
                raise GitHubPullRequestError("unexpected GitHub pull request search response")
            for node in nodes:
                if not isinstance(node, dict) or node.get("__typename") != "PullRequest":
                    continue
                repository = self._node_repository(node)
                facts.append(self._fact(repository, node))
            if not page_info.get("hasNextPage"):
                return tuple(facts)
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubPullRequestError(
                    "GitHub pull request search pagination cursor is missing"
                )
            if cursor in seen_cursors:
                raise GitHubPullRequestError(
                    "GitHub pull request search pagination cursor did not advance"
                )
            seen_cursors.add(cursor)
        raise GitHubPullRequestError(
            "GitHub pull request search pagination exceeded the safety limit"
        )

    def _authored_search_query(self, author: str, states: Sequence[str]) -> str:
        normalized: list[str] = []
        seen: set[str] = set()
        for state in states:
            value = self._cli_value("state", state).upper()
            if value not in _PR_STATES:
                raise GitHubPullRequestError(f"GitHub pull request state is invalid: {state}")
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        if not normalized:
            normalized = ["OPEN"]
        parts = [f"author:{author}", "is:pr"]
        if set(normalized) == _PR_STATES:
            return " ".join(parts)
        qualifiers: list[str] = []
        for state in normalized:
            if state == "OPEN":
                qualifiers.append("is:open")
            elif state == "MERGED":
                qualifiers.append("is:merged")
            else:
                qualifiers.append("is:closed is:unmerged")
        if len(qualifiers) == 1:
            parts.append(qualifiers[0])
        else:
            parts.append("(" + " OR ".join(qualifiers) + ")")
        return " ".join(parts)

    @staticmethod
    def _node_repository(node: Mapping[str, Any] | dict[str, Any]) -> str:
        repository_node = node.get("repository")
        if not isinstance(repository_node, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request repository")
        name = repository_node.get("nameWithOwner")
        if not isinstance(name, str) or not name:
            raise GitHubPullRequestError("unexpected GitHub pull request repository")
        return name

    def list_by_head(
        self, repository: str, head_ref_name: str
    ) -> tuple[tuple[PullRequestFact, ...], PullRequestQueryFailure | None]:
        try:
            owner, name = self._split_repository(repository)
            head = self._cli_value("head ref", head_ref_name)
        except GitHubPullRequestError as exc:
            return (), PullRequestQueryFailure(repository, str(exc))
        query = f"""
        query($owner:String!, $name:String!, $head:String!) {{
          repository(owner:$owner, name:$name) {{
            pullRequests(
              first:{self._HEAD_PAGE_SIZE},
              states:[OPEN, MERGED, CLOSED],
              headRefName:$head,
              orderBy:{{field:UPDATED_AT, direction:DESC}}
            ) {{
              nodes {{
{self._NODE_FIELDS}
              }}
            }}
          }}
        }}
        """
        try:
            payload = self._graphql(
                [
                    self.executable,
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"head={head}",
                ]
            )
            repository_node = payload["data"]["repository"]
            if repository_node is None:
                raise GitHubPullRequestError(f"GitHub repository was not found: {repository}")
            nodes = repository_node["pullRequests"]["nodes"]
        except GitHubPullRequestError as exc:
            return (), PullRequestQueryFailure(repository, str(exc))
        except GitHubCliError as exc:
            return (), PullRequestQueryFailure(repository, str(exc))
        except (KeyError, TypeError):
            return (), PullRequestQueryFailure(
                repository, "unexpected GitHub pull request response"
            )
        if not isinstance(nodes, list):
            return (), PullRequestQueryFailure(
                repository, "unexpected GitHub pull request response"
            )
        try:
            return tuple(self._fact(repository, node) for node in nodes), None
        except GitHubPullRequestError as exc:
            return (), PullRequestQueryFailure(repository, str(exc))

    def get(
        self, repository: str, number: int
    ) -> tuple[PullRequestFact | None, PullRequestQueryFailure | None]:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            return None, PullRequestQueryFailure(
                repository, "GitHub pull request number is invalid"
            )
        try:
            owner, name = self._split_repository(repository)
        except GitHubPullRequestError as exc:
            return None, PullRequestQueryFailure(repository, str(exc))
        query = f"""
        query($owner:String!, $name:String!, $number:Int!) {{
          repository(owner:$owner, name:$name) {{
            pullRequest(number:$number) {{
{self._NODE_FIELDS}
            }}
          }}
        }}
        """
        try:
            payload = self._graphql(
                [
                    self.executable,
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"number={number}",
                ]
            )
            repository_node = payload["data"]["repository"]
            if repository_node is None:
                raise GitHubPullRequestError(f"GitHub repository was not found: {repository}")
            node = repository_node["pullRequest"]
        except (GitHubPullRequestError, KeyError, TypeError) as exc:
            message = (
                str(exc)
                if isinstance(exc, GitHubPullRequestError)
                else "unexpected GitHub pull request response"
            )
            return None, PullRequestQueryFailure(repository, message)
        if node is None:
            return None, None
        try:
            return self._fact(repository, node), None
        except GitHubPullRequestError as exc:
            return None, PullRequestQueryFailure(repository, str(exc))

    def get_review_facts(
        self,
        repository: str,
        number: int,
        *,
        head_ref_name: str | None = None,
    ) -> tuple[dict[str, Any] | None, PullRequestQueryFailure | None]:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            return None, PullRequestQueryFailure(
                repository, "GitHub pull request number is invalid"
            )
        try:
            owner, name = self._split_repository(repository)
        except GitHubPullRequestError as exc:
            return None, PullRequestQueryFailure(repository, str(exc))
        compare_head = None
        if isinstance(head_ref_name, str) and head_ref_name.strip():
            try:
                compare_head = self._cli_value("head ref", head_ref_name.strip())
            except GitHubPullRequestError as exc:
                return None, PullRequestQueryFailure(repository, str(exc))
        compare_fields = ""
        if compare_head:
            compare_fields = """
              baseRef {
                compare(headRef: $head) {
                  behindBy
                }
              }
            """
            head_variable = ", $head:String!"
        else:
            head_variable = ""
        query = f"""
        query($owner:String!, $name:String!, $number:Int!{head_variable}) {{
          repository(owner:$owner, name:$name) {{
            pullRequest(number:$number) {{
              body
              baseRefName
              headRefName
{compare_fields}
              statusCheckRollup {{
                contexts(first:100) {{
                  nodes {{
                    __typename
                    ... on CheckRun {{ name status conclusion }}
                    ... on StatusContext {{ context state }}
                  }}
                }}
              }}
              reviews(first:100) {{
                pageInfo {{ hasNextPage }}
                nodes {{
                  author {{ __typename login resourcePath }}
                  authorAssociation
                  state
                  submittedAt
                  body
                }}
              }}
              comments(first:100) {{
                pageInfo {{ hasNextPage }}
                nodes {{
                  author {{ __typename login resourcePath }}
                  authorAssociation
                  createdAt
                  body
                }}
              }}
              reviewThreads(first:100) {{
                pageInfo {{ hasNextPage }}
                nodes {{
                  isResolved
                  isOutdated
                  resolvedBy {{ login }}
                  comments(first:30) {{
                    pageInfo {{ hasNextPage }}
                    nodes {{
                      author {{ __typename login resourcePath }}
                      authorAssociation
                      createdAt
                      body
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        arguments = [
            self.executable,
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if compare_head:
            arguments.extend(["-F", f"head={compare_head}"])
        try:
            payload = self._graphql(arguments)
            repository_node = payload["data"]["repository"]
            if repository_node is None:
                raise GitHubPullRequestError(f"GitHub repository was not found: {repository}")
            node = repository_node["pullRequest"]
        except (GitHubPullRequestError, KeyError, TypeError) as exc:
            message = (
                str(exc)
                if isinstance(exc, GitHubPullRequestError)
                else "unexpected GitHub pull request response"
            )
            return None, PullRequestQueryFailure(repository, message)
        if node is None:
            return None, None
        try:
            return self._review_facts(node), None
        except GitHubPullRequestError as exc:
            return None, PullRequestQueryFailure(repository, str(exc))

    def refresh_base(self, repository: str, number: int) -> dict[str, object]:
        """Ask GitHub to recompute the pull request base onto the current branch tip."""
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubPullRequestError("GitHub pull request number is invalid")
        fact, failure = self.get(repository, number)
        if failure is not None:
            raise GitHubPullRequestError(failure.message)
        if fact is None:
            raise GitHubPullRequestError(
                f"GitHub pull request was not found: {repository}#{number}"
            )
        if not fact.base_ref_name:
            raise GitHubPullRequestError("GitHub pull request base ref is missing")
        owner, name = self._split_repository(repository)
        base = self._cli_value("base ref", fact.base_ref_name)
        arguments = [
            self.executable,
            "api",
            "-X",
            "PATCH",
            f"repos/{owner}/{name}/pulls/{number}",
            "-f",
            f"base={base}",
        ]
        try:
            result = self.cli.run(arguments, timeout=60)
        except GitHubCliError as exc:
            raise GitHubPullRequestError(str(exc)) from exc
        if result.returncode:
            raise GitHubPullRequestError(result.stderr.strip() or "GitHub CLI failed")
        return {
            "repository": repository,
            "number": number,
            "base_ref_name": fact.base_ref_name,
            "refreshed": True,
        }

    @staticmethod
    def is_transient_message(message: str) -> bool:
        return any(token in message for token in _TRANSIENT_GITHUB_TOKENS)

    def _list_repository_with_retry(self, repository: str) -> tuple[PullRequestFact, ...]:
        try:
            return self._list_repository(repository)
        except (GitHubPullRequestError, GitHubCliError) as exc:
            if not self.is_transient_message(str(exc)):
                raise
            return self._list_repository(repository)

    def _split_repository(self, repository: str) -> tuple[str, str]:
        name = self._cli_value("repository", repository)
        owner, _, repo = name.partition("/")
        if not owner or not repo or "/" in repo:
            raise GitHubPullRequestError(f"GitHub repository is invalid: {repository}")
        return self._cli_value("owner", owner), self._cli_value("repository name", repo)

    def _list_repository(self, repository: str) -> tuple[PullRequestFact, ...]:
        owner, name = self._split_repository(repository)
        query = f"""
        query($owner:String!, $name:String!, $cursor:String) {{
          repository(owner:$owner, name:$name) {{
            pullRequests(
              first:{self._PAGE_SIZE},
              after:$cursor,
              states:OPEN,
              orderBy:{{field:UPDATED_AT, direction:DESC}}
            ) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{
{self._NODE_FIELDS}
              }}
            }}
          }}
        }}
        """
        facts: list[PullRequestFact] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(self._MAX_PAGES):
            arguments = [
                self.executable,
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
            ]
            if cursor:
                arguments.extend(["-F", f"cursor={self._cli_value('pagination cursor', cursor)}"])
            payload = self._graphql(arguments)
            try:
                repository_node = payload["data"]["repository"]
                if repository_node is None:
                    raise GitHubPullRequestError(f"GitHub repository was not found: {repository}")
                items = repository_node["pullRequests"]
                nodes = items["nodes"]
                page_info = items["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise GitHubPullRequestError("unexpected GitHub pull request response") from exc
            if (
                not isinstance(nodes, list)
                or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)
            ):
                raise GitHubPullRequestError("unexpected GitHub pull request response")
            for node in nodes:
                facts.append(self._fact(repository, node))
            if not page_info.get("hasNextPage"):
                return tuple(facts)
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubPullRequestError("GitHub pull request pagination cursor is missing")
            if cursor in seen_cursors:
                raise GitHubPullRequestError(
                    "GitHub pull request pagination cursor did not advance"
                )
            seen_cursors.add(cursor)
        raise GitHubPullRequestError("GitHub pull request pagination exceeded the safety limit")

    def _graphql(self, arguments: list[str]) -> dict[str, object]:
        try:
            result = self.cli.run(arguments, timeout=60)
        except GitHubCliError as exc:
            raise GitHubPullRequestError(str(exc)) from exc
        if result.returncode:
            raise GitHubPullRequestError(result.stderr.strip() or "GitHub CLI failed")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubPullRequestError("unexpected GitHub pull request response") from exc
        if not isinstance(response, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request response")
        errors = response.get("errors")
        if errors:
            detail = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = errors[0].get("message")
                detail = f": {message}" if isinstance(message, str) and message else ""
            raise GitHubPullRequestError(f"GitHub pull request GraphQL query failed{detail}")
        return response

    @staticmethod
    def _fact(repository: str, node: object) -> PullRequestFact:
        if not isinstance(node, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request")
        try:
            number = node["number"]
            title = node["title"]
            url = node["url"]
            state = node["state"]
            is_draft = node["isDraft"]
            head_ref_name = node["headRefName"]
        except KeyError as exc:
            raise GitHubPullRequestError("unexpected GitHub pull request") from exc
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(title, str)
            or not isinstance(url, str)
            or not isinstance(state, str)
            or not isinstance(is_draft, bool)
            or not isinstance(head_ref_name, str)
        ):
            raise GitHubPullRequestError("unexpected GitHub pull request")
        mergeable = node.get("mergeable")
        merge_state_status = node.get("mergeStateStatus")
        review_decision = node.get("reviewDecision")
        if mergeable is not None and not isinstance(mergeable, str):
            raise GitHubPullRequestError("unexpected GitHub pull request mergeable value")
        if merge_state_status is not None and not isinstance(merge_state_status, str):
            raise GitHubPullRequestError("unexpected GitHub pull request merge state")
        if review_decision is not None and not isinstance(review_decision, str):
            raise GitHubPullRequestError("unexpected GitHub pull request review decision")
        base_ref_name = node.get("baseRefName")
        if base_ref_name is not None and not isinstance(base_ref_name, str):
            raise GitHubPullRequestError("unexpected GitHub pull request base ref")
        base_ref_oid = GitHubPullRequests._optional_oid(node.get("baseRefOid"), "base oid")
        base_ref_tip_oid = GitHubPullRequests._base_ref_tip_oid(node.get("baseRef"))
        base_ref_stale = (
            None
            if base_ref_oid is None or base_ref_tip_oid is None
            else base_ref_oid != base_ref_tip_oid
        )
        author = GitHubPullRequests._author_login(node.get("author"))
        return PullRequestFact(
            repository,
            number,
            title,
            url,
            state,
            is_draft,
            mergeable,
            merge_state_status,
            review_decision,
            head_ref_name,
            GitHubPullRequests._assignees(node.get("assignees")),
            GitHubPullRequests._checks(node.get("statusCheckRollup")),
            GitHubPullRequests._unresolved_threads(node.get("reviewThreads")),
            GitHubPullRequests._comment_count(node.get("comments")),
            base_ref_name,
            author,
            base_ref_oid,
            base_ref_tip_oid,
            base_ref_stale,
        )

    @staticmethod
    def _optional_oid(value: object, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise GitHubPullRequestError(f"unexpected GitHub pull request {field}")
        return value

    @staticmethod
    def _base_ref_tip_oid(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request base ref target")
        target = value.get("target")
        if target is None:
            return None
        if not isinstance(target, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request base ref target")
        return GitHubPullRequests._optional_oid(target.get("oid"), "base tip oid")

    @staticmethod
    def _author_login(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request author")
        login = value.get("login")
        if login is None:
            return None
        if not isinstance(login, str) or not login:
            raise GitHubPullRequestError("unexpected GitHub pull request author")
        return login

    @staticmethod
    def _assignees(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request assignees")
        nodes = value.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubPullRequestError("unexpected GitHub pull request assignees")
        logins: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise GitHubPullRequestError("unexpected GitHub pull request assignees")
            login = node.get("login")
            if not isinstance(login, str) or not login:
                raise GitHubPullRequestError("unexpected GitHub pull request assignees")
            logins.append(login)
        return tuple(logins)

    @staticmethod
    def _checks(value: object) -> tuple[PullRequestCheck, ...]:
        if value is None:
            return ()
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request checks")
        contexts = value.get("contexts")
        if contexts is None:
            return ()
        if not isinstance(contexts, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request checks")
        nodes = contexts.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubPullRequestError("unexpected GitHub pull request checks")
        checks: list[PullRequestCheck] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise GitHubPullRequestError("unexpected GitHub pull request checks")
            typename = node.get("__typename")
            if typename == "CheckRun":
                name = node.get("name")
                status = node.get("status")
                conclusion = node.get("conclusion")
            elif typename == "StatusContext":
                name = node.get("context")
                status = node.get("state")
                conclusion = None
            else:
                continue
            if not isinstance(name, str) or not isinstance(status, str):
                raise GitHubPullRequestError("unexpected GitHub pull request checks")
            if conclusion is not None and not isinstance(conclusion, str):
                raise GitHubPullRequestError("unexpected GitHub pull request checks")
            is_required = node.get("isRequired")
            if is_required is not None and not isinstance(is_required, bool):
                raise GitHubPullRequestError("unexpected GitHub pull request checks")
            checks.append(PullRequestCheck(name, status, conclusion, is_required))
        return tuple(checks)

    @staticmethod
    def actor_is_bot(author: object) -> bool:
        if not isinstance(author, dict):
            return False
        if author.get("__typename") == "Bot":
            return True
        path = author.get("resourcePath")
        if isinstance(path, str) and path.lower().startswith("/apps/"):
            return True
        login = author.get("login")
        return isinstance(login, str) and login.lower().endswith("[bot]")

    @staticmethod
    def _review_facts(node: object) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request review facts")
        body = node.get("body")
        if body is not None and not isinstance(body, str):
            raise GitHubPullRequestError("unexpected GitHub pull request body")
        behind_by = GitHubPullRequests._behind_by(node.get("baseRef"))
        checks = [
            {
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
                "is_required": check.is_required,
            }
            for check in GitHubPullRequests._checks(node.get("statusCheckRollup"))
        ]
        reviews, reviews_truncated = GitHubPullRequests._connection_items(
            node.get("reviews"), GitHubPullRequests._review_item, "reviews"
        )
        comments, comments_truncated = GitHubPullRequests._connection_items(
            node.get("comments"), GitHubPullRequests._issue_comment_item, "comments"
        )
        threads, threads_truncated = GitHubPullRequests._connection_items(
            node.get("reviewThreads"), GitHubPullRequests._thread_item, "review threads"
        )
        return {
            "body": body,
            "behind_by": behind_by,
            "truncated": reviews_truncated or comments_truncated or threads_truncated,
            "checks": checks,
            "reviews": reviews,
            "comments": comments,
            "threads": threads,
        }

    @staticmethod
    def _behind_by(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request compare")
        compare = value.get("compare")
        if compare is None:
            return None
        if not isinstance(compare, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request compare")
        behind_by = compare.get("behindBy")
        if behind_by is None:
            return None
        if not isinstance(behind_by, int) or isinstance(behind_by, bool) or behind_by < 0:
            raise GitHubPullRequestError("unexpected GitHub pull request compare")
        return behind_by

    @staticmethod
    def _connection_items(
        value: object,
        parse_node: Callable[[object], Any],
        label: str,
    ) -> tuple[list[Any], bool]:
        if value is None:
            return [], False
        if not isinstance(value, dict):
            raise GitHubPullRequestError(f"unexpected GitHub pull request {label}")
        nodes = value.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubPullRequestError(f"unexpected GitHub pull request {label}")
        page_info = value.get("pageInfo")
        truncated = False
        if isinstance(page_info, dict):
            has_next = page_info.get("hasNextPage")
            if has_next is not None and not isinstance(has_next, bool):
                raise GitHubPullRequestError(f"unexpected GitHub pull request {label}")
            truncated = has_next is True
        items: list[Any] = []
        for node in nodes:
            items.append(parse_node(node))
        return items, truncated

    @staticmethod
    def _author_payload(author: object, association: object) -> dict[str, Any]:
        if association is not None and not isinstance(association, str):
            raise GitHubPullRequestError("unexpected GitHub pull request author")
        if author is None:
            return {
                "login": None,
                "is_bot": False,
                "author_association": association,
            }
        if not isinstance(author, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request author")
        login = author.get("login")
        if login is not None and not isinstance(login, str):
            raise GitHubPullRequestError("unexpected GitHub pull request author")
        return {
            "login": login,
            "is_bot": GitHubPullRequests.actor_is_bot(author),
            "author_association": association,
        }

    @staticmethod
    def _review_item(node: object) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request reviews")
        state = node.get("state")
        body = node.get("body")
        submitted_at = node.get("submittedAt")
        if not isinstance(state, str):
            raise GitHubPullRequestError("unexpected GitHub pull request reviews")
        if body is not None and not isinstance(body, str):
            raise GitHubPullRequestError("unexpected GitHub pull request reviews")
        if submitted_at is not None and not isinstance(submitted_at, str):
            raise GitHubPullRequestError("unexpected GitHub pull request reviews")
        payload = GitHubPullRequests._author_payload(
            node.get("author"), node.get("authorAssociation")
        )
        payload.update({"state": state, "submitted_at": submitted_at, "body": body or ""})
        return payload

    @staticmethod
    def _issue_comment_item(node: object) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request comments")
        body = node.get("body")
        created_at = node.get("createdAt")
        if body is not None and not isinstance(body, str):
            raise GitHubPullRequestError("unexpected GitHub pull request comments")
        if created_at is not None and not isinstance(created_at, str):
            raise GitHubPullRequestError("unexpected GitHub pull request comments")
        payload = GitHubPullRequests._author_payload(
            node.get("author"), node.get("authorAssociation")
        )
        payload.update({"created_at": created_at, "body": body or ""})
        return payload

    @staticmethod
    def _thread_item(node: object) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request review threads")
        is_resolved = node.get("isResolved")
        is_outdated = node.get("isOutdated")
        if not isinstance(is_resolved, bool) or not isinstance(is_outdated, bool):
            raise GitHubPullRequestError("unexpected GitHub pull request review threads")
        resolved_by = None
        resolved_node = node.get("resolvedBy")
        if resolved_node is not None:
            if not isinstance(resolved_node, dict):
                raise GitHubPullRequestError("unexpected GitHub pull request review threads")
            login = resolved_node.get("login")
            if not isinstance(login, str) or not login:
                raise GitHubPullRequestError("unexpected GitHub pull request review threads")
            resolved_by = login
        comments, comments_truncated = GitHubPullRequests._connection_items(
            node.get("comments"), GitHubPullRequests._issue_comment_item, "review thread comments"
        )
        return {
            "is_resolved": is_resolved,
            "is_outdated": is_outdated,
            "resolved_by": resolved_by,
            "truncated": comments_truncated,
            "comments": comments,
        }

    @staticmethod
    def _unresolved_threads(value: object) -> int:
        if value is None:
            return 0
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request review threads")
        nodes = value.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubPullRequestError("unexpected GitHub pull request review threads")
        unresolved = 0
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("isResolved"), bool):
                raise GitHubPullRequestError("unexpected GitHub pull request review threads")
            if node["isResolved"] is False:
                unresolved += 1
        return unresolved

    @staticmethod
    def _comment_count(value: object) -> int:
        if value is None:
            return 0
        if not isinstance(value, dict):
            raise GitHubPullRequestError("unexpected GitHub pull request comments")
        count = value.get("totalCount")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise GitHubPullRequestError("unexpected GitHub pull request comments")
        return count
