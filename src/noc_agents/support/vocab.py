"""The support desk's closed vocabularies, exactly as docs/SUPPORT_DESK.md spells them.

One module so that the API validator, the policy loader, the eval runner and the tests all
check against the same tuples; a value outside them is a bug, never a new category.
"""

from __future__ import annotations

CATEGORIES: tuple[str, ...] = (
    "network",
    "data_bundles",
    "mpesa",
    "billing",
    "sim_and_fraud",
    "device_settings",
    "roaming",
    "account",
    "other",
)
URGENCIES: tuple[str, ...] = ("low", "normal", "high", "critical")
SENTIMENTS: tuple[str, ...] = ("calm", "frustrated", "angry")
ROUTES: tuple[str, ...] = ("resolver", "action", "human")
STATUSES: tuple[str, ...] = (
    "answered",
    "action_taken",
    "awaiting_approval",
    "escalated",
    "in_progress",
    "resolved",
    "closed",
)
OUTCOMES: tuple[str, ...] = ("auto_resolved", "action_completed", "escalated", "human_resolved")
CHANNELS: tuple[str, ...] = ("web", "sms", "app", "call_centre", "social")
LANGUAGES: tuple[str, ...] = ("en", "sw", "mixed")
#: ``followup`` (docs/CLOSE_THE_LOOP.md) is the agent that keeps the desk's promise after the
#: complaint: it tells the customer service is back, records a still-down report, and links a
#: complaint to a ticket a confirmed surge opened.
AGENTS: tuple[str, ...] = ("intake", "triage", "resolver", "action", "escalation", "human", "followup")
TOOL_STATUSES: tuple[str, ...] = ("ok", "refused", "needs_approval", "approved", "rejected", "failed")

#: The escalation reason codes, in the contract's table order. ``config/support/policy.yaml``
#: must list exactly these (in whatever order the operator decides).
REASON_CODES: tuple[str, ...] = (
    "fraud_or_sim_swap",
    "legal_or_regulator",
    "threat_or_safety",
    "needs_verification",
    "over_refund_limit",
    "repeat_unresolved",
    "angry_high_value",
    "low_confidence",
    "not_grounded",
    "tool_failed",
)
#: Reasons a complaint goes BACK to a person after it was finished, set by the follow-up agent
#: and never by the intake table above (docs/CLOSE_THE_LOOP.md): no predicate, no place in the
#: escalation order, and no golden-set label, because no complaint can arrive with it.
FOLLOWUP_REASON_CODES: tuple[str, ...] = ("still_down_after_restore",)

#: How a ``closed`` complaint closed (``support_complaints.closure_reason``).
CLOSURE_REASONS: tuple[str, ...] = ("service_restored",)

#: Missing one of these is the dangerous error; the eval gate on them is zero.
SAFETY_REASONS: frozenset[str] = frozenset({"fraud_or_sim_swap", "legal_or_regulator", "threat_or_safety"})

#: Statuses in which a person owns the case (the escalation queue).
HUMAN_QUEUE: frozenset[str] = frozenset({"escalated", "awaiting_approval", "in_progress"})

#: The outcome each status implies; ``closed`` keeps whatever the case had.
OUTCOME_FOR_STATUS: dict[str, str] = {
    "answered": "auto_resolved",
    "action_taken": "action_completed",
    "awaiting_approval": "escalated",
    "escalated": "escalated",
    "in_progress": "escalated",
    "resolved": "human_resolved",
}
