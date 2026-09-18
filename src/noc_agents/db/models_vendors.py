"""Vendors and stop-clock events (spec §7.6.1) -- Phase 4 Lane 4A, step 1.

Two tables, both created by the generic additive migration (``db/migrate.py`` walks
``Base.metadata``; ``db/models_all.py`` imports this module before it runs), so there is no
hand-written DDL here and nothing to bump.

``vendors``
    The entity the codebase never had. ``incidents.msp_name`` is a free string copied from
    an assignment pool ("EGYPRO", "TETRANET"); a scorecard cannot be keyed on a string that
    a reassignment can spell differently, and a contract cannot be attached to it. Rows are
    seeded from ``cfg.msp_contacts`` (``services/vendors.py``) and ``incidents.vendor_id`` --
    a nullable Text column that has existed since Phase 1 with nothing writing it -- is
    resolved to a row id from ``msp_name``. The UNIQUE key includes ``active_from`` on
    purpose: a vendor re-contracted on new terms is a NEW row with a new ``active_from``,
    and the old row keeps the incidents (and scorecards) that were computed against the old
    terms. Never UPDATE a vendor row's identity fields to "rename" a vendor.

``incident_clock_events``
    Stop Clock Conditions (SCCs). A stop clock is the commercial crux of §7.6: every minute
    inside a non-reversed event is deducted from the vendor's restore time (AT&T CALNET
    §20.4.7 style). The table records three different clocks and they must not be confused:

    * ``started_at`` / ``ended_at`` -- the condition itself (the vendor-facing interval);
    * ``opened_at`` -- when OUR NOC recorded it. ``opened_at - started_at`` is the
      operator-side discipline counter of §7.6.2 ("SCCs recorded more than 60 min after
      their started_at"). It measures our data hygiene, never the vendor, so it is always
      the server clock and never client-supplied;
    * ``reversed_at`` -- a reversal. A reversed event stays in the table (the claim was
      made and must remain auditable) but deducts nothing; ``reversal_reason`` is mandatory.

    The table has no ``operator_id``: it hangs off an incident, exactly like ``hitl_tasks``
    and ``incident_briefs``, and is scoped through ``incident_id -> incidents.operator_id``
    by ``register_owned_via_incident`` below (see the long comment in ``api/deps.py`` for
    why the join beats a denormalised column).
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.api.deps import register_owned_via_incident
from noc_agents.db.models import Base, new_id, utcnow

#: ``vendors.type`` vocabulary (§7.6.1). A plain string column with no CHECK, so the
#: service layer (``services/vendors.py``) refuses anything else.
VENDOR_TYPES: tuple[str, ...] = ("MSP", "FE_CONTRACTOR", "OEM", "TOWERCO")

#: ``incident_clock_events.scc_code`` vocabulary (§7.6.1), in the spec's order. Closed set:
#: the scorecard's discipline counter looks specifically for ``UTILITY_POWER``, and an
#: evidence pack groups by code, so a typo must fail at the API rather than become an
#: eleventh code that no report knows about.
SCC_CODES: tuple[str, ...] = (
    "END_USER_REQUEST",
    "OBSERVATION",
    "CONTACT_UNAVAILABLE",
    "WIRING_NOT_OURS",
    "UTILITY_POWER",
    "SITE_ACCESS_DENIED",
    "SECURITY_INCIDENT",
    "PLANNED_MAINTENANCE",
    "FORCE_MAJEURE",
    "AWAITING_THIRD_PARTY_PERMIT",
)


class VendorRow(Base):
    """One contracted party under one set of terms (§7.6.1 ``vendors``)."""

    __tablename__ = "vendors"
    __table_args__ = (
        # The natural key noc-seed-v2 matches on too (scripts/seed_v2.py build_vendors), so
        # rows seeded from cfg.msp_contacts here and rows seeded from data/seed/v2/vendors.yaml
        # recognise each other and neither path ever inserts a second EGYPRO.
        UniqueConstraint("operator_id", "code", "active_from", name="uq_vendors_operator_code_from"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    code: Mapped[str] = mapped_column(Text)  # EGYPRO, TETRANET, ... the cfg.msp_contacts key, upper-cased
    display_name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)  # VENDOR_TYPES
    contract_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_from: Mapped[date] = mapped_column(Date)
    active_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    contacts_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")

    @property
    def contacts(self) -> dict:
        return json.loads(self.contacts_json or "{}")

    @contacts.setter
    def contacts(self, value: dict) -> None:
        self.contacts_json = json.dumps(value, sort_keys=True)

    def is_active_on(self, day: date) -> bool:
        """True when ``day`` falls inside ``[active_from, active_to]`` (open-ended when NULL)."""
        if day < self.active_from:
            return False
        return self.active_to is None or day <= self.active_to


class ClockEventRow(Base):
    """One Stop Clock Condition on one incident (§7.6.1 ``incident_clock_events``)."""

    __tablename__ = "incident_clock_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    scc_code: Mapped[str] = mapped_column(Text)  # SCC_CODES
    started_at: Mapped[datetime] = mapped_column(DateTime)  # naive UTC, the storage contract
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # NULL = still open
    opened_by: Mapped[str] = mapped_column(Text)
    opened_role: Mapped[str] = mapped_column(Text)  # only NOC/supervisor roles may open (§7.6.6)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # discipline counter: opened_at - started_at
    reason: Mapped[str] = mapped_column(Text)
    evidence_note_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # work_notes.id on the same incident
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reversed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def is_reversed(self) -> bool:
        return self.reversed_at is not None

    @property
    def is_open(self) -> bool:
        """Still running (no ``ended_at``). A reversed event is not "open" in any useful sense."""
        return self.ended_at is None and self.reversed_at is None


# Scope every read of incident_clock_events through the incident it belongs to, from the
# first query: ``_owned(ClockEventRow)`` joins incidents and filters on operator_id, and
# ``_get_owned`` answers 404 (never 403) for another operator's event.
register_owned_via_incident(ClockEventRow, ClockEventRow.incident_id)
