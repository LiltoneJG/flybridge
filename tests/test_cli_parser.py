from __future__ import annotations

import pytest
from flybridge_cli.parser import build_parser


def test_parser_keeps_canonical_top_level_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["workflow", "list"]).command == "workflow"
    assert parser.parse_args(["queue", "status"]).command == "queue"
    assert parser.parse_args(["board", "screen"]).command == "board"
    assert parser.parse_args(["inventory"]).command == "inventory"
    parsed = parser.parse_args(
        ["inventory", "--path-prefix", "/tmp/a", "--exclude-prefix", "/tmp/b", "--no-github"]
    )
    assert parsed.path_prefixes == ["/tmp/a"]
    assert parsed.exclude_prefixes == ["/tmp/b"]
    assert parsed.no_github is True
    assert parser.parse_args(["inventory", "--with-review-facts"]).with_review_facts is True
    named = parser.parse_args(
        ["inventory", "--exclude-name", "vendor-checkout", "--exclude-name", "other-workspace"]
    )
    assert named.exclude_names == ["vendor-checkout", "other-workspace"]
    board = parser.parse_args(
        [
            "board",
            "screen",
            "--with-refs",
            "--path-prefix",
            "/tmp/orca",
            "--exclude-name",
            "vendor-checkout",
        ]
    )
    assert board.path_prefixes == ["/tmp/orca"]
    assert board.exclude_names == ["vendor-checkout"]
    assert parser.parse_args(["board", "screen", "--with-refs"]).with_refs is True
    prs = parser.parse_args(
        ["prs", "screen", "--author", "bob", "--state", "MERGED", "--with-review-facts"]
    )
    assert prs.command == "prs"
    assert prs.author == "bob"
    assert prs.states == ["MERGED"]
    assert prs.with_review_facts is True
    assert parser.parse_args(
        ["workflow", "start", ".", "--issue", "https://github.com/e/r/issues/1"]
    ).issue.endswith("/issues/1")
    artifact = parser.parse_args(
        [
            "workflow",
            "artifact",
            "put",
            "workflow-1",
            "--kind",
            "plan",
            "--stdin",
        ]
    )
    assert artifact.artifact_command == "put"
    assert artifact.stdin is True
    coordinator_close = parser.parse_args(["workflow", "coordinator-close", "manager-1"])
    assert coordinator_close.workflow_command == "coordinator-close"
    assert coordinator_close.workflow_id == "manager-1"
    coordinator_retry = parser.parse_args(["workflow", "coordinator-retry", "manager-1"])
    assert coordinator_retry.workflow_command == "coordinator-retry"
    assert coordinator_retry.workflow_id == "manager-1"
    harvest = parser.parse_args(["workflow", "harvest", "manager-1", "manager-2", "--dry-run"])
    assert harvest.workflow_command == "harvest"
    assert harvest.workflow_ids == ["manager-1", "manager-2"]
    assert harvest.dry_run is True
    delivery_check = parser.parse_args(["workflow", "delivery-check", "manager-1", "manager-2"])
    assert delivery_check.workflow_command == "delivery-check"
    assert delivery_check.workflow_ids == ["manager-1", "manager-2"]
    retire = parser.parse_args(["workflow", "retire", "manager-1", "--keep", "none"])
    assert retire.workflow_command == "retire"
    assert retire.keep == "none"
    assert retire.workflow_ids == ["manager-1"]


def test_parser_rejects_inventory_review_facts_without_github() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["inventory", "--no-github", "--with-review-facts"])


@pytest.mark.parametrize("arguments", (["start", "."], ["cleanup", "--dry-run"]))
def test_parser_rejects_removed_root_workflow_commands(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(arguments)
