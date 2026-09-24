from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from flybridge_core.config import GitHubBoard, GitHubConfig

from .cli import GitHubCli, GitHubCliError


class GitHubProjectError(RuntimeError):
    pass


_UNSAFE_GH_MARKERS = ("{owner}", "{repo}", "{branch}")
_FIELD_VALUE_META_KEYS = frozenset(
    {"__typename", "id", "color", "optionId", "description", "issueFieldValue"}
)


@dataclass(frozen=True)
class Candidate:
    repository: str
    number: int
    title: str
    url: str
    status: str | None
    priority: str | None
    assignees: tuple[str, ...]
    owner: str
    project_number: int


class GitHubProject:
    """Read-only Project item listing; selection remains a human action."""

    _PAGE_SIZE = 100
    _MAX_PAGES = 100

    def __init__(
        self,
        config: GitHubConfig,
        executable: str = "gh",
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not config.enabled:
            raise GitHubProjectError("GitHub Project integration is disabled in configuration")
        self.config = config
        self.cli = GitHubCli(executable, user=config.login or None, runner=runner)
        self.executable = self.cli.executable
        self.runner = self.cli.runner
        self.seen_status_names: tuple[str, ...] = ()
        self.seen_priority_names: tuple[str, ...] = ()

    @staticmethod
    def _single_select_name(value: object, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise GitHubProjectError(f"unexpected GitHub Project {field_name} field value")
        nested = value.get("issueFieldValue")
        if nested is not None:
            return GitHubProject._single_select_name(nested, field_name)
        name = value.get("name")
        if isinstance(name, str) and name and not (set(value) - {"name"} - _FIELD_VALUE_META_KEYS):
            return name
        return None

    @staticmethod
    def _cli_value(field: str, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise GitHubProjectError(f"GitHub Project {field} is invalid")
        if value.startswith("@") or any(marker in value for marker in _UNSAFE_GH_MARKERS):
            raise GitHubProjectError(f"GitHub Project {field} contains an unsafe gh expansion")
        return value

    @staticmethod
    def _option_key(value: str) -> str:
        return "".join(value.split()).casefold()

    @classmethod
    def _option_matches(cls, actual: str | None, filters: set[str]) -> bool:
        if not filters:
            return True
        if actual is None:
            return False
        actual_key = cls._option_key(actual)
        return any(cls._option_key(item) == actual_key for item in filters)

    @classmethod
    def unmatched_option_filters(
        cls, filters: Sequence[str], seen: Sequence[str]
    ) -> tuple[str, ...]:
        if not filters:
            return ()
        seen_keys = {cls._option_key(name) for name in seen}
        return tuple(value for value in filters if cls._option_key(value) not in seen_keys)

    @staticmethod
    def _assignees(content: dict[str, object]) -> tuple[str, ...]:
        assignees = content.get("assignees")
        if assignees is None:
            return ()
        if not isinstance(assignees, dict):
            raise GitHubProjectError("unexpected eligible GitHub Project issue")
        nodes = assignees.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubProjectError("unexpected eligible GitHub Project issue")
        logins: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise GitHubProjectError("unexpected eligible GitHub Project issue")
            login = node.get("login")
            if not isinstance(login, str) or not login:
                raise GitHubProjectError("unexpected eligible GitHub Project issue")
            logins.append(login)
        return tuple(logins)

    def _parse_board_selector(self, selector: str) -> tuple[str | None, int]:
        text = self._cli_value("board selector", selector)
        if "/" in text:
            owner, _, number_text = text.rpartition("/")
            if not owner or not number_text.isdigit():
                raise GitHubProjectError("GitHub Project board selector is invalid")
            return owner, int(number_text)
        if not text.isdigit():
            raise GitHubProjectError("GitHub Project board selector is invalid")
        return None, int(text)

    def selected_boards(self, selectors: Sequence[str] | None) -> tuple[GitHubBoard, ...]:
        if not selectors:
            return self.config.boards
        selected: list[GitHubBoard] = []
        seen: set[tuple[str, int]] = set()
        for selector in selectors:
            owner, number = self._parse_board_selector(selector)
            matches = [
                board
                for board in self.config.boards
                if board.project_number == number and (owner is None or board.owner == owner)
            ]
            if not matches:
                raise GitHubProjectError(f"GitHub Project board is not configured: {selector}")
            if len(matches) > 1:
                raise GitHubProjectError(
                    "GitHub Project board selector matches more than one configured board"
                )
            board = matches[0]
            key = (board.owner, board.project_number)
            if key in seen:
                continue
            seen.add(key)
            selected.append(board)
        return tuple(selected)

    def screen(
        self,
        *,
        boards: Sequence[str] | None = None,
        statuses: Sequence[str] = (),
        priorities: Sequence[str] = (),
        assignee: str | None = None,
    ) -> list[Candidate]:
        status_filter = {self._cli_value("status", value) for value in statuses}
        priority_filter = {self._cli_value("priority", value) for value in priorities}
        assignee_filter = None if assignee is None else self._cli_value("assignee", assignee)
        seen_statuses: list[str] = []
        seen_priorities: list[str] = []
        candidates: list[Candidate] = []
        for board in self.selected_boards(boards):
            board_candidates, board_statuses, board_priorities = self._screen_board(
                board, status_filter, priority_filter, assignee_filter
            )
            candidates.extend(board_candidates)
            seen_statuses.extend(board_statuses)
            seen_priorities.extend(board_priorities)
        self.seen_status_names = tuple(dict.fromkeys(seen_statuses))
        self.seen_priority_names = tuple(dict.fromkeys(seen_priorities))
        return candidates

    def _screen_board(
        self,
        board: GitHubBoard,
        status_filter: set[str],
        priority_filter: set[str],
        assignee: str | None,
    ) -> tuple[list[Candidate], list[str], list[str]]:
        owner_field = "user" if board.owner_type == "user" else "organization"
        query = f"""
        query(
          $owner:String!,
          $number:Int!,
          $cursor:String,
          $statusField:String!,
          $priorityField:String!
        ) {{
          {owner_field}(login:$owner) {{
            projectV2(number:$number) {{
              items(first:{self._PAGE_SIZE}, after:$cursor) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{
                  content {{
                    __typename
                    ... on Issue {{
                      number
                      title
                      url
                      repository {{ nameWithOwner }}
                      assignees(first:20) {{ nodes {{ login }} }}
                    }}
                  }}
                  status: fieldValueByName(name:$statusField) {{
                    __typename
                    ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
                    ... on ProjectV2ItemIssueFieldValue {{
                      issueFieldValue {{
                        __typename
                        ... on IssueFieldSingleSelectValue {{ name }}
                      }}
                    }}
                  }}
                  priority: fieldValueByName(name:$priorityField) {{
                    __typename
                    ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
                    ... on ProjectV2ItemIssueFieldValue {{
                      issueFieldValue {{
                        __typename
                        ... on IssueFieldSingleSelectValue {{ name }}
                      }}
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        candidates: list[Candidate] = []
        seen_statuses: list[str] = []
        seen_priorities: list[str] = []
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
                f"owner={self._cli_value('owner', board.owner)}",
                "-F",
                f"number={board.project_number}",
                "-f",
                f"statusField={self._cli_value('status field', board.status_field)}",
                "-f",
                f"priorityField={self._cli_value('priority field', board.priority_field)}",
            ]
            if cursor:
                arguments.extend(["-F", f"cursor={self._cli_value('pagination cursor', cursor)}"])
            try:
                result = self.cli.run(arguments, timeout=60)
            except GitHubCliError as exc:
                raise GitHubProjectError(str(exc)) from exc
            if result.returncode:
                raise GitHubProjectError(result.stderr.strip() or "GitHub CLI failed")
            try:
                response = json.loads(result.stdout)
                if not isinstance(response, dict):
                    raise GitHubProjectError("unexpected GitHub Project response")
                errors = response.get("errors")
                if errors:
                    detail = ""
                    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                        message = errors[0].get("message")
                        detail = f": {message}" if isinstance(message, str) and message else ""
                    raise GitHubProjectError(f"GitHub Project GraphQL query failed{detail}")
                project = response["data"][owner_field]["projectV2"]
                items = project["items"]
                nodes = items["nodes"]
                page_info = items["pageInfo"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise GitHubProjectError("unexpected GitHub Project response") from exc
            if (
                not isinstance(nodes, list)
                or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)
            ):
                raise GitHubProjectError("unexpected GitHub Project response")
            for item in nodes:
                candidate, status, priority = self._candidate_from_item(
                    item, board, status_filter, priority_filter, assignee
                )
                if status is not None:
                    seen_statuses.append(status)
                if priority is not None:
                    seen_priorities.append(priority)
                if candidate is not None:
                    candidates.append(candidate)
            if not page_info.get("hasNextPage"):
                return candidates, seen_statuses, seen_priorities
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubProjectError("GitHub Project pagination cursor is missing")
            if cursor in seen_cursors:
                raise GitHubProjectError("GitHub Project pagination cursor did not advance")
            seen_cursors.add(cursor)
        raise GitHubProjectError("GitHub Project pagination exceeded the safety limit")

    def _candidate_from_item(
        self,
        item: object,
        board: GitHubBoard,
        status_filter: set[str],
        priority_filter: set[str],
        assignee: str | None,
    ) -> tuple[Candidate | None, str | None, str | None]:
        if not isinstance(item, dict):
            raise GitHubProjectError("unexpected GitHub Project item")
        content = item.get("content")
        if content is None:
            return None, None, None
        if not isinstance(content, dict) or not isinstance(content.get("__typename"), str):
            raise GitHubProjectError("unexpected GitHub Project item")
        if content["__typename"] != "Issue":
            return None, None, None
        if "status" not in item or "priority" not in item:
            raise GitHubProjectError("unexpected GitHub Project field values")
        status = self._single_select_name(item.get("status"), board.status_field)
        priority = self._single_select_name(item.get("priority"), board.priority_field)
        if not self._option_matches(status, status_filter):
            return None, status, priority
        if not self._option_matches(priority, priority_filter):
            return None, status, priority
        try:
            repository = content["repository"]["nameWithOwner"]
            number = content["number"]
            title = content["title"]
            url = content["url"]
            assignees = self._assignees(content)
        except (KeyError, TypeError) as exc:
            raise GitHubProjectError("unexpected eligible GitHub Project issue") from exc
        if assignee is not None and assignee not in assignees:
            return None, status, priority
        if (
            not isinstance(repository, str)
            or not repository
            or not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(title, str)
            or not isinstance(url, str)
        ):
            raise GitHubProjectError("unexpected eligible GitHub Project issue")
        return (
            Candidate(
                repository,
                number,
                title,
                url,
                status,
                priority,
                assignees,
                board.owner,
                board.project_number,
            ),
            status,
            priority,
        )
