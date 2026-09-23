from __future__ import annotations

import re
from dataclasses import dataclass

_ISSUE_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>\d+)/?",
    re.IGNORECASE,
)
_RESOURCE_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/(?P<kind>issues|pull)/(?P<number>\d+)/?",
    re.IGNORECASE,
)
_WORKFLOW_MARKER = re.compile(
    r"<!--\s*flybridge:run=[0-9a-f-]+;step=[0-9a-f-]+\s*-->", re.IGNORECASE
)


@dataclass(frozen=True)
class GitHubIssueRef:
    repository: str
    number: int
    url: str

    @property
    def join_key(self) -> str:
        return f"{self.repository.lower()}#{self.number}"


def canonical_issue_url(repository: str, number: int) -> str:
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError("GitHub repository is invalid")
    if number < 1:
        raise ValueError("GitHub issue number is invalid")
    return f"https://github.com/{owner}/{name}/issues/{number}"


def parse_issue_url(value: str) -> GitHubIssueRef:
    text = value.strip()
    match = _ISSUE_URL.fullmatch(text.rstrip("/"))
    if match is None:
        raise ValueError("GitHub issue URL is invalid")
    owner = match.group("owner")
    repo = match.group("repo")
    number = int(match.group("number"))
    repository = f"{owner}/{repo}"
    return GitHubIssueRef(repository, number, canonical_issue_url(repository, number))


def issue_urls_from_text(text: str) -> tuple[GitHubIssueRef, ...]:
    if not text:
        return ()
    seen: set[str] = set()
    refs: list[GitHubIssueRef] = []
    for match in _ISSUE_URL.finditer(text):
        repository = f"{match.group('owner')}/{match.group('repo')}"
        number = int(match.group("number"))
        ref = GitHubIssueRef(repository, number, canonical_issue_url(repository, number))
        if ref.join_key in seen:
            continue
        seen.add(ref.join_key)
        refs.append(ref)
    return tuple(refs)


def resource_urls_from_text(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    seen: set[str] = set()
    urls: list[str] = []
    for match in _RESOURCE_URL.finditer(text):
        owner = match.group("owner")
        repo = match.group("repo")
        kind = "issues" if match.group("kind").lower() == "issues" else "pull"
        number = int(match.group("number"))
        url = f"https://github.com/{owner}/{repo}/{kind}/{number}"
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return tuple(urls)


def merge_lifecycle_comment(existing: str, lifecycle: str, extra_urls: tuple[str, ...] = ()) -> str:
    refs = list(issue_urls_from_text(existing))
    seen = {ref.join_key for ref in refs}
    for raw in extra_urls:
        try:
            ref = parse_issue_url(raw)
        except ValueError:
            continue
        if ref.join_key in seen:
            continue
        seen.add(ref.join_key)
        refs.append(ref)
    lines = [ref.url for ref in refs]
    marker = _WORKFLOW_MARKER.search(existing)
    if marker is not None:
        lines.append(marker.group(0))
    detail = lifecycle.strip()
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def repository_from_orca_project_id(project_id: str | None) -> str | None:
    if not project_id or not isinstance(project_id, str):
        return None
    prefix, separator, rest = project_id.partition(":")
    if separator != ":" or prefix.lower() != "github" or not rest:
        return None
    owner, slash, name = rest.partition("/")
    if not slash or not owner or not name or "/" in name:
        return None
    return f"{owner}/{name}"


def repositories_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.lower() == right.lower()
