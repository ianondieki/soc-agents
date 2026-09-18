"""get_llm() is a three-way AND (flag, credential, SDK) and never raises or leaks the key."""

from __future__ import annotations

import json
import sys
import types

from noc_agents.llm import client as llm_client

KEY = "sk-ant-test-secret-value"


class FakeAnthropic:
    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        FakeAnthropic.last_kwargs = kwargs


def _fake_sdk(monkeypatch, cls=FakeAnthropic):
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=cls))


def test_disabled_returns_none_even_with_key_and_sdk(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch)
    assert llm_client.get_llm() is None
    assert llm_client.llm_status()["enabled"] is False


def test_enabled_without_credential_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "   ")
    _fake_sdk(monkeypatch)
    assert llm_client.get_llm() is None
    assert llm_client.credential_present() is False


def test_enabled_with_key_but_sdk_absent_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` raise ImportError
    assert llm_client.sdk_importable() is False
    assert llm_client.get_llm() is None
    assert llm_client.llm_status()["sdk_installed"] is False


def test_enabled_with_key_and_sdk_builds_client_with_timeout_and_retries(monkeypatch):
    """Phase 1 G13 update. This test used to set ANTHROPIC_AUTH_TOKEN and assert the SDK
    picked the credential up from the environment itself. Both halves of that are now
    deliberately wrong: a bare auth token is refused (see the guard tests below), and the
    API key is passed EXPLICITLY so the SDK cannot fall back to an ambient OAuth profile.
    The timeout/retry defaults this test actually exists to pin are unchanged.
    """
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    _fake_sdk(monkeypatch)
    client = llm_client.get_llm(settings=None)
    assert isinstance(client, FakeAnthropic)
    assert FakeAnthropic.last_kwargs == {"api_key": KEY, "timeout": 20.0, "max_retries": 1}
    # The key is now handed over explicitly (that is the point of G13), so it MUST still
    # never reach the status surface, which is served unauthenticated.
    assert KEY not in json.dumps(llm_client.llm_status())


def test_a_bare_subscription_auth_token_is_refused(monkeypatch):
    """G13, the two-lane licensing boundary: a Claude subscription token licenses a human
    using Claude Code to build this repo; it does NOT license this application to call the
    API at runtime. That needs a Console API key. Before Phase 1, ANTHROPIC_AUTH_TOKEN alone
    switched hosted calls on and LLM_ALLOW_AUTH_TOKEN was referenced nowhere in src/.
    """
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", KEY)
    monkeypatch.delenv("LLM_ALLOW_AUTH_TOKEN", raising=False)
    _fake_sdk(monkeypatch)
    assert llm_client.get_llm(settings=None) is None
    assert llm_client.credential_present() is False


def test_auth_token_is_honoured_only_behind_the_explicit_flag(monkeypatch):
    """An enterprise gateway token is legitimate — but only when the operator opts in."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", KEY)
    monkeypatch.setenv("LLM_ALLOW_AUTH_TOKEN", "true")
    _fake_sdk(monkeypatch)
    assert isinstance(llm_client.get_llm(settings=None), FakeAnthropic)


def test_env_overrides_and_bad_values(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_S", "5.5")
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    assert llm_client.timeout_s() == 5.5
    assert llm_client.max_retries() == 3
    monkeypatch.setenv("LLM_TIMEOUT_S", "banana")
    monkeypatch.setenv("LLM_MAX_RETRIES", "-2")
    assert llm_client.timeout_s() == 20.0
    assert llm_client.max_retries() == 1


def test_constructor_failure_never_raises(monkeypatch):
    class Boom:
        def __init__(self, **_):
            raise RuntimeError("no network stack")

    monkeypatch.setenv("LLM_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    _fake_sdk(monkeypatch, Boom)
    assert llm_client.get_llm() is None


def test_status_shape_has_no_secret(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "yes")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    status = llm_client.llm_status()
    assert set(status) == {"enabled", "sdk_installed", "credential_present", "complex_model", "standard_model"}
    assert status["enabled"] is True and status["credential_present"] is True
    assert status["complex_model"] == "claude-fable-5-1"
    assert status["standard_model"] == "claude-opus-5"
    assert KEY not in json.dumps(status)


def test_zero_timeout_is_rejected_but_zero_retries_is_allowed(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_S", "0")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    assert llm_client.timeout_s() == 20.0
    assert llm_client.max_retries() == 0


class _BrokenSdkFinder:
    """meta_path finder that makes ``import anthropic`` raise a non-ImportError (broken install)."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "anthropic":
            import importlib.util

            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise RuntimeError("broken install: [WinError 126] The specified module could not be found")


def test_broken_sdk_install_reads_as_unavailable_and_never_raises(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BrokenSdkFinder()] + sys.meta_path)
    assert llm_client.sdk_importable() is False
    assert llm_client.llm_status()["sdk_installed"] is False
    assert llm_client.get_llm() is None


def test_non_finite_or_huge_timeouts_are_rejected(monkeypatch):
    for bad in ("inf", "-inf", "nan", "1e9", "301", "0.5"):
        monkeypatch.setenv("LLM_TIMEOUT_S", bad)
        monkeypatch.setenv("LLM_REASONING_TIMEOUT_S", bad)
        assert llm_client.timeout_s() == 20.0, bad
        assert llm_client.reasoning_timeout_s() == 60.0, bad
    monkeypatch.setenv("LLM_TIMEOUT_S", "300")
    monkeypatch.setenv("LLM_REASONING_TIMEOUT_S", "120")
    monkeypatch.setenv("LLM_MAX_RETRIES", "99")
    assert llm_client.timeout_s() == 300.0 and llm_client.reasoning_timeout_s() == 120.0
    assert llm_client.max_retries() == 1  # above MAX_MAX_RETRIES → default


def test_client_is_memoised_until_the_settings_change(monkeypatch):
    class Counting:
        built = 0

        def __init__(self, **kwargs):
            Counting.built += 1
            self.kwargs = kwargs

    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("LLM_TIMEOUT_S", "9")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    _fake_sdk(monkeypatch, Counting)
    first = llm_client.get_llm()
    assert first is llm_client.get_llm() and Counting.built == 1
    # api_key is now passed explicitly (Phase 1 G13); timeout/retries unchanged.
    assert first.kwargs == {"api_key": KEY, "timeout": 9.0, "max_retries": 2}
    monkeypatch.setenv("LLM_TIMEOUT_S", "11")
    second = llm_client.get_llm()
    assert second is not first and Counting.built == 2 and second.kwargs["timeout"] == 11.0
    monkeypatch.setenv("LLM_ENABLED", "false")
    assert llm_client.get_llm() is None  # the cache never overrides the switches
