"""Immutable outage evidence packs (spec §7.6.1, §7.6.4 ``build_evidence_pack``) — Phase 4 Lane 4A.

An evidence pack is the answer to one question asked months later: *what exactly were the
CA, the vendor or the tribunal shown, and is this still the same thing?* §7.6.8 states the
exit criterion as "evidence pack hash stable", and Consumer Protection Regs 2010 reg 12 /
CA licence Condition 12.2 are what make it matter — the pack has to survive three years and
a challenge.

STABILITY IS A PROPERTY OF THE SERIALISATION, NOT A HOPE
--------------------------------------------------------
Three things can silently move a hash. All three are closed here:

1. **Dict iteration order.** Python preserves insertion order, so two code paths that build
   the same mapping in different orders produce different bytes. :func:`canonical_bytes`
   passes ``sort_keys=True``, so key order is a function of the key names and nothing else.
2. **A generation timestamp inside the hashed content.** The obvious mistake is to stamp
   ``generated_at`` into the pack. Then every regeneration is a different pack, the hash is
   never stable, and the exit criterion is unachievable by construction. ``generated_at``
   and ``generated_by`` are therefore **columns on the row**, outside the hash (see
   ``db/models_regulatory.EvidencePackRow``). Nothing this module puts inside ``pack_json``
   is read from the clock.
3. **Row order from the database.** ``SELECT`` without ``ORDER BY`` is unordered by
   definition, and SQLite is happy to change its mind after a VACUUM. Every list inside a
   pack is sorted here, in Python, by an explicit total-order key — never left to the query
   planner, and never left to a sort key that can tie (each one ends in the row id).

Floats are the fourth trap and are avoided rather than tamed: ``adjusted_duration_min`` is
an ``int`` of whole minutes, so no repr/rounding difference can move a byte.

WHAT IS IN A PACK
-----------------
Exactly the §7.6.1 field list::

    {outage_start_at, restored_at, restored_source, adjusted_duration_min, scc_breakdown[],
     users_affected, region, county, planned, force_majeure, notification_timestamps[],
     broadcasts[]}

Plus ``pack_version`` and the incident's identity, because a bare fact list with no schema
marker and no incident number is not evidence of anything. ``pack_version`` is part of the
hashed content on purpose: if the shape ever changes, the hash must change with it rather
than two different shapes colliding on one value.

STOP-CLOCK DEPENDENCY
---------------------
``scc_breakdown`` and ``adjusted_duration_min`` read ``incident_clock_events``, which is
owned by the vendors/clock-events lane (``db/models_vendors.ClockEventRow``). The import is
guarded: this lane must not be the reason the application fails to start if that module is
mid-edit, and a pack with an empty SCC breakdown is a degraded pack (it says so:
``scc_source``), not a wrong one. When the table is there, the deduction follows §7.6.2:
only events with ``reversed_at IS NULL`` count, and only the part of each that overlaps
``[failure_time, restored_at]``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned
from noc_agents.db.models import BroadcastRow, IncidentRow, utcnow
from noc_agents.db.models_regulatory import EvidencePackRow

log = logging.getLogger(__name__)

__all__ = [
    "PACK_VERSION",
    "build_pack",
    "canonical_bytes",
    "get_or_build_pack",
    "latest_pack",
    "pack_sha256",
    "scc_breakdown",
]

#: Bumped whenever the pack's field list changes. Inside the hash (see the module docstring).
PACK_VERSION = 1

#: ``scc_source`` values: what the SCC half of the pack is actually based on. A pack that
#: could not read the stop-clock table says so rather than presenting "no stop clocks" —
#: which is a materially different claim in a vendor dispute — as a fact.
SCC_SOURCE_TABLE = "incident_clock_events"
SCC_SOURCE_UNAVAILABLE = "unavailable"

#: The SCC code that means "nobody could have restored this any faster" (§7.6.1 vocabulary).
#: Surfaced as the pack's ``force_majeure`` flag because it is the single fact a regulator
#: asks about first when a 24-hour outage is reported.
FORCE_MAJEURE_CODE = "FORCE_MAJEURE"


def _clock_event_row() -> type | None:
    """``ClockEventRow`` if the vendors lane has landed it, else None.

    Guarded rather than a module-level import: ``services/evidence.py`` is reachable from the
    API router, so an ImportError here would be an application that does not start. A missing
    stop-clock table is a degraded pack, which the pack itself records; it is not an outage.
    """
    try:
        from noc_agents.db.models_vendors import ClockEventRow
    except Exception:  # noqa: BLE001 — a half-written sibling module must not break startup
        log.warning("evidence: incident_clock_events model unavailable; packs will carry no SCC breakdown")
        return None
    return ClockEventRow


# --------------------------------------------------------------------------- serialisation


def _iso(dt: datetime | None) -> str | None:
    """A stored (naive UTC) timestamp as ``"2026-09-16T09:00:00Z"``.

    Explicitly ``Z``, never a naive string: a pack is read by people and by browsers, and a
    naive ISO string is read in Nairobi as EAT — which backdates every fact in the pack by
    three hours. Same defect (#41) the serializers close with ``z_utc``; done with a string
    here because the pack is JSON on the way to a hash, not a pydantic model.
    """
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def canonical_bytes(pack: dict[str, Any]) -> bytes:
    """The exact bytes the hash is taken over.

    ``sort_keys`` removes insertion-order dependence; the compact separators remove
    whitespace drift; ``ensure_ascii=True`` (the default, stated here because it matters)
    means the output is pure ASCII, so no encoding or normalisation choice downstream can
    change a byte. ``default=str`` is a backstop only — ``build_pack`` already converts every
    datetime itself, and a value that reaches ``default`` would be a bug worth noticing.
    """
    return json.dumps(pack, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("ascii")


def pack_sha256(pack: dict[str, Any]) -> str:
    """Hex sha256 of :func:`canonical_bytes`. The value stored in ``evidence_packs.sha256``."""
    return hashlib.sha256(canonical_bytes(pack)).hexdigest()


# ----------------------------------------------------------------------------- stop clocks


def _overlap_minutes(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> int:
    """Whole minutes of ``[start, end]`` that fall inside ``[window_start, window_end]``.

    Truncated, not rounded: a deduction claimed against a vendor should never be larger than
    the evidence supports, and ``int()`` on a positive number is the conservative direction.
    """
    lo = max(start, window_start)
    hi = min(end, window_end)
    if hi <= lo:
        return 0
    return int((hi - lo).total_seconds() // 60)


def scc_breakdown(session: Session, inc: IncidentRow) -> tuple[list[dict[str, Any]], str]:
    """``(rows, source)`` — one entry per stop-clock event on the incident, deterministically ordered.

    The deduction rule is §7.6.2's: a **reversed** event stays in the breakdown (the claim
    was made and must remain auditable) but contributes ``deducted_minutes=0``. The window is
    ``[failure_time, restored_at]``; an event that ran entirely before the failure or after
    the restore deducts nothing even though it is a real event on the incident.

    Ordering is ``(started_at, scc_code, id)``. The id is there so the key cannot tie: two
    ``UTILITY_POWER`` events opened in the same second would otherwise swap places between
    two generations and move the hash.
    """
    model = _clock_event_row()
    if model is None:
        return [], SCC_SOURCE_UNAVAILABLE

    window_start = inc.failure_time or inc.outage_start_at
    window_end = inc.restored_at
    events: Iterable[Any] = session.scalars(_owned(model).where(model.incident_id == inc.id)).all()
    rows: list[dict[str, Any]] = []
    for ev in events:
        deducted = 0
        # Only a non-reversed, closed interval inside the outage window deducts. An event
        # still open (ended_at IS NULL) has no measured length yet: it is reported with
        # deducted_minutes=0 rather than silently extrapolated to "now", which would make the
        # pack's arithmetic depend on when it was generated — the one thing it must not do.
        if ev.reversed_at is None and ev.ended_at is not None and window_start is not None and window_end is not None:
            deducted = _overlap_minutes(ev.started_at, ev.ended_at, window_start, window_end)
        rows.append(
            {
                "id": ev.id,
                "scc_code": ev.scc_code,
                "started_at": _iso(ev.started_at),
                "ended_at": _iso(ev.ended_at),
                "opened_by": ev.opened_by,
                "opened_role": ev.opened_role,
                "opened_at": _iso(ev.opened_at),
                "reason": ev.reason,
                "reversed": ev.reversed_at is not None,
                "reversal_reason": ev.reversal_reason,
                "deducted_minutes": deducted,
            }
        )
    rows.sort(key=lambda r: (r["started_at"] or "", r["scc_code"], r["id"]))
    return rows, SCC_SOURCE_TABLE


# ----------------------------------------------------------------------------------- build


def _notification_timestamps(session: Session, inc: IncidentRow) -> list[dict[str, Any]]:
    """Every regulatory clock on the incident, as the pack records it.

    Imported lazily and read through ``_owned`` like everything else. Ordered by kind (the
    vocabulary is closed and unique per incident, so the key cannot tie).
    """
    from noc_agents.db.models_regulatory import RegulatoryNotificationRow

    rows = session.scalars(_owned(RegulatoryNotificationRow).where(RegulatoryNotificationRow.incident_id == inc.id)).all()
    out = [
        {
            "kind": n.kind,
            "status": n.status,
            "clock_started_at": _iso(n.clock_started_at),
            "due_at": _iso(n.due_at),
            "approved_by": n.approved_by,
            "approved_at": _iso(n.approved_at),
            "sent_at": _iso(n.sent_at),
            "external_ref": n.external_ref,
        }
        for n in rows
    ]
    out.sort(key=lambda r: r["kind"])
    return out


def _broadcasts(session: Session, inc: IncidentRow) -> list[dict[str, Any]]:
    """What the operator told whom, and when. Ordered by ``(sent_at, channel, audience, id)``.

    The message body is **not** in the pack. A broadcast body is already stored on
    ``broadcasts``; copying it here would duplicate personal-data-bearing text into a second
    record with a three-year retention floor, for no evidential gain over the row reference.

    **Scoping.** ``broadcasts`` carries no ``operator_id`` and is not registered with
    ``api.deps.register_owned_via_incident``, so ``_owned`` cannot build its clause. It is
    owned through its parent instead: ``inc`` was fetched with ``_get_owned`` (which applies
    the operator clause and 404s on another operator's incident), so filtering on
    ``incident_id == inc.id`` is bounded by that fetch — in the WHERE clause, not by a check
    after the read. Same argument as ``pir_action_items`` in ``db/models_pir.py``. This lane
    does not register the table itself: that is a global change to a shared dict, and
    ``broadcasts`` belongs to the Phase 2 alerting code, not here.
    """
    rows = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id)).all()
    out = [
        {
            "id": b.id,
            "channel": b.channel,
            "audience": b.audience,
            "status": b.status,
            "sent_at": _iso(b.sent_at),
        }
        for b in rows
    ]
    out.sort(key=lambda r: (r["sent_at"] or "", r["channel"], r["audience"], r["id"]))
    return out


def build_pack(session: Session, inc: IncidentRow) -> dict[str, Any]:
    """The §7.6.1 pack for one incident. Pure with respect to the clock: reads rows, never ``utcnow``.

    Call it twice on an unchanged database and you get two equal dicts and therefore one
    hash — that is the whole contract, and ``tests/unit/test_evidence_pack.py`` pins it.
    """
    breakdown, scc_source = scc_breakdown(session, inc)
    deducted = sum(int(r["deducted_minutes"]) for r in breakdown)

    outage_start = inc.failure_time or inc.outage_start_at
    raw_minutes: int | None = None
    if outage_start is not None and inc.restored_at is not None:
        raw_minutes = max(0, int((inc.restored_at - outage_start).total_seconds() // 60))
    adjusted: int | None = None if raw_minutes is None else max(0, raw_minutes - deducted)

    return {
        "pack_version": PACK_VERSION,
        "incident_id": inc.id,
        "incident_number": inc.incident_number,
        "priority": inc.priority,
        "site_id": inc.site_id,
        "site_type": inc.site_type,
        # --- the §7.6.1 field list ---
        "outage_start_at": _iso(outage_start),
        "restored_at": _iso(inc.restored_at),
        # NULL provenance is reported as null, not as a guess. §7.6.2's data-quality gate
        # exists precisely because "we do not know how the restore time was set" is a fact a
        # vendor is entitled to see on the evidence they are being billed against.
        "restored_source": inc.restored_source,
        "adjusted_duration_min": adjusted,
        "raw_duration_min": raw_minutes,
        "scc_minutes_deducted": deducted,
        "scc_source": scc_source,
        "scc_breakdown": breakdown,
        "users_affected": int(inc.users_affected or 0),
        "region": inc.region_code,
        "county": inc.county,
        "planned": bool(inc.planned_maintenance),
        "force_majeure": any(r["scc_code"] == FORCE_MAJEURE_CODE and not r["reversed"] for r in breakdown),
        "notification_timestamps": _notification_timestamps(session, inc),
        "broadcasts": _broadcasts(session, inc),
    }


# ------------------------------------------------------------------------------ persistence


def latest_pack(session: Session, incident_id: str) -> EvidencePackRow | None:
    """The most recently generated pack for the incident, or None.

    Ordered by ``(generated_at, id)``: two packs generated inside one second must still have
    a defined "latest", and the id breaks the tie deterministically.
    """
    return session.scalars(
        _owned(EvidencePackRow)
        .where(EvidencePackRow.incident_id == incident_id)
        .order_by(EvidencePackRow.generated_at.desc(), EvidencePackRow.id.desc())
    ).first()


def get_or_build_pack(
    session: Session,
    inc: IncidentRow,
    *,
    generated_by: str,
    refresh: bool = False,
) -> tuple[EvidencePackRow, bool]:
    """``(row, created)`` — the incident's pack, generating one only when it must.

    Default (``refresh=False``): the stored pack is returned untouched if there is one. A
    pack is evidence of what a human was shown; regenerating it on every read would quietly
    replace that with "whatever the database says today".

    ``refresh=True`` rebuilds from the current rows and stores the result **only if its hash
    differs** from the latest stored pack. That is what makes the §7.6.8 exit criterion
    structural rather than conventional: repeated refreshes of an unchanged incident cannot
    accumulate rows, and cannot produce a second hash. When the facts really have changed, a
    NEW row is appended and the old one keeps its own hash — the table is never updated.

    Runs inside the caller's transaction and commits nothing.
    """
    existing = latest_pack(session, inc.id)
    if existing is not None and not refresh:
        return existing, False

    pack = build_pack(session, inc)
    digest = pack_sha256(pack)
    if existing is not None and existing.sha256 == digest:
        return existing, False

    row = EvidencePackRow(
        operator_id=inc.operator_id,
        incident_id=inc.id,
        generated_at=utcnow(),  # a COLUMN, never a pack field — see the module docstring
        generated_by=generated_by,
        sha256=digest,
        pack_json=canonical_bytes(pack).decode("ascii"),
    )
    session.add(row)
    session.flush()
    return row, True
