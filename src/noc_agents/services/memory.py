"""Agent memory, the read side — "what happened here before", and the advisory bundle.

Spec §7.11 (Phase 4 Lane 4C). Nothing in the pipeline reads across incidents: the last
outage at this site and what actually fixed it are already in ``incidents`` and
``work_notes`` and are never recalled. **M0** was the smallest honest version of that recall
— pure SQL over the two existing tables, no new table, no job, no LLM, no network — so the
floor could say whether it was useful before anything was created that would have to be
migrated away. **M1** keeps every one of those functions and their behaviour and adds three
things on top: the FTS5 lexical tier of the §7.11.4 cascade, the single ``recall_for_incident()``
entry point, and ``advisory_block()``, the additive bundle the HITL card and the
single-incident serializer render. The vector tier, L2 facts, L3 playbooks and L4 memos are
still M2-M4 and are not here.

The **write** side — ``consolidate_incident``, the ``memory_consolidate`` job and
``expire_memory`` — lives in ``noc_agents.memory`` and is never imported by anything on the
hot path (MEM5). That package's ``__init__`` records why the read code stayed here.

WHAT THIS MODULE IS AND IS NOT
------------------------------
* **Advisory and inert (MEM1/G15).** Nothing here is read by ``services/priority.py``,
  ``services/assignment.py``, ``services/composition.py``, ``services/numbering.py``,
  ``services/lifecycle.py``, ``agents/correlate.py``, ``agents/severity.py`` or
  ``agents/assign.py``. From M1 there is exactly **one** hot-path caller — ``agents/hitl.py``,
  which freezes :func:`advisory_for_incident` onto a task's ``proposed_payload`` at creation
  (spec line 419: SupervisorAgent is the only hot-path reader). It reads; it decides nothing.
  The gate is already computed before that call and is not re-read, no existing key is
  touched, and with ``MEMORY_ENABLED`` unset the helper returns ``None`` and the payload is
  byte-identical. ``tests/unit/test_memory_recall.py`` and
  ``tests/unit/test_memory_advisory_is_inert.py`` pin that with an AST walk *and* with two
  byte-identical lifecycle runs — one against a seeded store with the flag **on**.
* **Read-only.** No function here commits, flushes, adds a row or issues DDL — the lexical
  tier *checks* for ``memory_note_fts`` and degrades when it is absent; only the consolidator
  creates it. So ``runner._fail_closed`` has nothing of ours to roll back (MEM5), which
  ``test_memory_consolidation.py::test_a_fail_closed_run_writes_zero_memory_rows`` pins.
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
``MEMORY_ENABLED`` (default **false**) is checked by the **entry points** —
``GET /api/v1/memory/sites/{site_id}``, :func:`recall_for_incident` and therefore
:func:`advisory_for_incident` (the HITL card and the ``advisory`` serializer key) — and
deliberately **not** inside the ``recall_*`` functions themselves. Two reasons: the spec's
own signatures (§7.11.4) give ``recall_site_history`` no config argument, and the M1
consolidator calls these same functions to *build* the episode index, which must keep
working independently of whether reads are switched on. Use :func:`memory_enabled` at the
point where memory reaches a human, not below it.
``tests/unit/test_memory_recall.py`` pins that; adding a check inside ``recall_*`` would look
like tightening and would quietly disable consolidation.

SEMANTICS WORTH KNOWING
-----------------------
* An "episode" is an incident in ``RESTORED`` or ``CLOSED``. ``memory_episodes`` is one row
  per such incident, so the population is the same; only the storage differs.
* **The exact tier still reads ``incidents``, not ``memory_episodes``.** §7.11.4 sketches it
  as "an index scan on ``memory_episodes``", and it is deliberately not, for two reasons: the
  derived index is a *cache*, so reading it would make "what happened here before" depend on
  whether a background job has run since the ticket closed — a panel that is silently a tick
  behind at 3 a.m. is worse than one query more — and every M0 test seeds ``incidents``
  directly. ``memory_episodes`` is what the *aggregates* are computed over (the fault-class
  median, the 3×IQR outlier rule), which is the job a population can do and a single row
  cannot. The FTS tier does read its own index, because a lexical search has no live-table
  equivalent.
* ``restore_minutes`` is NULL unless ``restored_source`` is ``MARK_RESTORED`` or
  ``SUPERVISOR`` (§7.0.8, the M4 rule). ``VENDOR_NOTE_INFERRED`` is a regex hit on a vendor's
  free text (brief defect #4) and ``close_incident`` back-fills ``restored_at = closed_at``
  with no provenance at all; both produce a number that looks like an MTTR and is not one.
  A wrong duration here becomes a wrong median in M2/M3, so it is refused at the source.
  The 3xIQR outlier rule of §7.11.7 needs a fault-class population and belongs to M1's
  consolidator, not to a per-row read.
* Every free-text value that leaves this module passes ``llm/redaction.py`` — the one
  ``NameMap``, never a second name list — seeded with every name the incident carries *and
  has carried*: the four person columns (``restored_by`` included, review M11), the people its
  ASSIGN step recorded, both sides of every reassign note (review M01) and the note authors.
  §7.11.8 rule 1: no person's name in any memory output. Once housekeeping has pseudonymised
  an incident its text is never re-derived (review M02). The honest limits: there is no NER,
  so a name typed into a note body that appears in none of those records is not recognised,
  and a three-letter first name used alone ("Ann") is not either (review M08) — both pinned
  as strict xfails in ``tests/unit/test_memory_privacy.py``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import column, func, literal_column, or_, select, table
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned, _settings
from noc_agents.config import OperatorConfig
from noc_agents.db.models import AgentRunRow, AgentRunStepRow, IncidentRow, WorkNoteRow, utcnow
from noc_agents.db.models_memory import MemoryEpisodeRow
from noc_agents.domain.enums import IncidentStatus
from noc_agents.llm.redaction import NameMap, scrub_text
from noc_agents.memory.schema import FTS_TABLE, fts_available, fts_query, fts_terms
from noc_agents.services.clock import z_utc
from noc_agents.services.lifecycle import (
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_SUPERVISOR,
    note_declares_restored,
)

log = logging.getLogger("noc_agents.services.memory")

__all__ = [
    "DEFAULT_MEMORY_SETTINGS",
    "EPISODE_SUMMARY_MAX_CHARS",
    "MEMORY_ENABLED_ENV",
    "MemoryBundle",
    "MemoryHit",
    "ScrubbedResolution",
    "SimilarEpisode",
    "SUMMARY_MAX_CHARS",
    "advisory_block",
    "advisory_for_incident",
    "episode_closed_at",
    "episode_dict",
    "episode_dicts",
    "episode_statuses",
    "fault_class",
    "fault_class_prior",
    "episode_closed_at_expr",
    "hit_dict",
    "is_pseudonymised",
    "memory_enabled",
    "memory_settings",
    "name_map_for",
    "outlier_bounds",
    "person_name_history",
    "quantile",
    "recall_for_incident",
    "recall_similar_episodes",
    "recall_site_history",
    "restore_minutes_population",
    "scrubbed_resolution",
    "trusted_restore_minutes",
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


# --------------------------------------------------------------------------- thresholds

#: §7.11.3's ``memory:`` YAML block, as code defaults. The spec puts these in
#: ``config/operators/<op>.yaml`` behind a ``memory: dict[str, Any]`` field on
#: ``OperatorConfig`` — and ``OperatorConfig`` is a pydantic model with the default
#: ``extra="ignore"``, so until that field exists a ``memory:`` block in the profile is
#: **silently dropped**. The field is a one-line change to ``config.py``, which this lane does
#: not own; :func:`memory_settings` therefore reads the block defensively and falls back to
#: these, exactly as the ``correlation:``/``recurrence:`` blocks are read at their call sites.
#: The day the field lands, the YAML starts winning with no change here.
DEFAULT_MEMORY_SETTINGS: dict[str, Any] = {
    # A prior with fewer than 3 episodes behind it is never returned. This is the primary
    # defence against the confidently-wrong neighbour (§7.11.7): one vendor note cannot
    # become a fact.
    "min_support": 3,
    "site_lookback_days": 365,
    # Max facts injected into any advisory block or prompt. A budget, not a page size.
    "recall_limit": 8,
    # Max similar episodes on one card. §7.11.5 capability 7: ≤ 8 facts and ≤ 5 episodes
    # (≈ 300-600 tokens) instead of a 15-25k-token history dump.
    "similar_limit": 5,
    # The prune horizon for the derived index (§7.11.3). NOT a retention rule for
    # ``incidents``, whose retention is statutory (§9.4, licence Condition 12.2).
    "episode_max_age_days": 730,
}


def memory_settings(cfg: OperatorConfig | None = None) -> dict[str, Any]:
    """:data:`DEFAULT_MEMORY_SETTINGS` overlaid with the profile's ``memory:`` block.

    Never raises and never returns a partial dict: a profile that sets one key still gets
    every other default, so a threshold cannot become ``None`` half-way through a recall.
    """
    values = dict(DEFAULT_MEMORY_SETTINGS)
    try:
        block = getattr(cfg if cfg is not None else _settings().operator, "memory", None)
        if isinstance(block, dict):
            for key, value in block.items():
                if key in values and isinstance(value, (int, float)):
                    values[key] = value
    except Exception:  # noqa: BLE001 — an unreadable profile must not break an advisory read
        log.warning("memory thresholds fell back to defaults", exc_info=True)
    return values


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


@dataclass(frozen=True)
class MemoryHit:
    """One derived fact, as §7.11.4 defines it. Frozen for the same reason as an episode.

    Every hit carries ``support_count``, ``confidence``, ``as_of`` and ``evidence`` because
    §7.11.4's rule is that memory never makes a naked assertion: a rendered fact reads "N
    prior cases, median X, as of D", and the incident ids behind it are on the row so any
    claim is traceable back to tickets.

    M1 produces exactly one kind: the ``FAULT_CLASS`` restore-time prior computed over
    ``memory_episodes`` (see :func:`fault_class_prior`). The ``SITE``, ``MSP``,
    ``ALARM_CLUSTER`` and ``PARTY_TOKEN`` subjects are M3/M4a/Phase 6 and the corresponding
    bundle tuples stay empty — present, so a renderer written today keeps working, and empty
    rather than absent, so nobody mistakes "not built yet" for "nothing to say".
    """

    layer: str  # "L1" | "L2" | "L3" | "L4"
    subject_type: str
    subject_id: str
    key: str
    value: str
    numeric: float | None
    unit: str
    support_count: int
    confidence: float  # 0.0-1.0, deterministic
    score: float
    as_of: datetime  # always rendered next to the value
    evidence: tuple[str, ...]  # incident ids, capped at EVIDENCE_MAX
    source: str  # "deterministic" | "llm_draft" (nothing writes the second in v2 — MEM6)


@dataclass(frozen=True)
class MemoryBundle:
    """Everything a caller may inject, already truncated to ``recall_limit`` (§7.11.4).

    ``degraded=True`` means "memory is off, empty or errored — behave exactly as you would
    have without it" (MEM4/G9). It is the default, so a bundle constructed with nothing in
    it is honest by construction rather than by remembering to set a flag.
    """

    site: tuple[MemoryHit, ...] = ()
    fault_class: tuple[MemoryHit, ...] = ()
    party: tuple[MemoryHit, ...] = ()
    correlation: tuple[MemoryHit, ...] = ()
    playbook: tuple[Any, ...] = ()  # PlaybookStep — M2
    similar: tuple[SimilarEpisode, ...] = ()
    memos: tuple[Any, ...] = ()  # ShiftMemo — M3
    token_estimate: int = 0
    degraded: bool = True


@dataclass(frozen=True)
class ScrubbedResolution:
    """"What fixed it", scrubbed, plus which note it came from — the consolidator's input.

    A value rather than a bare string because the consolidator has to store
    ``restoring_note_id`` beside the text, and re-deriving "which note was that?" a second
    time is how the two end up disagreeing.
    """

    text: str
    restoring_note_id: str | None


#: §7.11.3 caps evidence text at 240 characters. It is a cap on how much attacker-reachable
#: vendor free text can travel with a recall hit (MEM9), not a display preference, so the
#: same number is applied here even though M0 renders to a human rather than to a prompt.
SUMMARY_MAX_CHARS = 240

#: What ``memory_episodes.resolution_summary`` stores (§7.11.3). Larger than the recall cap
#: on purpose: the stored row is the derivation, the 240 is the *evidence* cap applied on the
#: way out, and re-deriving a longer summary later must not need a backfill.
EPISODE_SUMMARY_MAX_CHARS = 500

#: Incident ids carried on a :class:`MemoryHit` (§7.11.7, "evidence lists capped at 20").
EVIDENCE_MAX = 20

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

#: Relevance term for the exact tier (§7.11.4). 0.7 is "same fault class, different site"
#: (M2's playbook recall); the bm25 tier sits below that, and an exact hit outranks both.
_RELEVANCE_EXACT = 1.0

#: The lexical tier's relevance ceiling. Normalised bm25 is mapped into ``(0, 0.65]`` so it
#: can never reach the 0.7 that "same fault class, different site" will mean — a *lexical*
#: neighbour is a weaker claim than a structural one, whatever its raw score says.
_RELEVANCE_FTS_MAX = 0.65

#: Retrieval tiers (§7.11.4). These are a **hard ordering key**, not a weight: the ranking
#: sorts on ``(tier, score, closed_at)``, so an exact hit outranks a lexical hit regardless
#: of raw similarity. Folding the tier into the score instead would let a very recent, very
#: large lexical neighbour overtake an exact hit through the recency and impact terms — and
#: the confidently-wrong neighbour (§7.11.7) is the failure mode this whole cascade is
#: designed against: the similarly-worded incident about a different site that sends an
#: engineer to the wrong fault class.
TIER_EXACT = 2
TIER_LEXICAL = 1
TIER_VECTOR = 0  # M4c, only with MEMORY_EMBEDDINGS_ENABLED; nothing produces it today

#: ``match_reason`` strings. Fixed vocabulary so the UI and the ranking agree on them.
MATCH_SAME_SITE = "same site"
MATCH_SAME_SITE_AND_FAULT_CLASS = "same site + same fault class"
#: §7.11.4's example wording, e.g. ``"fts: mains, generator"`` — the terms that were searched
#: for, so a reader can see *why* a different site's incident is on their card.
MATCH_FTS_PREFIX = "fts: "
#: Terms named in a lexical ``match_reason``. Three is enough to explain the hit; the whole
#: query would push the ticket text off a card.
_MATCH_REASON_TERMS = 3

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


def trusted_restore_minutes(inc: IncidentRow) -> int | None:
    """Public door onto :func:`_restore_minutes` — the §7.0.8 M4 refusal rule, once.

    Exists so ``memory/consolidate.py`` cannot re-implement the four refusals. A second
    implementation of "is this duration trustworthy?" that drifts from this one would put a
    number the recall path refuses to show into the median the recall path *does* show.
    """
    return _restore_minutes(inc)


def episode_closed_at_expr():
    """Public door onto :func:`_closed_at_expr`, for the consolidator's 24-month horizon filter
    (review M12): the horizon must be measured by the same clock ``expire_memory`` prunes on,
    or the backfill would rebuild what the prune just removed."""
    return _closed_at_expr()


def episode_closed_at(inc: IncidentRow) -> datetime:
    """Public door onto :func:`_closed_at`: ``COALESCE(closed_at, restored_at, created_at)``.

    Shared with the consolidator for the same reason as above — the stored
    ``memory_episodes.closed_at`` must be the value the live-table recall orders by, or an
    episode could sit inside a window by one clock and outside it by another.
    """
    return _closed_at(inc)


#: The person columns of ``incidents`` (§9.4's personal class for this table, the same four
#: ``config/retention.yaml`` pseudonymises). ``restored_by`` is one of them (review M11): the
#: spec lists it as personal data and ``services/pir.py`` already treats it as a name, so a
#: restore attributed to someone who wrote no note must not leave their name in memory.
_PERSON_FIELDS: tuple[str, ...] = ("assignee_name", "fe_name", "rnio_name", "restored_by")

# ``lifecycle.reassign_incident`` writes one work note per reassignment with
# ``source="reassign"`` and the body ``f"Reassigned {old} → {new} ({type}). Reason: {reason}"``.
# That note is the ONLY durable record of an assignee the incident no longer carries: the
# column is overwritten, the realtime event is not persisted, and nothing else logs it. These
# three anchors are that template's own literals; ``_reassigned_names`` reads the two
# structured slots between them and never the free-text reason. They are a copy of a string
# another module owns, so ``tests/unit/test_memory_privacy.py`` drives the real
# ``reassign_incident`` and fails the day the template changes shape.
_REASSIGN_SOURCE = "reassign"
_REASSIGN_PREFIX = "Reassigned "
_REASSIGN_ARROW = " → "
_REASSIGN_TYPE_OPEN = " ("

#: The ASSIGN node's step row (``agents/assign.py``) records who the pipeline first assigned:
#: ``output_summary = f"{assignee_type}:{responsible_party}"`` and a rationale built by
#: ``services/assignment.assign`` as ``"; "``-joined ``key=value`` bits, of which these keys
#: carry a person (``"FE=..."`` on an FE assignment, ``"FE support=...; RNIO=..."`` on an MSP
#: one). It is the durable record of the ORIGINAL fe_name/rnio_name, which a later reassign or
#: a housekeeping pseudonymisation overwrites on the incident row itself.
_ASSIGN_NODE = "ASSIGN"
_ASSIGN_PERSON_KEYS = frozenset({"FE", "FE support", "RNIO"})


def _reassigned_names(notes: Sequence[WorkNoteRow]) -> list[str]:
    """Both names in every reassign note — the assignee handed away, and the one received.

    Read from the note's structure (the ``source`` column and the template's fixed literals),
    not by guessing at names in prose. Both sides are taken because in A → B → C, B is on
    neither the current row nor the first ASSIGN record, only in the two reassign notes. The
    literal ``"None"`` is what the f-string writes for an empty assignee and is skipped.
    """
    found: list[str] = []
    for note in notes:
        if (getattr(note, "source", None) or "") != _REASSIGN_SOURCE:
            continue
        body = note.body or ""
        if not body.startswith(_REASSIGN_PREFIX):
            continue
        old, arrow, rest = body[len(_REASSIGN_PREFIX):].partition(_REASSIGN_ARROW)
        if not arrow:
            continue
        new, _paren, _tail = rest.partition(_REASSIGN_TYPE_OPEN)
        found.extend(v.strip() for v in (old, new) if v.strip() and v.strip() != "None")
    return found


def _assigned_names(output_summary: str | None, rationale: str | None) -> list[str]:
    """The people named in one ASSIGN step record (see :data:`_ASSIGN_PERSON_KEYS`)."""
    found: list[str] = []
    _type, sep, party = (output_summary or "").partition(":")
    if sep and party.strip():
        found.append(party.strip())
    for bit in (rationale or "").split("; "):
        key, eq, value = bit.partition("=")
        if eq and key.strip() in _ASSIGN_PERSON_KEYS and value.strip():
            found.append(value.strip())
    return found


def person_name_history(session: Session, incident_ids: Sequence[str]) -> dict[str, list[str]]:
    """Every person the pipeline recorded as assigned to each incident, from its ASSIGN steps.

    Review M01: a NameMap seeded only from the *current* person columns forgets everyone the
    incident used to carry, so their names — in the system's own reassign note, or typed by a
    human before the handover — reached memory verbatim. This is the half of the history that
    lives in ``agent_run_steps`` (mirrored in each step's audit row); the reassign notes are
    the other half and are read from the notes the caller already holds.

    One query for all ids. ``agent_run_steps`` carries no ``operator_id``; ownership is the
    caller's: every id here came out of an ``_owned`` statement, exactly as for
    :func:`_notes_by_incident`. Degrades to ``{}`` — a missing history makes the scrub weaker,
    never the read fail.
    """
    ids = [i for i in incident_ids if i]
    if not ids:
        return {}
    try:
        rows = session.execute(
            select(AgentRunRow.incident_id, AgentRunStepRow.output_summary, AgentRunStepRow.rationale)
            .join(AgentRunStepRow, AgentRunStepRow.run_id == AgentRunRow.id)
            .where(AgentRunRow.incident_id.in_(ids), AgentRunStepRow.node_name == _ASSIGN_NODE)
        ).all()
    except Exception:  # noqa: BLE001 — history is a strengthener, not a dependency
        log.warning("memory: assignment history unavailable", exc_info=True)
        return {}
    history: dict[str, list[str]] = {}
    for incident_id, output_summary, rationale in rows:
        history.setdefault(incident_id, []).extend(_assigned_names(output_summary, rationale))
    return history


def _name_map(
    inc: IncidentRow, notes: Sequence[WorkNoteRow], history: Sequence[str] = ()
) -> NameMap:
    """The redaction ``NameMap`` for one incident: every name it carries AND has carried.

    §7.11.8 rule 1 and §7.7.6: role tokens, never names. Reusing the one ``NameMap``
    implementation — rather than writing a second name list here — is the point: a name that
    the outbound-LLM path knows how to hide is a name this path hides too, and a fix to one
    is a fix to both. Seeded, in this order (the order fixes the token numbers):

    * the four person columns as they are now (:data:`_PERSON_FIELDS`) — ``redact_incident``'s
      three plus ``restored_by`` (review M11). A ``restored_by`` that is only a role label
      (``by=author or author_role`` in ``lifecycle``, so ``"MSP"`` when nobody signed the
      note) is skipped when it equals a note's ``author_role``: registering the word "MSP" as
      a person would tokenise every mention of the ordinary NOC word;
    * ``history`` — the people the ASSIGN step recorded (:func:`person_name_history`);
    * both sides of every reassign note (:func:`_reassigned_names`) — review M01;
    * every note author, because "Kevin took over" in a note body is only scrubbed once
      "Kevin Ochieng" is a known name.

    The honest limits that remain: there is no NER, so a person named only in prose and in
    none of these records is not recognised (the strict xfail in ``test_memory_privacy.py``);
    ``NameMap`` registers name *parts* of four letters or more, so a three-letter first name
    alone ("Ann", "Ian") is not scrubbed (review M08, the second strict xfail there); and an
    ``fe_name`` changed by a reassign that did not also change the assignee is recorded
    nowhere the pipeline keeps, so it cannot be recovered here.
    """
    names = NameMap()
    roles = {(getattr(n, "author_role", None) or "").strip().upper() for n in notes}
    for field in _PERSON_FIELDS:
        value = getattr(inc, field, None)
        if field == "restored_by" and (value or "").strip().upper() in roles:
            continue
        names.token_for(value)
    for name in history:
        names.token_for(name)
    for name in _reassigned_names(notes):
        names.token_for(name)
    for note in notes:
        names.token_for(getattr(note, "author", None))
    return names


def _restoring_note(notes: Sequence[WorkNoteRow]) -> WorkNoteRow | None:
    """The newest work note that *declares* a restore, or ``None``.

    Uses ``lifecycle.note_declares_restored`` — the same predicate (negation guard included)
    the lifecycle uses to flip the status — so the note memory calls "the fix" is always the
    note the lifecycle called "the restore". ``notes`` arrive newest-first.
    """
    for note in notes:
        if (note.body or "").strip() and note_declares_restored(note.body or ""):
            return note
    return None


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
        note = _restoring_note(notes)
        raw = (note.body or "").strip() if note is not None else ""
    return (scrub_text(raw, names) or "")[:SUMMARY_MAX_CHARS]


def name_map_for(
    inc: IncidentRow, notes: Sequence[WorkNoteRow], *, history: Sequence[str] = ()
) -> NameMap:
    """Public door onto :func:`_name_map` for the consolidator (§7.11.8: one name list).

    The consolidator scrubs several fields of one incident — the resolution text and every
    note body it indexes — and they must all be scrubbed with the **same** map, or the same
    engineer becomes ``<PERSON_1>`` in one row and ``<PERSON_2>`` in the next and a reader
    cannot tell they are the same person. One map per incident, built here; ``history`` is
    that incident's entry from :func:`person_name_history`.
    """
    return _name_map(inc, notes, history)


# ------------------------------------------------------------ pseudonymised incidents (M02)
#
# Housekeeping's ``pseudonymise_personal_fields`` replaces an old incident's person columns
# with role tokens rendered from ``config/retention.yaml`` (``fe_name`` → ``"FE-{region_code}"``)
# and leaves ``work_notes.body`` and ``resolution_summary`` as they were — work_notes is
# deliberately unlisted there. After that, a NameMap built from the row no longer knows the
# names that are still sitting in the free text, so re-deriving anything from that text
# writes the real names back (review M02). The rule this lane follows is therefore simple:
# **once an incident is pseudonymised, its free text is never re-derived** — the consolidator
# keeps whatever scrubbed text the episode already had, and recall shows the episode's stored
# summary or nothing.
#
# HOW "pseudonymised" IS KNOWN — and how it must not be (review round 3). The first version
# inferred it from a person column *looking like* the token its retention template renders.
# That was wrong for live data: ``services/assignment.py`` writes ``rnio_name`` straight from
# the operator profile, and Safaricom's MTK, CST, RFT and WNY regions carry ``RNIO-MTK`` …
# ``RNIO-WNY`` — exactly what ``RNIO-{region_code}`` renders — while a region the profile does
# not list gets ``f"RNIO-{reg}"``, the same string again. Every new incident in those regions
# was read as pseudonymised, and its memory text silently stored and recalled as empty.
#
# Now the answer comes only from housekeeping's own durable record: one ``AuditRow`` per
# pseudonymised row, written in the same transaction as the column rewrite, at a
# DETERMINISTIC id (``housekeeping.pseudonymisation_marker_id``) so the check is a single
# primary-key probe — bounded on the hot path whatever the size of ``audit_events``.
#
# The heuristic is RETIRED, with no fallback. A fallback on column shape is exactly the defect
# above, and there is no population for it to serve: housekeeping ships with
# ``posture.dry_run: true`` and ``HOUSEKEEPING_APPLY`` unset, so no deployment has pseudonymised
# an incident before the marker existed. A deployment that HAD (it would have had to turn both
# keys on) must not guess either: its rows cannot be told apart from live RNIO-MTK rows after
# the fact, so the honest remedy there is a one-off marker backfill reviewed by a human from
# its own housekeeping audit trail — not an inference in this module. What still protects such
# a row meanwhile is the name history (ASSIGN step and reassign notes, review M01), which the
# pseudonymisation does not touch.


def is_pseudonymised(session: Session, inc: Any) -> bool:
    """True only when housekeeping RECORDED pseudonymising this incident.

    One primary-key probe into ``audit_events`` (the marker's id is derived from the incident
    id), and the marker's operator must be the incident's. Never raises: an unreadable marker
    reads as "not pseudonymised", which re-derives text with the full name history — the state
    every non-pseudonymised incident is in.
    """
    from noc_agents.services.housekeeping import is_marked_pseudonymised  # lazy: write-side module

    incident_id = getattr(inc, "id", None)
    if not incident_id:
        return False
    return is_marked_pseudonymised(
        session, "incidents", incident_id, operator_id=getattr(inc, "operator_id", None)
    )


def _stored_summaries(session: Session, incident_ids: Sequence[str]) -> dict[str, str]:
    """The already-scrubbed ``resolution_summary`` each episode stored, keyed by incident.

    What recall shows for a pseudonymised incident instead of re-deriving from source text.
    Operator-scoped through ``_owned``; ``memory_episodes.incident_id`` is UNIQUE, so this is
    one index probe per id.
    """
    found: dict[str, str] = {}
    for incident_id in (i for i in incident_ids if i):
        # One equality probe per id, not ``IN (...)``: with the operator clause beside an IN
        # list and no ANALYZE statistics, SQLite prefers the operator index and walks every
        # episode the operator owns (review M03's shape). Equality on the UNIQUE column is
        # always a single-row lookup.
        summary = session.execute(
            _owned(MemoryEpisodeRow)
            .where(MemoryEpisodeRow.incident_id == incident_id)
            .with_only_columns(MemoryEpisodeRow.resolution_summary)
        ).scalar()
        if summary is not None:
            found[incident_id] = summary
    return found


def scrubbed_resolution(
    inc: IncidentRow,
    notes: Sequence[WorkNoteRow],
    *,
    limit: int = SUMMARY_MAX_CHARS,
    names: NameMap | None = None,
) -> ScrubbedResolution:
    """:func:`_resolution_text` plus the id of the note it fell back to — the write-side door.

    The consolidator must not build a second name list (§7.11.8: one scrubber, one place to
    fix), and it must record ``restoring_note_id`` beside the text it stored. Both come from
    here so the stored summary and the recalled summary are produced by the same code with
    the same ``NameMap`` seeding; only ``limit`` differs (500 stored, 240 recalled).

    ``names`` lets a caller that is scrubbing several fields of one incident share one map;
    omitted, one is built from the same seeding ``redact_incident`` uses.
    """
    names = names if names is not None else _name_map(inc, notes)
    raw = (inc.resolution_summary or "").strip()
    note = _restoring_note(notes) if not raw else None
    if note is not None:
        raw = (note.body or "").strip()
    return ScrubbedResolution(
        text=(scrub_text(raw, names) or "")[:limit],
        restoring_note_id=note.id if note is not None else None,
    )


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
    # Bounded by the SITE, never by the operator (review M03): SQLite serves this from
    # ``ix_incidents_site_id``, so the cost is the site's history, not the operator's table.
    # SQLAlchemy cannot render SQLite's ``INDEXED BY``, and the operator clause must stay
    # ``_owned``'s, so the plan is not forced here — it is PINNED instead:
    # ``test_memory_recall.py`` runs EXPLAIN QUERY PLAN on every hot-path statement, with and
    # without ANALYZE statistics, and fails on any use of ``ix_incidents_operator_id``.
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
    relevance: float | dict[str, float],
    match_reason: str,
) -> list[SimilarEpisode]:
    """Turn owned incident rows into scrubbed, scored episodes.

    ``relevance`` is a constant for a tier that assigns one (exact: 1.0) or a mapping from
    ``incident_id`` to a per-row value for a tier that does not (lexical: normalised bm25).
    Both tiers go through this one function on purpose: the scrubbing, the duration refusals
    and the summary fallback are the properties that must not differ between tiers, and a
    second builder is how they would.
    """
    ids = [r.id for r in rows]
    notes = _notes_by_incident(session, ids)
    history = person_name_history(session, ids)
    # Review M02: a pseudonymised incident's text is never re-derived. It shows what its
    # episode stored while its names were still known, or nothing.
    frozen = {r.id for r in rows if is_pseudonymised(session, r)}
    stored = _stored_summaries(session, sorted(frozen)) if frozen else {}
    episodes: list[SimilarEpisode] = []
    for inc in rows:
        inc_notes = notes.get(inc.id, [])
        if inc.id in frozen:
            summary = stored.get(inc.id, "")[:SUMMARY_MAX_CHARS]
        else:
            summary = _resolution_text(inc, inc_notes, _name_map(inc, inc_notes, history.get(inc.id, ())))
        closed_at = _closed_at(inc)
        row_relevance = relevance.get(inc.id, 0.0) if isinstance(relevance, dict) else relevance
        episodes.append(
            SimilarEpisode(
                incident_id=inc.id,
                incident_number=inc.incident_number,
                site_id=inc.site_id,
                fault_class=fault_class(inc.failure_domain, inc.alarm_code, inc.site_type),
                closed_at=closed_at,
                restore_minutes=_restore_minutes(inc),
                resolution_code=(inc.resolution_code or ""),
                resolution_summary=summary,
                match_reason=match_reason,
                score=_score(inc, closed_at, now, float(row_relevance)),
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


#: The columns ranking needs — the fault-class key, the two impact inputs and the three
#: timestamps ``_closed_at`` coalesces — and nothing else. Ranking runs over up to
#: ``_MAX_ROWS`` candidates; hydrating ~70-column ORM rows for all of them was most of the
#: remaining cost once only the shown rows were built (profiled at 5,000 episodes).
_RANK_COLUMNS = (
    IncidentRow.id,
    IncidentRow.failure_domain,
    IncidentRow.alarm_code,
    IncidentRow.site_type,
    IncidentRow.users_affected,
    IncidentRow.priority,
    IncidentRow.closed_at,
    IncidentRow.restored_at,
    IncidentRow.created_at,
)


def _top_by_score(
    rows: Sequence[Any],
    *,
    now: datetime,
    relevance: float | dict[str, float],
    limit: int,
) -> list[str]:
    """Ids of the ``limit`` candidates :func:`_build` would rank highest — chosen *before*
    building them.

    ``score`` depends only on cheap columns (``users_affected``, ``priority`` and the episode's
    end time), while building an episode costs a notes query and a regex scrub per row.
    Ranking first and building only what will be shown is what keeps a chronic site — 200
    prior outages of one fault class — inside MEM11's hot-path budget: measured at 5,000
    episodes, building every candidate took ~1 s per call against a 25 ms target.

    ``rows`` are column-only result rows (:data:`_RANK_COLUMNS`) — :func:`_score` and
    :func:`_closed_at` read attributes, so a ``Row`` serves as well as the ORM object. The key
    is exactly the one the caller sorts the built episodes by — ``(score, closed_at)`` with the
    same :func:`_score` — and Python's sort is stable under ``reverse=True``, so the ids chosen
    here and their order are identical to building everything and cutting after.
    """

    def key(row: Any) -> tuple[float, datetime]:
        ended = _closed_at(row)
        row_relevance = relevance.get(row.id, 0.0) if isinstance(relevance, dict) else relevance
        return (_score(row, ended, now, float(row_relevance)), ended)

    return [row.id for row in sorted(rows, key=key, reverse=True)[: max(0, int(limit))]]


def _load_in_order(session: Session, ids: Sequence[str]) -> list[IncidentRow]:
    """The full rows for ``ids``, in that order, re-fetched through ``_owned``.

    The ids came from an ``_owned`` statement already; the operator clause is applied again
    anyway because this is a second statement, and "the WHERE clause, never a check after the
    fetch" is the rule for every one of them (MEM10).

    **One primary-key probe per id, never ``id IN (...)``** (review M03). With the operator
    clause beside an IN list and no ``ANALYZE`` statistics — this project never runs ANALYZE —
    SQLite 3.49 chose ``ix_incidents_operator_id`` for five ids and walked every incident the
    operator owns, inside the lifecycle's write transaction, on every HITL card: a cost that
    grew with the whole table rather than with the site. Equality on the primary key is
    always a single-row lookup whatever the statistics say, and there are at most
    ``similar_limit`` (5) of them. ``test_memory_recall.py`` pins the plan of every hot-path
    statement and counts SQLite VM steps as 20,000 incidents are added at other sites.
    """
    rows: list[IncidentRow] = []
    for incident_id in ids:
        inc = session.scalar(_owned(IncidentRow).where(IncidentRow.id == incident_id))
        if inc is not None:
            rows.append(inc)
    return rows


# --------------------------------------------------------------------------- lexical tier

#: How many matching FTS rows — **of this operator's episodes** — one query may rank. Several
#: rows can belong to one incident (its resolution plus each of its notes) and are collapsed
#: to its best; still bounded, because an advisory read must never become a full index scan
#: rendered into a panel.
_FTS_SCAN = 200

#: ``memory_note_fts`` as a selectable, so the lexical query can be JOINED to ``incidents``
#: through ``_owned`` rather than written as raw SQL beside it. Two columns are all it needs.
_FTS = table(FTS_TABLE, column("incident_id"), column("body"))


def _fts_candidates(session: Session, match: str) -> dict[str, tuple[Any, float]]:
    """``{incident_id: (rank_row, normalised_relevance)}`` — scored over THIS operator only.

    ``memory_note_fts`` carries no ``operator_id`` (the §7.11.3 DDL has none), so the lexical
    query is joined to ``incidents`` and the operator clause comes from ``_owned`` — the one
    door — in the same statement. That ordering is the fix for review M05: the first version
    ranked the top 200 rows of the whole shared index and min-max normalised bm25 over them
    *before* dropping the other operator's ids, so Airtel's notes set the range Safaricom's
    relevance was scaled into, reordered Safaricom's results, and (through the cap) could
    push a Safaricom match out altogether. Now the other operator's rows never reach the
    ranking, the normaliser or the cap.

    The honest limit that remains: ``bm25()`` itself uses corpus-wide statistics — IDF and the
    average document length are computed by FTS5 over the whole index, both operators' rows
    included — so the *raw* score of a term moves a little as the other tenant writes notes.
    The normalisation, the ranking among this operator's hits and the membership of the
    result no longer depend on anything the other operator holds. Removing that last
    dependence means one index per operator, which is a schema decision, not a query fix.

    ``bm25()`` is lower-is-better (negative for a good match); raw scores are min-max
    normalised into ``(0, _RELEVANCE_FTS_MAX]``, and when every hit scores the same they all
    take the ceiling. The plan is FTS-driven (``SCAN memory_note_fts VIRTUAL TABLE INDEX
    0:M…``) with one primary-key probe into ``incidents`` per match.

    Degrades to ``{}`` on any error: no index yet and a build without FTS5 are both "no
    lexical tier", which is a weaker answer, never a wrong one.
    """
    if not match:
        return {}
    # Checked, never created: this is a read path, and a read does not issue DDL. A file
    # that has never been consolidated simply has no lexical tier yet — the consolidator
    # (``memory/consolidate.py``) is the only thing that calls ``ensure_memory_schema``.
    if not fts_available(session):
        return {}
    score = func.bm25(literal_column(FTS_TABLE)).label("fts_score")
    try:
        rows = session.execute(
            _owned(IncidentRow)
            .join(_FTS, _FTS.c.incident_id == IncidentRow.id)
            .where(sql(f"{FTS_TABLE} MATCH :fts_q"), IncidentRow.status.in_(_EPISODE_STATUSES))
            .with_only_columns(*_RANK_COLUMNS, score)
            .order_by(score)
            .limit(_FTS_SCAN)
            .params(fts_q=match)
        ).all()
    except Exception:  # noqa: BLE001 — no index, no FTS5, or a query FTS5 refused
        log.warning("memory lexical tier degraded to empty", exc_info=True)
        return {}
    if not rows:
        return {}
    # One incident can own several matching rows (resolution + notes); keep its best.
    best: dict[str, tuple[Any, float]] = {}
    for row in rows:
        raw = float(row.fts_score)
        if row.id not in best or raw < best[row.id][1]:
            best[row.id] = (row, raw)
    lo = min(raw for _row, raw in best.values())
    hi = max(raw for _row, raw in best.values())
    span = hi - lo
    return {
        incident_id: (row, _RELEVANCE_FTS_MAX if span <= 0 else _RELEVANCE_FTS_MAX * (hi - raw) / span)
        for incident_id, (row, raw) in best.items()
    }


def _fts_episodes(
    session: Session, *, query_text: str, now: datetime, exclude: set[str], limit: int
) -> list[SimilarEpisode]:
    """The lexical tier of §7.11.4's cascade: FTS5 ``MATCH`` over scrubbed note bodies.

    ``exclude`` holds the incident ids the exact tier already returned, so the union is
    deduplicated by ``incident_id`` with the stronger tier winning — a hit is never shown
    twice, and never demoted by a weaker match reason.

    Unlike the exact tier this one is **not** restricted to the site: finding the incident at
    a different site whose note says "generator fuel" is the entire reason a lexical tier
    exists. That is also why its relevance is capped below the structural tiers and why the
    matched terms travel in ``match_reason`` — a reader has to be able to see why another
    site's ticket is on their card (§7.11.7).
    """
    match = fts_query(query_text)
    # Operator-scoped and episode-scoped inside the FTS statement itself (``_owned`` plus
    # ``status IN``): an index row for an incident that has since reopened is stale, and
    # stale is dropped there, before anything is ranked.
    scored = _fts_candidates(session, match)
    candidates = [row for incident_id, (row, _rel) in scored.items() if incident_id not in exclude]
    if not candidates:
        return []
    relevances = {incident_id: rel for incident_id, (_row, rel) in scored.items()}
    terms = fts_terms(query_text)[:_MATCH_REASON_TERMS]
    return _build(
        session,
        _load_in_order(
            session, _top_by_score(candidates, now=now, relevance=relevances, limit=limit)
        ),
        now=now,
        relevance=relevances,
        match_reason=MATCH_FTS_PREFIX + ", ".join(terms),
    )


# --------------------------------------------------------------------------- public API
# Signatures are §7.11.4 verbatim so that a caller never changes when a tier is added.


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
    """Prior incidents that look like this one — the §7.11.4 retrieval cascade.

    Two of the three tiers are live:

    1. **exact** — same ``site_id`` + same ``fault_class``, an index scan on ``incidents``;
    2. **lexical** — FTS5 ``MATCH`` over the scrubbed bodies in ``memory_note_fts``, run only
       when ``query_text`` is given and the index exists;
    3. **vector** — cosine over ``memory_embeddings``, M4c, only with
       ``MEMORY_EMBEDDINGS_ENABLED``. Nothing produces it; :data:`TIER_VECTOR` reserves its
       place in the ordering so adding it later cannot reorder the two tiers above it.

    The tiers are unioned, **deduplicated by ``incident_id`` with the stronger tier winning**,
    and ranked on ``(tier, score, closed_at)`` — so an exact hit always outranks a lexical
    hit *regardless of raw similarity*. That is a hard rule, not a weighting: the failure mode
    being designed against (§7.11.7) is the confidently-wrong neighbour — the similarly-worded
    incident about a different site that sends an engineer to the wrong fault class — and a
    weighting would let a recent, high-impact lexical hit overtake an exact one through the
    recency and impact terms of the score.

    Within a tier, ``score = recency + relevance + impact`` (§7.11.4), newest first on a tie,
    so a run of comparable outages at one site reads chronologically while a much larger prior
    outage can still surface above a trivial recent one.

    ``query_text`` is what the lexical tier searches for — typically the new alarm's title or
    description. Omitting it is not an error: the exact tier alone is the M0 behaviour, and
    every M0 test exercises exactly that path. ``cfg`` carries the §7.11.3 ``memory:``
    thresholds and stays optional because :func:`memory_settings` falls back to the code
    defaults; it is read for nothing else here.
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
        .with_only_columns(*_RANK_COLUMNS)
    )
    candidates = [
        row
        for row in session.execute(narrowed).all()
        if fault_class(row.failure_domain, row.alarm_code, row.site_type) == wanted
    ]
    exact = _build(
        session,
        _load_in_order(
            session, _top_by_score(candidates, now=at, relevance=_RELEVANCE_EXACT, limit=rows_wanted)
        ),
        now=at,
        relevance=_RELEVANCE_EXACT,
        match_reason=MATCH_SAME_SITE_AND_FAULT_CLASS,
    )
    ranked: list[tuple[int, SimilarEpisode]] = [(TIER_EXACT, e) for e in exact]
    if (query_text or "").strip():
        seen = {e.incident_id for e in exact}
        ranked += [
            (TIER_LEXICAL, e)
            for e in _fts_episodes(
                session, query_text=query_text or "", now=at, exclude=seen, limit=rows_wanted
            )
        ]
    # The tier is the FIRST sort key and it is not negotiable — see the docstring. ``score``
    # and ``closed_at`` only break ties inside a tier.
    ranked.sort(key=lambda t: (t[0], t[1].score, t[1].closed_at), reverse=True)
    return tuple(e for _tier, e in ranked[:rows_wanted])


# ------------------------------------------------------------- L2: the fault-class prior
#
# The one derived fact M1 produces. It is computed over ``memory_episodes`` — the derived
# index — rather than over ``incidents``, because it is an *aggregate*: "how long does this
# class of fault usually take to fix here" needs a population of comparable durations, and
# the population is precisely what the consolidator exists to build (the durations it refused
# to trust are already NULL). The full L2 table (``memory_facts``, bi-temporal, subject-typed)
# is M3; this is the single prior that M1's own data can honestly support, with the same
# support/confidence/as_of/evidence discipline every later fact will carry.


def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile of a *sorted-on-entry-or-not* sample. ``None`` when empty.

    No numpy: it is not a declared dependency (§7.11.4's vector tier is the only thing that
    would add it, and it is unbudgeted). Linear interpolation rather than nearest-rank so a
    four-sample IQR does not jump by a whole observation when one episode is added.
    """
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * max(0.0, min(1.0, q))
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


#: §7.11.7: a duration beyond 3×IQR from the quartiles is not a slow restore, it is a data
#: error (a ticket closed a week late, a clock skew), and one of them moves a median.
IQR_OUTLIER_FACTOR = 3.0
#: Below four samples there are no meaningful quartiles, so nothing is called an outlier.
#: Refusing to judge is the fail-safe direction: a wrong duration kept is visible in the
#: evidence ids, a right duration discarded is invisible.
IQR_MIN_SAMPLE = 4


def outlier_bounds(values: Sequence[float]) -> tuple[float, float] | None:
    """``(low, high)`` acceptable range at 3×IQR, or ``None`` when the sample is too small."""
    if len(values) < IQR_MIN_SAMPLE:
        return None
    q1, q3 = quantile(values, 0.25), quantile(values, 0.75)
    if q1 is None or q3 is None:
        return None
    span = IQR_OUTLIER_FACTOR * (q3 - q1)
    return (q1 - span, q3 + span)


#: The comparison population is the most recent this-many trusted durations of a fault class,
#: not all of them. Two reasons, both measured rather than assumed: loading every row made one
#: advisory read cost ~0.3-1 s at 5,000 episodes (MEM11's target is 25 ms), and it made the
#: backfill quadratic — every incident compared against every earlier one. 500 recent cases
#: is also the better reference statistically: a fault class's restore time drifts as MSP
#: contracts and site hardware change, and a median over five years answers a question
#: nobody on the floor is asking. Served by ``ix_memory_episodes_op_fault_closed``.
POPULATION_MAX = 500


def restore_minutes_population(
    session: Session,
    *,
    fault_class_key: str,
    exclude_incident_id: str | None = None,
    limit: int = POPULATION_MAX,
) -> list[int]:
    """The most recent trusted restore durations for one fault class. Operator-scoped.

    ``exclude_incident_id`` leaves the incident being consolidated out of its own comparison
    set. That is what makes the 3×IQR decision **idempotent**: without it, the first
    consolidation judges an incident against a population that does not contain it and the
    second against one that does, and a borderline row would flip between runs.

    NULL ``restore_minutes`` rows are absent by construction — the consolidator has already
    refused every duration whose provenance it cannot stand behind (§7.0.8's M4 rule), so this
    population contains only durations a median may be computed from. One column is selected,
    not the ORM row: this runs on the hot path (via :func:`fault_class_prior`) and once per
    incident in a backfill.
    """
    stmt = _owned(MemoryEpisodeRow).where(
        MemoryEpisodeRow.fault_class == fault_class_key,
        MemoryEpisodeRow.restore_minutes.isnot(None),
    )
    if exclude_incident_id:
        stmt = stmt.where(MemoryEpisodeRow.incident_id != exclude_incident_id)
    stmt = (
        stmt.with_only_columns(MemoryEpisodeRow.restore_minutes)
        .order_by(MemoryEpisodeRow.closed_at.desc())
        .limit(max(1, int(limit)))
    )
    return [int(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _episode_evidence(
    session: Session, *, fault_class_key: str, limit: int = EVIDENCE_MAX
) -> tuple[str, ...]:
    """The newest incident ids behind a fault-class prior — traceability, capped (§7.11.7)."""
    ids = session.execute(
        _owned(MemoryEpisodeRow)
        .where(
            MemoryEpisodeRow.fault_class == fault_class_key,
            MemoryEpisodeRow.restore_minutes.isnot(None),
        )
        .order_by(MemoryEpisodeRow.closed_at.desc())
        .limit(limit)
        .with_only_columns(MemoryEpisodeRow.incident_id)
    ).scalars().all()
    return tuple(str(i) for i in ids)


@_degrade_to_empty
def fault_class_prior(
    session: Session,
    *,
    fault_class_key: str,
    cfg: OperatorConfig | None = None,
    now: datetime | None = None,
) -> tuple[MemoryHit, ...]:
    """Median and p90 restore minutes for a fault class, or ``()`` when support is thin.

    **Nothing below ``min_support`` is ever returned** (§7.11.4) — the primary defence against
    the confidently-wrong neighbour. Two prior outages are an anecdote; rendering "median 214
    min" from them on a P1 approval card is how an advisory becomes a wrong expectation.

    ``confidence = support × (1 − dispersion)`` with ``dispersion = IQR / median`` (§7.11.4),
    clamped into ``[0, 1]``: a fault class that sometimes takes 20 minutes and sometimes six
    hours says so through a low confidence rather than through a confident-looking median.
    """
    key = (fault_class_key or "").strip()
    if not key:
        return ()
    limits = memory_settings(cfg)
    min_support = int(limits["min_support"])
    sample = restore_minutes_population(session, fault_class_key=key)
    if len(sample) < min_support:
        return ()
    median = quantile(sample, 0.5)
    p90 = quantile(sample, 0.9)
    q1, q3 = quantile(sample, 0.25), quantile(sample, 0.75)
    dispersion = ((q3 - q1) / median) if (median and median > 0) else 1.0
    support = min(len(sample) / float(min_support), 1.0)
    confidence = max(0.0, min(1.0, support * (1.0 - dispersion)))
    evidence = _episode_evidence(session, fault_class_key=key)
    at = now or utcnow()
    return tuple(
        MemoryHit(
            layer="L2",
            subject_type="FAULT_CLASS",
            subject_id=key,
            key=name,
            value=f"{int(round(value))} min",
            numeric=round(float(value), 2),
            unit="minutes",
            support_count=len(sample),
            confidence=round(confidence, 4),
            # ``support`` stands in for the Generative-Agents *importance* term on a fact
            # (§7.11.4); a fact has no recency of its own beyond the episodes behind it.
            score=round(support, 4),
            as_of=at,
            evidence=evidence,
            source="deterministic",
        )
        for name, value in (("median_restore_min", median), ("p90_restore_min", p90))
        if value is not None
    )


# -------------------------------------------------------- the one call every reader makes


def recall_for_incident(
    session: Session,
    *,
    cfg: OperatorConfig | None = None,
    site_id: str,
    failure_domain: str,
    alarm_code: str = "",
    site_type: str = "BTS",
    region_code: str = "",
    msp_name: str | None = None,
    now: datetime | None = None,
    query_text: str | None = None,
) -> MemoryBundle:
    """Single entry point (§7.11.4). **NEVER raises.**

    Any exception, a missing table, an empty store or ``MEMORY_ENABLED=false`` yields
    ``MemoryBundle(degraded=True)`` with every tuple empty — and every caller must already
    behave exactly as it does today in that state (MEM4/G9). The flag is checked *here*,
    because this is where memory reaches a human; it is deliberately not checked inside the
    ``recall_*`` functions, which the consolidator also uses.

    ``region_code`` and ``msp_name`` are accepted and unused: they key the L2 ``REGION`` and
    ``MSP`` priors of M3/M4a. Taking them now means the MSP prior arrives without touching a
    caller — and ``msp_name`` is a **company**, never a person (§7.11.8); the ``PARTY_TOKEN``
    branch does not exist in this code at all.

    ``query_text`` extends §7.11.4's signature the same way ``recall_similar_episodes``'s
    does: pass it to enable the lexical tier, omit it for the exact tier alone.

    Budgeted to ``recall_limit`` facts and ``similar_limit`` episodes, and
    ``token_estimate`` reports what that costs a caller that is about to prompt with it, so
    the bundle is truncated rather than silently oversized.
    """
    if not memory_enabled():
        # Not one query. "Off" must cost nothing, including on a hot path (MEM11).
        return MemoryBundle()
    try:
        limits = memory_settings(cfg)
        at = now or utcnow()
        key = fault_class(failure_domain, alarm_code, site_type)
        similar = recall_similar_episodes(
            session,
            site_id=site_id,
            failure_domain=failure_domain,
            alarm_code=alarm_code,
            site_type=site_type,
            cfg=cfg,
            limit=int(limits["similar_limit"]),
            now=at,
            query_text=query_text,
        )
        facts = fault_class_prior(session, fault_class_key=key, cfg=cfg, now=at)[
            : int(limits["recall_limit"])
        ]
        bundle = MemoryBundle(
            fault_class=tuple(facts),
            similar=tuple(similar),
            degraded=not (facts or similar),
        )
        return replace(bundle, token_estimate=_token_estimate(bundle))
    except Exception:  # noqa: BLE001 — MEM4: the caller behaves exactly as it does today
        log.warning("recall_for_incident degraded", exc_info=True)
        return MemoryBundle()


def _token_estimate(bundle: MemoryBundle) -> int:
    """``chars // 4`` over the rendered block — the same rule of thumb ``contracts.py`` uses.

    There is no tokenizer dependency in this project and ``count_tokens`` is a network call;
    an estimate is enough for a caller to decide whether a bundle fits a budget, and
    :class:`MemoryBundle` names the field ``token_estimate`` so nobody reads it as measured.
    """
    rendered = str(_bundle_rows(bundle))
    return len(rendered) // 4


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


def hit_dict(hit: MemoryHit) -> dict[str, Any]:
    """One derived fact on the wire. ``as_of``, ``support_count`` and ``evidence`` are not
    optional decoration: §7.11.4 forbids a naked assertion, so the row a reader sees always
    carries how many cases it rests on, when it was computed and which tickets prove it."""
    return {
        "layer": hit.layer,
        "subject_type": hit.subject_type,
        "subject_id": hit.subject_id,
        "key": hit.key,
        "value": hit.value,
        "numeric": hit.numeric,
        "unit": hit.unit,
        "support_count": hit.support_count,
        "confidence": round(hit.confidence, 4),
        "score": round(hit.score, 4),
        "as_of": hit.as_of,
        "evidence": list(hit.evidence),
        "source": hit.source,
    }


def _bundle_rows(bundle: MemoryBundle) -> dict[str, Any]:
    """The bundle's payload without the envelope — what ``token_estimate`` is measured over."""
    return {
        "site": [hit_dict(h) for h in bundle.site],
        "fault_class": [hit_dict(h) for h in bundle.fault_class],
        "party": [hit_dict(h) for h in bundle.party],
        "correlation": [hit_dict(h) for h in bundle.correlation],
        "playbook": [],  # M2 (§7.11.10) — gated on the §8.1 resolution_code census
        "similar": episode_dicts(bundle.similar),
        "memos": [],  # M3
    }


def _z(row: Any) -> Any:
    """Every outgoing timestamp as an ISO **string** ending in ``Z`` (§7.0.6, defect #41).

    The same rule ``api/serializers.py`` and ``api/routers/memory.py`` apply, applied here
    because the advisory block travels on two wires this lane does not own — and a naive ISO
    string is read by a browser as *local* time, which in Nairobi backdates every prior
    outage by three hours.

    A **string**, not the ``_UtcZ`` datetime those two emit, because one of the wires is
    ``hitl_tasks.proposed_payload_json``: its setter is a bare ``json.dumps`` (``models.py``)
    with no encoder, so a datetime anywhere in this dict would raise ``TypeError`` **inside
    the HITL node** and take a P1 approval card down with it. The block is therefore plain
    JSON by construction, which is also what the serializer wants. Recursive, so a timestamp
    nested inside an episode is converted too.
    """
    if isinstance(row, datetime):
        return z_utc(row).isoformat()
    if isinstance(row, dict):
        return {k: _z(v) for k, v in row.items()}
    if isinstance(row, list):
        return [_z(v) for v in row]
    return row


def advisory_block(bundle: MemoryBundle) -> dict[str, Any]:
    """§7.11.4's ``render.advisory_block`` — the bundle as the card and the API render it.

    One shape, two wires: the HITL packet freezes this dict at task creation and the
    single-incident serializer computes it at request time (§7.11.5, MEM2 channels (a) and
    (b)). Rendering both from one function is what stops the workspace panel and the approval
    card from disagreeing about the same ticket.

    ``degraded`` is on the wire for the same reason the Wallboard shows STALE badges: an
    empty advisory because the store has nothing must not read as "this site has a clean
    record". Every key is present even when its layer is M2/M3, so a renderer written today
    keeps working when they fill.
    """
    payload: dict[str, Any] = {
        "enabled": True,
        "degraded": bundle.degraded,
        "token_estimate": bundle.token_estimate,
    }
    payload.update(_bundle_rows(bundle))
    return _z(payload)


def advisory_for_incident(
    session: Session,
    inc: IncidentRow,
    cfg: OperatorConfig | None = None,
) -> dict[str, Any] | None:
    """The advisory block for one incident, or ``None`` when ``MEMORY_ENABLED`` is false.

    The single door both hot-path-adjacent readers use (spec line 419): ``agents/hitl.py``
    freezes it onto ``hitl_tasks.proposed_payload_json["advisory"]`` at task creation and
    ``GET /api/v1/incidents/{id}`` computes it per request. ``None`` is what makes the two
    treatments differ correctly: the HITL payload then gains **no key at all** (so a
    flag-off task is byte-identical to one written before this lane existed), while the
    serializer sets ``advisory: null`` (§7.11.11 test 25).

    **It never raises**, for anything, ever. A P1 approval card must not fail to open because
    an advisory read hit a half-migrated column. The caller in ``agents/hitl.py`` guards the
    call a second time; that is belt and braces on the one path where the cost of being wrong
    is an outage of the tool people use to fix outages.

    No ``query_text``, so the hot path runs the **exact tier only**. Deliberate: the FTS index
    is a cache that may be empty or a tick stale, MEM11 budgets this call at ~25 ms, and the
    lexical tier's whole value is surfacing a *different* site's ticket — which is exactly the
    confidently-wrong neighbour (§7.11.7) that should not reach an approval card until M2
    renders support counts beside it. ``recall_similar_episodes`` and ``recall_for_incident``
    still take it, so an opt-in caller loses nothing.
    """
    try:
        if not memory_enabled():
            return None
        # Review M13. Every query below takes its operator from the PROCESS profile
        # (``api.deps._owned`` reads ``get_settings()``), while this incident and ``cfg`` come
        # from the caller — and ``run_incident_lifecycle`` accepts explicit settings. If the
        # two ever disagree, recall would freeze the process operator's history onto this
        # operator's card. There is exactly one safe answer to "whose memory is this?" when
        # they disagree: nobody's. No advisory is always a valid state (MEM4).
        process_operator = _settings().operator.operator_id
        if (inc.operator_id or "") != process_operator or (
            cfg is not None and getattr(cfg, "operator_id", process_operator) != process_operator
        ):
            log.warning(
                "advisory withheld: incident operator %r / cfg operator %r != process operator %r",
                inc.operator_id, getattr(cfg, "operator_id", None), process_operator,
            )
            return None
        bundle = recall_for_incident(
            session,
            cfg=cfg,
            site_id=inc.site_id,
            failure_domain=inc.failure_domain,
            alarm_code=inc.alarm_code or "",
            site_type=inc.site_type or _SITE_TYPE_DEFAULT,
            region_code=inc.region_code or "",
            # A company (§7.11.8). The per-person prior is Phase 6 and is not in this code.
            msp_name=getattr(inc, "responsible_msp", None) or getattr(inc, "msp_name", None),
        )
        block = advisory_block(bundle)
        # Proven serialisable HERE, inside this function's own guard. The HITL node stores the
        # payload through ``HitlTaskRow.proposed_payload``'s bare ``json.dumps`` — and that
        # assignment sits *outside* the node's try/except, so a value that is not JSON would
        # raise in a fail-closed node and roll back the whole lifecycle run. It is not
        # hypothetical: an early draft of this block carried datetime objects, and the
        # inertness test caught exactly that. A block that cannot be serialised is dropped.
        json.dumps(block)
        return block
    except Exception:  # noqa: BLE001 — memory never blocks a card or a workspace load (MEM4)
        log.warning("advisory_for_incident degraded to None", exc_info=True)
        return None
