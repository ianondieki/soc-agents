from __future__ import annotations

from enum import Enum


class Priority(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"

    @property
    def rank(self) -> int:
        return {"P4": 0, "P3": 1, "P2": 2, "P1": 3}[self.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Priority):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Priority):
            return NotImplemented
        return self.rank <= other.rank


class IncidentStatus(str, Enum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    TICKETED = "TICKETED"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_VENDOR = "AWAITING_VENDOR"
    RESTORED = "RESTORED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class FailureDomain(str, Enum):
    POWER = "POWER"
    TRANSMISSION = "TRANSMISSION"
    RADIO = "RADIO"
    CORE = "CORE"
    ACCESS = "ACCESS"
    ENVIRONMENT = "ENVIRONMENT"
    UNKNOWN = "UNKNOWN"


class AssigneeType(str, Enum):
    FIELD_ENGINEER = "FIELD_ENGINEER"
    MSP = "MSP"
    NOC = "NOC"
    UNASSIGNED = "UNASSIGNED"


class HitlState(str, Enum):
    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class HitlTaskType(str, Enum):
    APPROVE_BROADCAST = "APPROVE_BROADCAST"
    APPROVE_PRIORITY = "APPROVE_PRIORITY"
    APPROVE_ASSIGNMENT = "APPROVE_ASSIGNMENT"
    APPROVE_EXEC_BRIEF = "APPROVE_EXEC_BRIEF"
    GENERIC = "GENERIC"
    # v2 (§7.1.2): gates for write-capable MCP cards; exercised only when such a card is actually connected.
    APPROVE_TICKET_SYNC = "APPROVE_TICKET_SYNC"
    APPROVE_PAGE = "APPROVE_PAGE"
    APPROVE_LEDGER_SYNC = "APPROVE_LEDGER_SYNC"


class HitlTaskStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HITL = "WAITING_HITL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    WAITING_HITL = "WAITING_HITL"


class SiteType(str, Enum):
    HUB = "HUB"
    BTS = "BTS"
    NODEB = "NODEB"
    ENODEB = "ENODEB"
    GNODEB = "GNODEB"
    BSC = "BSC"
    RNC = "RNC"
    CORE = "CORE"
    TX = "TX"
    POWER = "POWER"
    OTHER = "OTHER"


PRIORITY_ORDER = [Priority.P4, Priority.P3, Priority.P2, Priority.P1]


def max_priority(a: Priority, b: Priority) -> Priority:
    return a if a.rank >= b.rank else b
