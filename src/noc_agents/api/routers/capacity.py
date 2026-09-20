"""Capacity routes (spec §7.5.2; ``CAPACITY_ENABLED=false``) — Phase 5 Lane 5A.

    POST /api/v1/capacity/observations        JSON samples          (planning/admin)
    POST /api/v1/capacity/observations/csv    a CSV export          (planning/admin)
    GET  /api/v1/capacity/advisories          what Planning is told

plus four routes §7.5.2 does not list, each of which exists because the shipped data model is
otherwise unreachable — the same kind of reasoned deviation ``routers/maintenance.py`` and
``routers/pir.py`` document:

    GET  /api/v1/capacity/observations                what was ingested, to check a feed
    GET  /api/v1/capacity/sites/{site_id}             the live reading per cell, verdict included
    GET  /api/v1/capacity/advisories/{id}             one advisory with its working
    POST /api/v1/capacity/advisories/{id}/review      a named human says "seen" or "no action"

Without the site read there is no way to see *why* a cell has no advisory — and "no advisory"
has two very different meanings, "this cell is fine" and "this lane has not been given enough
data to say", which §7.5 is emphatic must never be confused. Without the review route
``capacity_advisories.status`` has exactly one reachable value and every advisory ever opened
stays OPEN for ever. None of them adds a column, a status or a vocabulary.

§7.5.2 lists one ingest path for both CSV and JSON. It is two here because one FastAPI
operation cannot declare both a multipart form and a JSON body: the content type decides which
parser runs, and accepting both on one path means reading the raw request and hand-rolling the
negotiation Starlette already does. Same surface, same roles, same service call.

**WHAT THIS MODULE DELIBERATELY DOES NOT OFFER.** There is no route that turns an advisory
into anything. No ``/advisories/{id}/schedule``, no ``/advisories/{id}/raise-window``, no
``proposed_payload`` and no HITL card. §7.5.3: "advisory routed to Planning; never an upgrade
order." Booking an outage is the maintenance lane's business and it costs two named human
approvals (``APPROVE_SCHEDULE`` and ``APPROVE_MAINTENANCE_WINDOW``) precisely because it takes
live customers off air; a convenience route here would be a third door into that decision with
none of its checks. If Planning acts on an advisory, they act on it in the maintenance lane and
the advisory is the argument they took with them.

**The flag is a 404, not a 403.** With ``CAPACITY_ENABLED`` unset the lane is supposed to be
invisible: today's system has no ``/capacity`` surface at all, and a 403 would announce that
the feature exists and the caller is merely not allowed it — a different, and false, statement.
The choice ``routers/maintenance.py``, ``routers/pir.py`` and ``routers/clocks.py`` all make.

**Operator scoping (§8).** Both capacity tables carry ``operator_id``, so ``_owned`` and
``_get_owned`` filter them directly and answer 404 (never 403) for another operator's row. Two
operators may legitimately run cells at the same ``site_id`` string, so the operator clause is
in the WHERE of every read — including the aggregates, which go through
``services.capacity``'s own scoped helpers rather than assembling a query here.

**Uploads.** The CSV route hands ``services.uploads`` an iterable read in chunks and never
calls ``await request.body()``: the size cap has to be enforced *while* consuming, or it is not
a cap. ``uploads.store()`` only ever sees an ``AcceptedUpload``, which is what makes "a rejected
file is never written to disk" structural rather than a rule somebody has to remember. The CSV
arrives as the raw request body rather than a multipart form, because ``python-multipart`` is
not a dependency of this project and one ``UploadFile`` parameter anywhere in a registered
router makes the whole application fail to import without it — see the route's own docstring.
"""

from __future__ import annotations

import json
from datetime import datetime

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import READERS, SUPERVISORS, _actor, _get_owned, _owned, _settings
from noc_agents.db.models import AuditRow, get_session
from noc_agents.db.models_capacity import ADVISORY_OPEN, CapacityAdvisoryRow, CapacityObservationRow
from noc_agents.services import capacity as svc
from noc_agents.services import sites as site_catalogue
from noc_agents.services import uploads

router = APIRouter(prefix="/api/v1", tags=["capacity"])

#: §7.5.2 restricts capacity ingest to ``planning``/``admin``, and ``uploads.POLICIES`` records
#: the same pair beside the 5 MB cap. Read from there rather than retyped, so the permission and
#: the upload policy cannot drift apart — which is the reason ``UploadPolicy`` carries ``roles``
#: in the first place.
INGEST_ROLES: tuple[str, ...] = uploads.policy_for(uploads.KIND_CAPACITY_CSV).roles

#: Who may record that advice has been read. Planning owns the advisory; supervisors are
#: included because a duty manager clearing a stale queue at 03:00 is a real thing that happens
#: and the alternative is a page nobody can ever tidy. ``deps.py`` has no tuple for this pair,
#: so it is named here rather than by editing a shared file (``routers/maintenance.py`` does the
#: same for its ``PLANNERS``).
REVIEWERS: tuple[str, ...] = ("planning",) + SUPERVISORS


def require_capacity_enabled() -> None:
    """404 the whole lane while ``CAPACITY_ENABLED`` is off (the default)."""
    if not svc.capacity_enabled():
        raise HTTPException(
            404,
            f"capacity observations are not enabled on this deployment ({svc.CAPACITY_ENABLED_ENV}=false)",
        )


_ENABLED = Depends(require_capacity_enabled)


# ------------------------------------------------------------------------------- bodies


class ObservationIn(BaseModel):
    """One sample. ``busy_hour_at`` may carry an offset; a naive value is read in ``naive_tz``."""

    site_id: str
    value: float
    busy_hour_at: datetime
    cell_id: str | None = None
    metric: str | None = None
    source: str | None = None


class ObservationsIn(BaseModel):
    """A batch of samples, all or nothing (see :func:`services.capacity.parse_csv`)."""

    observations: list[ObservationIn]
    #: Which zone a naive ``busy_hour_at`` is in — ``UTC`` (the storage contract, §7.0.6) or
    #: ``EAT``. Never inferred from the data: three hours of silent error moves every sample
    #: out of the busy hour it describes and out of the maintenance window that should have
    #: excluded it.
    naive_tz: str = svc.NAIVE_TZ_UTC


class ReviewIn(BaseModel):
    """``ACKNOWLEDGED`` ("seen, looking at it") or ``CLOSED`` ("decided"), with a note."""

    status: str
    note: str | None = None
    actor: str | None = None


# ------------------------------------------------------------------------------ helpers


def _cfg():
    return _settings().operator


def _audit(session, *, actor: str, action: str, entity_type: str, entity_id: str, rationale: str, payload: dict) -> None:
    session.add(
        AuditRow(
            operator_id=_settings().operator.operator_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            rationale=rationale,
            payload_json=json.dumps(payload, default=str),
        )
    )


def _rejected(exc: svc.CapacityRejected) -> HTTPException:
    """422 with **every** reason, not the first one (``services/pir.py`` house style)."""
    return HTTPException(422, {"message": "capacity submission rejected", "errors": list(exc.errors)})


def _sync_chunks(stream):
    """A **synchronous** iterable over an async request stream.

    ``uploads.accept`` is a sync function that consumes an ``Iterable[bytes]`` and raises at the
    first byte past the policy cap; the request body arrives as an *async* iterator. The bridge
    is anyio's, and it is used rather than the obvious ``await request.body()`` for the reason
    ``services/uploads.py`` states in its first rule: a single read materialises the whole body
    before any limit can apply, at which point the cap describes what already happened instead
    of preventing it. Here ``accept`` runs in a worker thread and pulls one chunk at a time back
    through the event loop, so a refused upload stops arriving the moment it is refused.

    ``anyio`` is not a new dependency: Starlette runs on it, and this is the portal it exists to
    provide. The same pattern would apply to a multipart form, which this route deliberately is
    not — ``python-multipart`` is not in ``pyproject.toml`` and an ``UploadFile`` parameter
    makes the whole application fail to import without it.
    """

    def generator():
        while True:
            try:
                yield anyio.from_thread.run(stream.__anext__)
            except StopAsyncIteration:
                return

    return generator()


# -------------------------------------------------------------------------------- ingest


@router.post("/capacity/observations", dependencies=[_ENABLED])
def post_observations(
    body: ObservationsIn,
    principal: auth.Principal = Depends(require_role(*INGEST_ROLES)),
) -> dict:
    """Ingest samples as JSON (§7.5.2). All or nothing; duplicates are a no-op, not an error."""
    if not body.observations:
        raise HTTPException(422, "no observations supplied")

    session = get_session()
    try:
        cfg = _cfg()
        drafts = []
        errors: list[str] = []
        for position, item in enumerate(body.observations, start=1):
            draft, row_errors = svc.validate_observation(
                item.model_dump(),
                cfg=cfg,
                naive_tz=body.naive_tz,
                default_source="MANUAL",
            )
            if row_errors:
                errors.extend(f"observation {position}: {message}" for message in row_errors)
            elif draft is not None:
                drafts.append(draft)
        if errors:
            raise _rejected(svc.CapacityRejected(errors))

        report = svc.ingest_observations(
            session,
            drafts,
            actor=_actor(principal, None),
            naive_tz=body.naive_tz,
            source="MANUAL",
        )
        session.commit()
        return report.as_json()
    finally:
        session.close()


@router.post("/capacity/observations/csv", dependencies=[_ENABLED])
async def post_observations_csv(
    request: Request,
    naive_tz: str = svc.NAIVE_TZ_UTC,
    store_file: bool = False,
    filename: str | None = None,
    principal: auth.Principal = Depends(require_role(*INGEST_ROLES)),
) -> dict:
    """Ingest a capacity CSV (§7.5.2, ≤ 5 MB; §7.5.4's ``data/seed/v2/capacity_sample.csv``).

    **The CSV is the request body**, not a multipart part: ``POST`` the file with
    ``Content-Type: text/csv`` and the options as query parameters. A form upload would need
    ``python-multipart``, which is not a dependency of this project — and a ``UploadFile``
    parameter in a registered router makes the *entire application* fail to import when it is
    missing, which is a steep price for a convenience. Nothing is lost: the body is still
    streamed, ``uploads.accept`` still sniffs the bytes, still enforces the 5 MB cap while
    reading, and ``uploads.store`` still refuses anything that was not accepted.

    ``Content-Length`` **is** handed to ``accept()`` here, which it could not be for a multipart
    request: with a raw body the declared length describes the CSV itself, so a declared 5 GB is
    refused without reading a byte (§7.9.5's early refusal). It remains a courtesy and never the
    enforcement — a chunked request need not send one, and a liar can send any number.

    ``store_file`` is off by default. The rows are the record; keeping the file as well is a
    second copy of the same data on a disk with its own retention question, and §9.4 has no
    class for it. It is offered because a disputed advisory is much easier to settle against the
    bytes that were actually uploaded.
    """
    if not uploads.uploads_enabled():
        # The second gate is the upload lane's own, not this one's: turning on capacity must not
        # silently turn on a file-upload path that §7.9.5 ships off. JSON ingest still works.
        raise HTTPException(
            404,
            f"file upload is not enabled on this deployment ({uploads.UPLOADS_ENABLED_ENV}=false); "
            "POST /api/v1/capacity/observations accepts JSON",
        )

    declared = request.headers.get("content-type")
    stream = request.stream()
    cfg = _cfg()
    session = get_session()
    try:
        actor = _actor(principal, None)
        try:
            # accept() blocks while it consumes, so it runs in a worker thread and pulls the
            # body back through the loop one chunk at a time; see _sync_chunks.
            accepted = await anyio.to_thread.run_sync(
                lambda: uploads.accept(
                    uploads.KIND_CAPACITY_CSV,
                    _sync_chunks(stream),
                    filename=filename,
                    declared_content_type=declared,
                    content_length=request.headers.get("content-length"),
                )
            )
            drafts = svc.parse_csv(accepted.text, cfg=cfg, naive_tz=naive_tz)
        except uploads.UploadRejected as exc:
            # §7.9.5: every rejection is audited. A refusal nobody can count is
            # indistinguishable from an endpoint nobody is attacking.
            _audit(
                session,
                actor=actor,
                action="upload.rejected",
                entity_type="capacity_observation",
                entity_id="-",
                rationale=exc.reason,
                payload=uploads.rejection_payload(
                    exc, kind=uploads.KIND_CAPACITY_CSV, filename=filename, declared=declared
                ),
            )
            session.commit()
            raise HTTPException(exc.status_code, exc.message) from exc
        except svc.CapacityRejected as exc:
            raise _rejected(exc) from exc

        report = svc.ingest_observations(session, drafts, actor=actor, naive_tz=naive_tz, source="CSV")
        body = report.as_json()
        body["sha256"] = accepted.sha256
        body["filename"] = accepted.filename
        if store_file:
            stored = uploads.store(accepted)
            body["stored_name"] = stored.stored_name
        session.commit()
        return body
    finally:
        session.close()


# --------------------------------------------------------------------------------- reads


@router.get("/capacity/observations", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def list_observations(
    site_id: str | None = None,
    cell_id: str | None = None,
    metric: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """Raw samples, newest first. For checking a feed, not for judging a cell."""
    session = get_session()
    try:
        stmt = _owned(CapacityObservationRow)
        if site_id:
            stmt = stmt.where(CapacityObservationRow.site_id == site_id.strip())
        if cell_id:
            stmt = stmt.where(CapacityObservationRow.cell_id == cell_id.strip())
        if metric:
            stmt = stmt.where(CapacityObservationRow.metric == metric.strip().upper())
        if since is not None:
            # Naive UTC is the storage contract (§7.0.6): an aware bound is converted, never
            # compared against naive column values.
            bound = since if since.tzinfo is None else since.astimezone(svc.UTC).replace(tzinfo=None)
            stmt = stmt.where(CapacityObservationRow.busy_hour_at >= bound)
        rows = session.scalars(
            stmt.order_by(CapacityObservationRow.busy_hour_at.desc()).limit(max(1, min(limit, 1000)))
        ).all()
        return [svc.observation_out(r) for r in rows]
    finally:
        session.close()


@router.get("/capacity/sites/{site_id}", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def get_site_capacity(site_id: str) -> dict:
    """The live reading for every cell at one site — including "not enough data to say".

    One reading per cell and never one averaged figure for the site: three sectors at 90 %,
    40 % and 40 % average to a comfortable 57 %, and the sector that is actually full is the one
    carrying the complaints. The site-level roll-up is a count of cells per verdict.
    """
    session = get_session()
    try:
        readings = svc.site_readings(session, _cfg(), site_id.strip())
        site = site_catalogue.lookup_site(site_id.strip())
        verdicts: dict[str, int] = {}
        for reading in readings:
            verdicts[reading.verdict] = verdicts.get(reading.verdict, 0) + 1
        return {
            "site_id": site_id.strip(),
            "site_name": site.site_name if site else None,
            "region_code": site.region_code if site else None,
            "cells": [r.as_json() for r in readings],
            "verdicts": verdicts,
            # Spelled out rather than left to be inferred from an empty list: a site this lane
            # has never received a sample for is not a site with no capacity problem.
            "has_data": bool(readings),
        }
    finally:
        session.close()


@router.get("/capacity/advisories", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def list_advisories(status: str | None = None, site_id: str | None = None, limit: int = 200) -> list[dict]:
    """§7.5.2's advisory list, newest first. Advice for Planning; it authorises nothing."""
    session = get_session()
    try:
        stmt = _owned(CapacityAdvisoryRow)
        if status:
            stmt = stmt.where(CapacityAdvisoryRow.status == status.strip().upper())
        if site_id:
            stmt = stmt.where(CapacityAdvisoryRow.site_id == site_id.strip())
        rows = session.scalars(
            stmt.order_by(CapacityAdvisoryRow.opened_at.desc()).limit(max(1, min(limit, 1000)))
        ).all()
        return [svc.advisory_out(r) for r in rows]
    finally:
        session.close()


@router.get("/capacity/advisories/{advisory_id}", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def get_advisory(advisory_id: str) -> dict:
    """One advisory with the day-by-day working it was opened on.

    The evidence is served **as it was recorded**, never recomputed on read: the site read
    answers "what is true now", this answers "what was this advisory actually based on", and
    the two diverge the moment more data arrives or somebody edits the trigger. An advisory
    that recomputes its own justification is an advisory that can quietly rewrite it.

    404 for another operator's id, never 403 (``api/deps._get_owned`` explains why).
    """
    session = get_session()
    try:
        row = _get_owned(session, CapacityAdvisoryRow, advisory_id, what="capacity advisory")
        body = svc.advisory_out(row)
        body["open"] = row.status == ADVISORY_OPEN
        return body
    finally:
        session.close()


# -------------------------------------------------------------------------------- review


@router.post("/capacity/advisories/{advisory_id}/review", dependencies=[_ENABLED])
def review_advisory(
    advisory_id: str,
    body: ReviewIn,
    principal: auth.Principal = Depends(require_role(*REVIEWERS)),
) -> dict:
    """Record that a named human read the advice (``ACKNOWLEDGED``) or decided it (``CLOSED``).

    This is the only write a person makes against an advisory, and it changes nothing outside
    the advisory: no window is booked, no task is proposed, no incident moves. Closing one means
    "Planning has decided", not "the work is approved" — approving work is the maintenance
    lane's two gates, and nothing here can reach them.
    """
    session = get_session()
    try:
        row = _get_owned(session, CapacityAdvisoryRow, advisory_id, what="capacity advisory")
        try:
            svc.review_advisory(
                session,
                row,
                status=body.status,
                actor=_actor(principal, body.actor),
                note=body.note,
            )
        except svc.CapacityRejected as exc:
            raise _rejected(exc) from exc
        session.commit()
        session.refresh(row)
        return svc.advisory_out(row)
    finally:
        session.close()
