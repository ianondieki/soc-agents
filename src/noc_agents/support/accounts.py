"""The demo customer accounts the action agent's tools read (``config/support/accounts.yaml``).

Read-only by design: a tool never edits this file. What a refund or a reversal *did* is the
tool call's recorded result, and the "already reversed" / "re-credited in the last 30 days"
checks read those rows back (see :mod:`tools`). That keeps the fixture a stable starting
point for the demo and for every eval run, however many complaints have been processed.

Times are relative (``hours_ago``, ``days_ago``) so the data never goes stale.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from noc_agents.support.policy import SUPPORT_CONFIG_DIR
from noc_agents.support.text import normalise_msisdn
from noc_agents.support.vocab import CATEGORIES

DEFAULT_ACCOUNTS_PATH = SUPPORT_CONFIG_DIR / "accounts.yaml"

#: The shape customers copy from the M-PESA confirmation SMS.
MPESA_CODE_PATTERN = r"^[A-Z0-9]{10}$"


class _Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transaction(_Fixture):
    code: str = Field(pattern=MPESA_CODE_PATTERN)
    type: str  # sent | paybill | buy_goods | withdraw | received
    amount_kes: int = Field(gt=0)
    counterparty: str
    counterparty_msisdn: str | None = None
    hours_ago: float = Field(ge=0)
    status: str = "completed"
    recipient_withdrawn: bool = False


class Bundle(_Fixture):
    id: str
    name: str
    size_mb: int = Field(gt=0)
    price_kes: int = Field(ge=0)
    bought_hours_ago: float = Field(ge=0)
    validity_hours: int = Field(gt=0)
    used_mb: int = Field(ge=0)
    status: str  # active | expired_early | not_applied | expired


class Charge(_Fixture):
    id: str
    kind: str  # airtime_deduction | premium_sms | duplicate | bundle_purchase | bill
    description: str
    amount_kes: int = Field(gt=0)
    hours_ago: float = Field(ge=0)
    refundable: bool = False


class PastComplaint(_Fixture):
    category: str
    days_ago: float = Field(ge=0)
    outcome: str = ""

    @field_validator("category")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"unknown category {value!r}")
        return value


class Account(_Fixture):
    msisdn: str
    name: str
    account_ref: str
    plan: str
    tier: str
    airtime_balance_kes: int = 0
    mpesa_balance_kes: int = 0
    device: str | None = None
    transactions: tuple[Transaction, ...] = ()
    bundles: tuple[Bundle, ...] = ()
    charges: tuple[Charge, ...] = ()
    complaint_history: tuple[PastComplaint, ...] = ()
    last_recredit_days_ago: float | None = None
    airtime_advance_outstanding_kes: int = 0

    @field_validator("msisdn")
    @classmethod
    def _e164(cls, value: str) -> str:
        return normalise_msisdn(value)

    def transaction(self, code: str) -> Transaction | None:
        return next((t for t in self.transactions if t.code == code), None)

    def bundle(self, bundle_id: str) -> Bundle | None:
        return next((b for b in self.bundles if b.id == bundle_id), None)

    def charge(self, charge_id: str) -> Charge | None:
        return next((c for c in self.charges if c.id == charge_id), None)

    def summary(self) -> dict[str, object]:
        """What ``lookup_account`` returns: enough to act on, nothing the tools do not need."""
        return {
            "found": True,
            "account_ref": self.account_ref,
            "name": self.name,
            "plan": self.plan,
            "tier": self.tier,
            "airtime_balance_kes": self.airtime_balance_kes,
            "mpesa_balance_kes": self.mpesa_balance_kes,
            "device": self.device,
            "recent_transactions": [
                {"code": t.code, "type": t.type, "amount_kes": t.amount_kes, "counterparty": t.counterparty,
                 "hours_ago": t.hours_ago, "status": t.status}
                for t in self.transactions
            ],
            "bundles": [
                {"id": b.id, "name": b.name, "status": b.status, "used_mb": b.used_mb, "size_mb": b.size_mb}
                for b in self.bundles
            ],
            "charges": [
                {"id": c.id, "kind": c.kind, "description": c.description, "amount_kes": c.amount_kes,
                 "hours_ago": c.hours_ago}
                for c in self.charges
            ],
        }


class AccountBook(_Fixture):
    version: int = 1
    accounts: tuple[Account, ...]

    @model_validator(mode="after")
    def _unique(self) -> "AccountBook":
        numbers = [a.msisdn for a in self.accounts]
        if len(numbers) != len(set(numbers)):
            raise ValueError("accounts.yaml lists an MSISDN twice")
        return self

    def find(self, msisdn: str) -> Account | None:
        """The account for an E.164 MSISDN, or None for a number the demo does not know."""
        return next((a for a in self.accounts if a.msisdn == msisdn), None)


@lru_cache(maxsize=8)
def load_accounts(path: Path = DEFAULT_ACCOUNTS_PATH) -> AccountBook:
    """Parse and validate the fixture. Cached per path."""
    with path.open(encoding="utf-8") as handle:
        return AccountBook.model_validate(yaml.safe_load(handle) or {})
