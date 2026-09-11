from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from flybridge_core.config import GitHubConfig


class GitHubProjectError(RuntimeError):
    pass


_UNSAFE_GH_MARKERS = ("{owner}", "{repo}", "{branch}")


@dataclass(frozen=True)
class Candidate:
    repository: str
    number: int
    title: str
    url: str
    priority: str | None


class GitHubProject:
    """Read-only Project candidate screening; selection remains a human action."""

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
        self.executable = executable
        self.runner = runner

    @staticmethod
    def _single_select_name(value: object, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {"name"}:
            raise GitHubProjectError(f"unexpected GitHub Project {field_name} field value")
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise GitHubProjectError(f"unexpected GitHub Project {field_name} field value")
        return name

    @staticmethod
    def _cli_value(field: str, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise GitHubProjectError(f"GitHub Project {field} is invalid")
        if value.startswith("@") or any(marker in value for marker in _UNSAFE_GH_MARKERS):
            raise GitHubProjectError(f"GitHub Project {field} contains an unsafe gh expansion")
        return value

    def screen(self) -> list[Candidate]:
        owner_field = "user" if self.config.owner_type == "user" else "organization"
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
                    ... on Issue {{ number title url repository {{ nameWithOwner }} }}
                  }}
                  status: fieldValueByName(name:$statusField) {{
                    ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
                  }}
                  priority: fieldValueByName(name:$priorityField) {{
                    ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        candidates: list[Candidate] = []
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
                f"owner={self._cli_value('owner', self.config.owner)}",
                "-F",
                f"number={self.config.project_number}",
                "-f",
                f"statusField={self._cli_value('status field', self.config.status_field)}",
                "-f",
                f"priorityField={self._cli_value('priority field', self.config.priority_field)}",
            ]
            if cursor:
                arguments.extend(["-F", f"cursor={self._cli_value('pagination cursor', cursor)}"])
            try:
                result = self.runner(
                    arguments, text=True, capture_output=True, check=False, timeout=60
                )
            except OSError as exc:
                raise GitHubProjectError(f"unable to run GitHub CLI: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                raise GitHubProjectError("GitHub CLI timed out") from exc
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
                if not isinstance(item, dict):
                    raise GitHubProjectError("unexpected GitHub Project item")
                content = item.get("content")
                if content is None:
                    continue
                if not isinstance(content, dict) or not isinstance(content.get("__typename"), str):
                    raise GitHubProjectError("unexpected GitHub Project item")
                if content["__typename"] != "Issue":
                    continue
                if "status" not in item or "priority" not in item:
                    raise GitHubProjectError("unexpected GitHub Project field values")
                status = self._single_select_name(item.get("status"), self.config.status_field)
                priority = self._single_select_name(
                    item.get("priority"), self.config.priority_field
                )
                eligible_priority = (
                    bool(self.config.priority_values) and priority in self.config.priority_values
                )
                if status != self.config.todo_status or not eligible_priority:
                    continue
                try:
                    repository = content["repository"]["nameWithOwner"]
                    number = content["number"]
                    title = content["title"]
                    url = content["url"]
                except (KeyError, TypeError) as exc:
                    raise GitHubProjectError("unexpected eligible GitHub Project issue") from exc
                if (
                    not isinstance(repository, str)
                    or not repository
                    or not isinstance(number, int)
                    or isinstance(number, bool)
                    or not isinstance(title, str)
                    or not isinstance(url, str)
                ):
                    raise GitHubProjectError("unexpected eligible GitHub Project issue")
                candidates.append(
                    Candidate(
                        repository,
                        number,
                        title,
                        url,
                        priority,
                    )
                )
            if not page_info.get("hasNextPage"):
                return candidates
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubProjectError("GitHub Project pagination cursor is missing")
            if cursor in seen_cursors:
                raise GitHubProjectError("GitHub Project pagination cursor did not advance")
            seen_cursors.add(cursor)
        raise GitHubProjectError("GitHub Project pagination exceeded the safety limit")
