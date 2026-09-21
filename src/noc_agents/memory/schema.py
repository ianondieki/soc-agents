"""The FTS5 index behind memory's lexical recall tier (spec §7.11.3).

``memory_note_fts`` is a **virtual table**, so it cannot be declared on ``Base`` and cannot
appear in ``Base.metadata`` — which means neither ``create_all`` nor ``migrate_additive``
(which walks ``Base.metadata.sorted_tables``) will ever create it. It is created here,
lazily and idempotently with ``CREATE VIRTUAL TABLE IF NOT EXISTS``, by whoever is about to
use it: the consolidator before it writes, the lexical recall tier before it reads. That is
the pattern ``services/contracts.ensure_fts`` already uses for ``contract_clauses_fts``
(§7.8.1), and it is shared here rather than reinvented: after the first call it is a
catalogue check.

Three properties worth stating plainly, because each is a decision:

* **It is a cache, never a source of truth.** Every row is derived from ``work_notes`` and
  ``incidents``; dropping the table costs a backfill and nothing else. That is why it may be
  created outside a migration at all.
* **It is populated by the consolidator, never by triggers** (§7.11.3). A trigger on
  ``work_notes`` would fire *inside* the hot-path transaction — and inside
  ``runner._fail_closed``'s rollback — which is exactly what MEM5 forbids.
* **It carries no ``operator_id``.** The spec's DDL has five UNINDEXED columns and none of
  them is the operator, so an FTS hit **cannot** be trusted on its own (MEM10). The lexical
  query therefore JOINS this table to ``incidents`` and takes its operator clause from
  ``api.deps._owned`` in the same statement, so the other operator's rows never reach the
  ranking or the normaliser (review M05; ``services/memory._fts_candidates``). The one thing
  that stays corpus-wide is FTS5's own bm25 statistics. Adding an ``operator_id`` column here
  would look like a fix and would not be one: an UNINDEXED FTS column is still a value the
  writer chose, not an ownership proof, and one place to check (``_owned``) beats two.

Bodies stored here are **scrubbed before insertion** (§7.11.8 rule 1) — this index is full of
attacker-reachable vendor free text (MEM9), and an unscrubbed FTS row would be a name
searchable by anyone with the route.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

log = logging.getLogger("noc_agents.memory.schema")

__all__ = [
    "FTS_TABLE",
    "FTS_DDL",
    "REF_NOTE",
    "REF_RESOLUTION",
    "ensure_memory_schema",
    "fts_available",
    "fts_query",
    "fts_terms",
    "optimize_index",
]

FTS_TABLE = "memory_note_fts"

#: §7.11.3 verbatim. ``porter`` stems ("refuelled" finds "refuel"), ``unicode61`` with
#: ``remove_diacritics 2`` folds the accents that reach a NOC through vendor copy-paste.
#: Only ``body`` is indexed; the five UNINDEXED columns are carried so a hit can be traced
#: back without a join, and are never searched.
FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    "body, "
    "site_id UNINDEXED, "
    "failure_domain UNINDEXED, "
    "fault_class UNINDEXED, "
    "incident_id UNINDEXED, "
    "ref_kind UNINDEXED, "
    "tokenize = 'porter unicode61 remove_diacritics 2'"
    ")"
)

#: ``ref_kind`` vocabulary. ``PROBLEM`` (``problems.summary``) is in the spec's list and lands
#: with L2 in M3 — a problem record is a different subject from an incident and its recall
#: belongs with the site profile, not with episode similarity.
REF_NOTE = "NOTE"
REF_RESOLUTION = "RESOLUTION"


def ensure_memory_schema(target: Session | Engine) -> bool:
    """Create ``memory_note_fts`` if absent. ``False`` (and a WARNING) when FTS5 is missing.

    Accepts a ``Session`` (the consolidator, the recall tier) or an ``Engine`` (§7.11.4's
    ``ensure_memory_schema(engine)`` signature, and what a start-up hook would hold). Both
    are one statement; the Engine form opens and commits its own transaction, the Session
    form joins the caller's so the DDL and the rows it is about to write land together.

    Returns a bool rather than raising because FTS5 is a *build option* of SQLite. It is
    present on this project's interpreter (verified 2026-09-16, §7.11.3), but a build without
    it must degrade to "no lexical tier" — the exact tier is unaffected, and an advisory panel
    with one tier instead of two is not an outage.
    """
    try:
        if isinstance(target, Session):
            target.execute(sql(FTS_DDL))
            _ensure_secure_delete(target)
        else:
            with target.begin() as conn:
                conn.execute(sql(FTS_DDL))
                _ensure_secure_delete(conn)
        return True
    except Exception:  # noqa: BLE001 — a build without FTS5 degrades; it never crashes the app
        log.warning("%s could not be created; FTS5 unavailable", FTS_TABLE, exc_info=True)
        return False


#: FTS5's persistent ``secure-delete`` option (SQLite ≥ 3.42; this interpreter has 3.49.1).
#: Review M09: without it, a ``DELETE`` from an FTS5 table removes the row from
#: ``memory_note_fts_content`` but leaves its terms in the ``memory_note_fts_data`` segment
#: b-trees until a merge — so a re-consolidation that scrubbed a newly-known name, and the
#: 24-month prune, both left that name's tokens in the file. With it on, a delete removes the
#: terms from the index there and then. It is stored in ``memory_note_fts_config`` and so
#: applies to every connection from then on.
_SECURE_DELETE = "secure-delete"


def _ensure_secure_delete(conn: Any) -> None:
    """Switch on FTS5 secure-delete once, idempotently; never fatal.

    Read first, write only when it is not already on, so the consolidator's per-incident call
    does not rewrite the config row every time. On a SQLite older than 3.42 the option does
    not exist and the INSERT raises; that is logged and tolerated, and ``expire_memory``'s
    ``optimize`` pass after a prune is the backstop that still merges the terms out.
    """
    try:
        current = conn.execute(
            sql(f"SELECT v FROM {FTS_TABLE}_config WHERE k = :k"), {"k": _SECURE_DELETE}
        ).scalar()
        if str(current) == "1":
            return
        conn.execute(
            sql(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rank) VALUES (:opt, 1)"), {"opt": _SECURE_DELETE}
        )
    except Exception:  # noqa: BLE001 — an old SQLite without the option still has optimize
        log.warning("FTS5 secure-delete unavailable on this SQLite; relying on optimize", exc_info=True)


def optimize_index(session: Session) -> bool:
    """Merge the FTS5 segments into one (``'optimize'``) — the prune's erasure backstop.

    Needed only where secure-delete could not be enabled or was enabled after rows had
    already been deleted (a file built by an earlier version of this lane); harmless
    otherwise. Its cost is proportional to the index, which is why it runs once per prune in
    the nightly housekeeping duty and never per incident.
    """
    if not fts_available(session):
        return False
    try:
        session.execute(sql(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('optimize')"))
        return True
    except Exception:  # noqa: BLE001 — an unoptimised index is still correct for every read
        log.warning("FTS5 optimize failed", exc_info=True)
        return False


def fts_available(session: Session) -> bool:
    """Whether the lexical tier can run at all — asked of the catalogue, never by failing.

    Read paths call this instead of :func:`ensure_memory_schema`: a read must not issue DDL,
    and it must not learn "the table is missing" from an exception either. On SQLite a failed
    statement leaves the transaction usable, but on PostgreSQL it aborts it
    ("current transaction is aborted"), and the advisory read shares its session with the
    rest of the request. Checking ``sqlite_master`` answers the question without a failure
    path; any other engine has no FTS5 and therefore no lexical tier.
    """
    try:
        if session.get_bind().dialect.name != "sqlite":
            return False
        row = session.execute(
            sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": FTS_TABLE},
        ).first()
        return row is not None
    except Exception:  # noqa: BLE001 — an unreadable catalogue means "no lexical tier"
        return False


#: Words that carry no signal in a NOC note and would otherwise match every row. Kept small
#: and deliberately *without* negations ("no", "not") — "generator not started" is precisely
#: the kind of note somebody is searching for. Mirrors the reasoning in
#: ``services/contracts._STOPWORDS``, with the list re-chosen for outage text rather than
#: contract text.
_STOPWORDS = frozenset(
    """a an the and or of to in on at for by with from as is are was were be been being it its
    this that these those there their they them we our you your he she his her do does did done
    has have had having will would shall should can could may might must if then than so such
    what which who whom whose when where why how into onto over under about after before during
    while per each any all both either neither also very site""".split()
)

#: Letters and digits only. Everything else — quotes, ``*``, ``:``, ``AND``/``NOT`` — is
#: discarded rather than escaped, so no free text a vendor typed can reach the FTS5 parser.
_TERM_RE = re.compile(r"[0-9a-zA-ZÀ-ɏ]+")


def fts_terms(query_text: str | None) -> list[str]:
    """The usable search terms in ``query_text``: lower-cased, de-duplicated, in order."""
    seen: list[str] = []
    for term in _TERM_RE.findall(query_text or ""):
        low = term.lower()
        if len(low) < 3 or low in _STOPWORDS or low in seen:
            continue
        seen.append(low)
    return seen


def fts_query(query_text: str | None) -> str:
    """A safe FTS5 ``MATCH`` expression: every term quoted, joined with ``OR``.

    Quoting neutralises FTS5 query syntax — a vendor note pasted into a search box contains
    quotes, colons and hyphens, all of which are operators to the FTS5 parser and would raise
    a syntax error out of an advisory read. ``OR`` rather than ``AND`` because a question is
    not a conjunction of required words and ``bm25`` already ranks a row matching more of
    them higher. Returns ``""`` when nothing usable remains, which callers read as "no hits"
    — never as "match everything".
    """
    return " OR ".join(f'"{t}"' for t in fts_terms(query_text))
