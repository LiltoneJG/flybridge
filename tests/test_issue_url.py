from __future__ import annotations

import pytest
from flybridge_core import (
    canonical_issue_url,
    issue_urls_from_text,
    merge_lifecycle_comment,
    parse_issue_url,
    repositories_match,
    repository_from_orca_project_id,
    resource_urls_from_text,
)


def test_parse_issue_url_normalizes_trailing_slash() -> None:
    ref = parse_issue_url("https://github.com/Example/Repo/issues/179/")
    assert ref.repository == "Example/Repo"
    assert ref.number == 179
    assert ref.url == "https://github.com/Example/Repo/issues/179"
    assert ref.join_key == "example/repo#179"


def test_parse_issue_url_rejects_pull_requests() -> None:
    with pytest.raises(ValueError, match="invalid"):
        parse_issue_url("https://github.com/example/repo/pull/1")


def test_issue_urls_from_text_deduplicates() -> None:
    refs = issue_urls_from_text(
        "https://github.com/example/repo/issues/1\n"
        "https://github.com/example/repo/issues/1/\n"
        "https://github.com/example/other/issues/2"
    )
    assert [ref.number for ref in refs] == [1, 2]


def test_merge_lifecycle_comment_keeps_urls() -> None:
    comment = merge_lifecycle_comment(
        "https://github.com/example/repo/issues/1\nFlybridge workflow is running.",
        "Flybridge workflow completed.",
        extra_urls=("https://github.com/example/repo/issues/1",),
    )
    assert comment == ("https://github.com/example/repo/issues/1\nFlybridge workflow completed.")


def test_resource_urls_from_text_includes_pulls() -> None:
    urls = resource_urls_from_text(
        "See https://github.com/example/repo/issues/1 and https://github.com/example/repo/pull/2"
    )
    assert urls == (
        "https://github.com/example/repo/issues/1",
        "https://github.com/example/repo/pull/2",
    )


def test_repository_from_orca_project_id() -> None:
    assert repository_from_orca_project_id("github:example/repo") == "example/repo"
    assert repository_from_orca_project_id("linear:abc") is None
    assert repositories_match("Example/Repo", "example/repo")
    assert canonical_issue_url("example/repo", 3) == "https://github.com/example/repo/issues/3"
