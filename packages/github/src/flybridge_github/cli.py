from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

_UNSAFE_GH_MARKERS = ("{owner}", "{repo}", "{branch}")
_UNSET = object()


class GitHubCliError(RuntimeError):
    pass


class GitHubCli:
    """Run gh with a per-process token for a configured GitHub login."""

    def __init__(
        self,
        executable: str = "gh",
        *,
        user: str | None = None,
        hostname: str = "github.com",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.user = user
        self.hostname = hostname
        self.runner = runner
        self._token: object = _UNSET

    @staticmethod
    def cli_value(field: str, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise GitHubCliError(f"GitHub {field} is invalid")
        if value.startswith("@") or any(marker in value for marker in _UNSAFE_GH_MARKERS):
            raise GitHubCliError(f"GitHub {field} contains an unsafe gh expansion")
        return value

    def run(
        self,
        arguments: list[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged = dict(os.environ if env is None else env)
        token = self.token()
        if token is not None:
            merged["GH_TOKEN"] = token
        try:
            return self.runner(
                arguments,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=merged,
            )
        except OSError as exc:
            raise GitHubCliError(f"unable to run GitHub CLI: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubCliError("GitHub CLI timed out") from exc

    def token(self) -> str | None:
        if self.user is None:
            return None
        if self._token is not _UNSET:
            return self._token if isinstance(self._token, str) else None
        login = self.cli_value("login", self.user)
        host = self.cli_value("hostname", self.hostname)
        arguments = [
            self.executable,
            "auth",
            "token",
            "--hostname",
            host,
            "--user",
            login,
        ]
        try:
            result = self.runner(
                arguments,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except OSError as exc:
            raise GitHubCliError(f"unable to run GitHub CLI: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubCliError("GitHub CLI timed out") from exc
        if result.returncode:
            raise GitHubCliError(f"github.login {login!r} is not authenticated for this gh host")
        token = (result.stdout or "").strip()
        if not token:
            raise GitHubCliError(f"github.login {login!r} is not authenticated for this gh host")
        self._token = token
        return token

    def inspect_accounts(self) -> dict[str, Any]:
        host = self.cli_value("hostname", self.hostname)
        result = self.runner(
            [
                self.executable,
                "auth",
                "status",
                "--hostname",
                host,
                "--json",
                "hosts",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode and not (result.stdout or "").strip():
            raise GitHubCliError(result.stderr.strip() or "GitHub CLI failed")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise GitHubCliError("unexpected GitHub auth status response") from exc
        if not isinstance(payload, dict):
            raise GitHubCliError("unexpected GitHub auth status response")
        hosts = payload.get("hosts")
        if hosts is None:
            accounts: list[dict[str, Any]] = []
        elif isinstance(hosts, dict):
            raw = hosts.get(host) or hosts.get(host.lower())
            if raw is None:
                accounts = []
            elif not isinstance(raw, list):
                raise GitHubCliError("unexpected GitHub auth status response")
            else:
                accounts = [item for item in raw if isinstance(item, dict)]
        elif isinstance(hosts, list):
            accounts = [item for item in hosts if isinstance(item, dict)]
        else:
            raise GitHubCliError("unexpected GitHub auth status response")
        active = None
        logins: list[str] = []
        for account in accounts:
            login = account.get("login")
            if not isinstance(login, str) or not login:
                continue
            logins.append(login)
            if account.get("active") is True and active is None:
                active = login
        configured = self.user
        authenticated = bool(
            configured and any(login.lower() == configured.lower() for login in logins)
        )
        if configured and not authenticated:
            try:
                self.token()
            except GitHubCliError:
                authenticated = False
            else:
                authenticated = True
        return {
            "hostname": host,
            "configured_login": configured,
            "active_login": active,
            "authenticated_logins": logins,
            "configured_login_authenticated": authenticated,
        }
