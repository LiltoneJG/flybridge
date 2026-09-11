from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from flybridge_orca.client import OrcaClient, OrcaError, OrcaStartError, OrcaTimeoutError


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
            return _missing_selector(arguments)
        else:
            response = {"ok": True, "result": {"updated": True}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    started = OrcaClient("orca-ide", runner=runner).start(
        tmp_path, "example", "single", "codex", "Implement the change."
    )

    assert started.worktree_id == "repo::/tmp/worktree"
    assert started.terminal == "terminal-1"
    assert calls[0][1:3] == ["worktree", "show"]
    assert calls[1][:3] == ["orca-ide", "worktree", "create"]
    assert "--no-parent" in calls[1]
    assert len(calls) == 2

    OrcaClient("orca-ide", runner=runner).set_lifecycle(started.worktree_id, "running")

    assert calls[2] == [
        "orca-ide",
        "worktree",
        "set",
        "--worktree",
        "id:repo::/tmp/worktree",
        "--comment",
        "Flybridge workflow is running.",
        "--workspace-status",
        "in-progress",
        "--json",
    ]


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
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"ok": True, "result": {}}), "")

    OrcaClient("orca-ide", runner=runner).set_lifecycle("repo::/tmp/worktree", state)

    assert calls == [
        [
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
    ]


def test_client_rejects_invalid_orca_response() -> None:
    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, "not-json", "")

    with pytest.raises(OrcaError, match="invalid JSON"):
        OrcaClient("orca-ide", runner=runner).close_terminals("repo::/tmp/worktree", "terminal-1")


def test_start_rejects_worktree_without_owned_startup_terminal(tmp_path: Path) -> None:
    def runner(arguments, **_kwargs):
        if arguments[1:3] == ["worktree", "show"]:
            return _missing_selector(arguments)
        payload = {"ok": True, "result": {"worktree": {"id": "owned", "path": "/tmp/owned"}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="startup terminal") as error:
        OrcaClient("orca-ide", runner=runner).start(
            tmp_path, "example", "single", "codex", "Prompt"
        )
    assert error.value.worktree_id == "owned"
    assert error.value.worktree_path == "/tmp/owned"


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
        return subprocess.CompletedProcess(arguments, 0, json.dumps(created), "")

    started = OrcaClient("orca-ide", runner=runner).start(
        tmp_path, "fresh", "single", "codex", "Prompt"
    )

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

    def runner(arguments, **_kwargs):
        calls.append(arguments)
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

    client = OrcaClient("orca-ide", runner=runner)
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
    assert calls[-1][-3:] == ["--wait-submit", "10", "--json"]


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
        return subprocess.CompletedProcess(arguments, 0, json.dumps(created), "")

    OrcaClient("orca-ide", runner=runner).start(
        tmp_path,
        "child",
        "orchestrated",
        "codex",
        "Prompt",
        parent_worktree_id="parent",
        base_branch="feature",
    )

    create = calls[1]
    assert "--parent-worktree" in create
    assert "id:parent" in create
    assert "--base-branch" in create
    assert "feature" in create


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


def test_verify_rejects_an_unreachable_runtime() -> None:
    def runner(arguments, **_kwargs):
        payload = {"ok": True, "result": {"runtime": {"reachable": False}}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(OrcaError, match="not reachable"):
        OrcaClient("orca-ide", runner=runner).verify()


def test_run_kills_a_timed_out_process_group() -> None:
    client = OrcaClient(__import__("sys").executable)
    command = "import time; print('receipt', flush=True); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as error:
        client._run([client.executable, "-c", command], timeout=0.3)

    assert error.value.stdout == "receipt\n"
