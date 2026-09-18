"""parse_structured: exact SDK call shape, fable→opus fallback, refusal/timeout/malformed → None."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from noc_agents.llm.client import MODEL_DRAFTING, MODEL_FALLBACK, MODEL_REASONING
from noc_agents.llm.outputs import ExecBriefDraft
from noc_agents.llm.structured import BETAS, FALLBACKS, is_fallback_error, parse_structured


def _ok_response(model: str, body: str = "draft"):
    return SimpleNamespace(
        parsed_output=ExecBriefDraft(body=body),
        stop_reason="end_turn",
        model=model,
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
    )


class FakeClient:
    """Scripted `beta.messages.parse`: each entry is a response, or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _call(client, model=MODEL_REASONING, effort="high"):
    return parse_structured(
        client, model=model, system="sys", user="{}", output_model=ExecBriefDraft, effort=effort, max_tokens=512
    )


def test_success_sends_the_documented_call_shape(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_S", "7")
    client = FakeClient(_ok_response(MODEL_REASONING))
    parsed, rec = _call(client)
    assert isinstance(parsed, ExecBriefDraft) and parsed.body == "draft"
    assert rec.ok and not rec.refused and not rec.fallback_used and rec.error is None
    assert rec.model_used == MODEL_REASONING and rec.input_tokens == 10 and rec.output_tokens == 20
    kw = client.calls[0]
    assert kw["model"] == MODEL_REASONING
    assert kw["output_format"] is ExecBriefDraft
    assert kw["output_config"] == {"effort": "high"}
    assert kw["betas"] == BETAS == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == FALLBACKS == "default"
    assert kw["timeout"] == 7.0
    assert kw["system"] == "sys" and kw["messages"] == [{"role": "user", "content": "{}"}]
    assert kw["max_tokens"] == 512
    for forbidden in ("thinking", "temperature", "top_p", "top_k", "tool_choice", "tools"):
        assert forbidden not in kw


def test_refusal_on_fable_falls_back_to_opus():
    refusal = SimpleNamespace(parsed_output=None, stop_reason="refusal", model=MODEL_REASONING, usage=None)
    client = FakeClient(refusal, _ok_response(MODEL_FALLBACK))
    parsed, rec = _call(client)
    assert parsed is not None and rec.ok and rec.fallback_used
    assert [c["model"] for c in client.calls] == [MODEL_REASONING, MODEL_FALLBACK]
    assert rec.model_used == MODEL_FALLBACK


def test_refusal_on_both_models_is_recorded_as_refused():
    refusal = SimpleNamespace(parsed_output=None, stop_reason="refusal", model="x", usage=None)
    client = FakeClient(refusal, refusal)
    parsed, rec = _call(client)
    assert parsed is None and not rec.ok and rec.refused and rec.fallback_used and rec.error == "refusal"


@pytest.mark.parametrize("name", ["APITimeoutError", "RateLimitError", "OverloadedError", "APIConnectionError", "InternalServerError"])
def test_api_errors_on_fable_retry_on_opus(name):
    exc_cls = type(name, (Exception,), {})
    client = FakeClient(exc_cls("boom"), _ok_response(MODEL_FALLBACK))
    parsed, rec = _call(client)
    assert parsed is not None and rec.ok and rec.fallback_used
    assert [c["model"] for c in client.calls] == [MODEL_REASONING, MODEL_FALLBACK]


def test_subclass_of_sdk_error_is_recognised_via_mro():
    APIStatusError = type("APIStatusError", (Exception,), {})
    Custom = type("SomethingElse", (APIStatusError,), {})
    assert is_fallback_error(Custom("x"))
    AuthenticationError = type("AuthenticationError", (APIStatusError,), {})
    assert not is_fallback_error(AuthenticationError("bad key"))
    assert not is_fallback_error(ValueError("nope"))


def test_api_error_on_both_models_returns_none_without_raising():
    exc_cls = type("APITimeoutError", (Exception,), {})
    client = FakeClient(exc_cls("slow"), exc_cls("slow again"))
    parsed, rec = _call(client)
    assert parsed is None and not rec.ok and rec.fallback_used
    assert rec.error.startswith("APITimeoutError")
    assert rec.models_tried == [MODEL_REASONING, MODEL_FALLBACK]


def test_malformed_output_does_not_fall_back():
    ValidationError = type("ValidationError", (ValueError,), {})  # what parse() raises on non-JSON text
    client = FakeClient(ValidationError("not json"))
    parsed, rec = _call(client)
    assert parsed is None and not rec.ok and not rec.fallback_used
    assert len(client.calls) == 1 and rec.error.startswith("ValidationError")


def test_auth_error_does_not_fall_back():
    AuthenticationError = type("AuthenticationError", (Exception,), {})
    client = FakeClient(AuthenticationError("401"))
    parsed, rec = _call(client)
    assert parsed is None and not rec.fallback_used and len(client.calls) == 1


def test_drafting_model_never_falls_back():
    exc_cls = type("RateLimitError", (Exception,), {})
    client = FakeClient(exc_cls("429"))
    parsed, rec = _call(client, model=MODEL_DRAFTING, effort="low")
    assert parsed is None and not rec.fallback_used and len(client.calls) == 1


def test_missing_parsed_output_is_unusable():
    client = FakeClient(SimpleNamespace(parsed_output=None, stop_reason="max_tokens", model=MODEL_REASONING, usage=None))
    parsed, rec = _call(client)
    assert parsed is None and not rec.ok and not rec.fallback_used and "max_tokens" in rec.error


def test_refusal_on_fable_stays_in_the_record_after_opus_succeeds():
    refusal = SimpleNamespace(parsed_output=None, stop_reason="refusal", model=MODEL_REASONING, usage=None)
    client = FakeClient(refusal, _ok_response(MODEL_FALLBACK))
    parsed, rec = _call(client)
    assert parsed is not None and rec.ok and rec.fallback_used
    assert rec.refused is True and rec.error is None  # the DPA record keeps the observed refusal


@pytest.mark.parametrize("name", ["BadRequestError", "UnprocessableEntityError"])
def test_malformed_request_errors_do_not_fall_back(name):
    APIStatusError = type("APIStatusError", (Exception,), {})
    exc_cls = type(name, (APIStatusError,), {})  # same MRO shape as the SDK
    assert not is_fallback_error(exc_cls("400"))
    client = FakeClient(exc_cls("400"))
    parsed, rec = _call(client)
    assert parsed is None and not rec.fallback_used and len(client.calls) == 1


def test_not_found_error_still_falls_back():
    APIStatusError = type("APIStatusError", (Exception,), {})
    NotFoundError = type("NotFoundError", (APIStatusError,), {})
    client = FakeClient(NotFoundError("no such model"), _ok_response(MODEL_FALLBACK))
    parsed, rec = _call(client)
    assert parsed is not None and rec.fallback_used


def test_model_used_names_the_last_model_attempted_and_audit_lists_models_tried():
    refusal = SimpleNamespace(parsed_output=None, stop_reason="refusal", model=MODEL_REASONING, usage=None)
    exc_cls = type("APITimeoutError", (Exception,), {})
    client = FakeClient(refusal, exc_cls("slow"))
    parsed, rec = _call(client)
    assert parsed is None and rec.refused and rec.fallback_used
    assert rec.model_used == MODEL_FALLBACK  # the model that failed last, not the one that refused
    assert rec.as_dict()["models_tried"] == [MODEL_REASONING, MODEL_FALLBACK]


def test_pydantic_validation_error_is_recorded_without_model_text():
    """parse() raises pydantic.ValidationError with the reply in input_value=...; the audit row keeps only field paths."""
    from pydantic import TypeAdapter, ValidationError

    from noc_agents.llm.structured import describe_error

    with pytest.raises(ValidationError) as info:
        TypeAdapter(ExecBriefDraft).validate_json('{"body": 12345}')
    assert "12345" in str(info.value)  # the raw message embeds the reply (input_value=12345)...
    hint = describe_error(info.value)
    assert hint == "ValidationError: 1 validation error(s) at body" and "12345" not in hint  # ...the hint does not

    class Leaky(ValueError):
        def errors(self):
            return [{"loc": ("hypotheses", 0, "cause"), "input": "SECRET-REPLY-TEXT"}]

    client = FakeClient(Leaky("SECRET-REPLY-TEXT"))
    parsed, rec = _call(client)
    assert parsed is None and rec.error == "Leaky: 1 validation error(s) at hypotheses.0.cause"
    assert "SECRET" not in rec.error


def test_status_errors_keep_only_class_and_status_code():
    from noc_agents.llm.structured import describe_error

    err = type("RateLimitError", (Exception,), {"status_code": 429})("Error code: 429 - {'message': 'slow down'}")
    assert describe_error(err) == "RateLimitError (status 429)"
    assert describe_error(type("APITimeoutError", (Exception,), {})("Request timed out.")) == "APITimeoutError: Request timed out."


def test_record_keeps_the_budget_and_the_timeout_override(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_S", "7")
    client = FakeClient(_ok_response(MODEL_REASONING))
    _, rec = _call(client)
    assert rec.as_dict()["effort"] == "high" and rec.as_dict()["max_tokens"] == 512 and rec.as_dict()["timeout_s"] == 7.0
    client2 = FakeClient(_ok_response(MODEL_REASONING))
    _, rec2 = parse_structured(client2, model=MODEL_REASONING, system="s", user="{}", output_model=ExecBriefDraft, timeout=45.0)
    assert client2.calls[0]["timeout"] == 45.0 and rec2.timeout_s == 45.0
