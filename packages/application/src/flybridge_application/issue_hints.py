from __future__ import annotations

from flybridge_core import (
    canonical_issue_url,
    issue_urls_from_text,
    repositories_match,
    repository_from_orca_project_id,
)


def github_hint_issues(
    *,
    comment: str,
    project_id: str | None,
    linked_issue: int | None,
    git_repository: str | None,
) -> list[dict[str, object]]:
    """Issue URLs from the Orca comment, plus a same-repo linkedIssue only."""
    issues: list[dict[str, object]] = []
    seen: set[str] = set()
    for ref in issue_urls_from_text(comment):
        if ref.join_key in seen:
            continue
        seen.add(ref.join_key)
        issues.append({"repository": ref.repository, "number": ref.number, "url": ref.url})
    if linked_issue is None:
        return issues
    project_repo = repository_from_orca_project_id(project_id)
    if git_repository and project_repo and not repositories_match(git_repository, project_repo):
        return issues
    checkout = git_repository or project_repo
    if not checkout:
        return issues
    url = canonical_issue_url(checkout, linked_issue)
    key = f"{checkout.lower()}#{linked_issue}"
    if key not in seen:
        issues.append({"repository": checkout, "number": linked_issue, "url": url})
    return issues
