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
    # Phase 4 (§7.6). The gate on a notice to the Communications Authority: metric M10 is
    # "100 % drafted, 0 auto-sent", so this card is the ONLY thing that lets a regulatory
    # draft reach the outbox, and approving it is a separate act from sending it.
    APPROVE_REGULATORY_NOTICE = "APPROVE_REGULATORY_NOTICE"
    # Phase 5 (§7.5). Planned maintenance touches live customers on purpose, so both the
    # plan and each individual window are gated: APPROVE_SCHEDULE signs off the programme,
    # APPROVE_MAINTENANCE_WINDOW signs off going ahead on the night, which is where the
    # rain guard and the customer notice actually bite.
    APPROVE_SCHEDULE = "APPROVE_SCHEDULE"
    APPROVE_MAINTENANCE_WINDOW = "APPROVE_MAINTENANCE_WINDOW"
    # Phase 4 Lane 4A (§7.6). A vendor may dispute a scorecard line, and a notice to a vendor
    # about their numbers is commercial correspondence: both are human decisions. Software
    # computes the evidence; it never adjudicates a dispute and never sends the notice itself.
    DISPUTE_SCORECARD_LINE = "DISPUTE_SCORECARD_LINE"
    APPROVE_VENDOR_NOTICE = "APPROVE_VENDOR_NOTICE"
    # Appendix C members that were in use, or named by the spec, without being members.
    # APPROVE_HANDOVER was live as a bare string literal in services/handover.py, which
    # matters: the registry's import-time assert only validates gate names that ARE enum
    # members, so a typo in that literal was a runtime bug rather than a startup failure.
    APPROVE_HANDOVER = "APPROVE_HANDOVER"
    CONFIRM_POWER_NOTICE = "CONFIRM_POWER_NOTICE"  # §7.3.2: a parsed KPLC notice is confirmed by a human
    APPROVE_PERFORMANCE_ACTION = "APPROVE_PERFORMANCE_ACTION"  # §7.6.3, Phase 6; declared, deliberately unused (D12)
    GENERIC = "GENERIC"
    # v2 (§7.1.2): gates for write-capable MCP cards; exercised only when such a card is actually connected.
    APPROVE_TICKET_SYNC = "APPROVE_TICKET_SYNC"
    APPROVE_PAGE = "APPROVE_PAGE"
    APPROVE_LEDGER_SYNC = "APPROVE_LEDGER_SYNC"
    # Close the loop (docs/CLOSE_THE_LOOP.md). The "service is back" SMS to the customers who
    # complained about an incident, when the autonomy ladder says a person sends it (incident-
    # bound: the SMS wait HELD behind it); and a burst of complaints about a place with no open
    # incident, which a person confirms into a ticket or dismisses (incident_id NULL, entity
    # support_surge). Both are §9.3 row-2 decisions (api/deps.HITL_DECIDERS).
    APPROVE_CUSTOMER_UPDATE = "APPROVE_CUSTOMER_UPDATE"
    CONFIRM_POSSIBLE_OUTAGE = "CONFIRM_POSSIBLE_OUTAGE"


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
