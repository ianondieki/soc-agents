"""Agent memory, step M0 — "what happened here before", read straight off the tables we have.

Spec §7.11 (Phase 4 Lane 4C, step M0). Nothing in the pipeline reads across incidents:
the last outage at this site and what actually fixed it are already in ``incidents`` and
``work_notes`` and are never recalled. M0 is the smallest honest version of that recall —
**pure SQL over the two existing tables, no new table, no job, no LLM, no network**. The
derived ``memory_episodes`` index, the FTS5 lexical tier, the vector tier, L2 facts, L3
playbooks and L4 memos are M1-M4; this module is deliberately the part of §7.11.4 that can
ship without a schema change, so the floor can say whether it is useful before anything is
created that would have to be migrated away.

WHAT THIS MODULE IS AND IS NOT
------------------------------
* **Advisory and inert (MEM1/G15).** Nothing here is read by ``services/priority.py``,
  ``services/assignment.py``, ``services/composition.py``, ``services/numbering.py``,
  ``services/lifecycle.py``, ``agents/correlate.py``, ``agents/severity.py`` or
  ``agents/assign.py``. The lifecycle does not import this module at all in M0 — the only
  caller is ``api/routers/memory.py``, a read route. ``tests/unit/test_memory_recall.py``
  pins that with an AST walk *and* with a byte-identical lifecycle run.
* **Read-only.** No function here commits, flushes or adds a row. M0 touches nothing on the
  hot path, so ``runner._fail_closed`` has nothing of ours to roll back (MEM5).
* **Operator-scoped by construction (MEM10).** Every query starts from
  ``api.deps._owned(IncidentRow)``. Recall is the most dangerous surface in this system for
  cross-operator leakage precisely because it *deliberately* reaches across incidents:
  ``config/default.yaml`` points both operator profiles at one SQLite file, so a missing
  ``WHERE operator_id = ?`` hands Safaricom's site history to Airtel. There is no
  post-fetch filter anywhere below — the scoping is in the statement or it does not exist.
* **Degrades to empty, never to an error (MEM4).** Every public function is wrapped so that
  a missing column, a bad row or a half-migrated database returns ``()``. The
  "Earlier at this site" panel rendering blank is acceptable; the incident workspace failing
  to load because recall raised is not. Returning ``()`` is also the fail-*safe* direction
  for the isolation rule above: the empty answer can never be another operator's rows.

WHERE THE FLAG IS CHECKED
-------------------------
``MEMORY_ENABLED`` (default **false**) is checked by the **entry points** — today that is
``GET /api/v1/memory/sites/{site_id}``, in M1 also ``recall_for_incident()`` and the
``advisory`` serializer key — and deliberately **not** inside the ``recall_*`` functions
themselves. Two reasons: the spec's own signatures (§7.11.4) give ``recall_site_history``
no config argument, and M1's consolidator calls these same functions to *build* the episode
index, which must keep working independently of whether reads are switched on. Use
:func:`memory_enabled` at the point where memory reaches a human, not below it.

M0 SEMANTICS WORTH KNOWING BEFORE YOU BUILD M1 ON THEM
-----------------------------------------------------
* An "episode" is an incident in ``RESTORED`` or ``CLOSED``. M1's ``memory_episodes`` is one
  row per such incident, so the population is the same; only the storage differs.
* ``restore_minutes`` is NULL unless ``restored_source`` is ``MARK_RESTORED`` or
  ``SUPERVISOR`` (§7.0.8, the M4 rule). ``VENDOR_NOTE_INFERRED`` is a regex hit on a vendor's
  free text (brief defect #4) and ``close_incident`` back-fills ``restored_at = closed_at``
  with no provenance at all; both produce a number that looks like an MTTR and is not one.
  A wrong duration here becomes a wrong median in M2/M3, so it is refused at the source.
  The 3xIQR outlier rule of §7.11.7 needs a fault-class population and belongs to M1's
  consolidator, not to a per-row read.
* Every free-text value that leaves this module passes ``llm/redaction.py`` — the *same*
  ``NameMap`` seeding ``redact_incident`` uses (``assignee_name``/``fe_name``/``rnio_name``
  plus the note authors), never a second name list. §7.11.8 rule 1: no person's name in any
  memory output. The honest limit is recorded in ``redaction.py`` itself: there is no NER,
  so a name typed into a note body that appears in none of those fields is not recognised.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned, _settings
from noc_agents.config import OperatorConfig
from noc_agents.db.models import IncidentRow, WorkNoteRow, utcnow
from noc_agents.domain.enums import IncidentStatus
from noc_agents.llm.redaction import NameMap, scrub_text
from noc_agents.services.lifecycle import (
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_SUPERVISOR,
    note_declares_restored,
)

log = logging.getLogger("noc_agents.services.memory")

__all__ = [
    "MEMORY_ENABLED_ENV",
    "SimilarEpisode",
    "SUMMARY_MAX_CHARS",
    "episode_dict",
    "episode_dicts",
    "episode_statuses",
    "fault_class",
    "memory_enabled",
    "recall_similar_episodes",
    "recall_site_history",
]

# --------------------------------------------------------------------------- flag

#: §7.11.3. An environment variable, not a YAML key: ``OperatorConfig`` is a pydantic model
#: with the default ``extra="ignore"``, so a ``memory:`` block in the operator profile is
#: silently dropped until the field exists on the model. Thresholds move to YAML in M1,
#: when there is something to threshold.
MEMORY_ENABLED_ENV = "MEMORY_ENABLED"

#: Spellings that read as true. The same set as ``agents/enrich.py`` and ``pollers/weather.py``
#: use, spelled out again rather than imported so that a flag-off process does not import the
#: weather lane to answer a question about memory.
_TRUE = frozenset({"1", "true", "yes", "on"})


def memory_enabled() -> bool:
    """``MEMORY_ENABLED`` — default **false**. Only an explicit true value switches recall on.

    Read at call time, never cached: the tests flip it with ``monkeypatch.setenv`` and the
    demo flips it from ``.env`` between runs. Costs one ``os.getenv`` on the off path.
    """
    return (os.getenv(MEMORY_ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------------------- shape


@dataclass(frozen=True)
class SimilarEpisode:
    """One prior incident at a site, as a human should read it (§7.11.4).

    Frozen because a recall result is evidence: a caller that wants to change a value is
    almost certainly about to let memory influence a decision, which MEM1 forbids.

    ``resolution_summary`` is scrubbed and truncated to :data:`SUMMARY_MAX_CHARS`;
    ``restore_minutes`` is ``None`` whenever its provenance is not trustworthy (see the
    module docstring), which is a different statement from "restored in 0 minutes".
    """

    incident_id: str
    incident_number: str
    site_id: str
    fault_class: str
    closed_at: datetime
    restore_minutes: int | None
    resolution_code: str
    resolution_summary: str
    match_reason: str
    score: float


#: §7.11.3 caps evidence text at 240 characters. It is a cap on how much attacker-reachable
#: vendor free text can travel with a recall hit (MEM9), not a display preference, so the
#: same number is applied here even though M0 renders to a human rather than to a prompt.
SUMMARY_MAX_CHARS = 240

#: An episode is a finished incident. ``CANCELLED`` is excluded deliberately: a cancelled
#: ticket is a NOC bookkeeping action, not something that happened at the site, and counting
#: it would inflate every "Nth outage here" figure M1 computes from the same population.
_EPISODE_STATUSES: tuple[str, ...] = (
    IncidentStatus.RESTORED.value,
    IncidentStatus.CLOSED.value,
)

#: Provenance values whose ``restored_at`` a duration may be computed from (§7.0.8, M4).
_TRUSTED_RESTORE_SOURCES = frozenset({RESTORE_SOURCE_MARK, RESTORE_SOURCE_SUPERVISOR})

#: Hard ceiling on rows a single recall may return, whatever the caller asks for. A recall is
#: a read that reaches across incidents; an unbounded ``limit`` from a query string would be
#: a full table scan serialised into an advisory panel.
_MAX_ROWS = 200

#: Generative-Agents recency decay (§7.11.1, verified): 0.995 per **day** rather than per
#: sandbox hour, because a NOC's "recent" is days.
_RECENCY_DECAY_PER_DAY = 0.995

#: Priority rank term of the impact blend (§7.11.4): P1 = 1.0 ... P4 = 0.25.
_PRIORITY_RANK: dict[str, float] = {"P1": 1.0, "P2": 0.75, "P3": 0.5, "P4": 0.25}

#: Fallback full-scale for the users-affected term when the operator profile cannot be read.
#: 500,000 is the P1 floor in ``config/operators/safaricom.yaml`` (``P2_max_users: 499999``),
#: so 1.0 means "a P1-scale outage" rather than an arbitrary constant.
_DEFAULT_USERS_FULL_SCALE = 500_000

#: Relevance term for the exact tier (§7.11.4). M1 adds 0.7 for "same fault class, different
#: site" and the bm25/cosine tiers below that; an exact hit must always outrank both.
_RELEVANCE_EXACT = 1.0

#: ``match_reason`` strings. Fixed vocabulary so the UI and M1's ranking agree on them.
MATCH_SAME_SITE = "same site"
MATCH_SAME_SITE_AND_FAULT_CLASS = "same site + same fault class"

#: What a missing ``failure_domain`` / ``site_type`` canonicalises to — the same defaults the
#: ORM columns carry (``db/models.py``), so a row written before a column existed and a row
#: written today land in the same fault class.
_DOMAIN_DEFAULT = "UNKNOWN"
_SITE_TYPE_DEFAULT = "BTS"


def episode_statuses() -> tuple[str, ...]:
    """The incident statuses M0 counts as history. Exposed so M1's consolidator shares it."""
    return _EPISODE_STATUSES


def fault_class(failure_domain: str | None, alarm_code: str | None, site_type: str | None) -> str:
    """``"{failure_domain}|{alarm_token}|{site_type}"`` — the §7.11.3 fault-class key.

    Built from the incident columns rather than stored, because M0 has no table to store it
    in; M1's ``memory_episodes.fault_class`` must produce the identical string from the same
    three columns or the exact tier will silently stop matching across the M0/M1 boundary.

    The alarm *token* is the alarm code as the NMS delivered it, upper-cased and stripped.
    Splitting compound codes into a primary token is an M2 question (it needs the
    ``resolution_code`` census of §8.1 to say which half carries the signal); doing it here
    would be a guess that M1 would then have to reproduce exactly.
    """
    return "|".join(_fault_parts(failure_domain, alarm_code, site_type))


def _fault_parts(
    failure_domain: str | None, alarm_code: str | None, site_type: str | None
) -> tuple[str, str, str]:
    """The three canonical components of a fault class, before they are joined.

    Separate from :func:`fault_class` so the SQL narrowing in :func:`recall_similar_episodes`
    compares the same canonical values rather than re-splitting the joined string — an alarm
    code containing a ``|`` would make that split wrong, and silently.
    """
    return (
        (failure_domain or _DOMAIN_DEFAULT).strip().upper() or _DOMAIN_DEFAULT,
        (alarm_code or "").strip().upper(),
        (site_type or _SITE_TYPE_DEFAULT).strip().upper() or _SITE_TYPE_DEFAULT,
    )


# --------------------------------------------------------------------------- scoring


def _users_full_scale() -> int:
    """P1 floor from the active operator profile; the denominator of the impact term."""
    try:
        thresholds = _settings().operator.priority_thresholds
        p2_max = int(getattr(thresholds, "P2_max_users", 0) or 0)
    except Exception:  # unreadable profile — scoring must not be the thing that breaks a read
        return _DEFAULT_USERS_FULL_SCALE
    return (p2_max + 1) if p2_max > 0 else _DEFAULT_USERS_FULL_SCALE


def _impact(users_affected: int | None, priority: str | None) -> float:
    """§7.11.4 impact: normalised users blended with the priority rank, both in [0, 1].

    The Generative-Agents formula's *importance* term is an LLM poignancy rating; §7.11.4
    replaces it with operational impact the pipeline already computed, which is what keeps
    this whole module runnable with ``LLM_ENABLED=false``.
    """
    users = max(int(users_affected or 0), 0) / float(_users_full_scale())
    rank = _PRIORITY_RANK.get((priority or "").strip().upper(), _PRIORITY_RANK["P4"])
    return (min(users, 1.0) + rank) / 2.0


def _recency(closed_at: datetime, now: datetime) -> float:
    """0.995 ** age_days, clamped at 0 days so a clock skew cannot score above the present."""
    age_days = max((now - closed_at).total_seconds() / 86_400.0, 0.0)
    return _RECENCY_DECAY_PER_DAY**age_days


def _score(inc: IncidentRow, closed_at: datetime, now: datetime, relevance: float) -> float:
    """``recency + relevance + impact`` (§7.11.4, the episode variant of the formula)."""
    return _recency(closed_at, now) + relevance + _impact(inc.users_affected, inc.priority)


# --------------------------------------------------------------------------- derivation


def _closed_at_expr():
    """The SQL expression for "when this episode ended".

    ``closed_at`` is NULL while an incident sits in ``RESTORED``, and ``restored_at`` is NULL
    on a ticket closed without a restore, so neither column alone orders the history
    correctly. ``created_at`` is NOT NULL on every row and is the last resort. The *same*
    expression is used for the lookback filter and for the ordering, so a row can never be
    inside the window by one clock and outside it by another.
    """
    return func.coalesce(IncidentRow.closed_at, IncidentRow.restored_at, IncidentRow.created_at)


def _closed_at(inc: IncidentRow) -> datetime:
    """Python twin of :func:`_closed_at_expr`; the two must agree row for row."""
    return inc.closed_at or inc.restored_at or inc.created_at


def _restore_minutes(inc: IncidentRow) -> int | None:
    """Outage duration in minutes, or ``None`` when the timestamps cannot carry one.

    Four refusals, each a real defect this guards against:

    * provenance outside ``{MARK_RESTORED, SUPERVISOR}`` — ``VENDOR_NOTE_INFERRED`` is the
      substring bug (defect #4) and a NULL source is ``close_incident``'s
      ``restored_at = closed_at`` back-fill (``lifecycle.py``);
    * no ``restored_at`` at all;
    * no outage start to measure from;
    * ``restored_at`` before the start, which is a data error, not a negative duration.
    """
    if (inc.restored_source or "") not in _TRUSTED_RESTORE_SOURCES:
        return None
    if inc.restored_at is None:
        return None
    start = inc.outage_start_at or inc.failure_time or inc.created_at
    if start is None or inc.restored_at < start:
        return None
    return int((inc.restored_at - start).total_seconds() // 60)


def _name_map(inc: IncidentRow, notes: Sequence[WorkNoteRow]) -> NameMap:
    """The redaction ``NameMap`` for one incident, seeded exactly as ``redact_incident`` does.

    §7.11.8 rule 1 and §7.7.6: role tokens, never names. Reusing the one ``NameMap``
    implementation — rather than writing a second name list here — is the point: a name that
    the outbound-LLM path knows how to hide is a name this path hides too, and a fix to one
    is a fix to both. Note authors are registered as well, because "Kevin took over" in a
    note body is only scrubbed once "Kevin Ochieng" is a known name.
    """
    names = NameMap()
    for field in ("assignee_name", "fe_name", "rnio_name"):
        names.token_for(getattr(inc, field, None))
    for note in notes:
        names.token_for(getattr(note, "author", None))
    return names


def _resolution_text(inc: IncidentRow, notes: Sequence[WorkNoteRow], names: NameMap) -> str:
    """What fixed it, scrubbed and capped — the one thing the 3 a.m. reader actually wants.

    ``resolution_summary`` is the right column and is very often empty: the pipeline writes a
    generic ``CLOSED_NORMAL``/``FIELD_RESTORED`` code and leaves the prose to whoever closed
    the ticket. Rather than render an empty panel, M0 falls back to the newest work note that
    *declares* a restore, using ``lifecycle.note_declares_restored`` — the same predicate the
    lifecycle uses to flip the status, negation guard included, so the two can never disagree
    about which note ended the outage. This is the whole reason M0 reads ``work_notes`` at
    all, and it is why M1's consolidator must keep the fallback when it materialises the
    column.

    Scrub first, truncate second: truncating first could slice a name in half and leave the
    surviving fragment unmatched by the scrubber.
    """
    raw = (inc.resolution_summary or "").strip()
    if not raw:
        for note in notes:  # notes arrive newest-first
            body = (note.body or "").strip()
            if body and note_declares_restored(body):
                raw = body
                break
    return (scrub_text(raw, names) or "")[:SUMMARY_MAX_CHARS]


# --------------------------------------------------------------------------- queries


def _notes_by_incident(session: Session, incident_ids: Sequence[str]) -> dict[str, list[WorkNoteRow]]:
    """Every note for the given incidents, newest first, in one query rather than N.

    ``work_notes`` carries no ``operator_id``; it is owned through
    ``incident_id -> incidents.operator_id`` (``api/deps.py``). That is safe here **only**
    because ``incident_ids`` are ids the ``_owned`` statement above already proved this
    operator owns — never widen this helper to take ids from a request.
    """
    if not incident_ids:
        return {}
    rows = session.scalars(
        select(WorkNoteRow)
        .where(WorkNoteRow.incident_id.in_(list(incident_ids)))
        .order_by(WorkNoteRow.created_at.desc())
    ).all()
    grouped: dict[str, list[WorkNoteRow]] = {}
    for note in rows:
        grouped.setdefault(note.incident_id, []).append(note)
    return grouped


def _episode_stmt(*, site_id: str, since: datetime | None):
    """``SELECT incidents`` for finished tickets at one site, operator-scoped.

    ``_owned`` is the first thing applied and the only place the operator clause is built
    (``api/deps.py`` is deliberately the single door for it). Do not replace it with a
    hand-written ``where(IncidentRow.operator_id == ...)``: the point of the shared helper is
    that a reviewer checks one implementation, not one per lane.
    """
    stmt = _owned(IncidentRow).where(
        IncidentRow.site_id == site_id,
        IncidentRow.status.in_(_EPISODE_STATUSES),
    )
    if since is not None:
        stmt = stmt.where(_closed_at_expr() >= since)
    return stmt


def _canonical_match(column, wanted: str, default: str):
    """SQL predicate for "this column canonicalises to ``wanted``" (see :func:`fault_class`).

    Exists so the exact tier narrows in the database instead of loading a chronic site's whole
    history and filtering it in Python. It mirrors the Python canonicalisation: ``UPPER(TRIM(...))``
    for case and padding, plus the NULL/empty case, which :func:`fault_class` folds into the
    column's default (``UNKNOWN`` for a failure domain, ``BTS`` for a site type).

    It is deliberately a *narrowing* step only — every row it returns is re-checked against
    :func:`fault_class` in Python, so the canonical key, not this ``WHERE`` clause, is what
    finally decides a match. The one way the two can disagree is a value padded with a
    whitespace character SQL ``TRIM`` does not strip but ``str.strip`` does (a tab inside an
    alarm code). That drops a history row rather than returning a wrong one — the fail-safe
    direction, and the reason the Python re-check is kept rather than removed as redundant.
    """
    normalised = func.upper(func.trim(func.coalesce(column, "")))
    if wanted == default:
        return or_(normalised == wanted, normalised == "")
    return normalised == wanted


def _build(
    session: Session,
    rows: Sequence[IncidentRow],
    *,
    now: datetime,
    relevance: float,
    match_reason: str,
) -> list[SimilarEpisode]:
    """Turn owned incident rows into scrubbed, scored episodes."""
    notes = _notes_by_incident(session, [r.id for r in rows])
    episodes: list[SimilarEpisode] = []
    for inc in rows:
        inc_notes = notes.get(inc.id, [])
        names = _name_map(inc, inc_notes)
        closed_at = _closed_at(inc)
        episodes.append(
            SimilarEpisode(
                incident_id=inc.id,
                incident_number=inc.incident_number,
                site_id=inc.site_id,
                fault_class=fault_class(inc.failure_domain, inc.alarm_code, inc.site_type),
                closed_at=closed_at,
                restore_minutes=_restore_minutes(inc),
                resolution_code=(inc.resolution_code or ""),
                resolution_summary=_resolution_text(inc, inc_notes, names),
                match_reason=match_reason,
                score=_score(inc, closed_at, now, relevance),
            )
        )
    return episodes


def _degrade_to_empty(fn: Callable[..., tuple]) -> Callable[..., tuple]:
    """MEM4: a recall returns an empty tuple rather than propagating an exception.

    The workspace panel going blank is acceptable; the workspace failing to load because a
    half-migrated column blew up an advisory read is not. Logged at WARNING so a silent
    empty panel is still visible to whoever is looking at the logs — swallowing an error
    without a trace is how a recall bug survives a release.
    """

    def guarded(*args: Any, **kwargs: Any) -> tuple:
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 — the whole contract of this decorator
            log.warning("memory recall degraded to empty in %s", fn.__name__, exc_info=True)
            return ()

    guarded.__name__ = fn.__name__
    guarded.__doc__ = fn.__doc__
    guarded.__wrapped__ = fn  # type: ignore[attr-defined]
    return guarded


def _clamp(limit: int) -> int:
    try:
        return max(0, min(int(limit), _MAX_ROWS))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- public API
# Signatures are §7.11.4 verbatim so that M1 replaces the *bodies* (reading the derived
# ``memory_episodes`` index and adding the FTS5 and vector tiers) without touching a caller.


@_degrade_to_empty
def recall_site_history(
    session: Session,
    *,
    site_id: str,
    lookback_days: int = 365,
    limit: int = 20,
) -> tuple[SimilarEpisode, ...]:
    """Raw per-site failure history, newest first (§7.11.4).

    Answers "what has happened at this site before", with no fault-class filter: it is the
    panel behind ``GET /api/v1/memory/sites/{site_id}`` and, from M1, the input to the
    seasonal-pattern consolidator. In M0 it is pure SQL over ``incidents`` + ``work_notes``.

    Ordering is strictly newest-first, not by ``score``. The history panel is a chronology —
    a reader scanning it expects the last outage at the top — whereas
    :func:`recall_similar_episodes` answers "which of these is most like the one in front of
    me" and *is* ranked. ``score`` is still populated on every row so a caller can rank them
    itself without a second query.

    ``lookback_days <= 0`` means "no window" rather than "no history": the caller asked for
    everything. An unknown ``site_id`` returns ``()`` — never an error (MEM4).
    """
    site = (site_id or "").strip()
    rows_wanted = _clamp(limit)
    if not site or rows_wanted == 0:
        return ()
    now = utcnow()
    since = (now - timedelta(days=int(lookback_days))) if lookback_days and lookback_days > 0 else None
    rows = list(
        session.scalars(
            _episode_stmt(site_id=site, since=since)
            .order_by(_closed_at_expr().desc())
            .limit(rows_wanted)
        ).all()
    )
    episodes = _build(session, rows, now=now, relevance=_RELEVANCE_EXACT, match_reason=MATCH_SAME_SITE)
    episodes.sort(key=lambda e: e.closed_at, reverse=True)
    return tuple(episodes)


@_degrade_to_empty
def recall_similar_episodes(
    session: Session,
    *,
    site_id: str,
    failure_domain: str,
    alarm_code: str = "",
    site_type: str = "BTS",
    cfg: OperatorConfig | None = None,
    limit: int = 5,
    now: datetime | None = None,
    query_text: str | None = None,
) -> tuple[SimilarEpisode, ...]:
    """Prior incidents that look like this one — **exact tier only** in M0 (§7.11.4).

    The full cascade is three tiers: exact (same ``site_id`` + same ``fault_class``), lexical
    (FTS5 ``MATCH`` over scrubbed note bodies) and vector (cosine, only with
    ``MEMORY_EMBEDDINGS_ENABLED``). M0 implements the first and only the first, because it is
    the one tier that needs no table: it is an index scan on ``incidents``. The ordering rule
    the other tiers must respect is already established here — an exact hit always outranks a
    lexical hit, which always outranks a vector hit, *regardless of raw similarity*, because
    the failure mode being designed against is the confidently-wrong neighbour: the
    similarly-worded incident about a different site that sends an engineer to the wrong
    fault class (§7.11.7).

    ``cfg`` and ``query_text`` are accepted and unused today, and that is deliberate rather
    than sloppy: ``cfg`` carries the ``memory:`` thresholds M1 reads and ``query_text`` is the
    FTS5 query string, so M1 adds tiers instead of changing a signature every caller depends
    on. ``cfg`` is optional here only because M0 has no threshold to read from it; M1 should
    make it required.

    Ranked by ``score`` (recency + relevance + impact), newest first on a tie — so a run of
    comparable outages at one site reads chronologically, and a much larger prior outage can
    still surface above a trivial recent one.
    """
    site = (site_id or "").strip()
    rows_wanted = _clamp(limit)
    if not site or rows_wanted == 0:
        return ()
    domain, alarm, stype = _fault_parts(failure_domain, alarm_code, site_type)
    wanted = "|".join((domain, alarm, stype))
    at = now or utcnow()
    # No lookback window: "the last time this exact fault happened here" is worth knowing
    # however long ago it was, and the fault-class filter already makes the result set small.
    # The 24-month prune of §7.11.3 applies to M1's derived index, not to `incidents`, whose
    # retention is statutory (§9.4). `_MAX_ROWS` is the only bound, and it bounds the *same*
    # fault class at one site — a site that has failed this exact way 200 times has a problem
    # record, not a memory problem.
    narrowed = (
        _episode_stmt(site_id=site, since=None)
        .where(
            _canonical_match(IncidentRow.failure_domain, domain, _DOMAIN_DEFAULT),
            _canonical_match(IncidentRow.alarm_code, alarm, ""),
            _canonical_match(IncidentRow.site_type, stype, _SITE_TYPE_DEFAULT),
        )
        .order_by(_closed_at_expr().desc())
        .limit(_MAX_ROWS)
    )
    rows = [
        inc
        for inc in session.scalars(narrowed).all()
        if fault_class(inc.failure_domain, inc.alarm_code, inc.site_type) == wanted
    ]
    episodes = _build(
        session,
        rows,
        now=at,
        relevance=_RELEVANCE_EXACT,
        match_reason=MATCH_SAME_SITE_AND_FAULT_CLASS,
    )
    episodes.sort(key=lambda e: (e.score, e.closed_at), reverse=True)
    return tuple(episodes[:rows_wanted])


# --------------------------------------------------------------------------- rendering


def episode_dict(episode: SimilarEpisode) -> dict[str, Any]:
    """One episode as the API and the workspace panel see it.

    Kept here rather than in the router so that the HITL packet and the ``advisory``
    serializer key (M1) render the identical shape, and so the field list has exactly one
    definition to review for "is anything personal on this wire".
    """
    return {
        "incident_id": episode.incident_id,
        "incident_number": episode.incident_number,
        "site_id": episode.site_id,
        "fault_class": episode.fault_class,
        "closed_at": episode.closed_at,
        "restore_minutes": episode.restore_minutes,
        "resolution_code": episode.resolution_code,
        "resolution_summary": episode.resolution_summary,
        "match_reason": episode.match_reason,
        "score": round(episode.score, 4),
    }


def episode_dicts(episodes: Iterable[SimilarEpisode]) -> list[dict[str, Any]]:
    return [episode_dict(e) for e in episodes]
