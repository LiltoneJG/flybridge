from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .storage import _chmod, configure_sqlite_connection, symlinked_component
from .types import WorkflowMode, WorkflowRole
from .workflows import WorkflowStore, _now

MAX_ARTIFACT_BYTES = 1024 * 1024
ARTIFACT_KINDS = frozenset({"plan", "verification", "review"})
ARTIFACT_ROLES = {
    "plan": WorkflowRole.MANAGER,
    "verification": WorkflowRole.WORKER,
    "review": WorkflowRole.REVIEWER,
}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WorkflowArtifact:
    root_manager_id: str
    workflow_id: str
    kind: str
    relative_path: str
    sha256: str
    byte_size: int
    created_at: str
    updated_at: str


class WorkflowArtifactStore:
    """Store bounded workflow documents outside implementation repositories."""

    def __init__(self, state_dir: Path, workflow_store: WorkflowStore | None = None) -> None:
        self.workflow_store = workflow_store or WorkflowStore(state_dir)
        self.state_dir = state_dir.expanduser().resolve()
        self.root = self.state_dir / "artifacts"
        self._prepare_directory(self.root)
        self.workflows_root = self.root / "workflows"
        self._prepare_directory(self.workflows_root)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.workflow_store.path, timeout=5)
        connection.row_factory = sqlite3.Row
        configure_sqlite_connection(connection, self.workflow_store.path, foreign_keys=True)
        return connection

    @staticmethod
    def _artifact(row: sqlite3.Row) -> WorkflowArtifact:
        return WorkflowArtifact(**dict(row))

    @staticmethod
    def _safe_identifier(value: str) -> str:
        if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("workflow identifier is unsafe for artifact storage")
        return value

    @staticmethod
    def _validate_kind(kind: str) -> str:
        if kind not in ARTIFACT_KINDS:
            raise ValueError("artifact kind must be plan, verification, or review")
        return kind

    @staticmethod
    def _validate_digest(digest: str) -> str:
        if not _SHA256_HEX.fullmatch(digest):
            raise ValueError("artifact SHA-256 is invalid")
        return digest

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        linked = symlinked_component(path)
        if linked is not None:
            raise OSError(f"artifact directory must not be a symbolic link: {linked}")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _chmod(path, 0o700)

    def _resolve_owner(self, workflow_id: str, kind: str) -> tuple[str, str]:
        workflow = self.workflow_store.get(workflow_id)
        if workflow.mode != WorkflowMode.ORCHESTRATED:
            raise ValueError("workflow artifacts require an orchestrated workflow")
        required_role = ARTIFACT_ROLES[kind]
        if workflow.role != required_role:
            raise ValueError(f"{kind} artifact may only be written by the {required_role.value}")
        root_id = workflow.id if workflow.role == WorkflowRole.MANAGER else workflow.parent_id
        if root_id is None:
            raise ValueError("orchestrated workflow has no root manager")
        return self._safe_identifier(root_id), workflow.id

    def _expected_relative_path(self, root_id: str, kind: str, digest: str, owner_id: str) -> str:
        if kind == "review":
            return str(PurePosixPath("workflows", root_id, f"{kind}.{owner_id}.{digest}.md"))
        return str(PurePosixPath("workflows", root_id, f"{kind}.{digest}.md"))

    def _validated_path(self, artifact: WorkflowArtifact) -> Path:
        root_id = self._safe_identifier(artifact.root_manager_id)
        kind = self._validate_kind(artifact.kind)
        digest = self._validate_digest(artifact.sha256)
        expected = self._expected_relative_path(root_id, kind, digest, artifact.workflow_id)
        relative = PurePosixPath(artifact.relative_path)
        if (
            artifact.relative_path != expected
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in artifact.relative_path
        ):
            raise ValueError("artifact metadata contains an unsafe relative path")
        path = self.root.joinpath(*relative.parts)
        linked = symlinked_component(path)
        if linked is not None:
            raise OSError(f"artifact path must not be a symbolic link: {linked}")
        return path

    def _write_immutable(
        self,
        directory: Path,
        kind: str,
        digest: str,
        payload: bytes,
        *,
        filename: str | None = None,
    ) -> Path:
        destination = directory / (filename or f"{kind}.{digest}.md")
        if destination.is_symlink():
            raise OSError(f"artifact path must not be a symbolic link: {destination}")
        if destination.exists():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest or existing != payload:
                raise OSError("content-addressed artifact file does not match its digest")
            return destination
        temporary = directory / f".{kind}.{digest}.{uuid.uuid4().hex}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            _chmod(destination, 0o600)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return destination

    def put(self, workflow_id: str, kind: str, content: str) -> WorkflowArtifact:
        kind = self._validate_kind(kind)
        if not isinstance(content, str):
            raise TypeError("artifact content must be UTF-8 text")
        payload = content.encode("utf-8")
        if not content.strip():
            raise ValueError("artifact content must not be empty")
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit")
        root_id, owner_id = self._resolve_owner(workflow_id, kind)
        digest = hashlib.sha256(payload).hexdigest()
        relative_path = self._expected_relative_path(root_id, kind, digest, owner_id)
        directory = self.workflows_root / root_id
        self._prepare_directory(directory)
        self._write_immutable(
            directory,
            kind,
            digest,
            payload,
            filename=Path(relative_path).name,
        )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO workflow_artifacts(
                    root_manager_id, workflow_id, kind, relative_path, sha256, byte_size,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_manager_id, workflow_id, kind) DO UPDATE SET
                    relative_path = excluded.relative_path,
                    sha256 = excluded.sha256,
                    byte_size = excluded.byte_size,
                    updated_at = excluded.updated_at
                """,
                (root_id, owner_id, kind, relative_path, digest, len(payload), now, now),
            )
        return self.get(owner_id, kind)

    def get(self, workflow_id: str, kind: str) -> WorkflowArtifact:
        kind = self._validate_kind(kind)
        workflow = self.workflow_store.get(workflow_id)
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        if root_id is None:
            raise ValueError("workflow has no root manager")
        with closing(self._connect()) as connection:
            if kind == "review" and workflow.role == WorkflowRole.REVIEWER:
                row = connection.execute(
                    "SELECT * FROM workflow_artifacts WHERE workflow_id = ? AND kind = ?",
                    (workflow.id, kind),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM workflow_artifacts WHERE root_manager_id = ? AND kind = ?",
                    (root_id, kind),
                ).fetchone()
        if row is None:
            raise ValueError(f"{kind} artifact was not found")
        artifact = self._artifact(row)
        self._validated_path(artifact)
        return artifact

    def list_for_workflow(self, workflow_id: str) -> list[WorkflowArtifact]:
        workflow = self.workflow_store.get(workflow_id)
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        if root_id is None:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_artifacts
                WHERE root_manager_id = ?
                ORDER BY CASE kind WHEN 'plan' THEN 1 WHEN 'verification' THEN 2 ELSE 3 END,
                    workflow_id
                """,
                (root_id,),
            ).fetchall()
        artifacts = [self._artifact(row) for row in rows]
        for artifact in artifacts:
            self._validated_path(artifact)
        return artifacts

    def list_for_kind(self, workflow_id: str, kind: str) -> list[WorkflowArtifact]:
        kind = self._validate_kind(kind)
        workflow = self.workflow_store.get(workflow_id)
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        if root_id is None:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT workflow_artifacts.* FROM workflow_artifacts
                JOIN workflows ON workflows.id = workflow_artifacts.workflow_id
                WHERE workflow_artifacts.root_manager_id = ? AND workflow_artifacts.kind = ?
                ORDER BY workflows.slot, workflow_artifacts.workflow_id
                """,
                (root_id, kind),
            ).fetchall()
        artifacts = [self._artifact(row) for row in rows]
        for artifact in artifacts:
            self._validated_path(artifact)
        return artifacts

    def read(self, workflow_id: str, kind: str) -> tuple[WorkflowArtifact, str]:
        artifact = self.get(workflow_id, kind)
        path = self._validated_path(artifact)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("artifact path is not a regular file")
            if details.st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact file exceeds the configured size limit")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                payload = stream.read(MAX_ARTIFACT_BYTES + 1)
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not payload:
            raise ValueError("artifact file is empty")
        if len(payload) != artifact.byte_size:
            raise ValueError("artifact byte size does not match metadata")
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            raise ValueError("artifact SHA-256 does not match metadata")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact file is not valid UTF-8") from exc
        return artifact, content

    def verify(self, workflow_id: str, kind: str) -> WorkflowArtifact:
        artifact, _content = self.read(workflow_id, kind)
        return artifact

    def read_digest(self, workflow_id: str, kind: str, digest: str) -> str:
        """Read an immutable content-addressed artifact independently of the mutable latest slot."""
        kind = self._validate_kind(kind)
        digest = self._validate_digest(digest)
        workflow = self.workflow_store.get(workflow_id)
        root_id = workflow.id if workflow.parent_id is None else workflow.parent_id
        if root_id is None:
            raise ValueError("workflow has no root manager")
        relative_path = self._expected_relative_path(
            self._safe_identifier(root_id), kind, digest, workflow.id
        )
        path = self.root.joinpath(*PurePosixPath(relative_path).parts)
        linked = symlinked_component(path)
        if linked is not None:
            raise OSError(f"artifact path must not be a symbolic link: {linked}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise ValueError("readiness artifact snapshot was not found") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("artifact path is not a regular file")
            if details.st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact file exceeds the configured size limit")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                payload = stream.read(MAX_ARTIFACT_BYTES + 1)
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not payload or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("readiness artifact snapshot does not match its SHA-256")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact file is not valid UTF-8") from exc
