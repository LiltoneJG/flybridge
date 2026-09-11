from enum import StrEnum


class WorkflowStatus(StrEnum):
    REQUESTED = "requested"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowMode(StrEnum):
    SINGLE = "single"
    ORCHESTRATED = "orchestrated"


class WorkflowRole(StrEnum):
    SINGLE = "single"
    MANAGER = "manager"
    WORKER = "worker"
    REVIEWER = "reviewer"


class QueueRequestStatus(StrEnum):
    WAITING = "waiting"
    LEASED = "leased"
    RELEASED = "released"
    CANCELLED = "cancelled"


class QueueEvent(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RELEASED = "released"
    CANCELLED = "cancelled"
    RECOVERED = "recovered"
