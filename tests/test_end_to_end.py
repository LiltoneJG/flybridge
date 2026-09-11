from __future__ import annotations

import json
from pathlib import Path

from conftest import write_config
from flybridge_cli.main import main
from flybridge_core import ResourceQueue, WorkflowStore


def _json_output(capsys) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _running_owner(tmp_path: Path, state_dir: Path, name: str) -> str:
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", name, "Test queue sharing.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "running")
    return workflow.id


def test_independent_cli_callers_share_fifo_queue_state(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir)
    first_id = _running_owner(tmp_path, state_dir, "cli-owner-one")
    second_id = _running_owner(tmp_path, state_dir, "cli-owner-two")

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "acquire",
                "exclusive",
                "--owner",
                first_id,
            ]
        )
        == 0
    )
    first_lease = _json_output(capsys)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "acquire",
                "exclusive",
                "--owner",
                second_id,
            ]
        )
        == 0
    )
    second = _json_output(capsys)
    assert first_lease["granted"] is True
    assert second["granted"] is False

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "inspect",
                str(second["request_id"]),
                "--owner",
                second_id,
            ]
        )
        == 0
    )
    before = _json_output(capsys)
    assert before["status"] == "waiting"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "inspect",
                str(second["request_id"]),
                "--owner",
                "wrong-owner",
            ]
        )
        == 2
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "release",
                "exclusive",
                "--lease",
                str(first_lease["lease_id"]),
                "--owner",
                first_id,
            ]
        )
        == 0
    )
    promoted = _json_output(capsys)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "inspect",
                str(second["request_id"]),
                "--owner",
                second_id,
            ]
        )
        == 0
    )
    after = _json_output(capsys)

    assert promoted == {"next_lease_id": second["request_id"]}
    assert after["status"] == "leased"
    assert ResourceQueue(state_dir).status("exclusive") == [
        {"resource": "exclusive", "status": "leased", "count": 1},
        {"resource": "exclusive", "status": "released", "count": 1},
    ]
