from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def mock_dispatcher_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit suite never starts a resident process or a real Orca client."""
    monkeypatch.setattr("flybridge_cli.commands.queue.ensure_dispatcher", lambda *_args: None)
    monkeypatch.setattr("flybridge_cli.commands.workflow.ensure_dispatcher", lambda *_args: None)


ENABLED_GITHUB = {
    "enabled": True,
    "login": "alice",
    "boards": [
        {
            "owner": "example",
            "owner_type": "user",
            "project_number": 1,
            "status_field": "Status",
            "priority_field": "Priority",
        }
    ],
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def required_config(**overrides: Any) -> dict[str, Any]:
    payload = {
        "default_mode": "single",
        "orca": {"agents": {}},
        "skills": {
            "sources": [],
            "roles": {},
            "operator": [],
            "response_language": "English",
        },
        "queue": {"observer": False, "resources": []},
        "github": {"enabled": False},
    }
    return _jsonable(_merge(payload, overrides))


def write_config(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps(required_config(**overrides), indent=2), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def require_mock_orca_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unmocked read-only status paths must never reach the installed Orca CLI."""
    import os
    import shutil
    import subprocess
    import sys

    from flybridge_orca.client import OrcaClient

    original = OrcaClient.__init__
    real_which = shutil.which
    real_run = subprocess.run
    blocked = {"orca", "orca-ide", "orca-dev", "codex", "claude", "gemini"}
    mock_bin = tmp_path / "mock-agent-bin"
    mock_bin.mkdir()
    for name in blocked:
        executable = mock_bin / name
        executable.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(mock_bin) + os.pathsep + os.environ.get("PATH", ""))
    original_popen = subprocess.Popen.__init__

    def guarded_popen(self, args, *positional, **kwargs):
        executable = args[0] if isinstance(args, (list, tuple)) else args.split()[0]
        if Path(executable).name in blocked or "orca-linux.AppImage" in str(args):
            raise AssertionError("test isolation: Orca/agent subprocess launch forbidden")
        original_popen(self, args, *positional, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen)

    def unavailable_runner(arguments, **kwargs):
        if arguments[0] == "git":
            return real_run(arguments, **kwargs)
        raise RuntimeError("test isolation: explicit mock Orca runner required")

    def mock_agent_which(name, *args, **kwargs):
        if name in blocked:
            return str(mock_bin / name)
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", mock_agent_which)

    def initialize(self, executable, **kwargs):
        # The process-group timeout test deliberately launches only this Python.
        if executable != sys.executable:
            kwargs.setdefault("runner", unavailable_runner)
        kwargs.setdefault("which", mock_agent_which)
        original(self, executable, **kwargs)

    monkeypatch.setattr(OrcaClient, "__init__", initialize)
