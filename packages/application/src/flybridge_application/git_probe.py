from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flybridge_core import ConfigError


class GitProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubmoduleState:
    path: str
    sha: str
    dirty: bool
    unpushed_commits: int | None
    github_repository: str | None = None
    branch: str = ""


@dataclass(frozen=True)
class GitWorktreeState:
    branch: str
    dirty: bool
    unpushed_commits: int | None
    ahead: int | None
    behind: int | None
    github_repository: str | None
    github_repositories: tuple[str, ...]
    submodules: tuple[SubmoduleState, ...]
    commit_sha: str = ""


_GITHUB_REMOTE = re.compile(
    r"(?:git@github\.com:|https://github\.com/)(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$"
)


class GitWorktreeProbe:
    """Inspect a filesystem checkout. Does not talk to Orca or GitHub APIs."""

    def __init__(
        self, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    ) -> None:
        self.runner = runner

    def inspect(self, worktree_path: str) -> GitWorktreeState:
        root = Path(worktree_path)
        if not root.is_dir():
            raise GitProbeError(f"worktree path does not exist: {worktree_path}")
        branch = self._output(["git", "-C", worktree_path, "branch", "--show-current"])
        commit_sha = self._output(["git", "-C", worktree_path, "rev-parse", "HEAD"])
        porcelain = self._output(["git", "-C", worktree_path, "status", "--porcelain"])
        dirty = bool(porcelain)
        ahead, behind, unpushed = self._divergence(worktree_path)
        repositories = self._github_repositories(worktree_path)
        origin = self._origin(worktree_path)
        origin_repository = github_repository_from_remote(origin) if origin else None
        submodules = self._submodules(worktree_path)
        submodule_repositories = tuple(
            item.github_repository for item in submodules if item.github_repository
        )
        ordered = tuple(
            dict.fromkeys(
                [
                    *([origin_repository] if origin_repository else ()),
                    *repositories,
                    *submodule_repositories,
                ]
            )
        )
        return GitWorktreeState(
            branch,
            dirty,
            unpushed,
            ahead,
            behind,
            origin_repository or (ordered[0] if ordered else None),
            ordered,
            submodules,
            commit_sha,
        )

    def _divergence(self, worktree_path: str) -> tuple[int | None, int | None, int | None]:
        result = self._run(
            ["git", "-C", worktree_path, "rev-parse", "--abbrev-ref", "@{upstream}"],
            check=False,
        )
        if result.returncode:
            return None, None, None
        counts = self._output(
            [
                "git",
                "-C",
                worktree_path,
                "rev-list",
                "--left-right",
                "--count",
                "@{upstream}...HEAD",
            ]
        )
        try:
            behind_text, ahead_text = counts.split()
            behind = int(behind_text)
            ahead = int(ahead_text)
        except ValueError as exc:
            raise GitProbeError("unable to parse ahead and behind counts") from exc
        return ahead, behind, ahead

    def _origin(self, worktree_path: str) -> str:
        result = self._run(["git", "-C", worktree_path, "remote", "get-url", "origin"], check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _github_repositories(self, worktree_path: str) -> tuple[str, ...]:
        result = self._run(["git", "-C", worktree_path, "remote", "-v"], check=False)
        if result.returncode:
            return ()
        found: list[str] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            repository = github_repository_from_remote(parts[1])
            if repository is None or repository in seen:
                continue
            seen.add(repository)
            found.append(repository)
        return tuple(found)

    def _submodules(self, worktree_path: str) -> tuple[SubmoduleState, ...]:
        result = self._run(
            ["git", "-C", worktree_path, "submodule", "status", "--recursive"],
            check=False,
        )
        if result.returncode:
            return ()
        submodules: list[SubmoduleState] = []
        for line in result.stdout.splitlines():
            parsed = _parse_submodule_status(line)
            if parsed is None:
                continue
            marker, sha, relative = parsed
            submodule_path = str(Path(worktree_path) / relative)
            unpushed = None
            dirty = marker in {"+", "U"}
            github_repository = None
            submodule_branch = ""
            if Path(submodule_path).is_dir():
                origin = self._origin(submodule_path)
                github_repository = github_repository_from_remote(origin) if origin else None
                branch_result = self._run(
                    ["git", "-C", submodule_path, "branch", "--show-current"],
                    check=False,
                )
                if branch_result.returncode == 0:
                    submodule_branch = branch_result.stdout.strip()
                try:
                    _ahead, _behind, unpushed = self._divergence(submodule_path)
                    porcelain = self._output(["git", "-C", submodule_path, "status", "--porcelain"])
                    dirty = dirty or bool(porcelain)
                except GitProbeError:
                    unpushed = None
            submodules.append(
                SubmoduleState(
                    relative,
                    sha,
                    dirty,
                    unpushed,
                    github_repository,
                    submodule_branch,
                )
            )
        return tuple(submodules)

    def _output(self, command: list[str]) -> str:
        result = self._run(command, check=True)
        return result.stdout.strip()

    def _run(self, command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(command, text=True, capture_output=True, check=False, timeout=60)
        except OSError as exc:
            raise GitProbeError(f"unable to inspect git worktree: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitProbeError("git worktree inspection timed out") from exc
        if check and result.returncode:
            raise GitProbeError(result.stderr.strip() or "git worktree inspection failed")
        return result


def github_repository_from_remote(url: str) -> str | None:
    match = _GITHUB_REMOTE.search(url.strip())
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_path_prefix(value: str) -> Path:
    if not value.strip():
        raise ConfigError("path prefix must be a non-empty string")
    return Path(value).expanduser().resolve()


def _parse_submodule_status(line: str) -> tuple[str, str, str] | None:
    text = line.rstrip()
    if not text:
        return None
    marker = text[0] if text[0] in {" ", "-", "+", "U"} else " "
    rest = text[1:].lstrip() if text[0] in {" ", "-", "+", "U"} else text
    parts = rest.split()
    if len(parts) < 2:
        return None
    return marker.strip() or " ", parts[0], parts[1]
