"""Vendors: seeding from ``cfg.msp_contacts``, resolving ``incidents.vendor_id``, and the
versioned ``sla_terms`` loader (spec §7.6.1) -- Phase 4 Lane 4A, step 1.

Everything here is behind ``SCORECARDS_ENABLED`` (Appendix B; default OFF). The flag is
read at call time like every other flag in this codebase, and the routes answer 404 while
it is off, so the API surface is indistinguishable from the one before this lane existed.

Three jobs, one module, because they are the same fact seen three ways:

1. **Seeding.** ``vendors`` rows are a pure function of the operator profile's
   ``msp_contacts`` block. ``seed_vendors_from_contacts`` is insert-only and keyed on the
   §7.6.1 natural key ``(operator_id, code, active_from)`` -- the same key ``noc-seed-v2``
   matches on -- so it may be called any number of times, from any path, and never
   overwrites a value a human edited or re-adds a row a human retired with ``active_to``.
2. **Resolution.** ``incidents.msp_name`` is whatever string the assignment pool held
   ("EGYPRO" for safaricom, "Camusat" for airtel). ``resolve_vendor`` normalises it to a
   code and finds the row whose active window contains the incident's failure time, so a
   vendor re-contracted mid-year attributes each incident to the right terms.
3. **Terms.** ``load_sla_terms`` reads ``config/sla_terms.yaml`` (``SLA_TERMS_PATH``
   overrides) and merges it over ``cfg.sla_minutes``: a band the file omits falls back to
   the profile and says so (``source="sla_minutes"``), and every value carries the
   ``yaml_path`` a scorecard line cites. The file's ``version`` is mandatory -- a card
   computed against unversioned terms cannot be reproduced later, which is the whole
   reason the terms are versioned.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.config import CONFIG_DIR, OperatorConfig
from noc_agents.db.models import IncidentRow, utcnow
from noc_agents.db.models_vendors import VENDOR_TYPES, VendorRow

# --------------------------------------------------------------------------- flag

#: One flag for the whole of §7.6 (Appendix B lists no finer one). Stop clocks are the
#: input to scorecards, so they switch on together; a shadow shift runs with this on and
#: SCORECARDS_SHADOW on, which is the scorecard job's concern, not this module's.
FLAG = "SCORECARDS_ENABLED"
_TRUE = {"1", "true", "yes", "on"}


def lane_enabled() -> bool:
    """``SCORECARDS_ENABLED`` -- default **false**; anything but an explicit truthy value is off."""
    return (os.getenv(FLAG) or "").strip().lower() in _TRUE


# ------------------------------------------------------------------- vendor rows

#: ``active_from`` for rows seeded from ``msp_contacts``. A PLACEHOLDER, not a commencement
#: date: the profile carries no dates, and the §7.6.1 UNIQUE needs a value. It equals
#: ``placeholder_active_from`` in data/seed/v2/vendors.yaml on purpose, so a row seeded
#: here and the same vendor seeded by noc-seed-v2 share a natural key and de-duplicate.
#: When Supply Chain supplies real dates they go on the profile entry as ``active_from``
#: (see ``seed_vendors_from_contacts``), and a re-contracted vendor becomes a NEW row.
VENDOR_SEED_ACTIVE_FROM = date(2026, 1, 1)

#: Values ``msp_name`` takes that are not vendors at all (see services/assignment.py).
_NOT_A_VENDOR = frozenset({"", "FIELD_ENGINEER", "NOC", "NOC-QUEUE", "UNASSIGNED"})

_CODE_SEP = re.compile(r"[^A-Z0-9]+")
_VENDOR_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://noc-agents.local/vendors")


def normalise_code(name: str | None) -> str:
    """``"Egypro Fibre"`` / ``"egypro-fibre"`` / ``"EGYPRO_FIBRE"`` -> ``"EGYPRO_FIBRE"``.

    The airtel profile spells its ``msp_contacts`` keys in title case ("Camusat") and its
    incidents carry that spelling in ``msp_name``; the safaricom profile is upper-case
    throughout. One normalisation on both the seeding and the resolving side means the
    two can never disagree about which string is which vendor.
    """
    return _CODE_SEP.sub("_", (name or "").strip().upper()).strip("_")


def display_name_for(code: str, key: str | None = None) -> str:
    """A human label for a code when the profile does not give one.

    Keeps the profile's own spelling when it already looks like a name ("Camusat"), else
    title-cases the code's words -- except words of three letters or fewer, which are
    read as acronyms ("ATC" stays "ATC"; "ECTA" becomes "Ecta", as the seed set spells it).
    """
    if key and any(ch.islower() for ch in key):
        return key.strip()
    words = [w for w in code.split("_") if w]
    return " ".join(w if len(w) <= 3 else w.capitalize() for w in words)


def vendor_id_for(operator_id: str, code: str, active_from: date) -> str:
    """Deterministic id for a seeded row, so a rebuilt database re-issues the same
    ``vendor_id`` values and the ones already stamped on incidents stay valid."""
    return str(uuid.uuid5(_VENDOR_NS, f"{operator_id}|{code}|{active_from.isoformat()}"))


def _as_date(value: Any, default: date | None) -> date | None:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def seed_vendors_from_contacts(session: Session, cfg: OperatorConfig) -> list[VendorRow]:
    """Insert a ``vendors`` row for every ``cfg.msp_contacts`` entry that has none yet.

    Insert-only and idempotent on the natural key; returns the rows it inserted (empty on
    every call after the first). Flushes, never commits -- the caller owns the transaction.

    The profile entry carries ``email`` / ``sms`` / ``domains`` today; those become
    ``contacts_json`` verbatim. Optional keys the profile MAY grow, read here so the row
    and the profile agree without a code change: ``type`` (default ``MSP`` -- the block is
    called msp_contacts; the seed set's TOWERCO/OEM classifications of ATC and HUAWEI_RADIO
    are flagged REVIEW there and belong on the profile once a human confirms them),
    ``display_name``, ``contract_ref``, ``active_from`` (ISO date), ``active_to``.
    """
    existing = {
        (row.code, row.active_from)
        for row in session.scalars(select(VendorRow).where(VendorRow.operator_id == cfg.operator_id))
    }
    inserted: list[VendorRow] = []
    for key, entry in (cfg.msp_contacts or {}).items():
        code = normalise_code(key)
        if not code:
            continue
        entry = dict(entry or {})
        vendor_type = str(entry.pop("type", None) or "MSP").upper()
        if vendor_type not in VENDOR_TYPES:
            raise ValueError(f"msp_contacts.{key}.type={vendor_type!r} is not one of {list(VENDOR_TYPES)}")
        active_from = _as_date(entry.pop("active_from", None), VENDOR_SEED_ACTIVE_FROM) or VENDOR_SEED_ACTIVE_FROM
        active_to = _as_date(entry.pop("active_to", None), None)
        display_name = str(entry.pop("display_name", None) or display_name_for(code, key))
        contract_ref = entry.pop("contract_ref", None)
        if (code, active_from) in existing:
            continue
        row = VendorRow(
            id=vendor_id_for(cfg.operator_id, code, active_from),
            operator_id=cfg.operator_id,
            code=code,
            display_name=display_name,
            type=vendor_type,
            contract_ref=str(contract_ref) if contract_ref else None,
            active_from=active_from,
            active_to=active_to,
        )
        row.contacts = entry  # whatever is left: email, sms, domains, ... verbatim
        session.add(row)
        existing.add((code, active_from))
        inserted.append(row)
    if inserted:
        session.flush()
    return inserted


def create_vendor(
    session: Session,
    *,
    operator_id: str,
    code: str,
    display_name: str | None,
    type: str,
    active_from: date,
    active_to: date | None = None,
    contract_ref: str | None = None,
    contacts: dict | None = None,
) -> VendorRow:
    """An admin-created vendor (``POST /api/v1/vendors``). Validates; flushes; never commits.

    Raises ``ValueError`` on a bad type, an empty code, or an inverted window, and
    ``LookupError`` when the natural key is taken -- the route turns those into 400 and 409.
    """
    norm = normalise_code(code)
    if not norm:
        raise ValueError("code is required")
    vendor_type = (type or "").strip().upper()
    if vendor_type not in VENDOR_TYPES:
        raise ValueError(f"type must be one of {list(VENDOR_TYPES)}")
    if active_to is not None and active_to < active_from:
        raise ValueError("active_to is before active_from")
    dup = session.scalar(
        select(VendorRow).where(
            VendorRow.operator_id == operator_id,
            VendorRow.code == norm,
            VendorRow.active_from == active_from,
        )
    )
    if dup is not None:
        raise LookupError(f"vendor {norm} active from {active_from.isoformat()} already exists")
    row = VendorRow(
        id=vendor_id_for(operator_id, norm, active_from),
        operator_id=operator_id,
        code=norm,
        display_name=(display_name or "").strip() or display_name_for(norm),
        type=vendor_type,
        contract_ref=(contract_ref or "").strip() or None,
        active_from=active_from,
        active_to=active_to,
    )
    row.contacts = dict(contacts or {})
    session.add(row)
    session.flush()
    return row


def list_vendors(session: Session, operator_id: str, *, as_of: date | None = None) -> list[VendorRow]:
    """This operator's vendors, code then newest terms first. ``as_of`` keeps only rows
    active on that day; ``None`` returns every row including retired ones."""
    rows = session.scalars(
        select(VendorRow)
        .where(VendorRow.operator_id == operator_id)
        .order_by(VendorRow.code, VendorRow.active_from.desc())
    ).all()
    if as_of is None:
        return list(rows)
    return [r for r in rows if r.is_active_on(as_of)]


def resolve_vendor(
    session: Session,
    *,
    operator_id: str,
    msp_name: str | None,
    as_of: datetime | date | None = None,
) -> VendorRow | None:
    """The ``vendors`` row ``msp_name`` denotes for this operator, or ``None``.

    Pure read. ``None`` for the strings that are not vendors (FIELD_ENGINEER, the NOC
    queue), for an unknown code, and for a code whose every row is outside its active
    window on ``as_of`` (default: today). With several rows for one code -- a vendor
    re-contracted on new terms -- the one whose window contains ``as_of`` wins; among
    overlapping windows the most recent ``active_from`` wins.

    This is the seam the scorecard lane calls per incident, so it must never write.
    """
    code = normalise_code(msp_name)
    if code in _NOT_A_VENDOR:
        return None
    day = as_of.date() if isinstance(as_of, datetime) else (as_of or utcnow().date())
    rows = session.scalars(
        select(VendorRow)
        .where(VendorRow.operator_id == operator_id, VendorRow.code == code)
        .order_by(VendorRow.active_from.desc())
    ).all()
    for row in rows:
        if row.is_active_on(day):
            return row
    return None


def incident_vendor_name(inc: IncidentRow) -> str | None:
    """The string on the incident that names its vendor: ``msp_name``, else the floor
    ticket's ``responsible_msp`` (the two are usually equal; the second is a fallback for
    a ticket typed by hand)."""
    return inc.msp_name or getattr(inc, "responsible_msp", None)


def incident_as_of(inc: IncidentRow) -> datetime:
    """The instant that decides which vendor terms an incident falls under: the outage
    start, not the moment someone typed the ticket."""
    return inc.failure_time or inc.outage_start_at or inc.created_at or utcnow()


def attach_vendor(session: Session, inc: IncidentRow, cfg: OperatorConfig) -> VendorRow | None:
    """Make ``inc.vendor_id`` mean something: set it from ``msp_name`` if it is still NULL.

    Seeds the vendor rows first (insert-only, so this is safe on every call), then
    resolves. Returns the row the incident now points at (or already pointed at), or
    ``None`` when the incident has no vendor. Flushes; the caller commits.
    """
    if inc.vendor_id:
        return session.get(VendorRow, inc.vendor_id)
    seed_vendors_from_contacts(session, cfg)
    row = resolve_vendor(
        session, operator_id=inc.operator_id, msp_name=incident_vendor_name(inc), as_of=incident_as_of(inc)
    )
    if row is not None:
        inc.vendor_id = row.id
        session.flush()
    return row


def backfill_incident_vendor_ids(session: Session, operator_id: str) -> int:
    """Stamp ``vendor_id`` on every incident of this operator that has a resolvable
    ``msp_name`` and no ``vendor_id`` yet. Returns how many were stamped. Idempotent.

    Only NULLs are touched: an incident already pointing at a vendor row -- possibly an
    older row for the same code, chosen because of its failure time -- is never re-pointed.
    Flushes; the caller commits.
    """
    rows = session.scalars(
        select(IncidentRow).where(IncidentRow.operator_id == operator_id, IncidentRow.vendor_id.is_(None))
    ).all()
    stamped = 0
    for inc in rows:
        name = incident_vendor_name(inc)
        if not name:
            continue
        vendor = resolve_vendor(session, operator_id=operator_id, msp_name=name, as_of=incident_as_of(inc))
        if vendor is None:
            continue
        inc.vendor_id = vendor.id
        stamped += 1
    if stamped:
        session.flush()
    return stamped


def vendor_out(row: VendorRow) -> dict:
    """Wire shape of one vendor (dates as ISO strings; no timestamps to Z-stamp)."""
    return {
        "id": row.id,
        "operator_id": row.operator_id,
        "code": row.code,
        "display_name": row.display_name,
        "type": row.type,
        "contract_ref": row.contract_ref,
        "active_from": row.active_from.isoformat(),
        "active_to": row.active_to.isoformat() if row.active_to else None,
        "contacts": row.contacts,
    }


# ------------------------------------------------------------------- sla_terms

DEFAULT_SLA_TERMS_PATH = CONFIG_DIR / "sla_terms.yaml"
SLA_TERMS_PATH_ENV = "SLA_TERMS_PATH"

PRIORITIES: tuple[str, ...] = ("P1", "P2", "P3", "P4")
#: The band keys. DECISION (see the header of config/sla_terms.yaml): the spec's YAML
#: block spelling, identical to ``config.SlaBand``'s fields, so a merge over
#: ``cfg.sla_minutes`` is key-for-key. ``ack_minutes`` (the other spelling the spec's DDL
#: comment shows) is not a key and ``yaml_path`` never emits it.
BAND_KEYS: tuple[str, ...] = ("ack", "restore", "note_interval")
#: Where a band value came from -- cited on the card so "defaults, not contract" is honest.
SOURCE_YAML = "yaml"
SOURCE_SLA_MINUTES = "sla_minutes"


@dataclass(frozen=True)
class SlaBandTerms:
    """One priority's minutes, with provenance: which file/profile and which YAML path."""

    priority: str
    ack: int
    restore: int
    note_interval: int
    source: str  # SOURCE_YAML | SOURCE_SLA_MINUTES
    yaml_path: str  # "sla_terms.default.P1" or "sla_terms.vendors.EGYPRO.P1"

    def path(self, key: str) -> str:
        """``yaml_path`` of one value: ``bands.path("ack") == "sla_terms.default.P1.ack"``."""
        if key not in BAND_KEYS:
            raise KeyError(f"{key!r} is not a band key; expected one of {list(BAND_KEYS)}")
        return f"{self.yaml_path}.{key}"

    def minutes(self, key: str) -> int:
        if key not in BAND_KEYS:
            raise KeyError(f"{key!r} is not a band key; expected one of {list(BAND_KEYS)}")
        return int(getattr(self, key))


@dataclass(frozen=True)
class VendorTerms:
    """One vendor's block: credit shape plus any band overrides."""

    code: str
    contract_ref: str | None
    contract_is_synthetic: bool
    credit_shape: str  # none | per_occurrence | escalating_consecutive
    credit_pct: tuple[float, ...]
    bands: dict[str, SlaBandTerms] = field(default_factory=dict)  # only the priorities it overrides
    note: str = ""

    @property
    def has_contract(self) -> bool:
        """A real contract reference: not NULL and not one of the synthetic samples."""
        return bool(self.contract_ref) and not self.contract_is_synthetic


@dataclass(frozen=True)
class SlaTerms:
    """The loaded, merged terms. Immutable; build one per scorecard run and stamp ``version``."""

    version: str
    path: Path
    source: str | None
    default_bands: dict[str, SlaBandTerms]
    availability_target_pct: float | None
    default_credit_shape: str
    vendors: dict[str, VendorTerms]
    scorecards: dict[str, Any]
    regulatory: dict[str, Any]
    raw: dict[str, Any]

    def vendor(self, code: str | None) -> VendorTerms | None:
        return self.vendors.get(normalise_code(code)) if code else None

    def bands_for(self, vendor_code: str | None, priority: str) -> SlaBandTerms:
        """The band that applies: the vendor's own override if it has one, else default.
        ``.yaml_path`` on the result says which, so the card cites the real source."""
        if priority not in PRIORITIES:
            raise KeyError(f"unknown priority {priority!r}")
        vendor = self.vendor(vendor_code)
        if vendor is not None and priority in vendor.bands:
            return vendor.bands[priority]
        return self.default_bands[priority]

    def credit_shape_for(self, vendor_code: str | None) -> str:
        vendor = self.vendor(vendor_code)
        return vendor.credit_shape if vendor is not None else self.default_credit_shape

    def resolve_path(self, yaml_path: str) -> Any:
        """Walk ``raw`` by a dotted ``yaml_path``; ``KeyError`` when it points at nothing.
        Exists so a test (and a reviewer) can prove every emitted path is a real key."""
        node: Any = self.raw
        for part in yaml_path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(yaml_path)
            node = node[part]
        return node


def sla_terms_path(path: Path | str | None = None) -> Path:
    """The file to read: explicit argument > ``SLA_TERMS_PATH`` > ``config/sla_terms.yaml``."""
    if path:
        return Path(path)
    env = (os.getenv(SLA_TERMS_PATH_ENV) or "").strip()
    return Path(env) if env else DEFAULT_SLA_TERMS_PATH


def _band(priority: str, block: dict[str, Any], *, source: str, yaml_path: str) -> SlaBandTerms:
    missing = [k for k in BAND_KEYS if block.get(k) is None]
    if missing:
        raise ValueError(f"{yaml_path} is missing {', '.join(missing)}")
    return SlaBandTerms(
        priority=priority,
        ack=int(block["ack"]),
        restore=int(block["restore"]),
        note_interval=int(block["note_interval"]),
        source=source,
        yaml_path=yaml_path,
    )


def load_sla_terms(path: Path | str | None = None, *, cfg: OperatorConfig | None = None) -> SlaTerms:
    """Read, validate and merge the terms. Raises rather than guessing:

    * ``FileNotFoundError`` -- no file. A scorecard job must stop, not compute on air.
    * ``ValueError`` -- no ``sla_terms.version``, a band with a missing key, or a vendor
      block naming an unknown priority. An unversioned card is irreproducible.

    A priority absent from ``sla_terms.default`` falls back to ``cfg.sla_minutes`` (the
    spec's "defaulting from sla_minutes") and is marked ``source="sla_minutes"``; when
    ``cfg`` is not given the active operator profile is used.
    """
    target = sla_terms_path(path)
    if not target.exists():
        raise FileNotFoundError(f"sla_terms file not found: {target}")
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("sla_terms"), dict):
        raise ValueError(f"{target}: top-level `sla_terms` mapping is missing")
    terms = raw["sla_terms"]
    version = terms.get("version")
    if version is None or not str(version).strip():
        raise ValueError(f"{target}: sla_terms.version is required (a scorecard must cite the terms it used)")
    version = str(version).strip()

    if cfg is None:
        from noc_agents.config import get_settings  # local: keep this module importable without a profile

        cfg = get_settings().operator

    default_block = terms.get("default") or {}
    default_bands: dict[str, SlaBandTerms] = {}
    for prio in PRIORITIES:
        block = default_block.get(prio)
        if isinstance(block, dict):
            default_bands[prio] = _band(prio, block, source=SOURCE_YAML, yaml_path=f"sla_terms.default.{prio}")
            continue
        fallback = cfg.sla_minutes.get(prio)
        if fallback is None:
            raise ValueError(f"{target}: sla_terms.default.{prio} is missing and cfg.sla_minutes has no {prio} either")
        default_bands[prio] = SlaBandTerms(
            priority=prio,
            ack=fallback.ack,
            restore=fallback.restore,
            note_interval=fallback.note_interval,
            source=SOURCE_SLA_MINUTES,
            yaml_path=f"sla_minutes.{prio}",  # the profile, not this file -- and the card will say so
        )

    vendors: dict[str, VendorTerms] = {}
    for key, block in (terms.get("vendors") or {}).items():
        code = normalise_code(key)
        block = dict(block or {})
        bands: dict[str, SlaBandTerms] = {}
        for k, v in block.items():
            if k in PRIORITIES and isinstance(v, dict):
                bands[k] = _band(k, v, source=SOURCE_YAML, yaml_path=f"sla_terms.vendors.{key}.{k}")
            elif k.startswith("P") and k[1:].isdigit() and k not in PRIORITIES:
                raise ValueError(f"{target}: sla_terms.vendors.{key}.{k} is not a priority")
        pct = block.get("credit_pct") or []
        vendors[code] = VendorTerms(
            code=code,
            contract_ref=str(block["contract_ref"]) if block.get("contract_ref") else None,
            contract_is_synthetic=bool(block.get("contract_is_synthetic", False)),
            credit_shape=str(block.get("credit_shape") or default_block.get("credit_shape") or "none"),
            credit_pct=tuple(float(p) for p in pct),
            bands=bands,
            note=str(block.get("note") or ""),
        )

    avail = default_block.get("availability_target_pct")
    return SlaTerms(
        version=version,
        path=target,
        source=str(terms["source"]) if terms.get("source") else None,
        default_bands=default_bands,
        availability_target_pct=float(avail) if avail is not None else None,
        default_credit_shape=str(default_block.get("credit_shape") or "none"),
        vendors=vendors,
        scorecards=dict(raw.get("scorecards") or {}),
        regulatory=dict(raw.get("regulatory") or {}),
        raw=raw,
    )


def sla_terms_version(path: Path | str | None = None, *, cfg: OperatorConfig | None = None) -> str:
    """Just the version string -- what ``vendor_scorecards.sla_terms_version`` stores."""
    return load_sla_terms(path, cfg=cfg).version
