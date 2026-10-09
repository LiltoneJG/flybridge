from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from flybridge_orca.client import OrcaClient
from flybridge_orca.resolve import resolve_terminal_command


def test_unmocked_orca_status_never_launches():
    with pytest.raises(RuntimeError, match="explicit mock Orca runner required"):
        OrcaClient("orca-ide").verify()


def test_default_agent_path_resolution_uses_temporary_stub(tmp_path):
    resolved = Path(resolve_terminal_command("codex"))
    assert resolved == tmp_path / "mock-agent-bin" / "codex"


def test_actual_agent_subprocess_is_rejected_before_spawn():
    with pytest.raises(AssertionError, match="subprocess launch forbidden"):
        subprocess.Popen(["codex", "--version"])
