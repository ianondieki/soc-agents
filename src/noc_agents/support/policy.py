"""``config/support/policy.yaml``, typed and validated (docs/SUPPORT_DESK.md "Escalation policy").

The policy file is the one place the desk's limits, thresholds, escalation order and eval
gates are written down, so it is loaded strictly: an unknown key is refused (a typo such as
``refund_auto_limt_kes`` must not silently leave the default in force), a reason code that is
not in the contract is refused, and a reason code the file forgets is refused too -- an
escalation rule that cannot fire because nobody listed it is a safety rule switched off.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from noc_agents.config import CONFIG_DIR
from noc_agents.support.vocab import REASON_CODES, URGENCIES

SUPPORT_CONFIG_DIR = CONFIG_DIR / "support"
DEFAULT_POLICY_PATH = SUPPORT_CONFIG_DIR / "policy.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RateLimit(_Strict):
    max_requests: int = Field(5, ge=1)  # per MSISDN
    per_ip_max_requests: int = Field(30, ge=1)  # per client address, so one caller cannot cycle numbers
    window_seconds: int = Field(600, ge=1)


class RepeatRule(_Strict):
    window_days: int = Field(7, ge=1)
    nth: int = Field(3, ge=2)


class EscalationRule(_Strict):
    reason_code: str
    reason: str
    when: str = ""
    #: Extra customer-facing advice appended to the holding reply (fraud and safety cases).
    advice: str = ""


class EvalGate(_Strict):
    metric: str
    op: Literal[">=", "<=", "=="]
    threshold: float

    def passes(self, value: float) -> bool:
        if self.op == ">=":
            return value >= self.threshold
        if self.op == "<=":
            return value <= self.threshold
        return abs(value - self.threshold) < 1e-9


#: The incident priorities the NOC uses; a priority outside them always waits for a person.
PRIORITIES: tuple[str, ...] = ("P1", "P2", "P3", "P4")


class CustomerUpdates(_Strict):
    """When the "service is back" SMS waits for a person (docs/CLOSE_THE_LOOP.md section 1)."""

    #: Priorities whose notice waits for a person, by the floor's autonomy level
    #: (``profile.autonomy_level``). A level not listed here waits for every priority.
    wait_for_priorities: dict[str, tuple[str, ...]] = Field(default_factory=lambda: {
        "L1_COPILOT": PRIORITIES, "L2_GUARDED": ("P1", "P2"), "L3_CONDITIONAL": ("P1",)})
    #: Any batch with more numbers than this waits, whatever the priority.
    auto_max_recipients: int = Field(20, ge=0)
    #: At most this many close-the-loop SMS (restore and confirmed-outage) to one number in any 24
    #: hours; one over it is not sent and the customer stays waiting. A bound on the one accepted
    #: risk: the public form cannot prove the caller owns the number they typed.
    max_sms_per_number_per_day: int = Field(4, ge=1)

    def waits(self, autonomy: str, priority: str | None, recipients: int) -> bool:
        """True when a person must approve the notice before it is sent."""
        if recipients > self.auto_max_recipients:
            return True
        rung = self.wait_for_priorities.get(autonomy)
        return rung is None or priority not in PRIORITIES or priority in rung


class SurgeRule(_Strict):
    """When complaints about one place become a "Possible outage" card (section 3)."""

    threshold: int = Field(3, ge=2)  # distinct numbers
    window_minutes: int = Field(30, ge=1)
    #: Still-down reports after a restore: this many distinct numbers raise the card on their own.
    still_down_threshold: int = Field(2, ge=1)
    still_down_window_minutes: int = Field(120, ge=1)


class TrackRule(_Strict):
    """The public Track page (section 2). Every request counts against its client address; only
    FAILED well-formed attempts (a reference and a number that do not match) count against the
    reference and against the number, so a customer's own look-ups never lock them out."""

    per_ip_max_requests: int = Field(20, ge=1)
    per_ref_max_failures: int = Field(10, ge=1)
    window_seconds: int = Field(600, ge=1)  # the address and reference windows
    per_msisdn_max_failures: int = Field(10, ge=1)
    per_msisdn_window_seconds: int = Field(86400, ge=1)
    #: "Still down" is offered for this long after the restore SMS ...
    still_down_within_hours: int = Field(72, ge=1)
    #: ... and at most once in this many hours.
    still_down_cooldown_hours: int = Field(24, ge=1)
    #: A still-down report is answered by a person within this many hours (7.3).
    still_down_reply_hours: int = Field(4, ge=1)
    #: The customer-facing reason for ``still_down_after_restore``.
    still_down_reason: str = "you told us service is still down, so a person will check it"


class LinkingRule(_Strict):
    """Late linking (7.3): when a new top-level incident opens, recent unlinked network complaints
    naming a place it covers (strongly) are linked to it."""

    late_link_hours: int = Field(6, ge=1)


class SupportPolicy(_Strict):
    version: int = 1
    grounding_threshold: float = Field(4.0, gt=0)
    cross_category_factor: float = Field(2.0, ge=1)
    category_boost: float = Field(0.25, ge=0)
    low_confidence_threshold: float = Field(0.55, ge=0, le=1)
    llm_tiebreak_below: float = Field(0.55, ge=0, le=1)
    refund_auto_limit_kes: int = Field(500, ge=0)
    reversal_auto_limit_kes: int = Field(5000, ge=0)
    reversal_window_hours: int = Field(24, ge=1)
    recredit_cooldown_days: int = Field(30, ge=1)
    dedupe_window_seconds: int = Field(120, ge=0)
    rate_limit: RateLimit = RateLimit()
    repeat: RepeatRule = RepeatRule()
    high_value_tiers: tuple[str, ...] = ("platinum", "gold")
    sla_hours: dict[str, int] = Field(default_factory=lambda: {"critical": 1, "high": 4, "normal": 24, "low": 72})
    escalation: tuple[EscalationRule, ...]
    #: What the customer is told instead of an account-derived reason (escalation.ACCOUNT_REASONS).
    account_review_reason: str = "we need to check some account details before we can finish this"
    eval_gates: tuple[EvalGate, ...] = ()
    # Close the loop (docs/CLOSE_THE_LOOP.md).
    customer_updates: CustomerUpdates = CustomerUpdates()
    surge: SurgeRule = SurgeRule()
    track: TrackRule = TrackRule()
    linking: LinkingRule = LinkingRule()

    @model_validator(mode="after")
    def _complete(self) -> "SupportPolicy":
        codes = [rule.reason_code for rule in self.escalation]
        unknown = sorted(set(codes) - set(REASON_CODES))
        missing = sorted(set(REASON_CODES) - set(codes))
        if unknown or missing or len(codes) != len(set(codes)):
            raise ValueError(
                f"escalation must list each contract reason code exactly once (unknown {unknown}, missing {missing})"
            )
        if set(self.sla_hours) != set(URGENCIES):
            raise ValueError(f"sla_hours must name exactly {list(URGENCIES)}")
        return self

    def rule(self, reason_code: str) -> EscalationRule:
        """The rule for ``reason_code`` (validated to exist at load)."""
        return next(rule for rule in self.escalation if rule.reason_code == reason_code)


@lru_cache(maxsize=8)
def load_policy(path: Path = DEFAULT_POLICY_PATH) -> SupportPolicy:
    """Parse and validate the policy file. Cached per path; the file is read once per process."""
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return SupportPolicy.model_validate(raw)
