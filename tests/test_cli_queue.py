from __future__ import annotations

from pathlib import Path

from conftest import write_config
from flybridge_cli.main import main


def test_queue_status_dispatches_through_the_public_entrypoint(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    assert main(["--config", str(config_path), "queue", "status"]) == 0
    assert capsys.readouterr().out == "[]\n"
