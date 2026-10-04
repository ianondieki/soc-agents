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
