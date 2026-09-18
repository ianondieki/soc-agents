"""The provider-neutral LLM port (§7.0.9): provider selection, the G13 subscription guard,
the spend circuit, config-driven prices and both adapters — with FAKE adapters only.

No test here opens a socket, reads a real credential or imports the ``anthropic`` package:
the SDK is faked through ``sys.modules`` and the OpenAI-compatible transport is injected.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from noc_agents.llm import client as llm_client
from noc_agents.llm.anthropic_adapter import AnthropicAdapter
from noc_agents.llm.client import (
    FALLBACK_REASON_BUDGET,
    FALLBACK_REASON_SPEND_CAP,
    LEGAL_URL,
    MODEL_DRAFTING,
    MODEL_FALLBACK,
    MODEL_REASONING,
    SubscriptionGuardError,
)
from noc_agents.llm.openai_compat_adapter import OpenAiCompatAdapter
from noc_agents.llm.outputs import ExecBriefDraft, Hypothesis, RootCauseAnalysis
from noc_agents.llm.port import (
    CITE_UNSUPPORTED,
    CitedDocument,
    LlmPort,
    record_llm_call,
)

KEY = "sk-ant-api03-console-key-value"
TOKEN = "sk-ant-oat01-subscription-token-value"


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic``; records exactly what it was constructed with."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        FakeAnthropic.last_kwargs = kwargs


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch, tmp_path):
    """Every test starts with: layer off, no credentials, no OAuth profile, circuit closed."""
    for name in (
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "LLM_ALLOW_AUTH_TOKEN",
        "LLM_ZDR_CONFIRMED",
        "LLM_MONTHLY_BUDGET_USD",
        "LLM_PRICES_PATH",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_MODEL",
        "OPENAI_COMPAT_API_KEY",
        "LLM_TIMEOUT_S",
        "LLM_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    # A real ~/.config/anthropic on the developer's machine must not decide a test.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    FakeAnthropic.last_kwargs = None
    llm_client.reset_spend_cap()
    llm_client._CLIENT_CACHE.clear()
    yield
    llm_client.reset_spend_cap()
    llm_client._CLIENT_CACHE.clear()


def _fake_sdk(monkeypatch, cls=FakeAnthropic):
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=cls))


def _oauth_profile(monkeypatch, tmp_path):
    """Create a Claude Code-shaped OAuth profile on disk. Its CONTENT is never read."""
    folder = tmp_path / "xdg" / "anthropic"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".credentials.json").write_text('{"access_token": "oauth-token-do-not-use"}', encoding="utf-8")
    return folder


# ============================================================ subscription guard (G13)


def test_case_1_console_api_key_is_the_licensed_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert llm_client.resolve_anthropic_credential() == ("api_key", KEY)
    assert llm_client.credential_present() is True
    assert llm_client.credential_kind() == "api_key"
    assert llm_client.guard_refusal() is None


def test_case_1_client_is_built_with_an_explicit_api_key(monkeypatch):
    """G13: the credential is passed explicitly so the SDK cannot pick another one up."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch)
    client = llm_client.build_anthropic_client()
    assert isinstance(client, FakeAnthropic)
    assert FakeAnthropic.last_kwargs == {"api_key": KEY, "timeout": 20.0, "max_retries": 1}
    assert "auth_token" not in (FakeAnthropic.last_kwargs or {})


def test_case_2_auth_token_is_honoured_only_with_the_flag(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("LLM_ALLOW_AUTH_TOKEN", "true")
    monkeypatch.setenv("LLM_ENABLED", "true")
    _fake_sdk(monkeypatch)
    assert llm_client.resolve_anthropic_credential() == ("auth_token", TOKEN)
    assert llm_client.credential_kind() == "auth_token"
    assert llm_client.build_anthropic_client() is not None
    assert FakeAnthropic.last_kwargs == {"auth_token": TOKEN, "timeout": 20.0, "max_retries": 1}


def test_case_3_auth_token_without_the_flag_is_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("LLM_ENABLED", "true")
    _fake_sdk(monkeypatch)
    with pytest.raises(SubscriptionGuardError) as exc:
        llm_client.resolve_anthropic_credential()
    message = str(exc.value)
    assert "\n" not in message and LEGAL_URL in message and "LLM_ALLOW_AUTH_TOKEN" in message
    assert TOKEN not in message
    assert llm_client.credential_present() is False
    assert llm_client.credential_kind() is None
    assert llm_client.guard_refusal() == message
    assert llm_client.build_anthropic_client() is None  # never raises out of the factory
    assert llm_client.get_llm() is None and llm_client.get_llm_port() is None
    assert FakeAnthropic.last_kwargs is None  # no client was constructed from the token


def test_case_4_oauth_profile_on_disk_is_refused_never_used(monkeypatch, tmp_path):
    folder = _oauth_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_ENABLED", "true")
    _fake_sdk(monkeypatch)
    assert llm_client.oauth_profile_present() is True
    with pytest.raises(SubscriptionGuardError) as exc:
        llm_client.resolve_anthropic_credential()
    message = str(exc.value)
    assert "\n" not in message and LEGAL_URL in message
    assert "oauth-token-do-not-use" not in message
    assert llm_client.credential_present() is False
    assert llm_client.get_llm() is None and llm_client.get_llm_port() is None
    assert FakeAnthropic.last_kwargs is None  # no client was constructed from the profile
    assert (folder / ".credentials.json").read_text(encoding="utf-8")  # still there, just unused


def test_case_5_no_credential_at_all_is_absence_not_a_refusal(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    assert llm_client.resolve_anthropic_credential() is None
    assert llm_client.credential_present() is False
    assert llm_client.guard_refusal() is None
    assert llm_client.llm_unavailable_reason() == "no_credential"


def test_api_key_wins_over_a_token_and_over_a_profile(monkeypatch, tmp_path):
    _oauth_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    assert llm_client.resolve_anthropic_credential() == ("api_key", KEY)


def test_guard_refusal_reaches_the_status_payload_without_any_secret(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    status = llm_client.llm_port_status()
    assert status["credential_present"] is False and status["auth_token_allowed"] is False
    assert LEGAL_URL in status["guard_refusal"]
    assert status["fallback_reason"] == "subscription_guard"
    assert TOKEN not in json.dumps(status)


# ============================================================ provider selection


def test_everything_is_off_by_default(monkeypatch):
    assert llm_client.llm_enabled() is False
    assert llm_client.get_llm_port() is None
    assert llm_client.llm_unavailable_reason() == "disabled"


def test_provider_none_returns_no_port_even_with_a_key(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch)
    assert llm_client.get_llm_port() is None
    assert llm_client.llm_unavailable_reason() == "provider_none"


def test_unknown_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")  # a typo must not reach a provider
    assert llm_client.llm_provider() == "none"
    monkeypatch.delenv("LLM_PROVIDER")
    assert llm_client.llm_provider() == "anthropic"  # unset keeps today's behaviour


def test_provider_anthropic_returns_the_anthropic_adapter(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch)
    port = llm_client.get_llm_port()
    assert isinstance(port, AnthropicAdapter) and port.provider == "anthropic"
    assert isinstance(port, LlmPort)


def test_provider_openai_compat_needs_no_credential_and_defaults_to_local_ollama(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    port = llm_client.get_llm_port()
    assert isinstance(port, OpenAiCompatAdapter) and port.provider == "openai_compat"
    assert port.base_url == "http://127.0.0.1:11434/v1" and port.model == "qwen3:4b"
    assert isinstance(port, LlmPort)
    assert llm_client.llm_unavailable_reason() is None
    status = llm_client.llm_port_status()
    assert status["base_url"] == "http://127.0.0.1:11434/v1" and status["local_model"] == "qwen3:4b"


def test_openai_compat_base_url_and_model_are_configurable(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://10.0.0.5:11434/v1/")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "qwen3:8b")
    port = llm_client.get_llm_port()
    assert port.base_url == "http://10.0.0.5:11434/v1" and port.model == "qwen3:8b"


def test_get_llm_is_anthropic_only_and_still_returns_a_raw_client(monkeypatch):
    """assist.py calls client.beta.messages.parse on whatever get_llm() returns."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch)
    assert isinstance(llm_client.get_llm(), FakeAnthropic)
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    assert llm_client.get_llm() is None  # the raw factory answers for anthropic only


# ============================================================ anthropic adapter


def _ok(model, parsed):
    return SimpleNamespace(
        parsed_output=parsed, stop_reason="end_turn", model=model,
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
    )


class FakeClient:
    """Same injection point tests/integration/test_llm_assist.py uses."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.creates: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self.messages = SimpleNamespace(create=self._create)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def _create(self, **kwargs):
        self.creates.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_anthropic_draft_makes_the_documented_call_unchanged():
    parsed = RootCauseAnalysis(
        summary="Mains failure", hypotheses=[Hypothesis(cause="Genset fault", likelihood="high", evidence=["a"])],
        recommended_checks=["check genset"],
    )
    fake = FakeClient(_ok(MODEL_REASONING, parsed))
    out, rec = AnthropicAdapter(fake).draft(
        model=MODEL_REASONING, system="sys", user="usr", output_model=RootCauseAnalysis,
        effort="medium", max_tokens=8192, timeout=60.0,
    )
    assert out is parsed and rec.ok is True and rec.model_used == MODEL_REASONING
    kw = fake.calls[0]
    assert kw["model"] == MODEL_REASONING and kw["output_format"] is RootCauseAnalysis
    assert kw["output_config"] == {"effort": "medium"} and kw["max_tokens"] == 8192
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["timeout"] == 60.0 and "thinking" not in kw and "tool_choice" not in kw
    assert rec.input_tokens == 10 and rec.output_tokens == 20


def test_anthropic_draft_keeps_the_model_fallback_and_never_raises():
    boom = type("APIStatusError", (Exception,), {})("upstream")
    parsed = ExecBriefDraft(body="INC-1 P1 all good")
    fake = FakeClient(boom, _ok(MODEL_FALLBACK, parsed))
    out, rec = AnthropicAdapter(fake).draft(
        model=MODEL_REASONING, system="s", user="u", output_model=ExecBriefDraft,
    )
    assert out is parsed and rec.fallback_used is True
    assert [c["model"] for c in fake.calls] == [MODEL_REASONING, MODEL_FALLBACK]


def test_anthropic_cite_sends_document_blocks_with_citations_enabled():
    response = SimpleNamespace(
        model=MODEL_DRAFTING, stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=100, output_tokens=40),
        content=[
            {
                "type": "text",
                "text": "The MSP must restore a hub site within 4 hours.",
                "citations": [
                    {"document_index": 0, "document_title": "MSP SLA 2026",
                     "cited_text": "restore within four (4) hours", "start_char_index": 10, "end_char_index": 44},
                ],
            }
        ],
    )
    fake = FakeClient(response)
    docs = [CitedDocument(title="MSP SLA 2026", text="Hub sites: restore within four (4) hours.", context="clause 7.2")]
    answer, rec = AnthropicAdapter(fake).cite(
        model=MODEL_DRAFTING, system="contract assistant", question="What is the hub restore SLA?",
        documents=docs, max_tokens=1024, timeout=30.0,
    )
    assert rec.ok is True and answer is not None and answer.is_grounded
    assert answer.citations[0].document_title == "MSP SLA 2026"
    assert answer.citations[0].cited_text == "restore within four (4) hours"
    block = fake.creates[0]["messages"][0]["content"][0]
    assert block["type"] == "document" and block["citations"] == {"enabled": True}
    assert block["source"] == {"type": "text", "media_type": "text/plain", "data": docs[0].text}
    assert block["title"] == "MSP SLA 2026" and block["context"] == "clause 7.2"
    assert fake.creates[0]["messages"][0]["content"][1] == {"type": "text", "text": "What is the hub restore SLA?"}
    assert fake.creates[0]["timeout"] == 30.0


def test_anthropic_cite_checks_refusal_before_reading_content():
    class Exploding(list):
        def __iter__(self):
            raise AssertionError("content was read on a refusal")

    content = Exploding(["a block that must never be read"])
    fake = FakeClient(SimpleNamespace(model=MODEL_DRAFTING, stop_reason="refusal", usage=None, content=content))
    answer, rec = AnthropicAdapter(fake).cite(model=MODEL_DRAFTING, system="s", question="q", documents=[])
    assert answer is None and rec.refused is True and rec.error == "refusal" and rec.ok is False


def test_anthropic_cite_never_raises_when_the_call_explodes():
    fake = FakeClient(RuntimeError("connection reset"))
    answer, rec = AnthropicAdapter(fake).cite(model=MODEL_DRAFTING, system="s", question="q", documents=[])
    assert answer is None and rec.ok is False and rec.error.startswith("RuntimeError")


# ======================================================= openai-compatible adapter


def _completion(content, *, model="qwen3:4b", finish="stop", prompt_tokens=120, completion_tokens=30):
    return {
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class FakePost:
    """Injected transport: records the request, returns a scripted body. No socket."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[tuple[str, dict, dict, float]] = []

    def __call__(self, url, payload, headers, timeout):
        self.requests.append((url, payload, headers, timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


BRIEF_JSON = '{"body": "INC-20260917-0001 (P1) Westlands Hub power outage. Next update 15 min."}'


def test_openai_compat_draft_uses_json_mode_and_validates_client_side():
    post = FakePost(_completion(BRIEF_JSON))
    port = OpenAiCompatAdapter(base_url="http://127.0.0.1:11434/v1", model="qwen3:4b", post=post)
    port.draft(
        model="qwen3:4b", system="You write NOC briefs.", user="draft it",
        output_model=ExecBriefDraft, max_tokens=512, timeout=20.0,
    )
    url, payload, headers, timeout = post.requests[0]
    assert url == "http://127.0.0.1:11434/v1/chat/completions"
    assert payload["response_format"] == {"type": "json_object"} and payload["stream"] is False
    assert payload["model"] == "qwen3:4b" and payload["max_tokens"] == 512 and timeout == 20.0
    assert payload["messages"][1] == {"role": "user", "content": "draft it"}
    assert "Authorization" not in headers  # a local Ollama is sent no bearer token


def test_openai_compat_draft_returns_the_pydantic_model():
    post = FakePost(_completion(BRIEF_JSON))
    port = OpenAiCompatAdapter(post=post)
    out, rec = port.draft(model="qwen3:4b", system="s", user="u", output_model=ExecBriefDraft)
    assert isinstance(out, ExecBriefDraft) and out.body.startswith("INC-20260917-0001")
    assert rec.ok is True and rec.model_used == "qwen3:4b"
    assert rec.input_tokens == 120 and rec.output_tokens == 30 and rec.latency_ms >= 0
    assert "JSON Schema" in post.requests[0][1]["messages"][0]["content"]  # shape goes in the system prompt


def test_openai_compat_strips_qwen_thinking_and_markdown_fences():
    post = FakePost(_completion("<think>the user wants JSON</think>\n```json\n" + BRIEF_JSON + "\n```"))
    out, rec = OpenAiCompatAdapter(post=post).draft(
        model="qwen3:4b", system="s", user="u", output_model=ExecBriefDraft
    )
    assert isinstance(out, ExecBriefDraft) and rec.ok is True


def test_openai_compat_unusable_output_is_a_record_not_an_exception():
    cases = {
        "prose": (_completion("Sorry, I cannot do that."), "no JSON object"),
        "wrong shape": (_completion('{"summary": "no body key"}'), "validation error"),
        "truncated": (_completion('{"body": "half a sen', finish="length"), "truncated"),
        "unclosed think": (_completion("<think>still thinking"), "<think>"),
        "no choices": ({"model": "qwen3:4b", "choices": []}, "no choices"),
    }
    for label, (body, hint) in cases.items():
        out, rec = OpenAiCompatAdapter(post=FakePost(body)).draft(
            model="qwen3:4b", system="s", user="u", output_model=ExecBriefDraft
        )
        assert out is None and rec.ok is False, label
        assert hint in rec.error, (label, rec.error)


def test_openai_compat_dead_endpoint_degrades_instead_of_raising():
    out, rec = OpenAiCompatAdapter(post=FakePost(ConnectionError("connection refused"))).draft(
        model="qwen3:4b", system="s", user="u", output_model=ExecBriefDraft
    )
    assert out is None and rec.ok is False and rec.error.startswith("ConnectionError")


def test_openai_compat_sends_a_bearer_token_only_when_one_is_configured():
    post = FakePost(_completion(BRIEF_JSON))
    OpenAiCompatAdapter(api_key="gsk-test", post=post).draft(
        model="qwen3:4b", system="s", user="u", output_model=ExecBriefDraft
    )
    assert post.requests[0][2]["Authorization"] == "Bearer gsk-test"


def test_openai_compat_cite_is_unsupported_and_makes_no_call():
    post = FakePost()  # empty script: any call would IndexError
    answer, rec = OpenAiCompatAdapter(post=post).cite(
        model="qwen3:4b", system="s", question="what does clause 7.2 say?",
        documents=[CitedDocument(title="MSP SLA", text="…")],
    )
    assert answer is None and rec.error == CITE_UNSUPPORTED and rec.ok is False
    assert rec.input_tokens == 0 and rec.output_tokens == 0 and post.requests == []


# ================================================================== spend cap


def _spend_limit_429():
    """The 429 the API returns when the monthly spend limit is reached."""
    exc = type("RateLimitError", (Exception,), {})("rate limit")
    exc.status_code = 429
    exc.body = {"type": "error", "error": {"type": "rate_limit_error", "error_code": "enforced_spend_limit_reached"}}
    return exc


def _ordinary_429():
    exc = type("RateLimitError", (Exception,), {})("slow down")
    exc.status_code = 429
    exc.body = {"type": "error", "error": {"type": "rate_limit_error"}}
    return exc


def test_spend_limit_429_is_told_apart_from_ordinary_rate_limiting():
    assert llm_client.is_spend_limit_error(_spend_limit_429()) is True
    assert llm_client.is_spend_limit_error(_ordinary_429()) is False
    assert llm_client.is_spend_limit_error(RuntimeError("enforced_spend_limit_reached")) is False  # no 429


def test_spend_limit_429_through_the_adapter_opens_the_circuit(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch)
    assert llm_client.spend_cap_open() is False

    fake = FakeClient(_spend_limit_429(), _spend_limit_429())
    out, rec = AnthropicAdapter(fake).draft(
        model=MODEL_REASONING, system="s", user="u", output_model=ExecBriefDraft
    )
    assert out is None and rec.ok is False  # caller falls back to its template

    assert llm_client.spend_cap_open() is True
    assert llm_client.llm_port_status()["spend_cap_open"] is True
    assert llm_client.llm_unavailable_reason() == FALLBACK_REASON_SPEND_CAP
    assert llm_client.get_llm() is None and llm_client.get_llm_port() is None  # circuit is open: no more calls
    llm_client.reset_spend_cap()
    assert llm_client.spend_cap_open() is False and llm_client.get_llm_port() is not None


def test_an_ordinary_429_does_not_open_the_circuit():
    fake = FakeClient(_ordinary_429(), _ordinary_429())
    AnthropicAdapter(fake).draft(model=MODEL_REASONING, system="s", user="u", output_model=ExecBriefDraft)
    assert llm_client.spend_cap_open() is False


def test_the_circuit_closes_by_itself_at_the_next_billing_month():
    now = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    llm_client.open_spend_cap(now)
    assert llm_client.spend_cap_open(now) is True
    assert llm_client.spend_cap_open(now + timedelta(days=13)) is True  # still September
    assert llm_client.spend_cap_open(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)) is False
    assert llm_client.spend_cap_open(now) is False  # and it stays closed once reopened by the rollover


def test_the_cite_path_opens_the_circuit_too():
    AnthropicAdapter(FakeClient(_spend_limit_429())).cite(
        model=MODEL_DRAFTING, system="s", question="q", documents=[]
    )
    assert llm_client.spend_cap_open() is True


# ============================================================ prices and budget


def test_prices_come_from_config_not_code():
    prices = llm_client.load_prices()
    assert prices["claude-opus-5"]["input"] == 5.0 and prices["claude-opus-5"]["output"] == 25.0
    assert prices["claude-fable-5-1"]["input"] == 10.0 and prices["claude-fable-5-1"]["output"] == 50.0
    assert prices["claude-sonnet-5"]["input"] == 2.0 and prices["claude-sonnet-5"]["output"] == 10.0
    assert prices["claude-haiku-4-5"]["input"] == 1.0 and prices["claude-haiku-4-5"]["output"] == 5.0
    # 10k in + 2k out on opus-5 = 10_000/1e6*5 + 2_000/1e6*25
    assert llm_client.estimate_cost_usd("claude-opus-5", 10_000, 2_000) == pytest.approx(0.10)
    assert llm_client.estimate_cost_usd("claude-fable-5-1", 10_000, 2_000) == pytest.approx(0.20)
    assert llm_client.estimate_cost_usd("qwen3:4b", 100_000, 50_000) == 0.0  # local costs nothing


def test_an_unpriced_model_costs_null_never_a_guess():
    assert llm_client.estimate_cost_usd("claude-something-unreleased", 1_000, 1_000) is None
    assert llm_client.estimate_cost_usd(None, 1_000, 1_000) is None


def test_editing_the_price_file_changes_the_cost_with_no_code_change(monkeypatch, tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text("models:\n  claude-opus-5:\n    input: 500.0\n    output: 0.0\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PRICES_PATH", str(path))
    assert llm_client.estimate_cost_usd("claude-opus-5", 1_000_000, 0) == pytest.approx(500.0)


def test_a_missing_or_broken_price_file_is_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PRICES_PATH", str(tmp_path / "gone.yaml"))
    assert llm_client.load_prices() == {}
    assert llm_client.estimate_cost_usd("claude-opus-5", 1_000, 1_000) is None
    broken = tmp_path / "broken.yaml"
    broken.write_text("models: [this is not a mapping\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PRICES_PATH", str(broken))
    assert llm_client.load_prices() == {}


def _call_row(session, settings, model, input_tokens, output_tokens, **kwargs):
    from noc_agents.llm.structured import LlmCallRecord

    rec = LlmCallRecord(model_requested=model, model_used=model, ok=True)
    rec.input_tokens, rec.output_tokens, rec.latency_ms = input_tokens, output_tokens, 1234
    row = record_llm_call(
        session,
        operator_id=settings.operator.operator_id,
        agent="TicketingAgent",
        purpose="analyse_incident",
        provider="anthropic",
        rec=rec,
        audit_id="audit-1",
        **kwargs,
    )
    session.commit()
    return row


def test_llm_calls_rows_carry_the_cost_and_no_content(tmp_db):
    settings, session = tmp_db
    row = _call_row(session, settings, "claude-opus-5", 10_000, 2_000, incident_id="inc-1", run_id="run-1", validated=True)
    assert row.est_cost_usd == pytest.approx(0.10)
    assert row.provider == "anthropic" and row.ok == 1 and row.validated == 1
    assert row.agent == "TicketingAgent" and row.purpose == "analyse_incident" and row.audit_id == "audit-1"
    assert row.incident_id == "inc-1" and row.run_id == "run-1" and row.latency_ms == 1234
    assert row.fallback_reason is None and row.cache_read_tokens is None


def test_budget_warns_at_eighty_percent_and_stops_at_a_hundred(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    assert llm_client.budget_state(session)["state"] == "ok"
    assert llm_client.spend_gate(session) is None

    _call_row(session, settings, "claude-opus-5", 80_000, 0)  # $0.40
    _call_row(session, settings, "claude-opus-5", 80_000, 0)  # $0.80 -> 80 %
    state = llm_client.budget_state(session)
    assert state["state"] == "warn" and state["spent_usd"] == pytest.approx(0.80)
    assert llm_client.spend_gate(session) is None  # a warning does not stop anything

    _call_row(session, settings, "claude-opus-5", 40_000, 0)  # $1.00 -> 100 %
    assert llm_client.budget_state(session)["state"] == "stop"
    assert llm_client.spend_gate(session) == FALLBACK_REASON_BUDGET


def test_the_spend_circuit_outranks_the_budget_in_the_gate(tmp_db):
    settings, session = tmp_db
    llm_client.open_spend_cap()
    assert llm_client.spend_gate(session) == FALLBACK_REASON_SPEND_CAP


def test_a_budget_of_zero_disables_the_local_ceiling(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0")
    _call_row(session, settings, "claude-fable-5-1", 1_000_000, 1_000_000)
    assert llm_client.budget_state(session)["state"] == "ok" and llm_client.spend_gate(session) is None


def test_unpriced_rows_do_not_poison_the_budget_sum(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    row = _call_row(session, settings, "claude-unpriced-9", 10_000_000, 10_000_000)
    assert row.est_cost_usd is None
    assert llm_client.month_spend_usd(session) == 0.0 and llm_client.budget_state(session)["state"] == "ok"


# =================================================================== status


def test_llm_status_keeps_its_published_shape(monkeypatch):
    """main.py returns this dict straight out of GET /api/v1/llm/status."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    status = llm_client.llm_status()
    assert set(status) == {"enabled", "sdk_installed", "credential_present", "complex_model", "standard_model"}
    assert KEY not in json.dumps(status)


def test_port_status_reports_retention_as_standard_until_zdr_is_confirmed(monkeypatch):
    assert llm_client.zdr_confirmed() is False  # default
    status = llm_client.llm_port_status()
    assert status["zdr_confirmed"] is False and status["retention"] == "standard"
    monkeypatch.setenv("LLM_ZDR_CONFIRMED", "true")
    confirmed = llm_client.llm_port_status()
    assert confirmed["zdr_confirmed"] is True and confirmed["retention"] == "zdr_asserted"


def test_port_status_never_contains_a_credential(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "gsk-secret")
    _fake_sdk(monkeypatch)
    body = json.dumps(llm_client.llm_port_status())
    assert KEY not in body and "gsk-secret" not in body
    assert '"credential_kind": "api_key"' in body


def test_no_module_in_the_port_imports_the_sdk_at_module_level(monkeypatch):
    """The anthropic package is an optional extra; importing the port must never need it."""
    import importlib

    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` raise ImportError
    for name in (
        "noc_agents.llm.port",
        "noc_agents.llm.client",
        "noc_agents.llm.anthropic_adapter",
        "noc_agents.llm.openai_compat_adapter",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
        assert importlib.import_module(name) is not None
    assert llm_client.sdk_importable() is False
