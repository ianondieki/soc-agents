"""The transfer register: every byte that leaves the machine leaves a row behind.

Four behaviours the Data Protection Act 2019 work hangs on, plus the seam's own hygiene:

* a Kenya-domiciled recipient (Africa's Talking, a local Ollama) is recorded with
  ``recipient_country="KE"`` and never trips the cross-border gate;
* a recipient abroad WITH a filed DPIA + TIA is recorded;
* a recipient abroad WITHOUT them raises under ``NOC_ENV=production`` and writes NOTHING
  (nothing left the machine, so there is nothing to record);
* the same case under ``NOC_ENV=demo`` records ``tia_ref="DEMO-UNFILED"`` instead of
  raising, so the demo register shows the gap rather than reading as clean;
* no credential the process holds can reach the audit payload.

Nothing here touches the network: ``record_transfer`` is the record-keeping seam, not the
call itself.
"""

from __future__ import annotations

import json
import logging

import pytest
import yaml
from sqlalchemy import func, select

from noc_agents.db.models import AuditRow
from noc_agents.services import external_calls as ec
from noc_agents.services.external_calls import (
    DEMO_UNFILED,
    PAPERWORK_FIELDS,
    STATUS_DEMO_UNFILED,
    STATUS_FILED,
    STATUS_NOT_REQUIRED,
    TransferPaperworkMissing,
    record_transfer,
)

LOGGER = "noc_agents.services.external_calls"

FILED = {
    "entity": "Anthropic PBC",
    "country": "US",
    "dpia_ref": "DPIA-SFC-2026-004",
    "tia_ref": "TIA-SFC-2026-004",
    "scc_ref": "ODPC-SCC-2026-0041",
    "confirmed_by": "DPO Office",
    "confirmed_at": "2026-05-04",
}


@pytest.fixture()
def register(tmp_path, monkeypatch):
    """Point the module at a throwaway ``config/operators/<op>/transfers.yaml``."""

    def _write(entries: dict[str, dict] | None = None, operator_id: str = "safaricom"):
        monkeypatch.setattr(ec, "OPERATORS_DIR", tmp_path)
        folder = tmp_path / operator_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transfers.yaml").write_text(yaml.safe_dump(entries or {}), encoding="utf-8")
        return folder / "transfers.yaml"

    return _write


def _audit_count(session) -> int:
    return session.scalar(select(func.count()).select_from(AuditRow)) or 0


def _payload(row: AuditRow) -> dict:
    return json.loads(row.payload_json)


def _record(session, settings, **overrides) -> AuditRow:
    kwargs = dict(
        recipient="Anthropic API",
        recipient_country="US",
        justification="root-cause analysis draft to speed restoration",
        data_description="redacted incident fields + scrubbed notes",
        actor="TicketingAgent",
        actor_role="agent",
        incident_id="inc-1",
        residency="abroad",
        settings=settings,
    )
    kwargs.update(overrides)
    return record_transfer(session, **kwargs)


# --------------------------------------------------------------------------- Kenya


def test_kenyan_recipient_is_recorded_without_tripping_the_gate(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({})  # nothing on file at all: a KE recipient still goes through

    row = _record(
        session,
        settings,
        recipient="Africa's Talking",
        recipient_country="KE",
        residency="kenya",
        justification="broadcast SMS to the field engineer on call",
        data_description="incident number, priority, site id, one-line impact",
        actor="BroadcastAgent",
    )

    assert row.id  # usable as envelope.governance.transfer_record_id
    assert row.action == "external.call"
    assert row.operator_id == settings.operator.operator_id
    payload = _payload(row)
    assert payload["recipient_country"] == "KE"
    assert payload["cross_border"] is False
    assert payload["paperwork_status"] == STATUS_NOT_REQUIRED
    # reg 41(2) fields, all present
    for key in ("ts", "recipient", "recipient_country", "justification", "data_description", "residency"):
        assert payload[key], key
    assert _audit_count(session) == 1


def test_local_ollama_is_recorded_as_kenya(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({})

    row = _record(session, settings, recipient="Ollama local", recipient_country="KE", residency="local")

    payload = _payload(row)
    assert payload["residency"] == "local"
    assert payload["cross_border"] is False


def test_kenyan_country_with_abroad_residency_is_treated_as_cross_border(tmp_db, register, monkeypatch):
    """A card that says ``abroad`` wins over a hopeful ``KE``: the strict reading."""
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({})

    with pytest.raises(TransferPaperworkMissing):
        _record(session, settings, recipient="Some SaaS", recipient_country="KE", residency="abroad")


# --------------------------------------------------------------------------- abroad, filed


def test_abroad_with_paperwork_is_recorded(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"anthropic_api": FILED})

    row = _record(session, settings)

    payload = _payload(row)
    assert payload["cross_border"] is True
    assert payload["paperwork_status"] == STATUS_FILED
    assert payload["recipient_key"] == "anthropic_api"
    assert payload["dpia_ref"] == FILED["dpia_ref"]
    assert payload["tia_ref"] == FILED["tia_ref"]
    assert payload["scc_ref"] == FILED["scc_ref"]
    assert payload["entity"] == FILED["entity"]
    assert payload["entity_country"] == "US"
    assert payload["confirmed_by"] == FILED["confirmed_by"]
    assert row.entity_type == "incident" and row.entity_id == "inc-1"
    assert _audit_count(session) == 1


def test_recipient_is_matched_by_entity_name_too(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"llm_primary": FILED})

    row = _record(session, settings, recipient="Anthropic PBC")

    assert _payload(row)["recipient_key"] == "llm_primary"


def test_row_without_incident_is_keyed_on_the_recipient(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"anthropic_api": FILED})

    row = _record(session, settings, incident_id=None)

    assert row.entity_type == "external_call"
    assert row.entity_id == "anthropic_api"
    assert _payload(row)["incident_id"] is None


# --------------------------------------------------------------------------- abroad, unfiled


def test_abroad_without_paperwork_raises_in_production_and_writes_nothing(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"anthropic_api": {**FILED, "dpia_ref": "", "tia_ref": ""}})

    with pytest.raises(TransferPaperworkMissing) as excinfo:
        _record(session, settings)

    assert excinfo.value.missing == ("dpia_ref", "tia_ref")
    assert excinfo.value.recipient_key == "anthropic_api"
    assert _audit_count(session) == 0  # nothing left the machine, so nothing is recorded


def test_missing_register_file_refuses_cross_border_in_production(tmp_db, tmp_path, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    monkeypatch.setattr(ec, "OPERATORS_DIR", tmp_path / "does-not-exist")

    with pytest.raises(TransferPaperworkMissing):
        _record(session, settings)
    assert _audit_count(session) == 0


def test_half_filed_paperwork_still_refuses(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"anthropic_api": {**FILED, "tia_ref": ""}})

    with pytest.raises(TransferPaperworkMissing) as excinfo:
        _record(session, settings)

    assert excinfo.value.missing == ("tia_ref",)


def test_unset_noc_env_reads_as_production(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.delenv("NOC_ENV", raising=False)
    register({})

    with pytest.raises(TransferPaperworkMissing):
        _record(session, settings)


# --------------------------------------------------------------------------- demo


def test_demo_records_unfiled_instead_of_raising(tmp_db, register, monkeypatch, caplog):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    register({"anthropic_api": {**FILED, "dpia_ref": "", "tia_ref": ""}})

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        row = _record(session, settings)

    payload = _payload(row)
    assert payload["tia_ref"] == DEMO_UNFILED
    assert payload["dpia_ref"] == DEMO_UNFILED
    assert payload["paperwork_status"] == STATUS_DEMO_UNFILED
    assert payload["cross_border"] is True
    assert payload["env"] == "demo"
    assert _audit_count(session) == 1
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1  # one line, not a wall of noise
    assert DEMO_UNFILED in warnings[0].getMessage()


def test_demo_keeps_the_refs_that_are_filed(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    register({"anthropic_api": {**FILED, "tia_ref": ""}})

    payload = _payload(_record(session, settings))

    assert payload["dpia_ref"] == FILED["dpia_ref"]  # a real filing is never overwritten
    assert payload["tia_ref"] == DEMO_UNFILED


def test_demo_does_not_relabel_a_fully_filed_recipient(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    register({"anthropic_api": FILED})

    assert _payload(_record(session, settings))["paperwork_status"] == STATUS_FILED


# --------------------------------------------------------------------------- hygiene


def test_no_secret_or_credential_reaches_the_audit_payload(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-ZZZtopsecretkeyvalue123456")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "hunter2-app-password-value")
    monkeypatch.setenv("SMTP_PASSWORD", "another-secret-password")
    register({"anthropic_api": FILED})

    row = _record(
        session,
        settings,
        justification=(
            "retry after auth failure with ANTHROPIC_API_KEY=sk-ant-api03-ZZZtopsecretkeyvalue123456 "
            "and Authorization: Bearer abcdef1234567890abcdef"
        ),
        data_description=(
            "incident fields; relay used password=hunter2-app-password-value and "
            "another-secret-password; contact noc.duty@example.com on +254712345678"
        ),
        actor_role="supervisor (token: another-secret-password)",
    )

    written = row.payload_json + "|" + row.rationale + "|" + row.actor
    for secret in (
        "sk-ant-api03-ZZZtopsecretkeyvalue123456",
        "hunter2-app-password-value",
        "another-secret-password",
        "abcdef1234567890abcdef",
    ):
        assert secret not in written, secret
    assert "noc.duty@example.com" not in written  # shared scrubber: e-mails
    assert "+254712345678" not in written  # shared scrubber: Kenyan MSISDNs
    assert "<REDACTED>" in row.payload_json
    assert _payload(row)["paperwork_status"] == STATUS_FILED  # still a usable record


def test_payload_stays_valid_json_within_the_cap(tmp_db, register, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"anthropic_api": FILED})

    row = _record(session, settings, justification="why " * 900, data_description="what " * 900)

    assert len(row.payload_json) <= ec.MAX_PAYLOAD_CHARS
    payload = json.loads(row.payload_json)  # never truncated into garbage
    assert payload["recipient_key"] == "anthropic_api"
    assert payload["tia_ref"] == FILED["tia_ref"]  # register fields survive the trim
    assert len(row.actor) <= 128  # AuditRow.actor column width


def test_redaction_shim_is_the_same_implementation():
    from noc_agents.llm import redaction as llm_redaction
    from noc_agents.services import redaction as shared

    for name in ("scrub_text", "scrub_contacts", "redact_incident", "restore_names", "NameMap"):
        assert getattr(shared, name) is getattr(llm_redaction, name), name
    assert shared.ALLOWLIST is llm_redaction.ALLOWLIST


def test_shipped_register_parses_and_is_shaped_as_documented():
    """The checked-in registers are honest: KE entries present, refs empty until filed."""
    for operator_id in ("safaricom", "airtel"):
        entries = ec.load_transfers(operator_id)
        assert entries, operator_id
        for key, entry in entries.items():
            assert set(PAPERWORK_FIELDS) <= set(entry), (operator_id, key)
        assert entries["africas_talking"]["country"] == "KE"
        # the name an adapter passes keys straight onto the register entry
        assert ec.normalise_key("Africa's Talking") in entries
        assert ec.normalise_key("Anthropic API") in entries
        assert entries["ollama_local"]["country"] == "KE"
        assert entries["anthropic_api"]["country"] != "KE"
