from __future__ import annotations

import json
import os
import subprocess

import pytest
from flybridge_github import GitHubCli, GitHubCliError, GitHubPullRequests


def test_cli_resolves_configured_login_token_and_overrides_parent_github_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "wrong-parent-token")
    calls: list[tuple[list[str], str | None]] = []

    def runner(arguments, **kwargs):
        env = kwargs.get("env") or {}
        calls.append((list(arguments), env.get("GH_TOKEN")))
        if arguments[1:3] == ["auth", "token"]:
            assert "--user" in arguments
            assert arguments[arguments.index("--user") + 1] == "alice"
            assert kwargs.get("env") is None or "GH_TOKEN" not in kwargs
            return subprocess.CompletedProcess(arguments, 0, "gho_alice_token\n", "")
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps({"data": {"repository": {"pullRequests": {"nodes": []}}}}),
            "",
        )

    GitHubPullRequests(user="alice", runner=runner).list_by_head("example/repo", "feature")

    assert calls[0][0][1:3] == ["auth", "token"]
    assert calls[0][1] is None
    assert calls[1][1] == "gho_alice_token"
    assert os.environ["GH_TOKEN"] == "wrong-parent-token"


def test_cli_token_fails_when_login_is_not_authenticated() -> None:
    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 1, "", "no token")

    with pytest.raises(GitHubCliError, match="not authenticated"):
        GitHubCli(user="alice", runner=runner).token()


def test_inspect_accounts_reports_active_and_configured_login_without_tokens() -> None:
    payload = {
        "hosts": {
            "github.com": [
                {"login": "bob", "active": True, "state": "success", "token": "secret"},
                {"login": "alice", "active": False, "state": "success"},
            ]
        }
    }

    def runner(arguments, **_kwargs):
        assert "--show-token" not in arguments
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    report = GitHubCli(user="alice", runner=runner).inspect_accounts()
    assert report["active_login"] == "bob"
    assert report["configured_login"] == "alice"
    assert report["configured_login_authenticated"] is True
    assert "secret" not in json.dumps(report)
    assert "token" not in report
