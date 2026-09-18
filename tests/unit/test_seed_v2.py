"""Phase 1 (spec §7.0.7): the ``data/seed/v2`` demo seed set and ``noc-seed-v2``.

Two properties carry the weight here, because the loader ships long before the
tables it targets:

* **Idempotent** — running it twice must not duplicate a row. Proven by creating
  the Phase 4/5 tables by hand from the spec's own DDL, loading twice, and
  asserting the second pass inserts nothing and the row count does not move.
* **Degrades, never crashes** — with no tables (today's reality), with only some
  of them, with a table whose columns do not match, and with a broken seed
  directory, ``run_seed`` returns a report instead of raising.

The rest pins the seed content itself: that the vendor rows really were derived
from ``cfg.msp_contacts`` rather than invented, and that the two sample
contracts are marked synthetic in every place a reader could land.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

from noc_agents.scripts import seed_v2
from noc_agents.scripts.seed_v2 import (
    DEFAULT_SEED_DIR,
    ERROR,
    LOADED,
    NO_TABLE,
    VALIDATED,
    build_datasets,
    read_contract_files,
    run_seed,
    validate_seed_set,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = DEFAULT_SEED_DIR / "contracts"

# The tables the seed set targets, as spec §7.6.1 / §7.5.1 / §7.8.1 declare them.
# Copied here rather than imported because they do not exist in db/models.py yet:
# that absence is the whole point of the degradation tests.
DDL = {
    "vendors": """
        CREATE TABLE vendors (id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, code TEXT NOT NULL,
          display_name TEXT NOT NULL, type TEXT NOT NULL,
          contract_ref TEXT, active_from DATE NOT NULL, active_to DATE,
          contacts_json TEXT NOT NULL DEFAULT '{}',
          UNIQUE (operator_id, code, active_from))
    """,
    "maintenance_plans": """
        CREATE TABLE maintenance_plans (id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, site_id TEXT,
          site_class TEXT, task_type TEXT NOT NULL, interval_days INTEGER, interval_hours INTEGER,
          consumption_driven INTEGER NOT NULL DEFAULT 0, owner_vendor_id TEXT,
          standard_ref TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at DATETIME NOT NULL)
    """,
    "capacity_observations": """
        CREATE TABLE capacity_observations (id TEXT PRIMARY KEY, operator_id TEXT NOT NULL,
          site_id TEXT NOT NULL, cell_id TEXT, metric TEXT NOT NULL, value REAL NOT NULL,
          busy_hour_at DATETIME NOT NULL, source TEXT NOT NULL, created_at DATETIME NOT NULL)
    """,
    "contracts": """
        CREATE TABLE contracts (id TEXT PRIMARY KEY, operator_id TEXT NOT NULL,
          counterparty_vendor_id TEXT NOT NULL, title TEXT NOT NULL, effective_date DATE NOT NULL,
          version TEXT NOT NULL, source_file TEXT NOT NULL, sha256 TEXT NOT NULL,
          confidentiality_checked_by TEXT, confidentiality_checked_at DATETIME,
          third_party_processing_permitted INTEGER NOT NULL DEFAULT 0,
          allowed_roles_json TEXT NOT NULL, token_count INTEGER NOT NULL, ingested_at DATETIME NOT NULL)
    """,
    "contract_faq": """
        CREATE TABLE contract_faq (id TEXT PRIMARY KEY, operator_id TEXT NOT NULL,
          contract_ids_json TEXT NOT NULL, question TEXT NOT NULL, approved_answer TEXT NOT NULL,
          clause_refs_json TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at DATETIME NOT NULL,
          active INTEGER NOT NULL DEFAULT 1)
    """,
}
WRITABLE = ("vendors", "sla_terms", "maintenance_plans", "capacity_observations", "contracts", "contract_faq")


def make_engine(tmp_path: Path, tables: tuple[str, ...] = (), extra_ddl: str | None = None):
    db = tmp_path / "seed_test.db"
    raw = sqlite3.connect(db)
    for name in tables:
        raw.execute(DDL[name])
    if extra_ddl:
        raw.execute(extra_ddl)
    raw.commit()
    raw.close()
    return create_engine(f"sqlite:///{db.as_posix()}")


def count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def quiet(_line: str) -> None:
    """Swallow the loader's chatter; individual tests capture it when they care."""


# ---------------------------------------------------------------------------
# It must not crash when a target table is missing
# ---------------------------------------------------------------------------


def test_empty_database_skips_everything_cleanly(tmp_path):
    """Today's reality: not one of the target tables exists."""
    engine = make_engine(tmp_path)
    lines: list[str] = []

    report = run_seed(engine, echo=lines.append)

    assert report.exit_code == 0
    assert report.errors == []
    assert report.inserted == 0
    assert sorted(report.skipped_tables) == sorted(WRITABLE)
    for name in WRITABLE:
        assert report.by_name(name).status == NO_TABLE
    # ...and it says so out loud rather than exiting quietly.
    out = "\n".join(lines)
    assert "no such table yet" in out
    assert "Phase 4" in out and "Phase 5" in out
    assert "idempotent" in out


def test_golden_set_is_parsed_but_never_written(tmp_path):
    report = run_seed(make_engine(tmp_path), echo=quiet)
    golden = report.by_name("contracts_golden")
    assert golden.status == VALIDATED
    assert golden.inserted == 0
    assert "12 entries" in golden.detail


def test_partial_schema_loads_what_it_can(tmp_path):
    """One table present, five absent: load the one, skip the rest, exit 0."""
    engine = make_engine(tmp_path, tables=("vendors",))

    report = run_seed(engine, echo=quiet)

    assert report.exit_code == 0
    assert report.by_name("vendors").status == LOADED
    assert report.by_name("vendors").inserted == count(engine, "vendors") > 0
    for name in ("maintenance_plans", "capacity_observations", "contracts", "contract_faq"):
        assert report.by_name(name).status == NO_TABLE


def test_table_missing_its_natural_key_is_skipped_not_crashed(tmp_path):
    """A table that exists but is shaped differently must not be guessed at."""
    engine = make_engine(
        tmp_path,
        extra_ddl="CREATE TABLE contracts (contract_uuid TEXT PRIMARY KEY, title TEXT)",
    )
    lines: list[str] = []

    report = run_seed(engine, echo=lines.append)

    res = report.by_name("contracts")
    assert res.status == NO_TABLE
    assert "natural key column" in res.detail
    assert report.exit_code == 0
    assert count(engine, "contracts") == 0


def test_narrow_table_takes_only_the_columns_it_has(tmp_path):
    """A table with a subset of columns loads the intersection, not an error."""
    engine = make_engine(
        tmp_path,
        extra_ddl=(
            "CREATE TABLE maintenance_plans "
            "(id TEXT PRIMARY KEY, operator_id TEXT, task_type TEXT)"
        ),
    )

    report = run_seed(engine, echo=quiet)

    assert report.by_name("maintenance_plans").status == LOADED
    assert count(engine, "maintenance_plans") == 3
    with engine.connect() as conn:
        types = {r[0] for r in conn.execute(text("SELECT task_type FROM maintenance_plans"))}
    assert types == {"GENERATOR_EXERCISE", "BATTERY_CHECK", "TOWER_VISUAL"}


def test_missing_seed_directory_reports_rather_than_raises(tmp_path):
    report = run_seed(make_engine(tmp_path), seed_dir=tmp_path / "nope", echo=quiet)
    assert report.exit_code == 1
    assert report.results[0].status == ERROR
    assert "missing directory" in report.results[0].detail


def test_malformed_seed_file_reports_rather_than_raises(tmp_path):
    broken = tmp_path / "seed"
    shutil.copytree(DEFAULT_SEED_DIR, broken)
    (broken / "vendors.yaml").write_text("vendors: [ this: is: not: yaml", encoding="utf-8")

    report = run_seed(make_engine(tmp_path), seed_dir=broken, echo=quiet)

    assert report.exit_code == 1
    assert any(r.status == ERROR for r in report.results)


def test_a_failing_table_does_not_stop_the_others(tmp_path):
    """A NOT NULL column the seed cannot fill fails one dataset, not the run."""
    engine = make_engine(
        tmp_path,
        tables=("maintenance_plans",),
        extra_ddl=(
            "CREATE TABLE vendors (id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, code TEXT NOT NULL, "
            "active_from DATE NOT NULL, mandatory_unknown TEXT NOT NULL)"
        ),
    )

    report = run_seed(engine, echo=quiet)

    assert report.by_name("vendors").status == ERROR
    assert report.by_name("maintenance_plans").status == LOADED  # ran anyway
    assert count(engine, "maintenance_plans") == 3
    assert report.exit_code == 1  # reported, not swallowed


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_run_inserts_nothing(tmp_path):
    engine = make_engine(tmp_path, tables=tuple(DDL))

    first = run_seed(engine, echo=quiet)
    counts_after_first = {t: count(engine, t) for t in DDL}
    second = run_seed(engine, echo=quiet)

    assert first.exit_code == second.exit_code == 0
    assert first.inserted > 0
    assert second.inserted == 0, "a second run must insert nothing"
    assert second.already_present == first.inserted
    assert {t: count(engine, t) for t in DDL} == counts_after_first


def test_third_run_still_changes_nothing(tmp_path):
    """Guards against an off-by-one that only shows up after the second pass."""
    engine = make_engine(tmp_path, tables=tuple(DDL))
    run_seed(engine, echo=quiet)
    run_seed(engine, echo=quiet)
    snapshot = {t: count(engine, t) for t in DDL}

    run_seed(engine, echo=quiet)

    assert {t: count(engine, t) for t in DDL} == snapshot


def test_loaded_row_counts_match_the_files(tmp_path):
    engine = make_engine(tmp_path, tables=tuple(DDL))
    sizes = {d.name: len(d.rows) for d in build_datasets(DEFAULT_SEED_DIR)}

    run_seed(engine, echo=quiet)

    assert count(engine, "vendors") == sizes["vendors"] == 14
    assert count(engine, "maintenance_plans") == 3  # §7.0.7: "three plans"
    assert count(engine, "capacity_observations") == sizes["capacity_observations"] == 72
    assert count(engine, "contracts") == 2
    assert count(engine, "contract_faq") == 1  # §7.0.7: "one FAQ row"


def test_ids_are_stable_across_runs(tmp_path):
    """Generated ids are uuid5, not uuid4 — otherwise nothing above would hold."""
    a = {r["id"] for r in build_datasets(DEFAULT_SEED_DIR)[3].rows}
    b = {r["id"] for r in build_datasets(DEFAULT_SEED_DIR)[3].rows}
    assert a == b and len(a) == 72


def test_a_row_deleted_by_hand_comes_back(tmp_path):
    """Idempotent is not the same as write-once: a gap must refill."""
    engine = make_engine(tmp_path, tables=("vendors",))
    run_seed(engine, echo=quiet)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM vendors WHERE code = 'TETRANET' AND operator_id = 'safaricom'"))
    before = count(engine, "vendors")

    report = run_seed(engine, echo=quiet)

    assert report.by_name("vendors").inserted == 1
    assert count(engine, "vendors") == before + 1


def test_a_row_edited_by_hand_is_left_alone(tmp_path):
    """Matching on the natural key must not clobber an operator's edit."""
    engine = make_engine(tmp_path, tables=("vendors",))
    run_seed(engine, echo=quiet)
    with engine.begin() as conn:
        conn.execute(text("UPDATE vendors SET display_name = 'Edited by a human' WHERE code = 'ECTA'"))

    run_seed(engine, echo=quiet)

    with engine.connect() as conn:
        name = conn.execute(text("SELECT display_name FROM vendors WHERE code = 'ECTA'")).scalar_one()
    assert name == "Edited by a human"


# ---------------------------------------------------------------------------
# The seed content itself
# ---------------------------------------------------------------------------


def test_seed_set_self_validates():
    assert validate_seed_set(DEFAULT_SEED_DIR) == []


def test_vendors_are_derived_from_msp_contacts():
    """Every code in cfg.msp_contacts has a vendor row carrying that entry verbatim."""
    from noc_agents.config import get_settings

    vendors = yaml.safe_load((DEFAULT_SEED_DIR / "vendors.yaml").read_text(encoding="utf-8"))["vendors"]
    by_code = {v["code"]: v for v in vendors if v["operator_id"] == "safaricom"}
    contacts = get_settings("safaricom").operator.msp_contacts

    assert set(by_code) == set(contacts), "vendors.yaml and cfg.msp_contacts have drifted apart"
    for code, entry in contacts.items():
        seeded = by_code[code]["contacts"]
        assert seeded["email"] == entry["email"]
        assert seeded["sms"] == entry["sms"]
        assert seeded.get("domains", []) == entry.get("domains", [])


def test_every_vendor_type_is_in_the_spec_vocabulary():
    vendors = yaml.safe_load((DEFAULT_SEED_DIR / "vendors.yaml").read_text(encoding="utf-8"))["vendors"]
    assert {v["type"] for v in vendors} <= {"MSP", "FE_CONTRACTOR", "OEM", "TOWERCO"}
    # Each one records what the guess rests on, because none of them came from a contract.
    assert all(v.get("type_basis") for v in vendors)


def test_sla_terms_defaults_equal_the_configured_sla_minutes():
    """§7.6.1: sla_terms default "from sla_minutes". If they drift, this file is stale."""
    from noc_agents.config import get_settings

    terms = yaml.safe_load((DEFAULT_SEED_DIR / "sla_terms.yaml").read_text(encoding="utf-8"))["sla_terms"]
    bands = get_settings("safaricom").operator.sla_minutes
    for prio in ("P1", "P2", "P3", "P4"):
        band = bands[prio]
        assert terms["default"][prio] == {
            "ack": band.ack,
            "restore": band.restore,
            "note_interval": band.note_interval,
        }


def test_maintenance_plans_are_scoped_one_way_or_the_other():
    plans = yaml.safe_load((DEFAULT_SEED_DIR / "maintenance_plans.yaml").read_text(encoding="utf-8"))
    plans = plans["maintenance_plans"]
    assert len(plans) == 3
    for p in plans:
        assert bool(p["site_id"]) != bool(p["site_class"]), "§7.5.1 allows one of the two, not both"
        assert p["standard_ref"], "an interval with no named standard is an invented number"
        assert "SECONDARY SOURCE" in p["standard_ref"]


def test_maintenance_plan_site_ids_exist_in_the_site_catalogue():
    from noc_agents.services.sites import lookup_site

    plans = yaml.safe_load((DEFAULT_SEED_DIR / "maintenance_plans.yaml").read_text(encoding="utf-8"))
    for p in plans["maintenance_plans"]:
        if p["site_id"]:
            assert lookup_site(p["site_id"]) is not None, f"{p['site_id']} is not a real seeded site"


def test_capacity_sample_contains_one_clear_trigger_and_one_clear_non_trigger():
    """§7.5.3: >= 70 % PRB for >= 3 busy hours/day on >= 7 days -> one advisory."""
    rows = [r for r in build_datasets(DEFAULT_SEED_DIR) if r.name == "capacity_observations"][0].rows
    by_cell: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        assert r["metric"] == "DL_TOTAL_PRB_USAGE"
        day = str(r["busy_hour_at"])[:10]
        by_cell.setdefault(r["cell_id"], {}).setdefault(day, []).append(r["value"])

    sustained = {
        cell: sum(1 for hours in days.values() if len(hours) >= 3 and all(v >= 70 for v in hours))
        for cell, days in by_cell.items()
    }
    assert sorted(sustained.values()) == [0, 4, 8], (
        "the sample needs one cell over the 7-day threshold, one under it and one quiet control"
    )


def test_capacity_sites_exist_in_the_site_catalogue():
    from noc_agents.services.sites import lookup_site

    rows = [r for r in build_datasets(DEFAULT_SEED_DIR) if r.name == "capacity_observations"][0].rows
    for site_id in {r["site_id"] for r in rows}:
        assert lookup_site(site_id) is not None, f"{site_id} is not a real seeded site"


# ---------------------------------------------------------------------------
# The synthetic contracts must be unmistakable
# ---------------------------------------------------------------------------

SAMPLE_FILES = sorted(CONTRACT_DIR.glob("*_sample.md"))


def test_there_are_exactly_two_sample_contracts():
    assert [p.name for p in SAMPLE_FILES] == ["egypro_msa_sample.md", "tetranet_sla_sample.md"]


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.name)
def test_sample_contract_is_marked_synthetic_everywhere_a_reader_lands(path):
    raw = path.read_text(encoding="utf-8")
    meta, body = seed_v2._split_front_matter(raw)

    # 1. the filename
    assert "sample" in path.name
    # 2. machine-readable front matter
    assert meta["synthetic"] is True
    assert meta["is_real_contract"] is False
    assert meta["document_class"] == "FICTIONAL_TRAINING_SAMPLE"
    # 3. the stored title, which is all some UI will ever show
    assert "SAMPLE" in meta["title"].upper() and "SYNTHETIC" in meta["title"].upper()
    # 4. a banner in the first screenful
    head = "\n".join(body.splitlines()[:30]).upper()
    assert "SYNTHETIC SAMPLE" in head
    assert "NOT A REAL" in head
    assert "THIS FILE IS FICTION" in head
    # 5. a banner at the end too, for anyone who scrolled past the top
    tail = "\n".join(raw.splitlines()[-10:]).upper()
    assert "EVERYTHING ABOVE IS FICTION" in tail


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.name)
def test_every_section_heading_carries_the_marking(path):
    """A clause extracted on its own must still announce that it is fiction.

    Phase 5 chunking keeps the heading as the clause's context_header, so the
    marking has to live in the heading rather than only in the banner.
    """
    headings = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("## ")]
    assert len(headings) >= 7
    for h in headings:
        assert "SAMPLE" in h.upper(), f"unmarked heading: {h}"
        assert "FICTIONAL" in h.upper(), f"unmarked heading: {h}"


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.name)
def test_sample_contract_names_no_real_party_and_no_signatory(path):
    """Clause text speaks of "the Operator"/"the Service Provider", not companies."""
    raw = path.read_text(encoding="utf-8")
    _meta, body = seed_v2._split_front_matter(raw)
    clause_text = "\n".join(
        ln for ln in body.splitlines() if ln[:1].isdigit() and "." in ln[:6]
    )
    for name in ("Safaricom", "Airtel", "Egypro", "Tetranet", "Camusat", "Huawei"):
        assert name.lower() not in clause_text.lower(), (
            f"{name} appears inside clause text; real parties belong in metadata, not in fiction"
        )
    assert "signed by: nobody" in raw.lower()


def test_sample_priority_1_row_differs_from_the_configured_defaults():
    """Fictional service levels must not coincide with the operator's real ones.

    If the sample's Priority 1 row happened to quote cfg.sla_minutes, a reader
    could take it for the operator's actual position — the exact confusion the
    banner denies. Checked on the table row itself rather than on the whole
    file, because "60 minutes" legitimately appears elsewhere as a P4 response.
    """
    from noc_agents.config import get_settings

    real = get_settings("safaricom").operator.sla_minutes["P1"]
    rows = [
        line
        for path in SAMPLE_FILES
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("|") and "priority 1" in line.lower()
    ]
    assert len(rows) == 1, "the Egypro sample is the one carrying a priority table"
    assert f"| {real.ack} minutes" not in rows[0]
    assert f"| {real.restore} minutes" not in rows[0]
    assert "| 10 minutes | 90 minutes |" in rows[0]  # the fictional pair


def test_loaded_contract_row_still_announces_itself(tmp_path):
    """The marking survives the trip into the database."""
    engine = make_engine(tmp_path, tables=("contracts",))
    run_seed(engine, echo=quiet)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT title, source_file, sha256 FROM contracts")).all()
    assert len(rows) == 2
    for title, source_file, sha in rows:
        assert "SAMPLE" in title.upper() and "SYNTHETIC" in title.upper()
        assert source_file.startswith("data/seed/v2/contracts/")
        assert len(sha) == 64


def test_contract_sha256_matches_the_file_on_disk():
    import hashlib

    rows = [d for d in build_datasets(DEFAULT_SEED_DIR) if d.name == "contracts"][0].rows
    for row in rows:
        raw = (ROOT / row["source_file"]).read_bytes()
        assert row["sha256"] == hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Golden set and FAQ
# ---------------------------------------------------------------------------


def test_golden_set_has_twelve_questions_including_refusals():
    golden = yaml.safe_load((CONTRACT_DIR / "golden.yaml").read_text(encoding="utf-8"))
    questions = golden["questions"]

    assert len(questions) == 12  # §7.0.7
    assert len({q["id"] for q in questions}) == 12
    refusals = [q for q in questions if q["expect"].get("must_refuse")]
    assert len(refusals) == 2, "a golden set with no refusal case cannot measure the refusal rate (M8)"


def test_golden_expected_clauses_exist_in_the_sample_contracts():
    """A golden set pointing at clauses nobody wrote is worse than none."""
    golden = yaml.safe_load((CONTRACT_DIR / "golden.yaml").read_text(encoding="utf-8"))
    bodies = {
        meta["contract_ref"]: seed_v2._split_front_matter(p.read_text(encoding="utf-8"))[1]
        for p, meta, _b in read_contract_files(DEFAULT_SEED_DIR)
    }
    checked = 0
    for q in golden["questions"]:
        for ref in q["expect"].get("clauses", []) + q["expect"].get("must_not_cite", []):
            body = bodies[ref["contract_ref"]]
            assert f"\n{ref['clause']} " in body, (
                f"{q['id']}: clause {ref['clause']} is not in {ref['contract_ref']}"
            )
            checked += 1
    assert checked >= 15


def test_faq_row_answers_a_golden_question():
    """§7.8.3 returns an FAQ hit before generating; the eval needs that path covered."""
    faq = yaml.safe_load((CONTRACT_DIR / "faq.yaml").read_text(encoding="utf-8"))["contract_faq"]
    golden = yaml.safe_load((CONTRACT_DIR / "golden.yaml").read_text(encoding="utf-8"))["questions"]

    assert len(faq) == 1
    row = faq[0]
    assert row["active"] == 1 and row["synthetic"] is True
    assert "NOT a legal approval" in row["approved_by"]
    assert "SYNTHETIC" in row["approved_answer"].upper()

    matching = [q for q in golden if q["question"] == row["question"]]
    assert len(matching) == 1
    assert matching[0]["expect"]["source"] == ["faq"]


# ---------------------------------------------------------------------------
# Console script
# ---------------------------------------------------------------------------


def test_console_script_is_declared():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'noc-seed-v2 = "noc_agents.scripts.seed_v2:main"' in pyproject


def test_main_runs_end_to_end_and_returns_zero(tmp_path, monkeypatch, capsys):
    """The real entry point, against a real (empty) database, twice."""
    import noc_agents.db.models as models

    saved = (models._engine, models.SessionLocal)
    db = tmp_path / "cli.db"
    argv = ["--database-url", f"sqlite:///{db.as_posix()}"]
    try:
        assert seed_v2.main(argv) == 0
        assert seed_v2.main(argv) == 0
    finally:
        models._engine, models.SessionLocal = saved

    out = capsys.readouterr().out
    assert "noc-seed-v2" in out
    assert "no such table yet" in out


def test_main_reports_a_bad_database_url_without_a_traceback(tmp_path, capsys):
    import noc_agents.db.models as models

    saved = (models._engine, models.SessionLocal)
    try:
        code = seed_v2.main(["--database-url", "not-a-url://nowhere"])
    finally:
        models._engine, models.SessionLocal = saved

    assert code == 1
    assert "could not open the database" in capsys.readouterr().out
