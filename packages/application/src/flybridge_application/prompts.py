from __future__ import annotations

import shlex
import sys
import unicodedata
from pathlib import Path

from flybridge_core import ConfigError, WorkflowMode, WorkflowRole

ROLE_RESPONSIBILITIES = {
    WorkflowRole.MANAGER: (
        "Plan the work and provide a concise implementation handoff. "
        "Ask the operator only when a design choice needs the requester; do not ask for a clear local fix, "
        "and do not treat missing branch records as a reason to ask. "
        "Do not implement the plan, create agents, delegate work, or start another orchestration; "
        "Flybridge launches the worker after this role reports readiness. "
        "Do not push; the coordinator pushes once from this manager worktree after every reviewer "
        "approves the same SHA. "
        "After readiness, remain parked in this terminal. If the operator asks about progress, "
        "read `workflow status` for this workflow id and, when needed, `workflow artifact show`; "
        "do not change lifecycle state. If the operator asks to keep only this manager or close "
        "the run, or cleanup reports `retire_recommended`, run `workflow harvest` and "
        "`workflow retire --keep manager|none` for this "
        "workflow id; do not tell child agents to close themselves."
    ),
    WorkflowRole.WORKER: (
        "Implement the approved plan and provide concise verification evidence for review. "
        "Do not push from this worktree."
    ),
    WorkflowRole.REVIEWER: (
        "Review the implementation and report an approval or requested changes. "
        "In an orchestrated workflow, this role performs any requested self-review. "
        "Do not push from this worktree."
    ),
    WorkflowRole.SINGLE: (
        "Complete the objective directly, including any requested local self-review, "
        "and report the result. After local verification passes, push once from this worktree."
    ),
}

OPERATOR_GUIDANCE = (
    "Read the routing skill first, then read and follow every applicable operator skill "
    "document. Decide workflow-wide operational constraints once, and pass roles only the "
    "decision, its scope, and its expiry or recheck condition. For issue scope, pull-request "
    "feedback, and review comments, pass the direct URL and require the responsible role to "
    "read the source; summaries are supplemental. Before `workflow proceed` or `workflow "
    "complete`, confirm that all required validation evidence is present. If required data, "
    "access, or an environment is missing, obtain it or treat the role as blocked; do not accept "
    "narrower validation or mark the objective complete. Required verification includes requested "
    "evidence and kicking hosted CI when a source asks for those, not code inspection alone. "
    "Hosted CI is complete once triggered; do not wait for its result. Classify a hosted CI "
    "failure from its logs before assigning code work; retry an authorized transient service or "
    "infrastructure failure instead of treating it as an implementation defect. If a reviewer "
    "requests changes, do not mark the objective complete; record the reviewer as failed if the "
    "workflow must end. For orchestrated work, workers and reviewers never push. After every "
    "configured reviewer approves the same SHA, the coordinator harvests into the manager "
    "worktree, requires `workflow delivery-check`, and fast-forward pushes once. Do not wait for "
    "the parent operator across worktrees. Queue wait is parking, not a blocked readiness. "
    "Do not copy private skill content into a repository."
)

ROLE_VERIFICATION_RESPONSIBILITIES = {
    WorkflowRole.MANAGER: (
        "Define acceptance criteria, the required kinds of verification, and any required data, "
        "access, or environment in the plan. Do not narrow verification because a prerequisite "
        "is unavailable."
    ),
    WorkflowRole.WORKER: (
        "Collect evidence for every acceptance criterion. Acceptance is not limited to code diffs; "
        "include requested evidence submission and kicking hosted CI. Hosted CI is complete once "
        "triggered; do not wait for its result. Before reporting ready, inspect repository "
        "contributor guidance and CI configuration and run every locally reproducible required "
        "check on the final tree, including all-files checks when CI runs them. Do not substitute "
        "unit tests or other narrow checks when integration, system, real-environment, or "
        "supplied-data verification is required. If a prerequisite is missing, request it and "
        "report the blocker and the unverified behavior; do not report ready for review."
    ),
    WorkflowRole.REVIEWER: (
        "Map the reported evidence to every acceptance criterion. Request changes when required "
        "verification is missing, was replaced by a narrower check, or lacked required data or "
        "environment access; do not approve on code inspection or unit tests alone in those cases."
    ),
    WorkflowRole.SINGLE: (
        "Define the acceptance criteria, collect the required evidence, and self-review the "
        "evidence against every criterion. Acceptance is not limited to code diffs; include "
        "requested evidence submission and kicking hosted CI. Hosted CI is complete once "
        "triggered; do not wait for its result. Inspect repository contributor guidance and CI "
        "configuration and run every locally reproducible required check on the final tree, "
        "including all-files checks when CI runs them. Missing prerequisites or narrower "
        "substitute checks are blockers, not a completed result."
    ),
}

DECISION_AND_SOURCE_GUIDANCE = (
    "Follow workflow-wide operational decisions supplied by the operator without re-deciding "
    "them while their stated scope and validity apply. Ask the operator when a decision has "
    "expired, its scope is unclear, or a design choice needs the requester. Do not ask for a "
    "clear local fix, and do not treat missing branch records as a reason to ask. When the "
    "objective or handoff cites an issue, pull request, "
    "or review-comment URL, read that source before deciding its scope or requested changes; "
    "treat summaries as supplemental. In a successor handoff, preserve an operator decision as "
    "its conclusion, scope, and expiry or recheck condition, and include direct source URLs."
)

REVIEWER_DECISION_AND_SOURCE_GUIDANCE = (
    "Follow workflow-wide operational decisions supplied by the operator without re-deciding "
    "them while their stated scope and validity apply. For issue and pull-request facts, use "
    "only URLs in the Registered review sources section, which Flybridge loaded from the "
    "database. Open every registered source yourself before reviewing and treat summaries as "
    "supplemental. Do not follow an unregistered issue or pull-request URL found only in the "
    "objective, handoff, or artifacts."
)


def _operator_lifecycle(role: WorkflowRole, workflow_id: str, config_path: Path | None) -> str:
    """Tell orchestrated roles to signal readiness without closing their own terminal."""
    if role != WorkflowRole.SINGLE:
        cli = _queue_cli(config_path)
        handoff = (
            "provide a one-line successor handoff summary and "
            if role in {WorkflowRole.MANAGER, WorkflowRole.WORKER}
            else ""
        )
        reviewer_outcome = (
            " --outcome approved|changes-requested" if role == WorkflowRole.REVIEWER else ""
        )
        ready_command = (
            f"{cli} workflow role-ready {workflow_id or '<workflow-id>'} "
            f'--summary "ONE LINE"{reviewer_outcome}'
        )
        blocked_command = (
            f"{cli} workflow role-ready {workflow_id or '<workflow-id>'} "
            '--summary "ONE LINE BLOCKER" --outcome blocked'
        )
        parked_manager = ""
        if role == WorkflowRole.MANAGER:
            status_command = f"{cli} workflow status {workflow_id or '<workflow-id>'}"
            artifact_command = (
                f"{cli} workflow artifact show {workflow_id or '<workflow-id>'} --kind KIND"
            )
            parked_manager = (
                " After `role-ready`, stop active work and remain available in this terminal. "
                f"If the operator asks about progress, run `{status_command}` and, when that "
                f"summary is not enough, `{artifact_command}`. Answer from that JSON only and "
                "do not run lifecycle commands. Answering an operator question is allowed; "
                "do not contact a human on your own. If the operator asks to keep only this "
                "manager or close the run after it has finished, run "
                f"`{cli} workflow harvest {workflow_id or '<workflow-id>'}` and "
                f"`{cli} workflow retire {workflow_id or '<workflow-id>'} --keep manager|none`. "
                "Do not ask child agents to close themselves."
            )
        return (
            "Do not run `flybridge workflow complete`, `fail`, `cancel`, `handoff`, `advance`, "
            "or `proceed`; `complete` closes this role's owned terminals. After storing the "
            f"required artifact, {handoff}run `{ready_command}`, then stop. `role-ready` records "
            "readiness and does not close "
            "this terminal. The durable coordinator advances the workflow automatically. "
            "If required input, access, environment, or verification is missing, store the "
            "required role artifact documenting both verified and unverified scope, then run "
            f"`{blocked_command}` and stop. A blocked readiness lets the coordinator close this "
            "role and terminate the run instead of waiting forever. "
            "Waiting on a Flybridge queue resource is not a blocker: report the request-id, remain "
            "parked, and resume after the lease-id arrives. After the grant, keep the lease "
            "through operation-specific cleanup and confirmation that the next holder will not "
            "be affected, then run `queue release`. If cleanup remains unconfirmed, report the "
            "remaining state and keep the lease; do not claim verification complete or role "
            "readiness. Do not run "
            "`role-ready --outcome blocked` for queue wait. "
            "Never create a pull request. Manager and reviewer roles never push. The worker never "
            "pushes; the coordinator pushes the approved tip from the manager worktree."
            f"{parked_manager}"
        )
    return (
        "Do not run `flybridge workflow complete`, `fail`, `cancel`, `handoff`, or `advance`. "
        "`complete` closes this role's owned terminals, so the role that runs it cannot continue. "
        "`workflow artifact put` and `workflow role-ready` are orchestrated-only; do not run "
        "them from a single role. "
        "Commit required work and, after local verification passes, push once from this worktree. "
        "Report the result or blocker using "
        f"`{_queue_cli(config_path)} workflow single-report {workflow_id or '<workflow-id>'} "
        '--outcome done|blocked --summary "ONE LINE RESULT"`, then stop. '
        "Do not run this report while a queue request is waiting or leased. "
        "Do not run `workflow complete`. "
        "If required input is missing, request it and report the blocker without claiming "
        "readiness or supplying a successor handoff. Waiting on a Flybridge queue resource is "
        "parking: report the request-id and stop until the lease-id arrives. "
        "A watchdog closes this role after the configured timeout if it is still running."
    )


def _reference_link_guidance(role: WorkflowRole, workflow_id: str, config_path: Path | None) -> str:
    if role not in {WorkflowRole.SINGLE, WorkflowRole.MANAGER, WorkflowRole.WORKER}:
        return ""
    cli = _queue_cli(config_path)
    identifier = workflow_id or "<workflow-id>"
    related = f"{cli} workflow link {identifier} <url> --relation related"
    primary = f"{cli} workflow link {identifier} <url> --relation primary"
    if role == WorkflowRole.MANAGER:
        origin = (
            "Do not create a branch or pull request. If you learn an issue or pull-request URL, "
        )
    else:
        origin = "After `gh issue create` or `gh pr create`, "
    return (
        f"Best-effort linking: {origin}run `{related}` with that URL. "
        f"Use `{primary}` only when the run has no primary issue yet. "
        "Linking records a URL and does not create a pull request. Continue if the command fails."
    )


ROLE_BRANCH_CONTINUITY = {
    WorkflowRole.MANAGER: (
        "The plan is a host-managed artifact, not a repository file. Leave the target repository "
        "unchanged."
    ),
    WorkflowRole.WORKER: (
        "Flybridge creates the reviewer's worktree from this branch, so only committed work "
        "reaches the review. Commit the implementation on this branch before reporting ready "
        "or blocked, and leave no other modified or untracked file behind."
    ),
    WorkflowRole.REVIEWER: (
        "This worktree was created from the worker's branch, so the implementation under review "
        "is the commit history of this branch rather than uncommitted files."
    ),
}


def _queue_cli(config_path: Path | None) -> str:
    if config_path is None:
        return "flybridge"
    text = str(config_path)
    if "\n" in text or any(unicodedata.category(character) == "Cc" for character in text):
        raise ConfigError("configured configuration path contains unsafe control characters")
    return shlex.join([sys.executable, "-m", "flybridge_cli.main", "--config", text])


def _artifact_guidance(role: WorkflowRole, workflow_id: str, config_path: Path | None) -> str:
    if role == WorkflowRole.SINGLE:
        return ""
    artifact_workflow_id = workflow_id or "<workflow-id>"
    kind = {
        WorkflowRole.MANAGER: "plan",
        WorkflowRole.WORKER: "verification",
        WorkflowRole.REVIEWER: "review",
    }[role]
    cli = _queue_cli(config_path)
    manager_rule = (
        " Never write or commit plan, handoff, status, or coordination documents in the target "
        "repository."
        if role == WorkflowRole.MANAGER
        else ""
    )
    return (
        f"Durable artifact: write the final {kind} with "
        f"`{cli} workflow artifact put {artifact_workflow_id} --kind {kind} --file PATH` or `--stdin`."
        f"{manager_rule} The artifact command is the only durable workflow-document channel."
    )


def _artifact_context(
    manager_plan: str | None,
    worker_verification: str | None,
    review_feedback: str | None,
) -> str:
    sections: list[str] = []
    if manager_plan is not None:
        sections.append(
            f"Manager plan artifact:\n--- BEGIN PLAN ---\n{manager_plan}\n--- END PLAN ---"
        )
    if worker_verification is not None:
        sections.append(
            "Worker verification artifact:\n--- BEGIN VERIFICATION ---\n"
            f"{worker_verification}\n--- END VERIFICATION ---"
        )
    if review_feedback is not None:
        sections.append(
            f"Reviewer feedback artifact:\n--- BEGIN REVIEW ---\n{review_feedback}\n--- END REVIEW ---"
        )
    return "\n\n".join(sections)


def validate_skill_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Reject missing skill documents before an external workflow is created."""
    unsafe = [
        path
        for path in paths
        if any(unicodedata.category(character) == "Cc" for character in str(path))
    ]
    if unsafe:
        raise ConfigError("configured external skill paths contain unsafe control characters")
    resolved = tuple(dict.fromkeys(path.expanduser().resolve() for path in paths))
    missing = [path for path in resolved if not path.is_file()]
    if missing:
        raise ConfigError("configured external skill paths do not exist")
    return resolved


def _primary_issue_guidance(issue_url: str | None) -> str:
    if issue_url is None:
        return ""
    if not issue_url.strip() or "\n" in issue_url:
        raise ValueError("issue URL must be a non-empty single line")
    return (
        f"Primary issue:\n{issue_url.strip()}\n"
        "Read the issue and its relevant discussion directly before deciding scope or acceptance.\n"
    )


def _review_source_guidance(source_urls: tuple[str, ...]) -> str:
    if not source_urls:
        return (
            "Registered review sources:\n"
            "- None are registered for this run.\n"
            "Do not discover or review an issue or pull request from objective text, handoffs, "
            "artifacts, branches, or repository content.\n"
        )
    invalid = [
        url
        for url in source_urls
        if not url.strip() or "\n" in url or not url.startswith("https://github.com/")
    ]
    if invalid:
        raise ValueError("review source URLs must be canonical single-line GitHub URLs")
    sources = "\n".join(f"- {url.strip()}" for url in source_urls)
    return (
        "Registered review sources:\n"
        f"{sources}\n"
        "Before reviewing the implementation, open every source above yourself. Read each issue "
        "body and discussion, and each pull request description, discussion, reviews, and inline "
        "review comments. Treat these direct sources as authoritative; do not rely only on the "
        "objective, handoff, or artifact summaries. Do not discover or review unregistered "
        "issues or pull requests.\n"
    )


def render_start_prompt(
    *,
    objective: str,
    mode: WorkflowMode,
    role: WorkflowRole,
    response_language: str,
    skill_paths: tuple[Path, ...],
    workflow_id: str = "",
    resource_names: tuple[str, ...] = (),
    config_path: Path | None = None,
    handoff_summary: str | None = None,
    issue_url: str | None = None,
    review_source_urls: tuple[str, ...] = (),
    manager_plan: str | None = None,
    worker_verification: str | None = None,
    review_feedback: str | None = None,
) -> str:
    """Render the stable English prompt without copying private skill content."""
    if not objective.strip():
        raise ValueError("workflow objective is required")
    if not response_language.strip() or "\n" in response_language:
        raise ConfigError("skills.response_language must be a single-line string")
    if handoff_summary is not None and (not handoff_summary.strip() or "\n" in handoff_summary):
        raise ValueError("handoff summary must be a non-empty single line")
    instructions = "\n".join(f"- {path}" for path in skill_paths)
    if not instructions:
        instructions = "- No external skill documents are configured."
    handoff = (
        f"Previous-role handoff:\n{handoff_summary}\n\n" if handoff_summary is not None else ""
    )
    primary_issue = _primary_issue_guidance(issue_url)
    review_sources = (
        _review_source_guidance(review_source_urls) if role == WorkflowRole.REVIEWER else ""
    )
    decision_guidance = (
        REVIEWER_DECISION_AND_SOURCE_GUIDANCE
        if role == WorkflowRole.REVIEWER
        else DECISION_AND_SOURCE_GUIDANCE
    )
    resource_coordination = ""
    if resource_names:
        if not workflow_id:
            raise ValueError("workflow identifier is required for resource coordination")
        resources = ", ".join(f"`{name}`" for name in resource_names)
        cli = _queue_cli(config_path)
        resource_coordination = f"""
Resource coordination:
- The following names identify mutually exclusive resources: {resources}.
- Before using one, run `{cli} queue acquire <resource> --owner {workflow_id}` once.
- The lease covers preparation, the interfering operation, operation-specific cleanup, and confirmation that the next holder can use the resource without interference. Clean up temporary state created or changed by your work on success or failure; do not change unrelated resources. A command exiting alone does not confirm cleanup.
- If the result is granted, run the interfering operation, complete and confirm cleanup, then `{cli} queue release <resource> --lease <lease-id> --owner {workflow_id}`. Do not claim verification complete before cleanup is confirmed.
- If the result is waiting, report the request-id and park in this terminal. Do not poll `queue inspect`, run `queue watch`, interpret observer JSON, or run `role-ready --outcome blocked` for the wait.
- Resume the interfering operation only when a later Flybridge message names the lease-id. After the grant, complete and confirm cleanup before `queue release`. If cleanup cannot be confirmed, keep the lease and this terminal available, report the remaining state for operator recovery, and do not claim verification complete or role readiness.
- Queue cancellation is operator-only. The Flybridge supervisor expires a lease only when its owner is dead.
"""
    continuity = ROLE_BRANCH_CONTINUITY.get(role)
    branch_continuity = f"\nBranch continuity: {continuity}\n" if continuity else ""
    artifact_guidance = _artifact_guidance(role, workflow_id, config_path)
    link_guidance = _reference_link_guidance(role, workflow_id, config_path)
    link_guidance = f"\n{link_guidance}\n" if link_guidance else ""
    artifact_context = _artifact_context(manager_plan, worker_verification, review_feedback)
    artifact_context = f"\n{artifact_context}\n" if artifact_context else ""
    return f"""You are the {role} role in a {mode} Flybridge workflow.

Role responsibility: {ROLE_RESPONSIBILITIES[role]}
Operator lifecycle: {_operator_lifecycle(role, workflow_id, config_path)}
{artifact_guidance}
{link_guidance}{branch_continuity}
Decision and source handling: {decision_guidance}
Verification responsibility: {ROLE_VERIFICATION_RESPONSIBILITIES[role]}

Objective:
{objective}

{primary_issue}{review_sources}
{handoff}{artifact_context}Read and follow the external skill documents listed below when they apply. Their
paths are local configuration; do not copy their contents into repository files
unless the user explicitly requests it.

External skill document index:
{instructions}
{resource_coordination}

Respond in the language named: {response_language}.
Keep code, identifiers, command names, file paths, and machine-readable values unchanged.
Preserve the meaning of requirements and error messages when explaining them.
Prefer established technical terms over literal translations that reduce clarity.
"""


def render_resume_prompt(
    *,
    objective: str,
    role: WorkflowRole,
    response_language: str,
    skill_paths: tuple[Path, ...],
    workflow_id: str,
    resource_names: tuple[str, ...] = (),
    config_path: Path | None = None,
    handoff_summary: str | None = None,
    issue_url: str | None = None,
    review_source_urls: tuple[str, ...] = (),
    manager_plan: str | None = None,
    worker_verification: str | None = None,
    review_feedback: str | None = None,
) -> str:
    """Render concise restart context without embedding private skill content."""
    if not objective.strip():
        raise ValueError("workflow objective is required")
    if not response_language.strip() or "\n" in response_language:
        raise ConfigError("skills.response_language must be a single-line string")
    if handoff_summary is not None and (not handoff_summary.strip() or "\n" in handoff_summary):
        raise ValueError("handoff summary must be a non-empty single line")
    skills = (
        ", ".join(str(path) for path in skill_paths)
        if skill_paths
        else "no external skill documents"
    )
    resources = (
        " Before using these mutually exclusive resources, acquire them once through "
        f"`{_queue_cli(config_path)} queue` with owner `{workflow_id}`: "
        f"{', '.join(resource_names)}. If waiting, report the request-id and park until a "
        "Flybridge message names the lease-id; do not poll inspect or watch, and do not report "
        "role-ready blocked for the wait. Once granted, hold the lease through the work, "
        "operation-specific cleanup, and confirmation that the next holder will not be "
        "affected. Release only after cleanup is confirmed. If cleanup cannot be confirmed, "
        "keep the lease, report the remaining state for operator recovery, and do not claim "
        "verification complete or role readiness."
        if resource_names
        else ""
    )
    continuity = ROLE_BRANCH_CONTINUITY.get(role)
    branch_continuity = f" Branch continuity: {continuity}" if continuity else ""
    handoff = f"Previous-role handoff: {handoff_summary} " if handoff_summary is not None else ""
    primary_issue = _primary_issue_guidance(issue_url).replace("\n", " ")
    review_sources = (
        _review_source_guidance(review_source_urls).replace("\n", " ")
        if role == WorkflowRole.REVIEWER
        else ""
    )
    decision_guidance = (
        REVIEWER_DECISION_AND_SOURCE_GUIDANCE
        if role == WorkflowRole.REVIEWER
        else DECISION_AND_SOURCE_GUIDANCE
    )
    artifact_guidance = _artifact_guidance(role, workflow_id, config_path)
    link_guidance = _reference_link_guidance(role, workflow_id, config_path)
    link_guidance = f" {link_guidance}" if link_guidance else ""
    artifact_context = _artifact_context(manager_plan, worker_verification, review_feedback)
    return (
        f"Resume the persisted Flybridge workflow as the {role.value} role. "
        f"Continue this objective: {objective.strip()} "
        f"Role responsibility: {ROLE_RESPONSIBILITIES[role]} "
        f"Operator lifecycle: {_operator_lifecycle(role, workflow_id, config_path)}{branch_continuity} "
        f"{artifact_guidance}{link_guidance} "
        f"Decision and source handling: {decision_guidance} "
        f"Verification responsibility: {ROLE_VERIFICATION_RESPONSIBILITIES[role]} "
        f"{primary_issue}{review_sources}"
        f"{handoff}"
        f"{artifact_context} "
        f"Read applicable configured skill documents from: {skills}.{resources} "
        f"Respond in {response_language}. Keep code, identifiers, command names, file paths, "
        "and machine-readable values unchanged. Preserve the meaning of requirements and error "
        "messages when explaining them. Prefer established technical terms over literal "
        "translations that reduce clarity."
    )


def render_lease_grant_prompt(
    *,
    resource: str,
    request_id: str,
    lease_id: str,
    workflow_id: str,
    config_path: Path | None = None,
) -> str:
    """Tell a parked agent that FIFO promotion granted its lease."""
    if not resource.strip() or "\n" in resource:
        raise ValueError("resource name must be a non-empty single line")
    if not request_id.strip() or "\n" in request_id:
        raise ValueError("request identifier must be a non-empty single line")
    if not lease_id.strip() or "\n" in lease_id:
        raise ValueError("lease identifier must be a non-empty single line")
    if not workflow_id.strip() or "\n" in workflow_id:
        raise ValueError("workflow identifier must be a non-empty single line")
    cli = _queue_cli(config_path)
    return (
        "Flybridge queue notification: your waiting request is now leased. "
        f"Treat this message as the start of the interfering operation for `{resource}`. "
        f"request-id `{request_id.strip()}`; lease-id `{lease_id.strip()}`. "
        "Hold the lease through the work, operation-specific cleanup, and confirmation that "
        "the next holder will not be affected. Clean up on success or failure. Release only "
        f"after cleanup is confirmed with `{cli} queue release {resource.strip()} "
        f"--lease {lease_id.strip()} --owner {workflow_id.strip()}`. If cleanup cannot be "
        "confirmed, keep the lease, report the remaining state for operator recovery, and "
        "do not claim verification complete or role readiness. "
        "Do not acquire again, poll inspect, or run queue watch."
    )
