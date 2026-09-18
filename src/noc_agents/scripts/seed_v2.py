"""``noc-seed-v2`` — load the demo seed set in ``data/seed/v2/`` (spec §7.0.7).

Why this is written defensively
-------------------------------
The seed *files* land in Phase 1. The *tables* they seed into do not: ``vendors``
and ``sla_terms`` arrive with Phase 4 (§7.6.1), ``maintenance_plans`` and
``capacity_observations`` with Phase 4 (§7.5.1), ``contracts`` and
``contract_faq`` with Phase 5 (§7.8.1). Run today, every dataset here has
nowhere to go.

So the loader **never assumes a table exists and never crashes when one does
not**. For each dataset it:

1. asks the live database whether the target table is there — if not, it prints
   one line saying so, names the phase the table is due in, and moves on;
2. reads the table's actual columns and writes only the intersection with the
   seed row, so a Phase 4/5 table that spells a column differently, or adds
   columns this file has never heard of, still loads rather than exploding;
3. looks each row up by its natural key first and inserts only what is missing —
   which is what makes a second run a no-op rather than a duplicate;
4. wraps the whole dataset in try/except, so a dataset that cannot load reports
   itself and the remaining datasets still run.

Exit code is 0 when every dataset either loaded or was cleanly skipped, and 1
when a dataset errored. Neither path raises out of ``main()``.

Two datasets never write rows by design and say so:
``sla_terms.yaml`` is config, not table data (§7.6.1: "sla_terms live in YAML"),
and ``contracts/golden.yaml`` is a retrieval eval set. Both are still parsed and
validated on every run, because a seed file that silently stopped parsing is a
worse failure than one that never loaded.

Clause chunking of the two sample contracts is deliberately NOT done here — it
belongs to ``services/contracts.py`` in Phase 5 (§7.8.3), which owns the clause
regex, the ``context_header`` wording and the FTS5 index.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Repo root: src/noc_agents/scripts/seed_v2.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED_DIR = ROOT / "data" / "seed" / "v2"

#: Deterministic id namespace, so a row generated on two machines gets one id.
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://noc-agents.local/seed/v2")

#: Columns we are willing to fill with "now" when the target table has them and
#: the seed row does not. Anything else missing is the table's business.
_STAMP_COLUMNS = ("created_at", "ingested_at")

# Why each table is absent, phrased as a whole sentence so the skip line reads
# like an explanation rather than a error.
_TABLE_PHASE = {
    "vendors": "that table arrives in Phase 4 (spec §7.6.1)",
    "sla_terms": "there is no sla_terms table in the spec at all — §7.6.1 keeps these in YAML",
    "maintenance_plans": "that table arrives in Phase 4 (spec §7.5.1)",
    "capacity_observations": "that table arrives in Phase 4 (spec §7.5.1)",
    "contracts": "that table arrives in Phase 5 (spec §7.8.1)",
    "contract_faq": "that table arrives in Phase 5 (spec §7.8.1)",
}

LOADED = "loaded"
NO_TABLE = "no-table"
VALIDATED = "validated-only"
ERROR = "error"


def _utcnow() -> datetime:
    """Naive UTC, matching the db/models.py contract."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_print(line: str) -> None:
    """print() that cannot raise on a legacy Windows console codepage.

    The output below contains '§' and '—'. A cp1252 stdout would raise
    UnicodeEncodeError on those, which for a seed script is a crash on the way
    out of a successful run — the one failure mode this module promises not to
    have.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, errors="replace").decode(enc, errors="replace"))


def _stable_id(*parts: Any) -> str:
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


@dataclass
class Dataset:
    """One seed file's worth of rows aimed at one table."""

    name: str
    table: str
    source: str  # path relative to the seed dir, for the log
    key: tuple[str, ...]  # natural key columns -> what "already there" means
    rows: list[dict[str, Any]] = field(default_factory=list)
    write: bool = True  # False for config/eval files that own no table
    note: str = ""


@dataclass
class DatasetResult:
    name: str
    table: str
    status: str
    inserted: int = 0
    already_present: int = 0
    unwritable: int = 0  # rows the table had no room for
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != ERROR


@dataclass
class SeedReport:
    results: list[DatasetResult] = field(default_factory=list)
    seed_dir: Path = DEFAULT_SEED_DIR

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def already_present(self) -> int:
        return sum(r.already_present for r in self.results)

    @property
    def errors(self) -> list[DatasetResult]:
        return [r for r in self.results if r.status == ERROR]

    @property
    def skipped_tables(self) -> list[str]:
        return [r.table for r in self.results if r.status == NO_TABLE]

    def by_name(self, name: str) -> DatasetResult | None:
        for r in self.results:
            if r.name == name:
                return r
        return None

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


# ---------------------------------------------------------------------------
# Reading the seed files
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _split_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Return (front matter, body) for a ``---`` fenced markdown file.

    A file without front matter returns ({}, raw) rather than raising: the
    caller reports it as unusable, which is friendlier than a traceback.
    """
    if not raw.startswith("---"):
        return {}, raw
    # Stop at the FIRST line that is exactly '---'. Splitting on the string
    # '\n---' instead truncates the body at the first markdown horizontal rule,
    # which both sample contracts have (and which silently halved token_count).
    lines = raw[3:].split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            meta = yaml.safe_load("\n".join(lines[:i])) or {}
            if not isinstance(meta, dict):
                return {}, raw
            return meta, "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, raw


def build_vendors(seed_dir: Path) -> Dataset:
    data = _read_yaml(seed_dir / "vendors.yaml")
    rows: list[dict[str, Any]] = []
    for v in data.get("vendors", []):
        rows.append(
            {
                "id": v["id"],
                "operator_id": v["operator_id"],
                "code": v["code"],
                "display_name": v["display_name"],
                "type": v["type"],
                "contract_ref": v.get("contract_ref"),
                "active_from": v.get("active_from"),
                "active_to": v.get("active_to"),
                "contacts_json": json.dumps(v.get("contacts") or {}, sort_keys=True),
            }
        )
    return Dataset(
        name="vendors",
        table="vendors",
        source="vendors.yaml",
        # The §7.6.1 UNIQUE is (operator_id, code, active_from); matching on it
        # means a row seeded under a different id is still recognised as present.
        key=("operator_id", "code", "active_from"),
        rows=rows,
        note="derived from cfg.msp_contacts; `type` values are a first pass and want review",
    )


def build_sla_terms(seed_dir: Path) -> Dataset:
    """Flatten sla_terms.yaml into rows, in case a Phase 4 table ever wants them.

    §7.6.1 says these live in YAML, so ``write`` is still True but the table is
    expected to be absent forever; the value of this dataset today is that the
    YAML is parsed and checked on every run.
    """
    data = _read_yaml(seed_dir / "sla_terms.yaml")
    terms = data.get("sla_terms") or {}
    version = terms.get("version", "unversioned")
    rows: list[dict[str, Any]] = []

    def _emit(scope: str, scope_ref: str, block: dict[str, Any], extra: dict[str, Any]) -> None:
        for prio in ("P1", "P2", "P3", "P4"):
            band = block.get(prio)
            if not isinstance(band, dict):
                continue
            rows.append(
                {
                    "id": _stable_id("sla_terms", version, scope, scope_ref, prio),
                    "operator_id": "safaricom",
                    "version": version,
                    "scope": scope,
                    "scope_ref": scope_ref,
                    "priority": prio,
                    "ack_minutes": band.get("ack"),
                    "restore_minutes": band.get("restore"),
                    "note_interval_minutes": band.get("note_interval"),
                    **extra,
                }
            )

    default = terms.get("default") or {}
    _emit(
        "DEFAULT",
        "default",
        default,
        {
            "availability_target_pct": default.get("availability_target_pct"),
            "credit_shape": default.get("credit_shape", "none"),
            "credit_pct_json": "[]",
            "contract_ref": None,
        },
    )
    for code, vendor in (terms.get("vendors") or {}).items():
        # Vendor blocks carry only credit shape; the bands fall back to default.
        _emit(
            "VENDOR",
            code,
            default,
            {
                "availability_target_pct": default.get("availability_target_pct"),
                "credit_shape": vendor.get("credit_shape", "none"),
                "credit_pct_json": json.dumps(vendor.get("credit_pct") or []),
                "contract_ref": vendor.get("contract_ref"),
            },
        )
    return Dataset(
        name="sla_terms",
        table="sla_terms",
        source="sla_terms.yaml",
        key=("version", "scope", "scope_ref", "priority"),
        rows=rows,
        note="§7.6.1 keeps sla_terms in YAML; these rows exist only if someone later adds a table",
    )


def build_maintenance_plans(seed_dir: Path) -> Dataset:
    data = _read_yaml(seed_dir / "maintenance_plans.yaml")
    rows: list[dict[str, Any]] = []
    for p in data.get("maintenance_plans", []):
        rows.append(
            {
                "id": p["id"],
                "operator_id": p["operator_id"],
                "site_id": p.get("site_id"),
                "site_class": p.get("site_class"),
                "task_type": p["task_type"],
                "interval_days": p.get("interval_days"),
                "interval_hours": p.get("interval_hours"),
                "consumption_driven": int(p.get("consumption_driven") or 0),
                "owner_vendor_id": p.get("owner_vendor_id"),
                "standard_ref": p["standard_ref"],
                "active": int(p.get("active", 1)),
            }
        )
    return Dataset(
        name="maintenance_plans",
        table="maintenance_plans",
        source="maintenance_plans.yaml",
        key=("id",),
        rows=rows,
        note="intervals and standards are the §7.5.1 secondary-sourced defaults",
    )


def build_capacity(seed_dir: Path) -> Dataset:
    path = seed_dir / "capacity_sample.csv"
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            site_id = (r.get("site_id") or "").strip()
            cell_id = (r.get("cell_id") or "").strip() or None
            metric = (r.get("metric") or "").strip()
            busy_hour_at = (r.get("busy_hour_at") or "").strip()
            if not site_id or not metric or not busy_hour_at:
                continue
            rows.append(
                {
                    "id": _stable_id("capacity", site_id, cell_id, metric, busy_hour_at),
                    "operator_id": "safaricom",
                    "site_id": site_id,
                    "cell_id": cell_id,
                    "metric": metric,
                    "value": float(r["value"]),
                    "busy_hour_at": busy_hour_at,
                    "source": (r.get("source") or "CSV").strip(),
                }
            )
    return Dataset(
        name="capacity_observations",
        table="capacity_observations",
        source="capacity_sample.csv",
        key=("id",),
        rows=rows,
        note="busy_hour_at is naive UTC (18:00/19:00/20:00 EAT); ids are uuid5, so re-import is a no-op",
    )


def read_contract_files(seed_dir: Path) -> list[tuple[Path, dict[str, Any], str]]:
    """Every ``*_sample.md`` under contracts/, as (path, front matter, body)."""
    folder = seed_dir / "contracts"
    out = []
    for path in sorted(folder.glob("*_sample.md")):
        meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
        out.append((path, meta, body))
    return out


def build_contracts(seed_dir: Path) -> Dataset:
    rows: list[dict[str, Any]] = []
    for path, meta, body in read_contract_files(seed_dir):
        if not meta.get("id"):
            continue  # reported by validate_seed_set, not silently loaded
        raw = path.read_bytes()
        rows.append(
            {
                "id": meta["id"],
                "operator_id": "safaricom",
                "counterparty_vendor_id": meta.get("counterparty_vendor_id", ""),
                # The stored title carries the SAMPLE/SYNTHETIC marking too, so a
                # UI that only ever shows the title still shows the warning.
                "title": meta.get("title", path.stem),
                "effective_date": meta.get("effective_date"),
                "version": meta.get("version", "SAMPLE-1.0"),
                "source_file": str(path.relative_to(ROOT)).replace(os.sep, "/"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "confidentiality_checked_by": meta.get("confidentiality_basis", ""),
                "confidentiality_checked_at": _utcnow(),
                "third_party_processing_permitted": int(meta.get("third_party_processing_permitted") or 0),
                "allowed_roles_json": json.dumps(meta.get("allowed_roles") or []),
                "token_count": len(body.split()),
            }
        )
    return Dataset(
        name="contracts",
        table="contracts",
        source="contracts/*_sample.md",
        key=("id",),
        rows=rows,
        note="SYNTHETIC samples only; clause chunking + FTS belong to Phase 5 services/contracts.py",
    )


def build_contract_faq(seed_dir: Path) -> Dataset:
    data = _read_yaml(seed_dir / "contracts" / "faq.yaml")
    ref_to_id = {
        meta.get("contract_ref"): meta.get("id")
        for _p, meta, _b in read_contract_files(seed_dir)
        if meta.get("contract_ref")
    }
    rows: list[dict[str, Any]] = []
    for f in data.get("contract_faq", []):
        contract_ids = [ref_to_id.get(ref) for ref in f.get("contract_refs", [])]
        rows.append(
            {
                "id": f["id"],
                "operator_id": f["operator_id"],
                "contract_ids_json": json.dumps([c for c in contract_ids if c]),
                "question": f["question"],
                "approved_answer": " ".join(str(f["approved_answer"]).split()),
                "clause_refs_json": json.dumps(f.get("clause_refs") or []),
                "approved_by": f["approved_by"],
                "approved_at": _utcnow(),
                "active": int(f.get("active", 1)),
            }
        )
    return Dataset(
        name="contract_faq",
        table="contract_faq",
        source="contracts/faq.yaml",
        key=("id",),
        rows=rows,
        note="one row (§7.0.7); approved_by names the seed file, not a lawyer",
    )


def build_golden(seed_dir: Path) -> Dataset:
    """Parsed and counted, never written: it is an eval set, not table data."""
    data = _read_yaml(seed_dir / "contracts" / "golden.yaml")
    rows = [dict(q) for q in data.get("questions", [])]
    return Dataset(
        name="contracts_golden",
        table="(none — retrieval eval set)",
        source="contracts/golden.yaml",
        key=("id",),
        rows=rows,
        write=False,
        note="§7.8.3 uses it for the BM25 recall@20 >= 0.9 decision",
    )


BUILDERS: tuple[Callable[[Path], Dataset], ...] = (
    build_vendors,
    build_sla_terms,
    build_maintenance_plans,
    build_capacity,
    build_contracts,
    build_contract_faq,
    build_golden,
)


def build_datasets(seed_dir: Path) -> list[Dataset]:
    return [b(seed_dir) for b in BUILDERS]


# ---------------------------------------------------------------------------
# Validation (runs whether or not anything can be written)
# ---------------------------------------------------------------------------


def validate_seed_set(seed_dir: Path) -> list[str]:
    """Cheap self-checks over the files. Returns a list of complaints, possibly empty.

    These catch the failure this seed set is most exposed to: a reference that
    stops resolving because someone renamed a vendor or a contract.
    """
    problems: list[str] = []
    try:
        vendors = _read_yaml(seed_dir / "vendors.yaml").get("vendors", [])
        vendor_ids = {v.get("id") for v in vendors}
        vendor_codes = {v.get("code") for v in vendors}

        for p in _read_yaml(seed_dir / "maintenance_plans.yaml").get("maintenance_plans", []):
            if bool(p.get("site_id")) == bool(p.get("site_class")):
                problems.append(f"maintenance plan {p.get('id')}: needs exactly one of site_id/site_class (§7.5.1)")
            owner = p.get("owner_vendor_id")
            if owner and owner not in vendor_ids:
                problems.append(f"maintenance plan {p.get('id')}: owner_vendor_id {owner!r} is not a seeded vendor")

        terms = _read_yaml(seed_dir / "sla_terms.yaml").get("sla_terms") or {}
        default = terms.get("default") or {}
        for prio in ("P1", "P2", "P3", "P4"):
            band = default.get(prio) or {}
            missing = [k for k in ("ack", "restore", "note_interval") if k not in band]
            if missing:
                problems.append(f"sla_terms default {prio}: missing {', '.join(missing)}")
        for code in (terms.get("vendors") or {}):
            if code not in vendor_codes:
                problems.append(f"sla_terms vendor {code!r} is not a code in vendors.yaml")

        contracts = read_contract_files(seed_dir)
        refs = set()
        for path, meta, _body in contracts:
            for required in ("id", "title", "contract_ref", "synthetic"):
                if required not in meta:
                    problems.append(f"{path.name}: front matter is missing {required!r}")
            if meta.get("synthetic") is not True:
                problems.append(f"{path.name}: every contract in this folder must be marked synthetic: true")
            if "SAMPLE" not in str(meta.get("title", "")).upper():
                problems.append(f"{path.name}: stored title must carry the SAMPLE marking")
            refs.add(meta.get("contract_ref"))
            cp = meta.get("counterparty_vendor_id")
            if cp and cp not in vendor_ids:
                problems.append(f"{path.name}: counterparty_vendor_id {cp!r} is not a seeded vendor")

        golden = _read_yaml(seed_dir / "contracts" / "golden.yaml").get("questions", [])
        if len(golden) != 12:
            problems.append(f"contracts/golden.yaml: expected 12 questions, found {len(golden)} (§7.0.7)")
        for q in golden:
            for ref in q.get("scope_contract_refs", []):
                if ref not in refs:
                    problems.append(f"golden {q.get('id')}: scope_contract_refs {ref!r} matches no sample contract")
        if not any((q.get("expect") or {}).get("must_refuse") for q in golden):
            problems.append("contracts/golden.yaml: no refusal case — M8 reports refusals separately, so one is required")

        faq = _read_yaml(seed_dir / "contracts" / "faq.yaml").get("contract_faq", [])
        for f in faq:
            for ref in f.get("contract_refs", []):
                if ref not in refs:
                    problems.append(f"faq {f.get('id')}: contract_ref {ref!r} matches no sample contract")
    except (OSError, yaml.YAMLError, AttributeError, TypeError) as exc:  # never crash on a bad file
        problems.append(f"seed set could not be fully validated: {type(exc).__name__}: {exc}")
    return problems


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _bind(value: Any) -> Any:
    """Make a value safe for a raw ``text()`` bind on SQLite.

    These INSERTs carry no SQLAlchemy type information, so a ``datetime`` would
    fall through to pysqlite's default adapter — deprecated since Python 3.12.
    Rendering it in the format SQLAlchemy's own SQLite DATETIME reads back
    ("YYYY-MM-DD HH:MM:SS.ffffff") keeps the column round-trippable.
    """
    if isinstance(value, datetime):
        return f"{value:%Y-%m-%d %H:%M:%S.%f}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return value


def _quote(name: str) -> str:
    """Quote an identifier for SQLite/ANSI. Seed column names are plain, but a
    Phase 4 table could name a column ``type`` or ``version``."""
    return '"' + name.replace('"', '""') + '"'


def _load_dataset(engine: Engine, ds: Dataset) -> DatasetResult:
    insp = inspect(engine)
    if not insp.has_table(ds.table):
        due = _TABLE_PHASE.get(ds.table, "it arrives in a later phase")
        return DatasetResult(
            ds.name,
            ds.table,
            NO_TABLE,
            detail=f"{len(ds.rows)} row(s) held in {ds.source}; {due}",
        )

    columns = {c["name"] for c in insp.get_columns(ds.table)}
    missing_key = [k for k in ds.key if k not in columns]
    if missing_key:
        return DatasetResult(
            ds.name,
            ds.table,
            NO_TABLE,
            detail=(
                f"table exists but its natural key column(s) {', '.join(missing_key)} do not — "
                f"{len(ds.rows)} row(s) left in {ds.source} rather than guessed at"
            ),
        )

    inserted = already = unwritable = 0
    with engine.begin() as conn:
        for row in ds.rows:
            usable = {k: v for k, v in row.items() if k in columns}
            if not all(k in usable for k in ds.key):
                unwritable += 1
                continue
            for stamp in _STAMP_COLUMNS:
                if stamp in columns and stamp not in usable:
                    usable[stamp] = _utcnow()

            # NULL-safe and engine-portable: "col IS NULL" for a null key part,
            # "col = :col" otherwise (SQLite's "IS x" form is not portable).
            params = {k: _bind(row[k]) for k in ds.key if row[k] is not None}
            where = " AND ".join(
                f"{_quote(k)} = :{k}" if row[k] is not None else f"{_quote(k)} IS NULL"
                for k in ds.key
            )
            found = conn.execute(
                text(f"SELECT 1 FROM {_quote(ds.table)} WHERE {where} LIMIT 1"), params
            ).first()
            if found is not None:
                already += 1
                continue

            cols = list(usable)
            conn.execute(
                text(
                    f"INSERT INTO {_quote(ds.table)} "
                    f"({', '.join(_quote(c) for c in cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})"
                ),
                {c: _bind(v) for c, v in usable.items()},
            )
            inserted += 1

    detail = ""
    if unwritable:
        detail = f"{unwritable} row(s) had no natural key in this table and were left alone"
    return DatasetResult(ds.name, ds.table, LOADED, inserted, already, unwritable, detail)


def run_seed(
    engine: Engine,
    *,
    seed_dir: Path = DEFAULT_SEED_DIR,
    echo: Callable[[str], None] = _safe_print,
) -> SeedReport:
    """Load every dataset it can and report on the rest. Raises nothing."""
    report = SeedReport(seed_dir=seed_dir)

    echo("noc-seed-v2: demo seed set (spec §7.0.7)")
    echo(f"  seed dir : {seed_dir}")
    echo(f"  database : {engine.url}")
    echo("")

    if not seed_dir.is_dir():
        echo(f"  ERROR    seed directory not found: {seed_dir}")
        report.results.append(
            DatasetResult("seed-dir", "(none)", ERROR, detail=f"missing directory {seed_dir}")
        )
        return report

    for problem in validate_seed_set(seed_dir):
        echo(f"  CHECK    {problem}")

    try:
        datasets = build_datasets(seed_dir)
    except Exception as exc:  # a malformed seed file must not produce a traceback
        echo(f"  ERROR    seed files could not be read: {type(exc).__name__}: {exc}")
        report.results.append(
            DatasetResult("seed-files", "(none)", ERROR, detail=f"{type(exc).__name__}: {exc}")
        )
        return report

    for ds in datasets:
        if not ds.write:
            res = DatasetResult(
                ds.name, ds.table, VALIDATED, detail=f"{len(ds.rows)} entries parsed from {ds.source}"
            )
        else:
            try:
                res = _load_dataset(engine, ds)
            except Exception as exc:  # one bad table must not stop the others
                res = DatasetResult(
                    ds.name, ds.table, ERROR, detail=f"{type(exc).__name__}: {exc}"
                )
        report.results.append(res)
        echo(_format(res, ds))

    echo("")
    echo(
        f"  {report.inserted} row(s) inserted, {report.already_present} already present, "
        f"{len(report.skipped_tables)} dataset(s) waiting on a table that does not exist yet."
    )
    if report.errors:
        echo(f"  {len(report.errors)} dataset(s) FAILED — see the ERROR lines above.")
    else:
        echo("  Nothing failed. Re-running this command changes nothing (it is idempotent).")
    return report


def _format(res: DatasetResult, ds: Dataset) -> str:
    label = {LOADED: "LOADED  ", NO_TABLE: "SKIPPED ", VALIDATED: "CHECKED ", ERROR: "ERROR   "}[res.status]
    if res.status == LOADED:
        body = f"{res.table}: +{res.inserted} new, {res.already_present} already there"
        if res.detail:
            body += f" ({res.detail})"
    elif res.status == NO_TABLE:
        body = f"{res.table}: no such table yet — {res.detail}"
    elif res.status == VALIDATED:
        body = f"{ds.source}: {res.detail}; nothing to load — {ds.note}"
    else:
        body = f"{res.table}: {res.detail}"
    return f"  {label} {body}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _resolve_database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    try:
        from noc_agents.config import get_settings

        return get_settings().database_url
    except Exception:  # config problems must not stop a seed run
        return "sqlite:///./data/noc_agents.db"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="noc-seed-v2",
        description="Load the data/seed/v2 demo seed set. Idempotent; skips tables that do not exist yet.",
    )
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument("--seed-dir", default=None, help="override data/seed/v2")
    args = parser.parse_args(argv)

    seed_dir = Path(args.seed_dir) if args.seed_dir else Path(os.getenv("NOC_SEED_V2_DIR", DEFAULT_SEED_DIR))
    url = _resolve_database_url(args.database_url)

    try:
        from noc_agents.db.models import init_db

        engine = init_db(url)
    except Exception as exc:
        _safe_print(f"noc-seed-v2: could not open the database at {url}: {type(exc).__name__}: {exc}")
        return 1

    report = run_seed(engine, seed_dir=seed_dir)
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
