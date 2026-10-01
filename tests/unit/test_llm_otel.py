"""CONFORMANCE C-22: the OpenTelemetry GenAI attribute names (spec §10.4).

§10.4 asks for "OpenTelemetry GenAI span attribute names on steps and ``llm_calls``
(``gen_ai.request.model``, ``gen_ai.usage.input_tokens/output_tokens``, ``error.type``) —
attribute names only; exporter only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; message
content never recorded", and ``OTEL_EXPORTER_OTLP_ENDPOINT`` was documented in ``.env.example``
and read by nothing.

No OpenTelemetry SDK is installed and none may be added, so what is pinned here is the naming
seam, not a tracing backend:

* the four names the spec lists verbatim are produced, from the ``LlmCallRecord`` the system
  already has, by ONE function;
* ``error.type`` is a low-cardinality token and never the SDK's message, so a 401 whose text
  echoes the API key cannot reach a span attribute (§9.5);
* NO message content: no prompt, no answer, no name of an attribute that could hold one;
* every ``llm_calls`` row emits exactly one such log line, because ``port.record_llm_call`` is
  the single writer and the single emitter — so the assist routes (CONFORMANCE A-15), the
  outbox ``LLM_CALL`` transmitter and ``services/contracts`` are all covered by construction;
* setting ``OTEL_EXPORTER_OTLP_ENDPOINT`` no longer does nothing in silence: it is read, and
  the code says plainly, once per endpoint and at WARNING, that nothing will be exported.

The names in ``GEN_AI_KEYS`` beyond the four the spec quotes could NOT be verified against the
upstream semantic-convention document (no network on the build machine); they are listed in one
place in ``llm/otel.py`` so that correcting one is a one-line change.
"""

from __future__ import annotations

import json
import logging

import pytest

from noc_agents.llm import otel
from noc_agents.llm.client import MODEL_FALLBACK, MODEL_REASONING
from noc_agents.llm.structured import LlmCallRecord

SECRET = "sk-ant-api03-not-a-real-key-0123456789"

#: The four names §10.4 quotes verbatim. These are the ones this repo can claim to have
#: verified: they come from the spec text itself.
SPEC_NAMES = ("gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "error.type")


@pytest.fixture(autouse=True)
def _clean_otel_env(monkeypatch):
    monkeypatch.delenv(otel.ENDPOINT_ENV, raising=False)
    otel.reset_exporter_warning()
    yield
    otel.reset_exporter_warning()


def _ok_record():
    rec = LlmCallRecord(model_requested=MODEL_REASONING, effort="medium", max_tokens=8192, timeout_s=60.0)
    rec.models_tried = [MODEL_REASONING]
    rec.model_used = MODEL_REASONING
    rec.ok = True
    rec.input_tokens, rec.output_tokens, rec.latency_ms = 1200, 340, 4521
    return rec


class _Row:
    """The fields ``call_attributes`` reads off an ``llm_calls`` row, without a database."""

    id = "call-1"
    operator_id = "safaricom"
    incident_id = "inc-1"
    run_id = "run-1"
    audit_id = "audit-1"
    est_cost_usd = 0.0289
    fallback_reason = None
    cache_read_tokens = None
    validated = 1


# ------------------------------------------------------------------ the names themselves


def test_the_four_names_the_spec_lists_are_produced_from_the_call_record():
    attrs = otel.gen_ai_attributes(_ok_record(), provider="anthropic", agent="TicketingAgent")
    assert attrs["gen_ai.request.model"] == MODEL_REASONING
    assert attrs["gen_ai.usage.input_tokens"] == 1200
    assert attrs["gen_ai.usage.output_tokens"] == 340
    assert "error.type" not in attrs  # a successful call has no error type
    assert attrs["gen_ai.response.model"] == MODEL_REASONING
    assert attrs["gen_ai.operation.name"] == otel.OPERATION_CHAT
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["gen_ai.request.max_tokens"] == 8192
    assert attrs["gen_ai.agent.name"] == "TicketingAgent"


def test_every_gen_ai_key_emitted_is_declared_in_one_place():
    """``GEN_AI_KEYS`` is the list a future exporter reads and the list a reviewer checks against
    upstream. A key that is emitted but not declared would escape both."""
    rec = _ok_record()
    rec.ok, rec.refused = False, True
    emitted = set(otel.gen_ai_attributes(rec, provider="anthropic", agent="A")) | set(
        otel.call_attributes(rec, provider="anthropic", agent="A", purpose="p", row=_Row())
    )
    convention = {key for key in emitted if key.startswith("gen_ai.")}
    assert convention <= set(otel.GEN_AI_KEYS)
    assert set(SPEC_NAMES) - {"error.type"} <= set(otel.GEN_AI_KEYS)
    # Everything else is explicitly OURS, so nothing can be mistaken for semantic convention.
    assert {key for key in emitted if not key.startswith(("gen_ai.", "error.", "noc."))} == set()


def test_the_local_namespace_carries_the_llm_calls_row_fields():
    attrs = otel.call_attributes(
        _ok_record(), provider="anthropic", agent="TicketingAgent", purpose="incident_analysis", row=_Row()
    )
    assert attrs["noc.llm.purpose"] == "incident_analysis"
    assert attrs["noc.llm.call_id"] == "call-1" and attrs["noc.audit_id"] == "audit-1"
    assert attrs["noc.incident_id"] == "inc-1" and attrs["noc.run_id"] == "run-1"
    assert attrs["noc.llm.est_cost_usd"] == pytest.approx(0.0289)
    assert attrs["noc.llm.latency_ms"] == 4521 and attrs["noc.llm.validated"] is True
    assert attrs["noc.llm.fallback_used"] is False
    assert "noc.llm.fallback_reason" not in attrs  # unknown is omitted, not emitted as null


def test_a_fallback_and_a_refusal_are_visible_in_the_attributes():
    rec = LlmCallRecord(model_requested=MODEL_REASONING)
    rec.models_tried = [MODEL_REASONING, MODEL_FALLBACK]
    rec.model_used = MODEL_FALLBACK
    rec.fallback_used, rec.refused, rec.ok = True, True, False
    rec.error = "refusal"
    attrs = otel.call_attributes(rec, provider="anthropic", agent="A", purpose="p")
    assert attrs["gen_ai.response.finish_reasons"] == ["refusal"]
    assert attrs["error.type"] == "refusal"
    assert attrs["noc.llm.fallback_used"] is True
    assert attrs["noc.llm.models_tried"] == [MODEL_REASONING, MODEL_FALLBACK]


def test_the_openai_compatible_provider_maps_to_the_convention_value():
    attrs = otel.gen_ai_attributes(_ok_record(), provider="openai_compat")
    assert attrs["gen_ai.provider.name"] == "openai"
    # An id nobody mapped is passed through, never guessed at.
    assert otel.gen_ai_attributes(_ok_record(), provider="nimbus")["gen_ai.provider.name"] == "nimbus"


# ------------------------------------------------------------------ error.type is a label


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (f"AuthenticationError: invalid x-api-key {SECRET}", "AuthenticationError"),
        ("APITimeoutError: Request timed out.", "APITimeoutError"),
        ("RateLimitError (status 429)", "RateLimitError"),
        ("ValidationError: 2 validation error(s) at body, summary", "ValidationError"),
        ("no parsed output (stop_reason='max_tokens')", "no parsed output"),
        ("output failed validation (length / INC number / priority)", "output failed validation"),
        ("", otel.ERROR_TYPE_UNUSABLE),
        (None, otel.ERROR_TYPE_UNUSABLE),
    ],
)
def test_error_type_is_the_leading_token_only(error, expected):
    rec = LlmCallRecord(model_requested=MODEL_REASONING)
    rec.ok = False
    rec.error = error
    assert otel.error_type(rec) == expected


def test_error_type_is_none_for_a_call_that_worked():
    assert otel.error_type(_ok_record()) is None


def test_error_type_is_bounded_even_for_an_unbounded_message():
    rec = LlmCallRecord(model_requested=MODEL_REASONING)
    rec.ok = False
    rec.error = "X" * 5000
    assert len(otel.error_type(rec)) == 64


def test_no_attribute_can_carry_a_credential_or_model_text():
    """The worst record a failing adapter could hand over: the raw key inside ``rec.error``."""
    rec = LlmCallRecord(model_requested=MODEL_REASONING)
    rec.ok = False
    rec.error = f"AuthenticationError: invalid x-api-key {SECRET}"
    rec.input_tokens = 120
    blob = json.dumps(otel.call_attributes(rec, provider="anthropic", agent="A", purpose="p", row=_Row()))
    assert SECRET not in blob and "sk-ant" not in blob
    assert "AuthenticationError" in blob  # the class survives; the message does not
    # No attribute NAME suggests a place message content could go, either (§10.4).
    keys = " ".join(otel.GEN_AI_KEYS)
    assert not any(word in keys for word in ("prompt", "completion", "content", "message", "text"))


# ------------------------------------------------------------------ the endpoint is read


def test_with_no_endpoint_nothing_is_warned_about():
    state = otel.exporter_state()
    assert state == {"endpoint": None, "sdk_installed": state["sdk_installed"], "exporting": False,
                     "reason": otel.REASON_NO_ENDPOINT}
    assert state["sdk_installed"] is False, "an OTel SDK appeared; C-22's premise and .env.example need revisiting"


def test_a_configured_endpoint_reports_exactly_why_it_exports_nothing(monkeypatch):
    monkeypatch.setenv(otel.ENDPOINT_ENV, "http://collector.internal:4318")
    state = otel.exporter_state()
    assert state["endpoint"] == "http://collector.internal:4318"
    assert state["exporting"] is False and state["reason"] == otel.REASON_NO_SDK


def test_a_blank_endpoint_reads_as_unset(monkeypatch):
    monkeypatch.setenv(otel.ENDPOINT_ENV, "   ")
    assert otel.otlp_endpoint() is None
    assert otel.exporter_state()["reason"] == otel.REASON_NO_ENDPOINT


def test_a_configured_endpoint_warns_once_per_endpoint(monkeypatch, caplog):
    monkeypatch.setenv(otel.ENDPOINT_ENV, "http://collector.internal:4318")
    attrs = otel.gen_ai_attributes(_ok_record(), provider="anthropic")
    with caplog.at_level(logging.WARNING, logger="noc_agents.llm.otel"):
        otel.log_gen_ai_call(attrs)
        otel.log_gen_ai_call(attrs)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "the operator is told once per endpoint, not once per model call"
    text = warnings[0].getMessage()
    assert otel.ENDPOINT_ENV in text and "http://collector.internal:4318" in text
    assert otel.REASON_NO_SDK in text and "nothing is exported" in text

    monkeypatch.setenv(otel.ENDPOINT_ENV, "http://other:4318")  # a different endpoint is a new fact
    with caplog.at_level(logging.WARNING, logger="noc_agents.llm.otel"):
        otel.log_gen_ai_call(attrs)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_no_endpoint_means_no_warning_at_all(caplog):
    with caplog.at_level(logging.DEBUG, logger="noc_agents.llm.otel"):
        otel.log_gen_ai_call(otel.gen_ai_attributes(_ok_record(), provider="anthropic"))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ------------------------------------------------------------------ the log line


def test_the_log_line_carries_the_attributes_and_no_content(caplog):
    attrs = otel.call_attributes(
        _ok_record(), provider="anthropic", agent="TicketingAgent", purpose="incident_analysis", row=_Row()
    )
    with caplog.at_level(logging.INFO, logger="noc_agents.llm.otel"):
        assert otel.log_gen_ai_call(attrs) is attrs
    (record,) = [r for r in caplog.records if r.name == "noc_agents.llm.otel"]
    assert getattr(record, "gen_ai") == attrs  # a JSON handler gets the whole mapping in one field
    rendered = record.getMessage()
    assert rendered.startswith("gen_ai.call ")
    for name in SPEC_NAMES[:3]:  # error.type is absent on a successful call
        assert f"{name}=" in rendered


def test_every_llm_calls_row_emits_exactly_one_gen_ai_line(tmp_db, caplog):
    """The seam, at the one place it is wired: ``port.record_llm_call``. Every path that pays
    into ``llm_calls`` — the outbox ``LLM_CALL`` transmitter, ``services/contracts`` and (since
    CONFORMANCE A-15) the on-demand assist routes — is therefore covered by construction, and
    none of them can be visible in the register but invisible in the attributes."""
    from noc_agents.llm.port import record_llm_call

    settings, session = tmp_db
    rec = _ok_record()
    with caplog.at_level(logging.INFO, logger="noc_agents.llm.otel"):
        row = record_llm_call(
            session,
            operator_id=settings.operator.operator_id,
            agent="TicketingAgent",
            purpose="incident_analysis",
            provider="anthropic",
            rec=rec,
            audit_id="audit-1",
            run_id="run-1",
            incident_id="inc-1",
            validated=True,
        )
        session.commit()

    (line,) = [r for r in caplog.records if r.name == "noc_agents.llm.otel"]
    attrs = line.gen_ai
    assert attrs["gen_ai.request.model"] == rec.model_requested
    assert attrs["gen_ai.usage.input_tokens"] == row.input_tokens
    assert attrs["gen_ai.usage.output_tokens"] == row.output_tokens
    assert attrs["noc.llm.call_id"] == row.id and attrs["noc.audit_id"] == "audit-1"
    assert attrs["noc.llm.purpose"] == "incident_analysis"
    assert attrs["noc.llm.est_cost_usd"] == row.est_cost_usd
    assert attrs["noc.incident_id"] == "inc-1" and attrs["noc.run_id"] == "run-1"


def test_a_broken_attribute_value_cannot_fail_a_call_that_already_happened(caplog):
    class Exploding:
        def __repr__(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.INFO, logger="noc_agents.llm.otel"):
        assert otel.log_gen_ai_call({"gen_ai.request.model": Exploding()}) is not None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
