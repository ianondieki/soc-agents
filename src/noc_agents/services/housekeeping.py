"""HousekeepingAgent — retention, outbox sweep, redaction scan, daily backup (spec §5.3.22).

One scheduled job (``housekeeping``, daily, ``HOUSEKEEPING_ENABLED``, A0, fail-soft) with
four duties, in this order and for this reason:

1. **Backup first.** ``backup_db`` runs before anything mutates a row, so today's copy
   predates today's purge. A backup taken after a bad delete is a backup of the damage.
2. **Retention by column class** (§9.4): ``purge_expired`` deletes rows whose class says
   delete, ``pseudonymise_personal_fields`` turns staff/vendor names into role tokens while
   the network facts beside them stay. Both read ``config/retention.yaml``; neither has a
   default that deletes.
3. **Outbox sweep**: terminal rows older than 90 days keep their delivery record and lose
   their payload; FAILED rows are counted; stale CLAIMED rows are *reported and left alone*.
4. **Post-send redaction scan** (§9.6): outbox rows SENT in the last 24 h are re-scanned for
   e-mails and Kenyan MSISDNs, and a hit raises ``AuditRow(action="redaction.miss")`` and
   ``security.redaction_miss``.

Then the memory-expiry seam (§7.11.8) and a freshness report.

Why this module is written more defensively than its neighbours
---------------------------------------------------------------
This is the only code in the system that deletes operational data, and the floor it is
deleting against is a **licence condition**, not a preference: CA Network Facilities
Provider Tier 1 licence Condition 12.2 requires operational records for at least 3 years.
Deleting an incident record early is a regulatory breach, not a bug. So:

* **Nothing is deleted unless a rule says so.** There is no wildcard. A table with no rule
  in ``config/retention.yaml``, or one whose class resolves to ``keep``, is never touched,
  and a table the schema does not have yet is reported as skipped, never guessed at.
* **The licence floor is enforced in code, not only in config.** :func:`validate_policy`
  refuses any delete rule on a table classed ``network_facts`` (and any rule under
  ``licence_floor_days``); a policy that fails validation deletes *nothing at all* — the
  purge declines rather than proceeding with the rules that happen to be legal.
* **Dry run is the default posture.** Deleting for real needs BOTH ``posture.dry_run:
  false`` in the YAML AND ``HOUSEKEEPING_APPLY=true`` in the environment. Neither key on
  its own can start a delete, and every run's summary and audit row state which posture
  was in force, so "did it actually delete anything?" is answerable from the audit trail.
* **Every duty is idempotent.** Purge deletes by cutoff (a second run finds nothing);
  pseudonymisation compares against the token it would write and skips a row already
  carrying it; the outbox archive skips rows already archived; the redaction scan skips a
  row that already has its ``redaction.miss`` audit row; the daily backup skips a file
  already written for today's EAT date. §5.3.22's exit criterion is "second run deletes
  nothing", and that is a test here, not an aspiration.
* **Every write is operator-scoped.** Multi-tenancy is a data-protection boundary, not a
  filter: a purge that crosses it deletes another controller's records.

What this module must NOT do to the outbox
------------------------------------------
``orchestrator/outbox.py`` reclaims a CLAIMED row whose 120 s lease expired by putting it
back to PENDING, and the dispatcher then sends it. That is the dispatcher's own
at-least-once design, with ``attempts`` bounding it. Housekeeping **never writes
``outbox.status``** — not to reclaim, not to tidy, not to close. Releasing a stale lease
looks like cleanup and is actually a re-dispatch: a row whose SMS reached the customer but
whose outcome commit died would be sent a second time. The sweep therefore counts stuck
rows and reports them for a human, and only ever rewrites ``payload_json`` /
``envelope_json`` on rows that are already terminal.

What this module does NOT own
-----------------------------
``expire_memory()`` (§7.11.8) is the memory lane's. The ``memory_*`` tables do not exist in
this schema yet, so :func:`expire_memory` here is a **seam**: it imports
``noc_agents.memory.consolidate.expire_memory`` lazily and reports "seam not wired" when
that module is absent. No memory SQL is written here, and none should be added here.

Gating
------
``HOUSEKEEPING_ENABLED`` defaults to **false**. :func:`run` re-checks it itself (as
``pollers.weather.poll`` does with ``WEATHER_ENABLED``) so wiring :data:`HOUSEKEEPING_JOB`
into ``scheduler.loop.SCHEDULED_JOBS`` can never start deleting on a machine that did not
opt in. Off means: no read, no write, one skipped step.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NoReturn

import yaml
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import Table

from noc_agents.config import CONFIG_DIR, ROOT, AppSettings, get_settings
from noc_agents.db.models import AuditRow, Base, ExternalSignalRow, ScheduledJobStateRow, new_id, utcnow
from noc_agents.orchestrator import outbox as outbox_mod
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import eat_date
from noc_agents.services.redaction import EMAIL_RE, PHONE_RE, NameMap, scrub_text

log = logging.getLogger("noc_agents.housekeeping")

__all__ = [
    "AGENT",
    "APPLY_ENV",
    "ARCHIVED_KEY",
    "AUDIT_ACTION",
    "ENABLED_ENV",
    "GRAPH_NAME",
    "HOUSEKEEPING_JOB",
    "INTERVAL_S",
    "JOB_NAME",
    "LICENCE_FLOOR_DAYS",
    "MEMORY_EXPIRY_SEAM",
    "PSEUDONYMISED_ACTION",
    "REDACTION_MISS_ACTION",
    "BackupReport",
    "HousekeepingError",
    "HousekeepingReport",
    "MemoryExpiryReport",
    "OutboxSweepReport",
    "PseudonymiseReport",
    "PurgeReport",
    "RedactionScanReport",
    "RetentionPolicy",
    "RetentionPolicyError",
    "RetentionRule",
    "TableRule",
    "applying",
    "backup_db",
    "default_policy_path",
    "expire_memory",
    "freshness_report",
    "housekeeping_enabled",
    "is_marked_pseudonymised",
    "load_policy",
    "post_send_redaction_scan",
    "pseudonymisation_marker_id",
    "pseudonymise_personal_fields",
    "purge_expired",
    "run",
    "sweep_outbox",
    "validate_policy",
]

JOB_NAME = "housekeeping"
INTERVAL_S = 86400  # daily 03:00 EAT (§5.3.22); the loop's cadence is an interval, not a cron
AGENT = "HousekeepingAgent"
GRAPH_NAME = "housekeeping"  # agent_runs.graph_name, as "outbox" / "monitor" do (§10.4)
ENABLED_ENV = "HOUSEKEEPING_ENABLED"
APPLY_ENV = "HOUSEKEEPING_APPLY"
MAX_SECONDS = 600  # §10.5 budget

#: CA Network Facilities Provider Tier 1 licence Condition 12.2 — operational records ≥ 3 years.
#: Kept in code as well as in the YAML so that deleting the line from the YAML cannot lower it.
LICENCE_FLOOR_DAYS = 1095

AUDIT_ACTION = "retention.purge"  # §5.3.22 outputs
REDACTION_MISS_ACTION = "redaction.miss"  # §9.6
REDACTION_MISS_EVENT = "security.redaction_miss"  # §9.6 WS type
ACTOR = "agent:HousekeepingAgent"
ACTOR_ROLE = "AGENT"

#: Marker key written into an archived outbox payload. Its presence is what makes the
#: archive idempotent, and what keeps the redaction scan from re-scanning a summary.
ARCHIVED_KEY = "_archived"

#: The durable, per-row record that ``pseudonymise_personal_fields`` rewrote a row's person
#: columns (memory review round 3). One ``AuditRow`` per pseudonymised row, written in the SAME
#: transaction as the column rewrite — the duty wrapper in :func:`run` commits both or neither —
#: so the marker cannot exist without the rewrite, nor the rewrite without the marker.
#:
#: Why a marker at all: the memory lane must never re-derive an incident's free text once its
#: names are gone from the person columns (the NameMap could no longer see the names still in
#: the notes). The first attempt inferred "pseudonymised" from a column *looking like* its role
#: token, and ``assignment`` legitimately writes ``RNIO-MTK`` / ``RNIO-{region}`` — exactly what
#: ``RNIO-{region_code}`` renders — so live incidents in four of six Safaricom regions were
#: misread as pseudonymised. Only housekeeping knows it pseudonymised a row, so housekeeping
#: records it.
#:
#: Why its id is DETERMINISTIC: ``audit_events`` is indexed on ``ts`` alone, and the memory lane
#: asks this question on the lifecycle's hot path. A uuid5 of ``(table, row id)`` makes the
#: lookup a primary-key probe — one B-tree descent whatever the size of the audit table — with
#: no new index and no schema change. uuid5 ids carry version nibble 5, so they can never
#: collide with the uuid4 ids every other audit row has.
#:
#: Durability: ``audit_events`` is in the ``llm_call_records`` class, whose action is ``keep``
#: (``config/retention.yaml``); ``test_housekeeping.py`` pins that, because a purge of these
#: rows would silently turn frozen memory text back into re-derivable text.
PSEUDONYMISED_ACTION = "retention.pseudonymised"
_PSEUDONYMISED_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:noc-agents:retention:pseudonymised")


def pseudonymisation_marker_id(table: str, row_id: Any) -> str:
    """The deterministic ``audit_events.id`` of ``table``/``row_id``'s pseudonymisation marker."""
    return str(uuid.uuid5(_PSEUDONYMISED_NAMESPACE, f"{table}:{row_id}"))


def is_marked_pseudonymised(session: Session, table: str, row_id: Any, *, operator_id: str | None = None) -> bool:
    """Whether housekeeping recorded pseudonymising this row. One primary-key probe; never raises.

    ``operator_id``, when given, must match the marker's — a marker is another controller's
    record otherwise, and cannot speak for this operator's row.
    """
    try:
        marker = session.get(AuditRow, pseudonymisation_marker_id(table, row_id))
    except Exception:  # noqa: BLE001 — an unreadable audit table proves nothing either way
        log.warning("housekeeping: pseudonymisation marker lookup failed", exc_info=True)
        return False
    if marker is None or marker.action != PSEUDONYMISED_ACTION:
        return False
    return operator_id is None or marker.operator_id == operator_id


def _mark_pseudonymised(
    session: Session,
    *,
    operator_id: str,
    table: str,
    row_id: Any,
    columns: Iterable[str],
    class_name: str,
    cutoff: datetime,
    now: datetime,
) -> None:
    """Write the marker once per row. Column NAMES only in the payload — never a value, so the
    marker itself holds nothing personal. A second pseudonymisation of the same row (a person
    column filled in after the first pass) finds the marker and leaves it: it records that the
    row HAS been pseudonymised, and the first time is the one that matters."""
    marker_id = pseudonymisation_marker_id(table, row_id)
    if session.get(AuditRow, marker_id) is not None:
        return
    session.add(
        AuditRow(
            id=marker_id,
            ts=now,
            operator_id=operator_id,
            actor=ACTOR,
            action=PSEUDONYMISED_ACTION,
            entity_type=table,
            entity_id=str(row_id),
            rationale=f"personal columns replaced by role tokens under retention class {class_name}",
            payload_json=json.dumps(
                {"table": table, "columns": sorted(columns), "class": class_name, "cutoff": cutoff.isoformat()}
            ),
        )
    )


#: Where ``expire_memory()`` will live when Lane 4C ships it (§7.11, spec line ~2153).
#: Housekeeping calls it through :func:`expire_memory` and does no memory SQL of its own.
MEMORY_EXPIRY_SEAM = "noc_agents.memory.consolidate:expire_memory"

# Actions the engine understands. Anything else in the YAML is a policy error, never a guess.
KEEP, DELETE, PSEUDONYMISE, ARCHIVE_PAYLOAD, ROTATE, MEMORY_LANE, MANUAL = (
    "keep", "delete", "pseudonymise", "archive_payload", "rotate", "memory_lane", "manual"
)
_KNOWN_ACTIONS = frozenset({KEEP, DELETE, PSEUDONYMISE, ARCHIVE_PAYLOAD, ROTATE, MEMORY_LANE, MANUAL})
#: The only actions that touch rows in this module. `rotate` is files; `memory_lane` is a
#: seam; `keep`/`manual` are the do-nothing defaults.
_ROW_ACTIONS = frozenset({DELETE, PSEUDONYMISE, ARCHIVE_PAYLOAD})

DEFAULT_POLICY_FILENAME = "retention.yaml"
POLICY_PATH_ENV = "RETENTION_POLICY_PATH"  # tests and operators may point elsewhere

_UNKNOWN = "UNKNOWN"  # what a role-token template renders for a field the row does not carry


class RetentionPolicyError(RuntimeError):
    """The retention policy is unusable, so NOTHING is deleted.

    Raised by :func:`load_policy` when the YAML is missing, malformed, or asks for something
    the licence floor forbids. Deliberately loud: a broken retention config must stop the
    purge and show up as a FAILED run, never degrade into "delete what still parses".
    """


class HousekeepingError(RuntimeError):
    """One or more duties failed. Raised by :func:`run` AFTER every duty has had its turn
    and committed its own work, so the job is fail-soft (the other duties still ran) and
    still visible (a FAILED run row, and the circuit breaker counts it)."""


# ------------------------------------------------------------------------------ environment
#
# A local copy of scheduler.loop's _env_bool rather than an import: scheduler/loop.py is the
# module that will import THIS one to register the job card, and importing it back would be
# the cycle that scheduler/__init__.py exists to prevent.

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def housekeeping_enabled() -> bool:
    """``HOUSEKEEPING_ENABLED`` — default **false** (§5.3.22, Appendix B)."""
    return _env_bool(ENABLED_ENV, False)


def apply_requested() -> bool:
    """``HOUSEKEEPING_APPLY`` — default **false**. One of the two keys deletion needs."""
    return _env_bool(APPLY_ENV, False)


def applying(policy: "RetentionPolicy") -> bool:
    """True only when BOTH keys agree: the YAML is out of dry run AND the env flag is set.

    Two independent keys on purpose. A config edit alone cannot start deleting (the file is
    reviewed by Legal, not by whoever runs the box), and an env flag alone cannot either
    (the flag is set by whoever runs the box, not by Legal). Both parties have to act.
    """
    return (not policy.dry_run) and apply_requested()


# ---------------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class RetentionRule:
    """One column class from §9.4: what happens, after how long, and why it is lawful."""

    name: str
    action: str
    days: int | None = None
    keep: int | None = None  # `rotate` only: how many files survive
    licence_floor: bool = False
    legal_basis: str = ""

    @property
    def deletes_rows(self) -> bool:
        return self.action == DELETE


@dataclass(frozen=True)
class TableRule:
    """One table's rule: which class it belongs to and how its age is measured."""

    table: str
    class_name: str
    timestamp_column: str | None = None
    statuses: tuple[str, ...] = ()
    personal_class: str | None = None
    role_tokens: Mapping[str, str] = field(default_factory=dict)
    scrub_text_columns: tuple[str, ...] = ()

    @property
    def has_personal_block(self) -> bool:
        return bool(self.role_tokens or self.scrub_text_columns)


@dataclass(frozen=True)
class RetentionPolicy:
    """``config/retention.yaml``, parsed and validated."""

    path: Path
    version: int
    dry_run: bool
    licence_floor_days: int
    classes: Mapping[str, RetentionRule]
    tables: Mapping[str, TableRule]
    backup_dir: str
    backup_keep_daily: int
    outbox_archive_after_days: int
    redaction_lookback_hours: int

    def rule_for(self, table: str) -> RetentionRule:
        """The class rule for a table. **An unlisted table is `keep`** — the default that
        over-retains, because the other default is a licence breach."""
        entry = self.tables.get(table)
        if entry is None:
            return RetentionRule("unclassified", KEEP, legal_basis="not classified in §9.4: kept by default")
        return self.classes[entry.class_name]

    def personal_rule_for(self, table: str) -> RetentionRule | None:
        entry = self.tables.get(table)
        if entry is None or entry.personal_class is None:
            return None
        return self.classes.get(entry.personal_class)


def default_policy_path() -> Path:
    """``config/retention.yaml``; ``RETENTION_POLICY_PATH`` overrides (tests, per-site files)."""
    override = (os.getenv(POLICY_PATH_ENV) or "").strip()
    return Path(override) if override else CONFIG_DIR / DEFAULT_POLICY_FILENAME


def _as_int(raw: Any, field_name: str, where: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RetentionPolicyError(f"{where}: {field_name}={raw!r} is not an integer") from exc


def load_policy(path: Path | str | None = None) -> RetentionPolicy:
    """Parse and validate ``config/retention.yaml``. Raises :class:`RetentionPolicyError`.

    Never "best effort": a policy that does not parse, or that fails :func:`validate_policy`,
    raises, and :func:`run` then skips the two deleting duties entirely. That is the whole
    point — an unreadable retention policy must not become an implicit licence to delete,
    and it must not become a silent licence to keep either: the run goes FAILED and someone
    looks at it.
    """
    target = Path(path) if path else default_policy_path()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise RetentionPolicyError(f"retention policy {target} could not be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RetentionPolicyError(f"retention policy {target} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RetentionPolicyError(f"retention policy {target} must be a mapping, got {type(raw).__name__}")

    posture = raw.get("posture") or {}
    # An absent or non-boolean dry_run reads as TRUE. The safe direction for a missing key
    # in a deletion policy is "do not delete".
    dry_run = posture.get("dry_run", True)
    dry_run = True if not isinstance(dry_run, bool) else dry_run

    floor = _as_int(raw.get("licence_floor_days", LICENCE_FLOOR_DAYS), "licence_floor_days", str(target))
    # The YAML may RAISE the floor (a stricter operator) but never lower it below the
    # licence condition compiled into this module.
    floor = max(floor, LICENCE_FLOOR_DAYS)

    classes_raw = raw.get("classes") or {}
    tables_raw = raw.get("tables") or {}
    # A list where a mapping belongs would otherwise raise an AttributeError deep in the
    # loop below; the caller needs to see "your retention policy is malformed", because the
    # decision it drives is whether to delete.
    for label, section in (("classes", classes_raw), ("tables", tables_raw)):
        if not isinstance(section, dict):
            raise RetentionPolicyError(f"{target}: `{label}` must be a mapping, got {type(section).__name__}")

    classes: dict[str, RetentionRule] = {}
    for name, body in classes_raw.items():
        if not isinstance(body, dict):
            raise RetentionPolicyError(f"{target}: class {name!r} must be a mapping")
        action = str(body.get("action") or KEEP).strip()
        classes[str(name)] = RetentionRule(
            name=str(name),
            action=action,
            days=_as_int(body["days"], "days", f"{target} class {name}") if body.get("days") is not None else None,
            keep=_as_int(body["keep"], "keep", f"{target} class {name}") if body.get("keep") is not None else None,
            licence_floor=bool(body.get("licence_floor", False)),
            legal_basis=str(body.get("legal_basis") or "").strip(),
        )

    tables: dict[str, TableRule] = {}
    for name, body in tables_raw.items():
        if not isinstance(body, dict):
            raise RetentionPolicyError(f"{target}: table {name!r} must be a mapping")
        personal = body.get("personal") or {}
        tables[str(name)] = TableRule(
            table=str(name),
            class_name=str(body.get("class") or ""),
            timestamp_column=(str(body["timestamp_column"]) if body.get("timestamp_column") else None),
            statuses=tuple(str(s) for s in (body.get("statuses") or ())),
            personal_class=(str(personal.get("class")) if personal.get("class") else None),
            role_tokens={str(k): str(v) for k, v in (personal.get("role_tokens") or {}).items()},
            scrub_text_columns=tuple(str(c) for c in (personal.get("scrub_text_columns") or ())),
        )

    backups = raw.get("backups") or {}
    outbox_cfg = raw.get("outbox") or {}
    scan_cfg = raw.get("redaction_scan") or {}
    policy = RetentionPolicy(
        path=target,
        version=_as_int(raw.get("version", 1), "version", str(target)),
        dry_run=dry_run,
        licence_floor_days=floor,
        classes=classes,
        tables=tables,
        backup_dir=str(backups.get("dir") or "data/backups"),
        backup_keep_daily=_as_int(backups.get("keep_daily", 14), "backups.keep_daily", str(target)),
        outbox_archive_after_days=_as_int(outbox_cfg.get("archive_after_days", 90), "outbox.archive_after_days", str(target)),
        redaction_lookback_hours=_as_int(scan_cfg.get("lookback_hours", 24), "redaction_scan.lookback_hours", str(target)),
    )
    problems = validate_policy(policy)
    if problems:
        raise RetentionPolicyError(
            f"retention policy {target} is unsafe and will not be used ({len(problems)} problem(s)): "
            + "; ".join(problems)
        )
    return policy


def validate_policy(policy: RetentionPolicy) -> list[str]:
    """Every reason this policy must not be executed. Empty list = safe to run.

    The checks exist because each one is a way a well-meaning YAML edit becomes a
    regulatory breach or a silent no-op:

    * an unknown action would otherwise be treated as *something*;
    * a delete rule on a ``licence_floor`` class deletes an operational record — refused at
      any age, because Condition 12.2 has no upper bound that makes deletion safe here;
    * a delete or archive rule with no ``days`` would compute a cutoff of "now" and take
      everything;
    * a delete rule under the floor deletes early;
    * a delete or archive rule with no ``timestamp_column`` has no age to measure;
    * a table pointing at a class that does not exist would silently fall through to keep,
      which is safe but hides a typo that Legal believes is in force.
    """
    problems: list[str] = []
    for rule in policy.classes.values():
        if rule.action not in _KNOWN_ACTIONS:
            problems.append(f"class {rule.name!r}: unknown action {rule.action!r} (known: {sorted(_KNOWN_ACTIONS)})")
        if rule.action in (DELETE, ARCHIVE_PAYLOAD) and rule.days is None:
            problems.append(f"class {rule.name!r}: action {rule.action!r} needs `days`")
        if rule.action == DELETE and rule.licence_floor:
            problems.append(
                f"class {rule.name!r}: refuses to delete a licence_floor class — CA licence Condition 12.2 "
                f"requires these operational records for at least {policy.licence_floor_days} days"
            )
        if rule.action == DELETE and rule.days is not None and rule.licence_floor and rule.days < policy.licence_floor_days:
            problems.append(f"class {rule.name!r}: days={rule.days} is below the licence floor of {policy.licence_floor_days}")
        if rule.action == ROTATE and (rule.keep is None or rule.keep < 1):
            problems.append(f"class {rule.name!r}: action 'rotate' needs keep >= 1")
    for entry in policy.tables.values():
        rule = policy.classes.get(entry.class_name)
        if rule is None:
            problems.append(f"table {entry.table!r}: class {entry.class_name!r} is not defined")
            continue
        if rule.action in (DELETE, ARCHIVE_PAYLOAD) and not entry.timestamp_column:
            problems.append(f"table {entry.table!r}: action {rule.action!r} needs a `timestamp_column`")
        if entry.personal_class and entry.personal_class not in policy.classes:
            problems.append(f"table {entry.table!r}: personal class {entry.personal_class!r} is not defined")
        if entry.has_personal_block and not entry.personal_class:
            problems.append(f"table {entry.table!r}: has a personal block but no personal class")
    return problems


# --------------------------------------------------------------------------- schema access

_MODELS_LOADED = False


def _ensure_models_loaded() -> None:
    """Import every model module once so ``Base.metadata`` is complete (db/models_all.py).

    Wrapped: a lane's half-finished model module must not stop housekeeping from importing.
    A table that fails to register is simply absent, and an absent table is *skipped*, which
    is the safe direction everywhere in this module.
    """
    global _MODELS_LOADED
    if _MODELS_LOADED:
        return
    try:
        import noc_agents.db.models_all  # noqa: F401  (side effect: registers the lane tables)
    except Exception:  # noqa: BLE001 — see the docstring
        log.exception("housekeeping: db.models_all could not be imported; some tables will read as absent")
    _MODELS_LOADED = True


def _metadata_table(name: str) -> Table | None:
    _ensure_models_loaded()
    return Base.metadata.tables.get(name)


def _live_tables(session: Session) -> set[str]:
    """Table names actually present in the file, so a metadata-only table is never queried."""
    try:
        return set(inspect(session.get_bind()).get_table_names())
    except Exception:  # noqa: BLE001 — an inspector failure must not be read as "delete away"
        log.exception("housekeeping: could not inspect the schema; treating every table as absent")
        return set()


def _pk_column(table: Table):
    cols = list(table.primary_key.columns)
    return cols[0] if len(cols) == 1 else None


def _scope(table: Table, operator_id: str) -> list[Any]:
    """Operator scoping as a WHERE fragment. Multi-tenancy is a controller boundary: a
    purge that crosses it deletes somebody else's records."""
    return [table.c.operator_id == operator_id] if "operator_id" in table.c else []


# ---------------------------------------------------------------------------------- reports


@dataclass
class TableOutcome:
    table: str
    action: str
    class_name: str
    matched: int = 0  # rows the rule selected
    changed: int = 0  # rows actually written (0 in dry run, by construction)
    skipped: str = ""  # why nothing happened: "not in this schema", "keep", ...
    cutoff: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "action": self.action,
            "class": self.class_name,
            "matched": self.matched,
            "changed": self.changed,
            "skipped": self.skipped or None,
            "cutoff": self.cutoff,
        }


@dataclass
class PurgeReport:
    applied: bool = False
    tables: list[TableOutcome] = field(default_factory=list)
    refused: str = ""  # a policy problem: nothing was even attempted

    @property
    def matched(self) -> int:
        return sum(t.matched for t in self.tables)

    @property
    def deleted(self) -> int:
        return sum(t.changed for t in self.tables)

    def summary(self) -> str:
        if self.refused:
            return f"purge REFUSED: {self.refused}"
        verb = "deleted" if self.applied else "would delete"
        return f"purge {verb} {self.deleted if self.applied else self.matched} row(s) across {len(self.tables)} rule(s)"

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "housekeeping.purge_expired",
            "ok": not self.refused,
            "applied": self.applied,
            "matched": self.matched,
            "deleted": self.deleted,
            "tables": [t.as_dict() for t in self.tables],
            "refused": self.refused or None,
        }


@dataclass
class PseudonymiseReport:
    applied: bool = False
    tables: list[TableOutcome] = field(default_factory=list)
    columns_changed: int = 0
    refused: str = ""

    @property
    def matched(self) -> int:
        return sum(t.matched for t in self.tables)

    @property
    def rows_changed(self) -> int:
        return sum(t.changed for t in self.tables)

    def summary(self) -> str:
        if self.refused:
            return f"pseudonymise REFUSED: {self.refused}"
        verb = "pseudonymised" if self.applied else "would pseudonymise"
        return f"{verb} {self.rows_changed} row(s) / {self.columns_changed} column value(s)"

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "housekeeping.pseudonymise_personal_fields",
            "ok": not self.refused,
            "applied": self.applied,
            "rows": self.rows_changed,
            "columns": self.columns_changed,
            "tables": [t.as_dict() for t in self.tables],
            "refused": self.refused or None,
        }


@dataclass
class OutboxSweepReport:
    applied: bool = False
    archivable: int = 0  # terminal rows past the cutoff still carrying a payload
    archived: int = 0
    failed_rows: int = 0  # FAILED rows summarised, never touched
    dead_rows: int = 0
    stuck_claimed: int = 0  # CLAIMED past the dispatcher's lease: REPORTED, never reclaimed
    by_kind: dict[str, int] = field(default_factory=dict)
    cutoff: str | None = None

    def summary(self) -> str:
        verb = "archived" if self.applied else "would archive"
        base = f"outbox {verb} {self.archived if self.applied else self.archivable} payload(s)"
        base += f"; failed={self.failed_rows} dead={self.dead_rows}"
        if self.stuck_claimed:
            base += f"; {self.stuck_claimed} row(s) CLAIMED past the lease — reported, NOT reclaimed"
        return base

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "housekeeping.sweep_outbox",
            "ok": True,
            "applied": self.applied,
            "archivable": self.archivable,
            "archived": self.archived,
            "failed": self.failed_rows,
            "dead": self.dead_rows,
            "stuck_claimed": self.stuck_claimed,
            "by_kind": dict(self.by_kind),
            "cutoff": self.cutoff,
            "note": "status is never written by housekeeping: releasing a lease is a re-dispatch",
        }


@dataclass
class RedactionHit:
    outbox_id: str
    kind: str
    incident_number: str | None
    sent_at: str | None
    email_matches: int
    phone_matches: int
    paths: tuple[str, ...]
    already_reported: bool = False

    def as_payload(self) -> dict[str, Any]:
        """The audit/WS payload. **Never the matched text** (§9.5): recording the MSISDN
        that leaked would make the breach record a second copy of the breach."""
        return {
            "outbox_id": self.outbox_id,
            "kind": self.kind,
            "incident_number": self.incident_number,
            "sent_at": self.sent_at,
            "email_matches": self.email_matches,
            "phone_matches": self.phone_matches,
            "paths": list(self.paths),
            "note": "pattern counts and JSON paths only; matched values are never recorded (§9.5)",
        }


@dataclass
class RedactionScanReport:
    scanned: int = 0
    hits: list[RedactionHit] = field(default_factory=list)
    new_audit_rows: int = 0
    lookback_hours: int = 24

    @property
    def misses(self) -> int:
        return len(self.hits)

    def summary(self) -> str:
        if not self.hits:
            return f"redaction scan: {self.scanned} SENT row(s) in {self.lookback_hours}h, no contact details found"
        return (
            f"redaction scan: {self.misses} MISS(ES) in {self.scanned} SENT row(s) over {self.lookback_hours}h "
            f"({self.new_audit_rows} new audit row(s))"
        )

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "housekeeping.post_send_redaction_scan",
            "ok": not self.hits,  # a miss is a finding, not a healthy result
            "scanned": self.scanned,
            "misses": self.misses,
            "new_audit_rows": self.new_audit_rows,
            "lookback_hours": self.lookback_hours,
            "hits": [h.as_payload() for h in self.hits],
        }


@dataclass
class BackupReport:
    path: str | None = None
    created: bool = False
    skipped: str = ""
    rotated: list[str] = field(default_factory=list)  # files removed (or that would be)
    rotate_applied: bool = False
    keep: int = 14
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"backup FAILED: {self.error}"
        if self.skipped:
            return f"backup skipped ({self.skipped})"
        head = f"backup written to {self.path}"
        if self.rotated:
            head += f"; {'rotated' if self.rotate_applied else 'would rotate'} {len(self.rotated)} old daily file(s)"
        return head

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "housekeeping.backup_db",
            "ok": not self.error,
            "created": self.created,
            "path": self.path,
            "skipped": self.skipped or None,
            "rotated": len(self.rotated),
            "rotate_applied": self.rotate_applied,
            "keep": self.keep,
            "error": self.error or None,
        }


@dataclass
class MemoryExpiryReport:
    """The §7.11.8 seam's result. ``available`` is False until Lane 4C ships the module."""

    available: bool = False
    called: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    note: str = ""

    def summary(self) -> str:
        if not self.available:
            return f"memory expiry: {self.note}"
        if not self.called:
            return f"memory expiry: seam wired but not called ({self.note})"
        return "memory expiry: " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "memory.expire_memory",
            "ok": True,
            "seam": MEMORY_EXPIRY_SEAM,
            "available": self.available,
            "called": self.called,
            "counts": dict(self.counts),
            "note": self.note,
        }


@dataclass
class HousekeepingReport:
    """Everything one run did, in the order it did it."""

    started_at: datetime
    applied: bool
    dry_run: bool
    policy_path: str
    backup: BackupReport = field(default_factory=BackupReport)
    purge: PurgeReport = field(default_factory=PurgeReport)
    pseudonymise: PseudonymiseReport = field(default_factory=PseudonymiseReport)
    outbox: OutboxSweepReport = field(default_factory=OutboxSweepReport)
    redaction: RedactionScanReport = field(default_factory=RedactionScanReport)
    memory: MemoryExpiryReport = field(default_factory=MemoryExpiryReport)
    freshness: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        posture = "APPLY" if self.applied else "dry-run"
        parts = [
            f"housekeeping ({posture})",
            self.backup.summary(),
            self.purge.summary(),
            self.pseudonymise.summary(),
            self.outbox.summary(),
            self.redaction.summary(),
            self.memory.summary(),
        ]
        if self.errors:
            parts.append(f"{len(self.errors)} duty/duties FAILED: " + " | ".join(self.errors))
        return "; ".join(parts)

    def as_audit_payload(self) -> dict[str, Any]:
        return {
            "ts": self.started_at.isoformat(),
            "posture": "apply" if self.applied else "dry_run",
            "dry_run": self.dry_run,
            "apply_env": apply_requested(),
            "policy_path": self.policy_path,
            "licence_floor_days": LICENCE_FLOOR_DAYS,
            "backup": self.backup.as_tool(),
            "purge": self.purge.as_tool(),
            "pseudonymise": self.pseudonymise.as_tool(),
            "outbox": self.outbox.as_tool(),
            "redaction_scan": {k: v for k, v in self.redaction.as_tool().items() if k != "hits"},
            "memory": self.memory.as_tool(),
            "freshness": self.freshness,
            "errors": list(self.errors),
        }

    def tools(self) -> tuple[dict[str, Any], ...]:
        return (
            self.backup.as_tool(),
            self.purge.as_tool(),
            self.pseudonymise.as_tool(),
            self.outbox.as_tool(),
            self.redaction.as_tool(),
            self.memory.as_tool(),
            {"name": "housekeeping.freshness_report", "ok": True, **self.freshness},
        )


# ----------------------------------------------------------------------------------- purge


def purge_expired(
    session: Session,
    settings: AppSettings,
    policy: RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
    apply: bool | None = None,
) -> PurgeReport:
    """Delete rows whose column class says delete, and nothing else (§9.4).

    The spec writes this as ``purge_expired(table, column_class)``; the loop over every
    delete rule is the caller-facing shape because "purge one table" is never what the job
    wants and makes it too easy to run a rule the policy did not authorise.

    Idempotent: the rule is ``timestamp < now - days``, so the second run of the day finds
    the same (now empty) set. Operator-scoped. Returns counts; deletes only when
    :func:`applying` (or an explicit ``apply=True``) says so.
    """
    policy = policy or load_policy()
    now = now or utcnow()
    do_apply = applying(policy) if apply is None else bool(apply)
    report = PurgeReport(applied=do_apply)

    problems = validate_policy(policy)
    if problems:
        # Belt and braces: load_policy() already raises on these. A caller that built a
        # policy by hand gets the same refusal, and the refusal is total — not "run the
        # rules that happen to be legal", because a policy nobody can trust authorises nothing.
        report.refused = "; ".join(problems)
        log.error("housekeeping: purge refused, the retention policy is unsafe: %s", report.refused)
        return report

    live = _live_tables(session)
    operator_id = settings.operator.operator_id
    for name, entry in sorted(policy.tables.items()):
        rule = policy.classes[entry.class_name]
        if rule.action != DELETE:
            continue  # keep / pseudonymise / archive / rotate / memory_lane / manual: not this duty
        outcome = TableOutcome(table=name, action=DELETE, class_name=rule.name)
        table = _metadata_table(name)
        if table is None or name not in live:
            # The lane that owns this table has not shipped. The rule is policy, not code:
            # it starts working the day the table appears, with no edit to the YAML.
            outcome.skipped = "not in this schema"
            report.tables.append(outcome)
            continue
        assert rule.days is not None  # validate_policy guarantees it
        cutoff = now - timedelta(days=rule.days)
        outcome.cutoff = cutoff.isoformat()
        ts_col = table.c.get(entry.timestamp_column or "")
        if ts_col is None:
            outcome.skipped = f"timestamp column {entry.timestamp_column!r} is not on this table"
            report.tables.append(outcome)
            continue
        where = [ts_col < cutoff, *_scope(table, operator_id)]
        if entry.statuses and "status" in table.c:
            where.append(table.c.status.in_(entry.statuses))
        outcome.matched = int(session.scalar(select(func.count()).select_from(table).where(*where)) or 0)
        if do_apply and outcome.matched:
            outcome.changed = int(session.execute(delete(table).where(*where)).rowcount or 0)
            log.info("housekeeping: purged %s row(s) from %s older than %s", outcome.changed, name, cutoff)
        report.tables.append(outcome)
    return report


# --------------------------------------------------------------------------- pseudonymise


class _RowFields(dict):
    """Format mapping for a role-token template: a field the row does not carry (or carries
    empty) renders ``UNKNOWN`` rather than raising, so one odd row cannot stop the pass."""

    def __missing__(self, key: str) -> str:
        return _UNKNOWN


def _row_fields(row: Mapping[str, Any]) -> _RowFields:
    out = _RowFields()
    for key, value in row.items():
        text = "" if value is None else str(value).strip()
        out[key] = text or _UNKNOWN
    return out


def role_token(template: str, row: Mapping[str, Any]) -> str:
    """Render one role token from the row's own surviving network facts.

    ``"FE-{region_code}"`` on a Machakos incident becomes ``"FE-MTK"``: the operational
    meaning ("which field-engineer lane was on this") survives, the person does not.
    """
    return template.format_map(_row_fields(row))


def pseudonymise_personal_fields(
    session: Session,
    settings: AppSettings,
    policy: RetentionPolicy | None = None,
    *,
    before: datetime | None = None,
    now: datetime | None = None,
    apply: bool | None = None,
) -> PseudonymiseReport:
    """Replace staff/vendor names with role tokens on rows older than the personal class's
    ``days``, leaving every network field exactly as it is (§9.4, §5.3.22 acceptance).

    ``before`` overrides the computed cutoff (the spec's ``pseudonymise_personal_fields(before)``).

    Idempotent by construction: the token a row would get is computed first and a column
    already equal to it is neither rewritten nor fed into the :class:`NameMap`, so a second
    pass has no names to match and the scrubbed free text comes back byte-identical.
    """
    policy = policy or load_policy()
    now = now or utcnow()
    do_apply = applying(policy) if apply is None else bool(apply)
    report = PseudonymiseReport(applied=do_apply)

    problems = validate_policy(policy)
    if problems:
        report.refused = "; ".join(problems)
        return report

    live = _live_tables(session)
    operator_id = settings.operator.operator_id
    for name, entry in sorted(policy.tables.items()):
        if not entry.has_personal_block:
            continue
        personal = policy.classes.get(entry.personal_class or "")
        if personal is None or personal.action != PSEUDONYMISE:
            continue
        outcome = TableOutcome(table=name, action=PSEUDONYMISE, class_name=personal.name)
        table = _metadata_table(name)
        if table is None or name not in live:
            outcome.skipped = "not in this schema"
            report.tables.append(outcome)
            continue
        pk = _pk_column(table)
        ts_col = table.c.get(entry.timestamp_column or "")
        if pk is None or ts_col is None:
            outcome.skipped = "needs a single-column primary key and a timestamp column"
            report.tables.append(outcome)
            continue
        cutoff = before or (now - timedelta(days=personal.days if personal.days is not None else 400))
        outcome.cutoff = cutoff.isoformat()
        where = [ts_col < cutoff, *_scope(table, operator_id)]
        rows = session.execute(select(table).where(*where)).mappings().all()
        for row in rows:
            changes, _ = _pseudonymise_row(row, entry, table)
            if not changes:
                continue
            outcome.matched += 1
            report.columns_changed += len(changes)
            if do_apply:
                session.execute(update(table).where(pk == row[pk.name]).values(**changes))
                # Same session, same transaction as the rewrite (see PSEUDONYMISED_ACTION).
                _mark_pseudonymised(
                    session,
                    operator_id=operator_id,
                    table=name,
                    row_id=row[pk.name],
                    columns=changes.keys(),
                    class_name=personal.name,
                    cutoff=cutoff,
                    now=now,
                )
                outcome.changed += 1
        report.tables.append(outcome)
    if do_apply and report.rows_changed:
        log.info("housekeeping: pseudonymised %s row(s)", report.rows_changed)
    return report


def _pseudonymise_row(
    row: Mapping[str, Any], entry: TableRule, table: Table
) -> tuple[dict[str, Any], NameMap]:
    """The change set for one row, or ``{}`` when it is already pseudonymised.

    Order matters. The name columns are resolved first so that the :class:`NameMap` used on
    the free text is seeded only from names that are *still names* — a column already
    holding its role token is skipped, which is exactly what stops a second pass from
    turning ``FE-MTK`` in a note into ``<PERSON_1>``.
    """
    changes: dict[str, Any] = {}
    names = NameMap()
    for column, template in entry.role_tokens.items():
        if column not in table.c:
            continue
        current = row.get(column)
        if current is None or not str(current).strip():
            continue  # nothing personal here
        token = role_token(template, row)
        if str(current) == token:
            continue  # already pseudonymised: do not re-register it as a name
        names.token_for(str(current))
        changes[column] = token
    for column in entry.scrub_text_columns:
        if column not in table.c:
            continue
        current = row.get(column)
        if current is None or not str(current).strip():
            continue
        # The SAME scrubber the pre-send path uses (llm/redaction.scrub_text): e-mails,
        # Kenyan MSISDNs and this row's own names. One implementation, one place to fix.
        scrubbed = scrub_text(str(current), names)
        if scrubbed is not None and scrubbed != str(current):
            changes[column] = scrubbed
    return changes, names


# ---------------------------------------------------------------------------- outbox sweep


def _is_archived(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get(ARCHIVED_KEY))


def _archive_summary(row: Mapping[str, Any], payload: Any, now: datetime) -> str:
    """What replaces an old payload: the delivery facts, never the prose or the addresses."""
    original = payload if isinstance(payload, dict) else {}
    return json.dumps(
        {
            ARCHIVED_KEY: True,
            "archived_at": now.isoformat(),
            "kind": row.get("kind"),
            "incident_number": original.get("incident_number"),
            "audience": original.get("audience"),
            "broadcast_ids": original.get("broadcast_ids") or [],
            "original_bytes": len(row.get("payload_json") or ""),
            "note": "payload removed by HousekeepingAgent under §9.4 outbox retention (90 days)",
        }
    )


def sweep_outbox(
    session: Session,
    settings: AppSettings,
    policy: RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
    apply: bool | None = None,
) -> OutboxSweepReport:
    """§5.3.22 ``sweep_outbox_failures``: archive terminal payloads, summarise the rest.

    **This function never writes ``outbox.status``.** ``orchestrator/outbox.py`` owns every
    status transition, and the only transition that looks like housekeeping — handing a
    stale CLAIMED row back to PENDING — is a re-dispatch: the dispatcher would transmit it
    again, and a row whose SMS already reached the customer would be sent twice. Stale
    claims are therefore counted and reported for a human; ``drain_once`` reclaims them
    under its own 120 s lease with ``attempts`` bounding the retries, which is where that
    decision belongs.
    """
    policy = policy or load_policy()
    now = now or utcnow()
    do_apply = applying(policy) if apply is None else bool(apply)
    report = OutboxSweepReport(applied=do_apply)

    table = _metadata_table("outbox")
    if table is None or "outbox" not in _live_tables(session):
        return report
    operator_id = settings.operator.operator_id
    scope = _scope(table, operator_id)
    cutoff = now - timedelta(days=policy.outbox_archive_after_days)
    report.cutoff = cutoff.isoformat()

    terminal = (outbox_mod.SENT, outbox_mod.DEAD)
    rows = session.execute(
        select(table).where(table.c.status.in_(terminal), table.c.updated_at < cutoff, *scope)
    ).mappings().all()
    pk = _pk_column(table)
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if _is_archived(payload):
            continue  # already swept: idempotent
        report.archivable += 1
        report.by_kind[str(row["kind"])] = report.by_kind.get(str(row["kind"]), 0) + 1
        if do_apply and pk is not None:
            archived = session.execute(
                update(table)
                .where(pk == row[pk.name])
                # Re-check what the SELECT saw, in the same statement: the outbox dispatcher
                # or an admin retry (POST /outbox/{id}/retry) may have moved the row back to
                # PENDING between the read and this write, and archiving a queued row would
                # leave it with nothing left to send. A row that moved is simply not archived.
                .where(table.c.status.in_(terminal), table.c.updated_at < cutoff)
                # payload_json is NOT NULL, so it becomes the summary rather than NULL;
                # envelope_json is nullable and simply goes. `status` is NOT in this
                # values() and must never be — see the docstring.
                .values(payload_json=_archive_summary(row, payload, now), envelope_json=None, updated_at=now)
            ).rowcount
            report.archived += int(archived or 0)

    report.failed_rows = int(
        session.scalar(select(func.count()).select_from(table).where(table.c.status == outbox_mod.FAILED, *scope)) or 0
    )
    report.dead_rows = int(
        session.scalar(select(func.count()).select_from(table).where(table.c.status == outbox_mod.DEAD, *scope)) or 0
    )
    # The dispatcher's own lease window (orchestrator.outbox.LEASE): reused, not re-declared,
    # so "stuck" here can never mean something different from "stuck" there.
    stale_before = now - outbox_mod.LEASE
    report.stuck_claimed = int(
        session.scalar(
            select(func.count())
            .select_from(table)
            .where(table.c.status == outbox_mod.CLAIMED, table.c.claimed_at < stale_before, *scope)
        )
        or 0
    )
    if report.stuck_claimed:
        log.warning(
            "housekeeping: %s outbox row(s) have been CLAIMED past the %ss lease; reporting only — "
            "reclaiming them here would re-dispatch them",
            report.stuck_claimed,
            int(outbox_mod.LEASE.total_seconds()),
        )
    return report


# -------------------------------------------------------------------------- redaction scan
#
# §9.6. The pre-send gate is validate_no_contacts, per row; this is the after-the-fact sweep
# that catches what the gate missed. It reuses the shared scrubber's OWN regexes
# (services/redaction re-exports llm/redaction) rather than writing a second pattern set: a
# scanner that disagrees with the scrubber reports misses that are not misses, and — far
# worse — misses the ones the scrubber's patterns already know about.
#
# There is no exclusion list for "addressing" fields, and that is deliberate. An outbox
# payload carries recipient REFERENCES, never addresses ("recipients_ref": "DEMO_EMAIL_TO",
# resolved by adapters/email_smtp.demo_recipients() at dispatch — services/notify.py:52), so
# a real e-mail address or MSISDN anywhere in the payload is by definition something that
# should not be there. Scanning everything therefore costs no false positives and catches a
# leak into a field nobody thought to list.


def _walk_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Every string in a nested JSON structure with its dotted path."""
    if isinstance(value, str):
        yield path or "$", value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")


def _scan_payload(payload: Any) -> tuple[int, int, tuple[str, ...]]:
    emails = phones = 0
    paths: list[str] = []
    for path, text in _walk_strings(payload):
        found_email = len(EMAIL_RE.findall(text))
        # An e-mail's local part can look like a phone number, so count phones on the text
        # with e-mails already removed. Without this a single address is reported as both.
        found_phone = len(PHONE_RE.findall(EMAIL_RE.sub("", text)))
        if found_email or found_phone:
            emails += found_email
            phones += found_phone
            paths.append(path)
    return emails, phones, tuple(paths)


def post_send_redaction_scan(
    session: Session,
    settings: AppSettings,
    policy: RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
) -> tuple[RedactionScanReport, list[RealtimeEvent]]:
    """Scan ``outbox.payload_json`` of rows SENT in the last 24 h (§9.6).

    A hit writes ``AuditRow(action="redaction.miss")`` and returns a ``security.redaction_miss``
    event for the caller to publish **after the commit** (the outbox drain's rule: what the
    UI hears is already durable). Neither the audit row nor the event carries the matched
    text — §9.5 forbids addresses and MSISDNs in an audit payload, and a breach record that
    quotes the breach is a second copy of it.

    Not gated by the dry-run posture: this duty only ever ADDS an alarm, and an operator who
    has not enabled deletion still wants to know that a contact detail left the building.
    Idempotent: a row that already has its ``redaction.miss`` row is counted, not re-raised.
    """
    policy = policy or load_policy()
    now = now or utcnow()
    report = RedactionScanReport(lookback_hours=policy.redaction_lookback_hours)
    events: list[RealtimeEvent] = []

    table = _metadata_table("outbox")
    if table is None or "outbox" not in _live_tables(session):
        return report, events
    operator_id = settings.operator.operator_id
    since = now - timedelta(hours=policy.redaction_lookback_hours)
    rows = session.execute(
        select(table).where(
            table.c.status == outbox_mod.SENT,
            func.coalesce(table.c.sent_at, table.c.updated_at) >= since,
            *_scope(table, operator_id),
        )
    ).mappings().all()

    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if _is_archived(payload):
            continue  # a swept row holds a summary, not the message that was sent
        report.scanned += 1
        emails, phones, paths = _scan_payload(payload)
        if not (emails or phones):
            continue
        sent_at = row.get("sent_at") or row.get("updated_at")
        hit = RedactionHit(
            outbox_id=str(row["id"]),
            kind=str(row["kind"]),
            incident_number=(payload.get("incident_number") if isinstance(payload, dict) else None),
            sent_at=sent_at.isoformat() if isinstance(sent_at, datetime) else None,
            email_matches=emails,
            phone_matches=phones,
            paths=paths,
        )
        existing = session.scalar(
            select(func.count())
            .select_from(AuditRow)
            .where(
                AuditRow.action == REDACTION_MISS_ACTION,
                AuditRow.entity_id == hit.outbox_id,
                AuditRow.operator_id == operator_id,
            )
        )
        hit.already_reported = bool(existing)
        report.hits.append(hit)
        if hit.already_reported:
            continue
        session.add(
            AuditRow(
                id=new_id(),
                ts=now,
                operator_id=operator_id,
                actor=ACTOR,
                action=REDACTION_MISS_ACTION,
                entity_type="outbox",
                entity_id=hit.outbox_id,
                rationale=(
                    f"Post-send redaction scan (§9.6) found {emails} e-mail and {phones} MSISDN pattern(s) in the "
                    f"payload of a SENT {hit.kind} row. Run the breach drill in docs/RUNBOOK.md."
                ),
                payload_json=json.dumps(hit.as_payload()),
            )
        )
        report.new_audit_rows += 1
        events.append(
            RealtimeEvent(
                type=REDACTION_MISS_EVENT,
                operator_id=operator_id,
                incident_id=row.get("incident_id"),
                payload=hit.as_payload(),
            )
        )
        log.error(
            "housekeeping: REDACTION MISS on outbox row %s (%s): %s e-mail / %s MSISDN pattern(s) at %s",
            hit.outbox_id, hit.kind, emails, phones, ", ".join(paths),
        )
    return report, events


# ---------------------------------------------------------------------------- daily backup


def _resolve_backup_dir(policy: RetentionPolicy, db_file: Path) -> Path:
    """``data/backups`` means "next to the database file", which is what db/migrate.py's
    ``default_backup_dir`` produces; anything else is used as written (absolute, or relative
    to the project root)."""
    configured = (policy.backup_dir or "").strip()
    if not configured or configured in ("data/backups", "data\\backups"):
        return db_file.parent / "backups"
    path = Path(configured)
    return path if path.is_absolute() else (ROOT / path)


def _sqlite_file(engine: Engine) -> Path | None:
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def backup_db(
    session: Session,
    policy: RetentionPolicy | None = None,
    *,
    today: date | None = None,
    now: datetime | None = None,
    apply: bool | None = None,
) -> BackupReport:
    """One consistent daily copy under ``data/backups/``, keeping 14 (§5.3.22, §9.4).

    Uses the **sqlite3 backup API**, exactly as ``db/migrate.py::_backup`` does, and for the
    same reason its comments give: the database runs with ``PRAGMA journal_mode=WAL``, so a
    plain ``shutil.copy`` of the ``.db`` file can capture a torn database — the committed
    pages it needs may still be in ``-wal`` and not in the file being copied. ``Connection.
    backup()`` reads a consistent snapshot while writers carry on.

    Named ``<stem>.daily.<YYYY-MM-DD>.db`` on the **EAT** date, because 03:00 EAT is 00:00
    UTC and a UTC-named file would carry yesterday's date for the operator reading it. A
    file that already exists for today is left alone, which is what makes the duty
    idempotent; it is never overwritten.

    Rotation obeys the dry-run posture (deleting a file is still deleting) and matches only
    this job's own ``*.daily.*.db`` files: ``db/migrate.py``'s pre-migration backups are the
    rollback path for a damaged database and are never rotated away by age.
    """
    policy = policy or load_policy()
    do_apply = applying(policy) if apply is None else bool(apply)
    report = BackupReport(keep=policy.backup_keep_daily, rotate_applied=do_apply)

    engine = session.get_bind()
    db_file = _sqlite_file(engine) if isinstance(engine, Engine) else None
    if db_file is None:
        report.skipped = "not a file-backed SQLite database"
        return report

    backup_dir = _resolve_backup_dir(policy, db_file)
    stamp = (today or eat_date(now or utcnow())).isoformat()
    target = backup_dir / f"{db_file.stem}.daily.{stamp}.db"
    if target.exists():
        report.path = str(target)
        report.skipped = "a daily backup already exists for this EAT date"
    else:
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            raw = engine.raw_connection()
            try:
                source: sqlite3.Connection = raw.driver_connection  # the pooled sqlite3 connection
                dest = sqlite3.connect(str(target))
                try:
                    source.backup(dest)
                finally:
                    dest.close()
            finally:
                raw.close()
            report.path = str(target)
            report.created = True
        except Exception as exc:  # noqa: BLE001 — fail-soft: a failed backup must not stop the run
            report.error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("housekeeping: the daily backup could not be written to %s", target)
            return report

    # Rotation: newest `keep_daily` by name (ISO dates sort lexicographically) survive.
    try:
        dailies = sorted(backup_dir.glob(f"{db_file.stem}.daily.*.db"))
    except OSError:
        dailies = []
    surplus = dailies[: max(0, len(dailies) - policy.backup_keep_daily)]
    for old in surplus:
        report.rotated.append(str(old))
        if do_apply:
            try:
                old.unlink()
            except OSError:
                log.warning("housekeeping: could not rotate old backup %s", old)
    return report


# ------------------------------------------------------------------------ memory seam (4C)


def expire_memory(
    session: Session,
    settings: AppSettings,
    policy: RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
    apply: bool | None = None,
) -> MemoryExpiryReport:
    """**Seam only.** Calls the memory lane's ``expire_memory()`` if it exists (§7.11.8).

    §9.4 gives housekeeping the *call*, not the SQL: the person-scoped hard delete, the
    bi-temporal window closing, the memo expiry and the 24-month episode prune all live in
    ``memory/consolidate.py`` (:data:`MEMORY_EXPIRY_SEAM`) because they need the
    ``memory_*`` tables and the bi-temporal rules that lane owns. Those tables are not in
    this schema yet, so until Lane 4C lands this reports "seam not wired" and does nothing.

    Two properties this seam must keep when the lane arrives:

    * it is called **regardless of ``MEMORY_ENABLED``** — retention must never depend on a
      read flag (§9.4), and a lane switched off for reading still holds personal data;
    * it is **not called in dry run**, because the lane's own signature has no dry-run mode
      and its person-scoped branch is a hard delete.
    """
    policy = policy or load_policy()
    do_apply = applying(policy) if apply is None else bool(apply)
    module_path, _, func_name = MEMORY_EXPIRY_SEAM.partition(":")
    try:
        module = __import__(module_path, fromlist=[func_name])
        lane_expire = getattr(module, func_name)
    except (ImportError, AttributeError):
        return MemoryExpiryReport(
            available=False,
            note=f"seam not wired: {MEMORY_EXPIRY_SEAM} does not exist yet (Lane 4C owns it, §7.11.8)",
        )
    if not do_apply:
        return MemoryExpiryReport(
            available=True,
            called=False,
            note="dry-run posture: expire_memory() hard-deletes person-scoped facts and has no dry-run mode",
        )
    counts = lane_expire(session, settings=settings, now=now or utcnow())
    return MemoryExpiryReport(
        available=True,
        called=True,
        counts={str(k): int(v) for k, v in dict(counts or {}).items()},
        note="called regardless of MEMORY_ENABLED (§9.4): retention never depends on a read flag",
    )


# ------------------------------------------------------------------------- freshness report


def freshness_report(session: Session, settings: AppSettings, *, now: datetime | None = None) -> dict[str, Any]:
    """``/metrics/summary.freshness`` (§5.3.22): how old is what this system relies on.

    Pure read, operator-scoped, no network. Three questions an operator actually asks at
    03:00: is the outside-world cache current, is the outbox backing up, and when did the
    last housekeeping run and backup happen.
    """
    now = now or utcnow()
    operator_id = settings.operator.operator_id
    out: dict[str, Any] = {"as_of": now.isoformat()}

    signals: list[dict[str, Any]] = []
    try:
        rows = session.execute(
            select(
                ExternalSignalRow.source,
                func.max(ExternalSignalRow.fetched_at),
                func.count(),
            )
            .where(ExternalSignalRow.operator_id == operator_id)
            .group_by(ExternalSignalRow.source)
        ).all()
        for source, fetched_at, count in rows:
            signals.append(
                {
                    "source": source,
                    "rows": int(count or 0),
                    "last_fetched_at": fetched_at.isoformat() if isinstance(fetched_at, datetime) else None,
                    "age_minutes": (
                        round((now - fetched_at).total_seconds() / 60.0, 1) if isinstance(fetched_at, datetime) else None
                    ),
                }
            )
    except Exception:  # noqa: BLE001 — a freshness report must never be the thing that fails a run
        log.exception("housekeeping: external signal freshness could not be read")
    out["signals"] = sorted(signals, key=lambda s: str(s["source"]))

    table = _metadata_table("outbox")
    if table is not None and "outbox" in _live_tables(session):
        counts: dict[str, int] = {}
        for status, count in session.execute(
            select(table.c.status, func.count()).where(*_scope(table, operator_id)).group_by(table.c.status)
        ).all():
            counts[str(status)] = int(count or 0)
        oldest = session.scalar(
            select(func.min(table.c.created_at)).where(
                table.c.status.in_((outbox_mod.PENDING, outbox_mod.HELD)), *_scope(table, operator_id)
            )
        )
        out["outbox"] = {
            "by_status": counts,
            "oldest_undelivered_at": oldest.isoformat() if isinstance(oldest, datetime) else None,
            "oldest_undelivered_age_minutes": (
                round((now - oldest).total_seconds() / 60.0, 1) if isinstance(oldest, datetime) else None
            ),
        }

    state = session.get(ScheduledJobStateRow, JOB_NAME)
    out["housekeeping"] = {
        "last_started_at": state.last_started_at.isoformat() if state is not None and state.last_started_at else None,
        "last_status": state.last_status if state is not None else None,
        "enabled": housekeeping_enabled(),
    }
    return out


# ---------------------------------------------------------------------------------- the job


def _audit(session: Session, settings: AppSettings, report: HousekeepingReport) -> None:
    """One ``audit_events(action="retention.purge")`` row per run (§5.3.22 outputs).

    Written even for a dry run, and even for a run that deleted nothing: the question an
    auditor asks is "what did retention do on the night of the 14th", and "nothing, in
    dry-run posture, here are the counts it would have deleted" is an answer. A missing row
    is not.
    """
    session.add(
        AuditRow(
            id=new_id(),
            ts=report.started_at,
            operator_id=settings.operator.operator_id,
            actor=ACTOR,
            action=AUDIT_ACTION,
            entity_type="housekeeping",
            entity_id=eat_date(report.started_at).isoformat(),
            rationale=report.summary()[:4000],
            payload_json=json.dumps(report.as_audit_payload(), default=str),
        )
    )


def run(session: Session, settings: AppSettings | None = None, *, now: datetime | None = None) -> JobResult:
    """The ``housekeeping`` job (§5.3.22). A0, fail-soft, daily.

    Duties run in a fixed order and each one is wrapped: a duty that raises is recorded and
    the rest still run (fail-soft), and the job then raises :class:`HousekeepingError` at the
    end so the run is FAILED and visible. Work already committed survives that raise — the
    scheduler's ``_record_failure`` is built for a job that commits before raising.

    The backup is first on purpose: today's copy must predate today's purge.
    """
    settings = settings or get_settings()
    now = now or utcnow()
    if not housekeeping_enabled():
        # Belt and braces with the card's default_enabled=False, exactly as pollers.weather
        # re-checks WEATHER_ENABLED: wiring the card must not be able to start deleting.
        return JobResult(
            summary=f"housekeeping skipped: {ENABLED_ENV} is not true",
            rationale="§5.3.22 ships this agent OFF; with the flag unset the system behaves exactly as it did before it existed",
            tools=({"name": "housekeeping.run", "ok": True, "skipped": "disabled"},),
        )

    errors: list[str] = []
    try:
        policy = load_policy()
    except RetentionPolicyError as exc:
        # No policy means no deletion, but the backup and the redaction scan are still worth
        # running — they add data and alarms, they never remove anything. So carry on with a
        # policy-free subset and fail the run at the end.
        log.error("housekeeping: %s", exc)
        return _run_without_policy(session, settings, now, str(exc))

    report = HousekeepingReport(
        started_at=now,
        applied=applying(policy),
        dry_run=policy.dry_run,
        policy_path=str(policy.path),
    )
    events: list[RealtimeEvent] = []
    session.commit()  # start clean: the backup below reads the file, not this session

    def duty(name: str, fn: Callable[[], None]) -> None:
        """One duty = one short transaction (§4.5 contention budget).

        Committing per duty is what makes "fail-soft" true: a later duty that raises rolls
        back only its own work. Sharing one transaction across all of them would mean the
        rollback after a failed redaction scan silently undid the purge that succeeded
        before it — the run would report deletions that never happened.
        """
        try:
            fn()
            session.commit()
        except Exception as exc:  # noqa: BLE001 — fail-soft per duty; the run still reports FAILED
            message = f"{name}: {type(exc).__name__}: {exc}"[:500]
            errors.append(message)
            log.exception("housekeeping: duty %s failed", name)
            session.rollback()

    duty("backup_db", lambda: setattr(report, "backup", backup_db(session, policy, now=now)))
    duty("purge_expired", lambda: setattr(report, "purge", purge_expired(session, settings, policy, now=now)))
    duty(
        "pseudonymise_personal_fields",
        lambda: setattr(report, "pseudonymise", pseudonymise_personal_fields(session, settings, policy, now=now)),
    )
    duty("sweep_outbox", lambda: setattr(report, "outbox", sweep_outbox(session, settings, policy, now=now)))

    def _scan() -> None:
        scan_report, scan_events = post_send_redaction_scan(session, settings, policy, now=now)
        report.redaction = scan_report
        events.extend(scan_events)

    duty("post_send_redaction_scan", _scan)
    duty("expire_memory", lambda: setattr(report, "memory", expire_memory(session, settings, policy, now=now)))
    duty("freshness_report", lambda: report.freshness.update(freshness_report(session, settings, now=now)))

    report.errors = errors
    _audit(session, settings, report)
    session.commit()
    for event in events:  # after the commit: what the UI hears is already durable
        hub.publish_sync(event)

    summary = report.summary()
    rationale = (
        f"Retention by column class from {report.policy_path} (§9.4); licence floor "
        f"{LICENCE_FLOOR_DAYS} days (CA licence Condition 12.2) enforced in code. Posture: "
        f"{'APPLY' if report.applied else 'dry run'} (dry_run={policy.dry_run}, {APPLY_ENV}={apply_requested()}). "
        "Backup taken before any mutation; outbox statuses never written."
    )
    if errors:
        raise HousekeepingError(f"{summary} :: {'; '.join(errors)}")
    return JobResult(summary=summary, rationale=rationale, tools=report.tools())


def _run_without_policy(session: Session, settings: AppSettings, now: datetime, problem: str) -> NoReturn:
    """The retention policy is unusable. Delete nothing; still scan for leaks.

    The scan is the one duty that only ever raises an alarm, so a broken YAML must not also
    blind the operator to a redaction miss. Everything that could remove data is skipped and
    the run is FAILED so somebody fixes the file.
    """
    events: list[RealtimeEvent] = []
    scanned = RedactionScanReport()
    try:
        scanned, events = post_send_redaction_scan(session, settings, _SCAN_ONLY_POLICY, now=now)
    except Exception:  # noqa: BLE001
        log.exception("housekeeping: the redaction scan also failed while the policy was unusable")
    session.add(
        AuditRow(
            id=new_id(),
            ts=now,
            operator_id=settings.operator.operator_id,
            actor=ACTOR,
            action=AUDIT_ACTION,
            entity_type="housekeeping",
            entity_id=eat_date(now).isoformat(),
            rationale=f"retention policy unusable; NOTHING was deleted or pseudonymised: {problem}"[:4000],
            payload_json=json.dumps({"refused": problem, "redaction_scan": scanned.as_tool()}, default=str),
        )
    )
    session.commit()
    for event in events:
        hub.publish_sync(event)
    raise RetentionPolicyError(f"housekeeping ran no retention duties: {problem}")


#: A minimal, keep-everything policy used only when the real one cannot be loaded, so the
#: redaction scan still has its lookback window. It classifies nothing, so it can delete nothing.
_SCAN_ONLY_POLICY = RetentionPolicy(
    path=Path("<fallback>"),
    version=0,
    dry_run=True,
    licence_floor_days=LICENCE_FLOOR_DAYS,
    classes={},
    tables={},
    backup_dir="data/backups",
    backup_keep_daily=14,
    outbox_archive_after_days=90,
    redaction_lookback_hours=24,
)


#: The scheduler card (§4.4 roster, §5.3.22). **Not** wired into
#: ``scheduler.loop.SCHEDULED_JOBS`` here — that file belongs to another wave, and adding
#: ``services.housekeeping.HOUSEKEEPING_JOB`` to the tuple is the whole change.
#: ``default_enabled=False`` so an unset ``HOUSEKEEPING_ENABLED`` reads as off in
#: ``/scheduler/status`` too, and :func:`run` re-checks the flag itself, so wiring it can
#: never start deleting on a machine that did not opt in.
HOUSEKEEPING_JOB = JobCard(
    JOB_NAME, INTERVAL_S, run, ENABLED_ENV, AGENT, GRAPH_NAME,
    max_seconds=MAX_SECONDS,
    default_enabled=False,
)
