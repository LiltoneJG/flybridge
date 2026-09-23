"""Orca client unit tests.

Mock the Orca CLI runner and agent PATH resolution. Do not launch Orca or an
LLM, and do not require `orca-ide`, `codex`, or `cursor-agent` on PATH.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from flybridge_core import AgentLaunchPreset
from flybridge_orca import resolve_launch_command
from flybridge_orca.client import OrcaClient, OrcaError, OrcaStartError, OrcaTimeoutError
from flybridge_orca.resolve import resolve_prompt_command


def _missing_selector(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    payload = {
        "ok": False,
        "error": {"code": "selector_not_found", "message": "selector_not_found"},
    }
    return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "")


def test_start_uses_worktree_id_and_defers_orca_lifecycle_until_owned(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "create"]:
            response = {
                "ok": True,
                "result": {
                    "worktree": {"id": "repo::/tmp/worktree", "path": "/tmp/worktree"},
                    "startupTerminal": {"handle": "terminal-1"},
                },
            }
        elif arguments[1:3] == ["worktree", "show"]:
            if not any(call[1:3] == ["worktree", "create"] for call in calls[:-1]):
                return _missing_selector(arguments)
            response = {
                "ok": True,
                "result": {
                    "worktree": {
                        "id": "repo::/tmp/worktree",
                        "path": "/tmp/worktree",
                        "comment": "https://github.com/example/repo/issues/1",
                    }
                },
            }
        elif arguments[1:3] == ["terminal", "create"]:
            response = {"ok": True, "result": {"terminal": {"handle": "agent-terminal"}}}
        elif arguments[1:3] == ["terminal", "wait"]:
            response = {"ok": True, "result": {"wait": {"satisfied": True}}}
        elif arguments[1:3] == ["terminal", "send"]:
            response = {"ok": True, "result": {"send": {"accepted": True}}}
        elif arguments[1:3] == ["terminal", "rename"]:
            response = {"ok": True, "result": {"terminal": {"handle": "terminal-1"}}}
        else:
            response = {"ok": True, "result": {"updated": True}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    started = OrcaClient("orca-ide", runner=runner, which=lambda name: f"/tmp/{name}").start(
        tmp_path, "example", "single", "codex", "Implement the change."
    )

    assert started.worktree_id == "repo::/tmp/worktree"
    assert started.terminal == "agent-terminal"
    assert calls[0][1:3] == ["worktree", "show"]
    assert calls[1][:3] == ["orca-ide", "worktree", "create"]
    assert "--no-parent" in calls[1]
    assert calls[3][1:3] == ["terminal", "create"]
    assert shlex.split(calls[3][calls[3].index("--command") + 1]) == [
        str(Path("/tmp/codex").resolve()),
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "Implement the change.",
    ]

    OrcaClient("orca-ide", runner=runner).set_lifecycle(started.worktree_id, "running")

    assert calls[4][1:3] == ["worktree", "show"]
    assert calls[5] == [
        "orca-ide",
        "worktree",
        "set",
        "--worktree",
        "id:repo::/tmp/worktree",
        "--comment",
        "https://github.com/example/repo/issues/1\nFlybridge workflow is running.",
        "--workspace-status",
        "in-progress",
        "--json",
    ]


def test_start_launches_an_absolute_custom_agent_after_allocating_worktree(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    agent = tmp_path / "local-agent"
    agent.write_text("#!/bin/sh\n", encoding="utf-8")

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        operation = arguments[1:3]
        if operation == ["worktree", "show"]:
            return _missing_selector(arguments)
        if operation == ["worktree", "create"]:
            result = {"worktree": {"id": "repo::/tmp/custom", "path": "/tmp/custom"}}
        elif operation == ["terminal", "create"]:
            result = {"terminal": {"handle": "local-agent-terminal"}}
        elif operation == ["terminal", "wait"]:
            result = {"wait": {"satisfied": True}}
        elif operation == ["terminal", "send"]:
            result = {"send": {"accepted": True}}
        else:
            result = {"closed": True}
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    started = OrcaClient("orca-ide", runner=runner).start(
        tmp_path, "custom", "single", str(agent), "Implement locally."
    )

    create = calls[1]
    assert "--agent" not in create
    assert "--prompt" not in create
    assert calls[2][1:3] == ["terminal", "create"]
    assert calls[2][calls[2].index("--command") + 1] == str(agent)
    assert calls[3][1:3] == ["terminal", "wait"]
    assert calls[4][1:3] == ["terminal", "send"]
    assert started.terminal == "local-agent-terminal"


@pytest.mark.parametrize(
    ("name", "role", "mode", "expected"),
    [
        ("task", "single", "single", "[S] task"),
        ("task", "manager", "orchestrated", "[M] task"),
        ("arbitrary-name", "worker", "orchestrated", "[W] arbitrary-name"),
        ("arbitrary-name", "reviewer", "orchestrated", "[R] arbitrary-name"),
    ],
)
def test_agent_terminal_title_reflects_workflow_role(
    name: str, role: str, mode: str, expected: str
) -> None:
    assert OrcaClient._agent_terminal_title(name, role, mode) == expected


@pytest.mark.parametrize(
    ("state", "comment"),
    [
        ("completed", "Flybridge workflow completed."),
        ("failed", "Flybridge workflow failed."),
        ("cancelled", "Flybridge workflow cancelled."),
    ],
)
def test_terminal_lifecycle_states_close_the_orca_workspace(state: str, comment: str) -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            payload = {
                "ok": True,
                "result": {"worktree": {"id": "repo::/tmp/worktree", "comment": ""}},
            }
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"ok": True, "result": {}}), "")

    OrcaClient("orca-ide", runner=runner).set_lifecycle("repo::/tmp/worktree", state)

    assert calls[0][1:3] == ["worktree", "show"]
    assert calls[1] == [
        "orca-ide",
        "worktree",
        "set",
        "--worktree",
        "id:repo::/tmp/worktree",
        "--comment",
        comment,
        "--workspace-status",
        "completed",
        "--json",
    ]


def test_client_rejects_invalid_orca_response() -> None:
    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, "not-json", "")

    with pytest.raises(OrcaError, match="invalid JSON"):
        OrcaClient("orca-ide", runner=runner).close_terminals("repo::/tmp/worktree", "terminal-1")


def test_new_codex_agent_runs_its_initial_prompt_in_one_shot_mode(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["terminal", "create"]:
            result = {"terminal": {"handle": "fresh-terminal"}}
        payload = {"ok": True, "result": result}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    handle = OrcaClient(
        "orca-ide", runner=runner, which=lambda name: f"/tmp/{name}"
    ).create_new_agent_terminal(
        "repo::/tmp/worktree", str(tmp_path), "codex", "Inspect changes and continue."
    )

    assert handle == "fresh-terminal"
    command = calls[0][calls[0].index("--command") + 1]
    assert shlex.split(command) == [
        str(Path("/tmp/codex").resolve()),
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "Inspect changes and continue.",
    ]
    assert [call[1:3] for call in calls] == [["terminal", "create"]]


def test_codex_prompt_terminal_recovers_an_accepted_create_after_timeout(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    worktree_id = "repo::/tmp/worktree"

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["terminal", "create"]:
            payload = {
                "ok": False,
                "error": {"code": "timeout", "message": "terminal startup timed out"},
            }
            return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "")
        title = calls[0][calls[0].index("--title") + 1]
        payload = {
            "ok": True,
            "result": {
                "terminals": [
                    {
                        "handle": "recovered-terminal",
                        "title": title,
                        "worktreeId": worktree_id,
                    }
                ]
            },
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    handle = OrcaClient(
        "orca-ide", runner=runner, which=lambda name: f"/tmp/{name}"
    ).create_new_agent_terminal(
        worktree_id,
        str(tmp_path),
        "codex",
        "Continue the work.",
        model="gpt-5.6-luna",
    )

    assert handle == "recovered-terminal"
    create, listing = calls
    assert create[create.index("--title") + 1].startswith("FLYBRIDGE FRESH AGENT ")
    assert listing[1:4] == ["terminal", "list", "--worktree"]
    assert listing[listing.index("--worktree") + 1] == f"id:{worktree_id}"


def test_codex_prompt_terminal_does_not_recover_an_unmatched_create_timeout() -> None:
    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["terminal", "create"]:
            payload = {
                "ok": False,
                "error": {"code": "timeout", "message": "terminal startup timed out"},
            }
        else:
            payload = {"ok": True, "result": {"terminals": []}}
        return subprocess.CompletedProcess(
            arguments, 1 if arguments[1:3] == ["terminal", "create"] else 0, json.dumps(payload), ""
        )

    with pytest.raises(OrcaError, match="terminal startup timed out"):
        OrcaClient(
            "orca-ide", runner=runner, which=lambda name: f"/tmp/{name}"
        ).create_new_agent_terminal(
            "repo::/tmp/worktree", "/tmp/worktree", "codex", "Continue the work."
        )


def test_codex_model_arguments_come_from_the_tracked_preset() -> None:
    command = resolve_launch_command(
        "codex",
        "gpt-5.6",
        which=lambda name: f"/opt/bin/{name}",
    )

    assert shlex.split(command) == [
        "/opt/bin/codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5.6",
    ]


def test_codex_prompt_command_quotes_prompt_for_one_shot_exec() -> None:
    prompt = "Implement 'this' safely; keep the spaces."
    command = resolve_prompt_command(
        "codex",
        "gpt-5.6-luna",
        prompt,
        which=lambda name: f"/opt/bin/{name}",
    )

    assert shlex.split(command) == [
        "/opt/bin/codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5.6-luna",
        prompt,
    ]


def test_local_launch_preset_can_override_cursor_arguments() -> None:
    presets = {
        "default": AgentLaunchPreset(("{agent}",), (), ("--model", "{model}")),
        "cursor": AgentLaunchPreset(
            ("cursor-agent",),
            ("--trust", "--yolo"),
            ("--model", "{model}"),
            builtin_models=("auto",),
        ),
    }

    command = resolve_launch_command(
        "cursor",
        "claude-opus-5-low",
        presets=presets,
        which=lambda name: f"/opt/bin/{name}",
    )

    assert shlex.split(command) == [
        "/opt/bin/cursor-agent",
        "--trust",
        "--yolo",
        "--model",
        "claude-opus-5-low",
    ]


def test_new_non_codex_agent_uses_a_new_terminal_and_initial_prompt() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["terminal", "create"]:
            result = {"terminal": {"handle": "new-agent"}}
        elif arguments[1:3] == ["terminal", "wait"]:
            result = {"wait": {"satisfied": True}}
        else:
            result = {"send": {"accepted": True}}
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    handle = OrcaClient(
        "orca-ide", runner=runner, which=lambda name: f"/tmp/{name}"
    ).create_new_agent_terminal(
        "repo::/tmp/worktree", "/tmp/worktree", "claude", "Continue the work."
    )

    assert handle == "new-agent"
    assert calls[0][1:3] == ["terminal", "create"]
    assert calls[0][calls[0].index("--command") + 1] == str(Path("/tmp/claude").resolve())
    assert calls[1][1:3] == ["terminal", "wait"]
    assert calls[2][1:3] == ["terminal", "send"]
    assert calls[2][calls[2].index("--text") + 1] == "Continue the work."


def test_custom_start_can_create_terminal_when_worktree_start_omits_one(tmp_path: Path) -> None:
    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        if arguments[1:3] == ["terminal", "create"]:
            payload = {"ok": True, "result": {"terminal": {"handle": "agent"}}}
        elif arguments[1:3] == ["terminal", "wait"]:
            payload = {"ok": True, "result": {"wait": {"satisfied": True}}}
        elif arguments[1:3] == ["terminal", "send"]:
            payload = {"ok": True, "result": {"send": {"accepted": True}}}
        else:
            payload = {"ok": True, "result": {"worktree": {"id": "owned", "path": "/tmp/owned"}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    started = OrcaClient("orca-ide", runner=runner, which=lambda name: f"/tmp/{name}").start(
        tmp_path, "example", "single", "codex", "Prompt"
    )
    assert started.worktree_id == "owned"
    assert started.worktree == "/tmp/owned"
    assert started.terminal == "agent"


def test_start_rejects_legacy_top_level_worktree_fields(tmp_path: Path) -> None:
    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        payload = {
            "ok": True,
            "result": {
                "worktreeId": "owned",
                "id": "owned",
                "worktreePath": "/tmp/owned",
                "agentTerminalHandle": "terminal",
                "handle": "terminal",
            },
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="missing worktree"):
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )
    payload = {
        "ok": True,
        "result": {
            "worktree": {"id": "owned", "name": "example", "path": "/tmp/owned"},
            "startupTerminal": {"handle": "terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        error = subprocess.TimeoutExpired(arguments, 60)
        error.stdout = json.dumps(payload)
        raise error

    with pytest.raises(OrcaStartError, match="timed out after allocation") as error:
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )

    assert error.value.worktree_id == "owned"
    assert error.value.worktree_path == "/tmp/owned"
    assert error.value.terminal_handle == "terminal"


def test_start_never_owns_a_foreign_same_name_worktree_after_timeout(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    foreign = {
        "ok": True,
        "result": {
            "worktree": {"id": "foreign", "path": "/tmp/foreign"},
            "startupTerminal": {"handle": "foreign-terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "create"]:
            raise subprocess.TimeoutExpired(arguments, 60)
        if len(calls) == 1:
            return _missing_selector(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(foreign), "")

    with pytest.raises(OrcaTimeoutError) as error:
        OrcaClient("orca-ide", runner=runner).start(tmp_path, "unique", "single", "codex", "Prompt")

    assert not isinstance(error.value, OrcaStartError)
    assert not hasattr(error.value, "worktree_id")
    assert error.value.possible_orphan_id == "foreign"
    assert error.value.possible_orphan_path == "/tmp/foreign"
    assert "cannot prove it created that worktree" in str(error.value)
    assert len(calls) == 3
    assert not any(argument[1:3] == ["worktree", "rm"] for argument in calls)


def test_start_ignores_a_partial_response_for_a_different_worktree_name(tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "result": {
            "worktree": {"id": "other", "name": "other-name", "path": "/tmp/other"},
            "startupTerminal": {"handle": "terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        error = subprocess.TimeoutExpired(arguments, 60)
        error.stdout = json.dumps(payload)
        raise error

    with pytest.raises(OrcaTimeoutError) as error:
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )

    assert not isinstance(error.value, OrcaStartError)
    assert error.value.possible_orphan_id == ""
    assert "no worktree named example was visible" in str(error.value)


def test_start_preserves_exact_ownership_receipt_on_keyboard_interrupt(tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "result": {
            "worktree": {"id": "owned", "name": "example", "path": "/tmp/owned"},
            "startupTerminal": {"handle": "terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        error = KeyboardInterrupt()
        error.stdout = json.dumps(payload)
        raise error

    with pytest.raises(KeyboardInterrupt) as error:
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )

    assert error.value.worktree_id == "owned"
    assert error.value.worktree_path == "/tmp/owned"
    assert error.value.terminal_handle == "terminal"


def test_start_preserves_allocation_from_success_json_with_nonzero_exit(tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "result": {
            "worktree": {"id": "owned", "name": "example", "path": "/tmp/owned"},
            "startupTerminal": {"handle": "terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "late failure")

    with pytest.raises(OrcaError, match="late failure") as error:
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )

    assert error.value.worktree_id == "owned"
    assert error.value.worktree_path == "/tmp/owned"
    assert error.value.terminal_handle == "terminal"


def test_start_ignores_a_partial_response_without_the_requested_name(tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "result": {
            "worktree": {"id": "unknown", "path": "/tmp/unknown"},
            "startupTerminal": {"handle": "terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        error = subprocess.TimeoutExpired(arguments, 60)
        error.stdout = json.dumps(payload)
        raise error

    with pytest.raises(OrcaTimeoutError) as error:
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )

    assert not isinstance(error.value, OrcaStartError)
    assert "without an ownership receipt" in str(error.value)


def test_start_compensates_an_exact_partial_response_without_a_name_lookup(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    payload = {
        "ok": True,
        "result": {
            "worktree": {"id": "owned", "name": "example", "path": "/tmp/owned"},
            "startupTerminal": {"handle": "terminal"},
        },
    }

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        error = subprocess.TimeoutExpired(arguments, 60)
        error.stdout = json.dumps(payload)
        raise error

    client = OrcaClient("orca-ide", runner=runner)
    with pytest.raises(OrcaStartError) as error:
        client.start(tmp_path, "example", "single", "codex", "Prompt")

    assert error.value.worktree_id == "owned"
    assert error.value.worktree_path == "/tmp/owned"
    assert error.value.terminal_handle == "terminal"
    assert len(calls) == 2

    def removal_runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": {"removed": True}}), ""
        )

    assert OrcaClient("orca-ide", runner=removal_runner).remove_worktree(
        error.value.worktree_id
    ) == {"removed": True}
    assert calls[-1] == [
        "orca-ide",
        "worktree",
        "rm",
        "--worktree",
        "id:owned",
        "--force",
        "--json",
    ]


def test_start_repeats_timeout_name_lookup_with_a_fixed_bound(tmp_path: Path) -> None:
    calls = 0

    def runner(arguments, **_kwargs):
        nonlocal calls
        calls += 1
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        raise subprocess.TimeoutExpired(arguments, 60)

    with pytest.raises(OrcaTimeoutError):
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "durable-workflow-name", "single", "codex", "Prompt"
        )

    assert calls == 5


def test_start_treats_structured_selector_miss_as_available_name(tmp_path: Path) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": "selector_not_found",
            "message": "selector_not_found",
        },
    }
    created = {
        "ok": True,
        "result": {
            "worktree": {"id": "repo::/tmp/new", "path": "/tmp/new"},
            "startupTerminal": {"handle": "terminal-1"},
        },
    }

    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "")
        if arguments[1:3] == ["terminal", "create"]:
            result = {"terminal": {"handle": "agent"}}
        elif arguments[1:3] == ["terminal", "wait"]:
            result = {"wait": {"satisfied": True}}
        elif arguments[1:3] == ["terminal", "send"]:
            result = {"send": {"accepted": True}}
        else:
            return subprocess.CompletedProcess(arguments, 0, json.dumps(created), "")
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    started = OrcaClient(
        "orca-ide", runner=runner, which=lambda name: "/tmp/codex" if name == "codex" else None
    ).start(tmp_path, "fresh", "single", "codex", "Prompt")

    assert started.worktree_id == "repo::/tmp/new"


def test_start_rejects_an_existing_orca_worktree_name(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    payload = {
        "ok": True,
        "result": {"worktree": {"id": "existing", "path": "/tmp/existing"}},
    }

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="name already exists"):
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "existing", "single", "codex", "Prompt"
        )

    assert len(calls) == 1


def test_closing_an_already_absent_terminal_is_idempotent() -> None:
    def runner(arguments, **_kwargs):
        payload = {
            "ok": False,
            "error": {"code": "selector_not_found", "message": "selector_not_found"},
        }
        return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "")

    assert OrcaClient("orca-ide", runner=runner).close_terminals("owned", "terminal") == {
        "already_closed": True
    }


def test_observer_requires_and_returns_an_owned_terminal_handle() -> None:
    def runner(arguments, **_kwargs):
        payload = {"ok": True, "result": {"terminal": {"handle": "observer-1"}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    assert (
        OrcaClient("orca-ide", runner=runner).create_observer("owned", "flybridge queue watch")
        == "observer-1"
    )


def test_verify_requires_reachable_runtime_and_orchestration_guide() -> None:
    def runner(arguments, **_kwargs):
        if arguments[1:2] == ["status"]:
            payload = {"ok": True, "result": {"runtime": {"reachable": True}}}
        else:
            payload = {"ok": True, "result": {"name": "orchestration", "full": True}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    assert OrcaClient("orca-ide", runner=runner).verify() == {
        "app_version": "",
        "skill": "orchestration",
    }


def test_repository_preparation_only_updates_declared_submodules(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    envs: list[dict[str, str] | None] = []

    def runner(arguments, **kwargs):
        calls.append(arguments)
        envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    client = OrcaClient("orca-ide", runner=runner)
    client.prepare_repository(tmp_path)
    assert calls == []
    (tmp_path / ".gitmodules").write_text('[submodule "example"]', encoding="utf-8")
    client.prepare_repository(tmp_path)
    assert calls[0][-7:] == [
        "-c",
        "core.hooksPath=/dev/null",
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--checkout",
    ]
    assert envs[0] is not None
    assert envs[0]["GIT_LFS_SKIP_SMUDGE"] == "1"


def test_repository_preparation_timeout_reports_dirty_worktree(tmp_path: Path) -> None:
    (tmp_path / ".gitmodules").write_text('[submodule "example"]', encoding="utf-8")
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["-C", str(tmp_path)] and "submodule" in arguments:
            raise subprocess.TimeoutExpired(arguments, 120)
        if arguments[-1] == "--porcelain":
            return subprocess.CompletedProcess(arguments, 0, " M external/dep\n", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    client = OrcaClient("orca-ide", runner=runner)
    with pytest.raises(OrcaError, match="timed out.*dirty: M external/dep"):
        client.prepare_repository(tmp_path)
    assert any("submodule" in call for call in calls)


def test_repository_registration_uses_the_resolved_path(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"ok": True, "result": {}}), "")

    OrcaClient("orca-ide", runner=runner).register_repository(tmp_path)

    assert calls == [["orca-ide", "repo", "add", "--path", str(tmp_path.resolve()), "--json"]]


def test_remove_worktree_uses_only_the_exact_persisted_reference() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"ok": True, "result": {}}), "")

    OrcaClient("orca-ide", runner=runner).remove_worktree("repo::/tmp/owned")

    assert calls[0] == [
        "orca-ide",
        "worktree",
        "rm",
        "--worktree",
        "id:repo::/tmp/owned",
        "--force",
        "--json",
    ]


def test_resume_operations_use_exact_worktree_and_replace_a_stale_terminal() -> None:
    calls: list[list[str]] = []
    wait_count = 0

    def runner(arguments, **_kwargs):
        nonlocal wait_count
        calls.append(arguments)
        operation = arguments[1:3]
        if operation == ["worktree", "show"]:
            payload = {
                "ok": True,
                "result": {"worktree": {"id": "repo::/tmp/owned", "path": "/tmp/owned"}},
            }
        elif operation == ["terminal", "show"]:
            payload = {
                "ok": False,
                "error": {"code": "terminal_handle_stale", "message": "stale"},
            }
        elif operation == ["terminal", "create"]:
            payload = {"ok": True, "result": {"terminal": {"handle": "agent-new"}}}
        elif operation == ["terminal", "wait"]:
            wait_count += 1
            payload = {
                "ok": True,
                "result": {"wait": {"satisfied": wait_count == 2}},
            }
        else:
            payload = {"ok": True, "result": {"send": {"accepted": True}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    client = OrcaClient("orca-ide", runner=runner, which=lambda name: f"/tmp/{name}")
    client.verify_worktree("repo::/tmp/owned", "/tmp/owned")
    assert client.terminal_is_valid("repo::/tmp/owned", "agent-old") is False
    replacement = client.create_agent_terminal("repo::/tmp/owned", "codex")
    client.wait_for_agent(replacement)
    client.send_prompt(replacement, "Resume the objective.")

    assert replacement == "agent-new"
    assert not any(call[1:3] == ["worktree", "create"] for call in calls)
    assert calls[0][1:5] == [
        "worktree",
        "show",
        "--worktree",
        "id:repo::/tmp/owned",
    ]
    assert [call[call.index("--timeout-ms") + 1] for call in calls if "--timeout-ms" in call] == [
        "60000",
        "120000",
    ]
    assert calls[-1][-2:] == ["--enter", "--json"]


def test_terminal_is_invalid_when_orca_reports_it_disconnected() -> None:
    def runner(arguments, **_kwargs):
        payload = {
            "ok": True,
            "result": {"terminal": {"handle": "agent-old", "connected": False}},
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    assert OrcaClient("orca-ide", runner=runner).terminal_is_valid("owned", "agent-old") is False


def test_resume_reports_a_missing_persisted_worktree_without_terminal_creation() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        payload = {
            "ok": False,
            "error": {"code": "selector_not_found", "message": "worktree not found"},
        }
        return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="worktree not found"):
        OrcaClient("orca-ide", runner=runner).verify_worktree("repo::/tmp/missing", "/tmp/missing")

    assert len(calls) == 1
    assert calls[0][1:3] == ["worktree", "show"]


def test_resume_requires_tui_idle_and_prompt_acceptance_signals() -> None:
    def unsatisfied_runner(arguments, **_kwargs):
        payload = {"ok": True, "result": {"wait": {"satisfied": False}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaTimeoutError, match="TUI-idle"):
        OrcaClient("orca-ide", runner=unsatisfied_runner).wait_for_agent("agent")

    def unaccepted_runner(arguments, **_kwargs):
        payload = {"ok": True, "result": {"send": {"accepted": False}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="did not accept"):
        OrcaClient("orca-ide", runner=unaccepted_runner).send_prompt("agent", "Resume.")


def test_authentication_not_found_is_not_treated_as_a_missing_selector() -> None:
    def runner(arguments, **_kwargs):
        payload = {
            "ok": False,
            "error": {"code": "auth_failed", "message": "authentication token not found"},
        }
        return subprocess.CompletedProcess(arguments, 1, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="authentication token not found"):
        OrcaClient("orca-ide", runner=runner).close_terminals("owned", "terminal")


def test_start_passes_parent_worktree_and_base_branch(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    created = {
        "ok": True,
        "result": {
            "worktree": {"id": "child", "path": "/tmp/child"},
            "startupTerminal": {"handle": "term"},
        },
    }

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        if arguments[1:3] == ["terminal", "create"]:
            result = {"terminal": {"handle": "agent"}}
        elif arguments[1:3] == ["terminal", "wait"]:
            result = {"wait": {"satisfied": True}}
        elif arguments[1:3] == ["terminal", "send"]:
            result = {"send": {"accepted": True}}
        else:
            result = created["result"]
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    OrcaClient(
        "orca-ide", runner=runner, which=lambda name: "/tmp/codex" if name == "codex" else None
    ).start(
        tmp_path,
        "child",
        "orchestrated",
        "codex",
        "Prompt",
        parent_worktree_id="canonical-repo::/tmp/parent",
        base_branch="feature",
    )

    create = calls[1]
    assert create[create.index("--repo") + 1] == "id:canonical-repo"
    assert "--parent-worktree" in create
    assert "id:canonical-repo::/tmp/parent" in create
    assert "--base-branch" in create
    assert "feature" in create


def test_child_start_rejects_a_parent_without_canonical_repository_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(OrcaError, match="canonical repository ID"):
        OrcaClient("orca-ide").start(
            tmp_path,
            "child",
            "orchestrated",
            "codex",
            "Prompt",
            parent_worktree_id="path-only-id",
            base_branch="feature",
        )


def test_implementation_identity_separates_github_and_runtime_ids_and_verifies_ancestry() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        output = (
            "git@github.com:Example/Implementation.git\n" if "remote" in arguments else "abc123\n"
        )
        return subprocess.CompletedProcess(arguments, 0, output, "")

    client = OrcaClient("orca-ide", runner=runner)

    assert client.implementation_identity(
        "github:example/implementation::/tmp/worktree", "/tmp/worktree"
    ) == ("Example/Implementation", "github:example/implementation", "abc123")
    client.verify_implementation_identity(
        "github:example/implementation::/tmp/worktree",
        "/tmp/worktree",
        "Example/Implementation",
        "github:example/implementation",
        "abc123",
    )

    assert calls == [
        ["git", "-C", "/tmp/worktree", "remote", "get-url", "origin"],
        ["git", "-C", "/tmp/worktree", "rev-parse", "--verify", "HEAD"],
        ["git", "-C", "/tmp/worktree", "remote", "get-url", "origin"],
        [
            "git",
            "-C",
            "/tmp/worktree",
            "merge-base",
            "--is-ancestor",
            "abc123",
            "HEAD",
        ],
    ]

    with pytest.raises(OrcaError, match="does not match"):
        client.verify_implementation_identity(
            "github:example/other::/tmp/worktree",
            "/tmp/worktree",
            "Example/Implementation",
            "github:example/implementation",
            "abc123",
        )


def test_implementation_worktree_present_requires_a_git_worktree() -> None:
    def present_runner(arguments, **_kwargs):
        assert arguments[:3] == ["git", "-C", "/tmp/worktree"]
        return subprocess.CompletedProcess(arguments, 0, "true\n", "")

    assert OrcaClient("orca-ide", runner=present_runner).implementation_worktree_present(
        "/tmp/worktree"
    )

    def missing_runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 128, "", "no such file")

    assert not OrcaClient("orca-ide", runner=missing_runner).implementation_worktree_present(
        "/tmp/gone"
    )


def test_uncommitted_changes_reports_only_git_visible_entries() -> None:
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, " M packages/core/config.py\n?? tmp/report.md\n\n", ""
        )

    client = OrcaClient("orca-ide", runner=runner)

    assert client.uncommitted_changes("/tmp/worker") == (
        "M packages/core/config.py",
        "?? tmp/report.md",
    )
    assert calls[0] == ["git", "-C", "/tmp/worker", "status", "--porcelain"]

    def clean_runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, "", "")

    assert OrcaClient("orca-ide", runner=clean_runner).uncommitted_changes("/tmp/worker") == ()

    def failing_runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 128, "", "not a git repository")

    with pytest.raises(OrcaError, match="not a git repository"):
        OrcaClient("orca-ide", runner=failing_runner).uncommitted_changes("/tmp/worker")


def test_verify_pristine_start_requires_exact_head_and_clean_worktree() -> None:
    outputs = iter(("abc123\n", ""))

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, next(outputs), "")

    OrcaClient("orca-ide", runner=runner).verify_pristine_start("/tmp/worktree", "abc123")

    dirty_outputs = iter(("abc123\n", "?? status.md\n"))

    def dirty_runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, next(dirty_outputs), "")

    with pytest.raises(OrcaError, match="not clean"):
        OrcaClient("orca-ide", runner=dirty_runner).verify_pristine_start("/tmp/worktree", "abc123")


def test_verify_rejects_an_unreachable_runtime() -> None:
    def runner(arguments, **_kwargs):
        payload = {"ok": True, "result": {"runtime": {"reachable": False}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="not reachable"):
        OrcaClient("orca-ide", runner=runner).verify()


def test_attach_uses_existing_worktree_and_never_creates_one(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    repository = tmp_path / "existing"
    repository.mkdir()

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            payload = {
                "ok": True,
                "result": {
                    "worktree": {
                        "id": "repo::" + str(repository),
                        "path": str(repository),
                    }
                },
            }
        elif arguments[1:3] == ["terminal", "create"]:
            payload = {"ok": True, "result": {"terminal": {"handle": "agent-1"}}}
        elif arguments[1:3] == ["terminal", "wait"]:
            payload = {"ok": True, "result": {"wait": {"satisfied": True}}}
        elif arguments[1:3] == ["terminal", "send"]:
            payload = {"ok": True, "result": {"send": {"accepted": True}}}
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    started = OrcaClient(
        "orca-ide",
        runner=runner,
        which=lambda name: "/tmp/fake/cursor-agent" if name == "cursor-agent" else None,
    ).attach(repository, "existing", "single", "cursor", "Continue the PR.")

    assert started.worktree_id == "repo::" + str(repository)
    assert started.owns_worktree is False
    assert [call[1:3] for call in calls] == [
        ["worktree", "show"],
        ["terminal", "create"],
        ["terminal", "wait"],
        ["terminal", "send"],
    ]
    assert "worktree" not in {
        call[2] for call in calls if call[1] == "worktree" and call[2] != "show"
    }
    assert all(call[2] != "create" or call[1] != "worktree" for call in calls)
    create = next(call for call in calls if call[1:3] == ["terminal", "create"])
    assert create[create.index("--command") + 1] == shlex.join(
        [str(Path("/tmp/fake/cursor-agent").resolve()), "--trust", "--yolo"]
    )


def test_start_launches_cursor_with_explicit_trust_flags(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\n")

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        if arguments[1:3] == ["worktree", "create"]:
            result = {"worktree": {"id": "repo::/tmp/worktree", "path": "/tmp/worktree"}}
        elif arguments[1:3] == ["terminal", "create"]:
            result = {"terminal": {"handle": "terminal-1"}}
        elif arguments[1:3] == ["terminal", "wait"]:
            result = {"wait": {"satisfied": True}}
        else:
            result = {"send": {"accepted": True}}
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    OrcaClient(
        "orca-ide",
        runner=runner,
        which=lambda name: str(binary) if name == "cursor-agent" else None,
    ).start(tmp_path, "example", "single", "cursor", "Implement the change.", model="auto")

    create = next(call for call in calls if call[1:3] == ["worktree", "create"])
    assert "--agent" not in create
    assert "--prompt" not in create
    terminal = next(call for call in calls if call[1:3] == ["terminal", "create"])
    assert terminal[terminal.index("--command") + 1] == shlex.join(
        [str(binary.resolve()), "--trust", "--yolo", "--model", "auto"]
    )
    assert any(call[1:3] == ["terminal", "wait"] for call in calls)
    assert any(call[1:3] == ["terminal", "send"] for call in calls)


def test_create_agent_terminal_resolves_cursor_to_an_absolute_cli(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\n")

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        payload = {"ok": True, "result": {"terminal": {"handle": "term-1"}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    OrcaClient(
        "orca-ide",
        runner=runner,
        which=lambda name: str(binary) if name == "cursor-agent" else None,
    ).create_agent_terminal("repo::/tmp/worktree", "cursor")

    create = calls[0]
    assert create[1:3] == ["terminal", "create"]
    assert create[create.index("--command") + 1] == shlex.join(
        [str(binary.resolve()), "--trust", "--yolo"]
    )


def test_create_new_agent_terminal_resolves_cursor_then_waits_and_sends(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\n")

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["terminal", "create"]:
            result = {"terminal": {"handle": "new-agent"}}
        elif arguments[1:3] == ["terminal", "wait"]:
            result = {"wait": {"satisfied": True}}
        else:
            result = {"send": {"accepted": True}}
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "result": result}), ""
        )

    handle = OrcaClient(
        "orca-ide",
        runner=runner,
        which=lambda name: str(binary) if name == "agent" else None,
    ).create_new_agent_terminal(
        "repo::/tmp/worktree", str(tmp_path), "cursor", "Continue the work."
    )

    assert handle == "new-agent"
    assert calls[0][calls[0].index("--command") + 1] == shlex.join(
        [str(binary.resolve()), "--trust", "--yolo"]
    )
    assert calls[1][1:3] == ["terminal", "wait"]
    assert calls[2][1:3] == ["terminal", "send"]


def test_create_agent_terminal_rejects_an_unresolved_cursor_id() -> None:
    with pytest.raises(OrcaError, match="TUI id cursor has no CLI binary"):
        OrcaClient("orca-ide", which=lambda _name: None).create_agent_terminal(
            "repo::/tmp/worktree", "cursor"
        )


def test_start_with_cursor_model_uses_terminal_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\n")

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        if arguments[1:3] == ["worktree", "create"]:
            payload = {
                "ok": True,
                "result": {"worktree": {"id": "repo::/tmp/worktree", "path": "/tmp/worktree"}},
            }
        elif arguments[1:3] == ["terminal", "create"]:
            payload = {"ok": True, "result": {"terminal": {"handle": "model-term"}}}
        elif arguments[1:3] == ["terminal", "wait"]:
            payload = {"ok": True, "result": {"wait": {"satisfied": True}}}
        else:
            payload = {"ok": True, "result": {"send": {"accepted": True}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    started = OrcaClient(
        "orca-ide",
        runner=runner,
        which=lambda name: str(binary) if name == "cursor-agent" else None,
    ).start(
        tmp_path,
        "example",
        "single",
        "cursor",
        "Review the change.",
        model="claude-opus-5-low",
    )

    create = next(call for call in calls if call[1:3] == ["worktree", "create"])
    command = next(call for call in calls if call[1:3] == ["terminal", "create"])
    assert "--agent" not in create
    assert started.terminal == "model-term"
    assert command[command.index("--command") + 1] == shlex.join(
        [
            str(binary.resolve()),
            "--trust",
            "--yolo",
            "--model",
            "claude-opus-5-low",
        ]
    )


def test_start_with_ollama_runs_the_named_model(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    binary = tmp_path / "ollama"
    binary.write_text("#!/bin/sh\n")

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        if arguments[1:3] == ["worktree", "create"]:
            payload = {
                "ok": True,
                "result": {"worktree": {"id": "repo::/tmp/worktree", "path": "/tmp/worktree"}},
            }
        elif arguments[1:3] == ["terminal", "create"]:
            payload = {"ok": True, "result": {"terminal": {"handle": "ollama-term"}}}
        elif arguments[1:3] == ["terminal", "wait"]:
            payload = {"ok": True, "result": {"wait": {"satisfied": True}}}
        else:
            payload = {"ok": True, "result": {"send": {"accepted": True}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    OrcaClient(
        "orca-ide",
        runner=runner,
        which=lambda name: str(binary) if name == "ollama" else None,
    ).start(
        tmp_path,
        "example",
        "orchestrated",
        "ollama",
        "Review the change.",
        role="reviewer",
        model="gemma4:26b",
    )

    command = next(call for call in calls if call[1:3] == ["terminal", "create"])
    assert command[command.index("--command") + 1] == shlex.join(
        [str(binary.resolve()), "run", "gemma4:26b"]
    )


def test_run_kills_a_timed_out_process_group() -> None:
    client = OrcaClient(__import__("sys").executable)
    command = "import time; print('receipt', flush=True); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as error:
        client._run([client.executable, "-c", command], timeout=0.3)

    assert error.value.stdout == "receipt\n"


def test_list_worktrees_normalizes_ps_rows() -> None:
    payload = {
        "ok": True,
        "result": {
            "truncated": False,
            "worktrees": [
                {
                    "worktreeId": "repo::/tmp/a",
                    "path": "/tmp/a",
                    "displayName": "alpha",
                    "workspaceStatus": "in-progress",
                    "comment": "note",
                    "branch": "refs/heads/feature",
                    "linkedIssue": 12,
                    "linkedPR": {"number": 34, "state": "open"},
                    "projectId": "github:example/repo",
                }
            ],
        },
    }

    def runner(arguments, **_kwargs):
        assert arguments[1:3] == ["worktree", "ps"]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    worktrees, truncated = OrcaClient("orca-ide", runner=runner).list_worktrees()

    assert truncated is False
    assert worktrees[0].worktree_id == "repo::/tmp/a"
    assert worktrees[0].name == "alpha"
    assert worktrees[0].linked_issue == 12
    assert worktrees[0].linked_pull_request == (34, "open")
    assert worktrees[0].project_id == "github:example/repo"


def test_list_worktrees_rejects_a_missing_path() -> None:
    payload = {"ok": True, "result": {"worktrees": [{"worktreeId": "repo::/tmp/a"}]}}

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="missing path"):
        OrcaClient("orca-ide", runner=runner).list_worktrees()


def _git_args(arguments: list[str]) -> tuple[str, list[str]]:
    assert arguments[0:2] == ["git", "-C"]
    return arguments[2], arguments[3:]


def test_integrate_worker_commit_fast_forwards_when_manager_is_ancestor(monkeypatch) -> None:
    manager_head = "aaa111"
    worker_sha = "bbb222"
    calls: list[list[str]] = []
    prepared: list[Path] = []
    monkeypatch.setattr(
        OrcaClient,
        "prepare_repository",
        lambda _self, path: prepared.append(path),
    )

    def runner(arguments, **_kwargs):
        nonlocal manager_head
        calls.append(arguments)
        path, git_args = _git_args(arguments)
        if git_args[:2] == ["rev-parse", "--verify"]:
            sha = worker_sha if path == "/tmp/worker" else manager_head
            return subprocess.CompletedProcess(arguments, 0, sha + "\n", "")
        if git_args[:2] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if git_args[0] == "merge-base":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if git_args[0] == "fetch":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if git_args[:2] == ["merge", "--ff-only"]:
            manager_head = worker_sha
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    result = OrcaClient("orca-ide", runner=runner).integrate_worker_commit(
        "/tmp/manager", "/tmp/worker", worker_sha
    )

    assert result == {"method": "ff", "before": "aaa111", "after": worker_sha}
    assert any(call[3] == "fetch" for call in calls)
    assert any(call[3:5] == ["merge", "--ff-only"] for call in calls)
    assert prepared == [Path("/tmp/manager")]


def test_integrate_worker_commit_is_noop_when_already_integrated(monkeypatch) -> None:
    calls: list[list[str]] = []
    prepared: list[Path] = []
    monkeypatch.setattr(
        OrcaClient,
        "prepare_repository",
        lambda _self, path: prepared.append(path),
    )

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        _path, git_args = _git_args(arguments)
        if git_args[:2] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(arguments, 0, "same123\n", "")
        if git_args[:2] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    result = OrcaClient("orca-ide", runner=runner).integrate_worker_commit(
        "/tmp/manager", "/tmp/worker", "same123"
    )

    assert result == {"method": "already", "before": "same123", "after": "same123"}
    assert all(call[3] != "fetch" for call in calls)
    assert prepared == [Path("/tmp/manager")]


def test_integrate_worker_commit_rejects_a_dirty_manager() -> None:
    def runner(arguments, **_kwargs):
        path, git_args = _git_args(arguments)
        if git_args[:2] == ["rev-parse", "--verify"]:
            sha = "bbb" if path == "/tmp/worker" else "aaa"
            return subprocess.CompletedProcess(arguments, 0, sha + "\n", "")
        if git_args[:2] == ["status", "--porcelain"]:
            output = " M dirty.py\n" if path == "/tmp/manager" else ""
            return subprocess.CompletedProcess(arguments, 0, output, "")
        raise AssertionError(arguments)

    with pytest.raises(OrcaError, match="manager worktree is not clean"):
        OrcaClient("orca-ide", runner=runner).integrate_worker_commit(
            "/tmp/manager", "/tmp/worker", "bbb"
        )


def test_integrate_worker_commit_aborts_a_merge_conflict_without_changing_head() -> None:
    manager_head = "aaa111"
    aborted = False

    def runner(arguments, **_kwargs):
        nonlocal aborted
        path, git_args = _git_args(arguments)
        if git_args[:2] == ["rev-parse", "--verify"]:
            sha = "ccc333" if path == "/tmp/worker" else manager_head
            return subprocess.CompletedProcess(arguments, 0, sha + "\n", "")
        if git_args[:2] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if git_args[0] == "merge-base":
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if git_args[0] == "fetch":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if git_args[:2] == ["merge", "--no-edit"]:
            return subprocess.CompletedProcess(arguments, 1, "", "conflict")
        if git_args[:2] == ["merge", "--abort"]:
            aborted = True
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    with pytest.raises(OrcaError, match="conflict"):
        OrcaClient("orca-ide", runner=runner).integrate_worker_commit(
            "/tmp/manager", "/tmp/worker", "ccc333"
        )

    assert aborted is True
    assert manager_head == "aaa111"
