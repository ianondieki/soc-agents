"""Provider selection, credentials, prices and the spend circuit. Never imports ``anthropic``
(or ``httpx``) at module level.

The LLM is on only when ALL of these hold: ``LLM_ENABLED`` is truthy, ``LLM_PROVIDER``
names a provider, that provider's credential or base URL resolves, and (for Anthropic) the
SDK imports. ``get_llm``/``get_llm_port`` never raise and nothing here ever logs or returns
secret material.

SUBSCRIPTION GUARD (spec §7.0.9 G13) — a licensing boundary, not a nicety.
A Claude subscription licenses a HUMAN using Claude Code to build this repository. It does
NOT license this application to call the API at runtime; that needs a Console API key. So:

  * the client is constructed with an explicit ``api_key=os.environ["ANTHROPIC_API_KEY"]``
    rather than letting the SDK pick a credential out of the environment;
  * ``ANTHROPIC_AUTH_TOKEN`` is honoured ONLY with ``LLM_ALLOW_AUTH_TOKEN=true`` (the
    enterprise-gateway case);
  * an OAuth profile found on disk is NEVER used: with no API key and a profile in
    ``~/.config/anthropic/``, this module refuses with a one-line error citing
    https://code.claude.com/docs/en/legal-and-compliance.

SPEND CAP. An HTTP 429 whose ``error_code`` is ``enforced_spend_limit_reached`` carries no
``retry-after`` and SDK retries keep failing until 00:00 UTC on the 1st
(https://platform.claude.com/docs/en/api/rate-limits), so it opens a circuit that stays
open until the next month or ``reset_spend_cap()``. Separately ``LLM_MONTHLY_BUDGET_USD``
warns at 80 % and stops at 100 % of summed ``llm_calls.est_cost_usd``; prices come from
``config/llm_prices.yaml``, never from code.

RETENTION. ``zdr_confirmed()`` is the ``LLM_ZDR_CONFIRMED`` flag and defaults to FALSE.
Until it is true, every transfer record assumes STANDARD retention — see ``llm/port.py``
for the facts that must not be misstated.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Model routing (exact ids, no date suffix — see SDK facts).
MODEL_REASONING = "claude-fable-5-1"  # incident analysis / root-cause hypothesis / supervisor recommendation
MODEL_DRAFTING = "claude-opus-5"  # standard drafting (exec brief, comms wording)
MODEL_FALLBACK = "claude-opus-5"  # used when the reasoning model errors or refuses

# Two request budgets: drafting (opus, low effort, short JSON) fits in 20 s; the reasoning
# route runs fable with thinking always on, and its thinking tokens count against
# max_tokens, so it needs a longer budget. Both are bounded so a hung request can never
# hold an assist slot and a threadpool worker for more than MAX_TIMEOUT_S.
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_REASONING_TIMEOUT_S = 60.0
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 300.0
DEFAULT_MAX_RETRIES = 1
MAX_MAX_RETRIES = 5

# Providers (spec §7.0.9). LLM_PROVIDER unset means "anthropic": that is what this system
# has always done when LLM_ENABLED was true, and introducing the knob must not silently
# switch an existing deployment off. An UNKNOWN value fails closed to "none".
PROVIDER_NONE = "none"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPAT = "openai_compat"
PROVIDERS = (PROVIDER_NONE, PROVIDER_ANTHROPIC, PROVIDER_OPENAI_COMPAT)
DEFAULT_PROVIDER = PROVIDER_ANTHROPIC

DEFAULT_OPENAI_COMPAT_BASE_URL = "http://127.0.0.1:11434/v1"  # Ollama on the NOC box
DEFAULT_OPENAI_COMPAT_MODEL = "qwen3:4b"  # 2.5 GB, 256K context — https://ollama.com/library/qwen3

LEGAL_URL = "https://code.claude.com/docs/en/legal-and-compliance"
AUTH_TOKEN_REFUSAL = (
    "ANTHROPIC_AUTH_TOKEN refused: a Claude subscription token does not license application "
    f"API calls — set a Console ANTHROPIC_API_KEY, or LLM_ALLOW_AUTH_TOKEN=true only for an "
    f"enterprise gateway token ({LEGAL_URL})"
)
OAUTH_PROFILE_REFUSAL = (
    "Claude Code OAuth profile in ~/.config/anthropic refused: a subscription licenses a person "
    f"using Claude Code, not this application's API calls — set a Console ANTHROPIC_API_KEY ({LEGAL_URL})"
)

# Spend-cap vocabulary written to llm_calls.fallback_reason.
SPEND_LIMIT_ERROR_CODE = "enforced_spend_limit_reached"
FALLBACK_REASON_SPEND_CAP = "spend_cap"  # the provider's 429 opened the circuit
FALLBACK_REASON_BUDGET = "budget_exhausted"  # our own LLM_MONTHLY_BUDGET_USD ceiling
DEFAULT_MONTHLY_BUDGET_USD = 20.0
BUDGET_WARN_FRACTION = 0.8

# config/llm_prices.yaml — prices are CONFIG, not code.
_ROOT = Path(__file__).resolve().parents[3]  # .../src/noc_agents/llm/client.py -> repo root
DEFAULT_PRICES_PATH = _ROOT / "config" / "llm_prices.yaml"

_TRUTHY = ("1", "true", "yes", "on")

# One client per (SDK class, timeout, retries, credential fingerprint): the SDK owns a
# connection pool, so building a fresh one per request would churn connections. Rebuilt
# only when those settings change. The fingerprint is a hash — never the credential.
_CLIENT_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple[Any, ...], Any] = {}

# Spend circuit + price cache state.
_SPEND_LOCK = threading.Lock()
_SPEND_CAP: dict[str, Any] = {"open": False, "period": None, "opened_at": None}
_PRICES_LOCK = threading.Lock()
_PRICES_CACHE: dict[str, Any] = {"key": None, "data": {}}


class SubscriptionGuardError(RuntimeError):
    """The only credential we could find is not licensed for application API access."""


# --------------------------------------------------------------------- flags


def llm_enabled() -> bool:
    """``LLM_ENABLED`` env flag; default off."""
    return _flag("LLM_ENABLED")


def llm_provider() -> str:
    """``LLM_PROVIDER`` ∈ {none, anthropic, openai_compat}; unset = anthropic, unknown = none."""
    raw = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not raw:
        return DEFAULT_PROVIDER
    return raw if raw in PROVIDERS else PROVIDER_NONE


def allow_auth_token() -> bool:
    """``LLM_ALLOW_AUTH_TOKEN``: the ONLY switch that makes ANTHROPIC_AUTH_TOKEN usable."""
    return _flag("LLM_ALLOW_AUTH_TOKEN")


def zdr_confirmed() -> bool:
    """``LLM_ZDR_CONFIRMED``, default FALSE.

    True asserts a per-organisation Zero Data Retention agreement is in force, which is
    requested from Anthropic sales and is NOT the case on a self-serve Console account.
    Until it is true every transfer record assumes standard retention.
    """
    return _flag("LLM_ZDR_CONFIRMED")


def openai_compat_base_url() -> str:
    """``OPENAI_COMPAT_BASE_URL``; default the local Ollama endpoint."""
    return (os.getenv("OPENAI_COMPAT_BASE_URL") or "").strip() or DEFAULT_OPENAI_COMPAT_BASE_URL


def openai_compat_model() -> str:
    """``OPENAI_COMPAT_MODEL``; default ``qwen3:4b``."""
    return (os.getenv("OPENAI_COMPAT_MODEL") or "").strip() or DEFAULT_OPENAI_COMPAT_MODEL


def openai_compat_api_key() -> str:
    """``OPENAI_COMPAT_API_KEY`` — empty for a local Ollama, required by Groq/Gemini."""
    return (os.getenv("OPENAI_COMPAT_API_KEY") or "").strip()


def _flag(name: str) -> bool:
    return (os.getenv(name) or "false").strip().lower() in _TRUTHY


# --------------------------------------------------------- subscription guard


def oauth_profile_dir() -> Path:
    """``~/.config/anthropic`` (XDG_CONFIG_HOME honoured) — read to REFUSE, never to use."""
    base = (os.getenv("XDG_CONFIG_HOME") or "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / "anthropic"


def oauth_profile_present() -> bool:
    """True when that directory holds anything at all. Never reads a file's contents."""
    try:
        folder = oauth_profile_dir()
        return folder.is_dir() and any(folder.iterdir())
    except Exception:  # noqa: BLE001 — an unreadable home directory is not a profile
        return False


def resolve_anthropic_credential() -> tuple[str, str] | None:
    """Return ``(kind, value)`` for the credential this application may use, or ``None``.

    ``kind`` is ``"api_key"`` or ``"auth_token"``. Raises ``SubscriptionGuardError`` with a
    one-line, secret-free message when the only credential available is one a Claude
    subscription provides. Order matters: a Console key always wins.
    """
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return "api_key", key
    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if token:
        if allow_auth_token():
            return "auth_token", token
        raise SubscriptionGuardError(AUTH_TOKEN_REFUSAL)
    if oauth_profile_present():
        raise SubscriptionGuardError(OAUTH_PROFILE_REFUSAL)
    return None


def credential_present() -> bool:
    """True when a credential this application is LICENSED to use is set.

    Post-G13 this is no longer "any credential": a bare ``ANTHROPIC_AUTH_TOKEN`` without
    ``LLM_ALLOW_AUTH_TOKEN=true``, and an OAuth profile on disk, both read as absent. The
    value itself is never returned. Never raises.
    """
    try:
        return resolve_anthropic_credential() is not None
    except SubscriptionGuardError:
        return False
    except Exception:  # noqa: BLE001 — a probe must never take the process down
        return False


def credential_kind() -> str | None:
    """``"api_key"`` | ``"auth_token"`` | ``None``. Never the value. Never raises."""
    try:
        resolved = resolve_anthropic_credential()
    except Exception:  # noqa: BLE001
        return None
    return resolved[0] if resolved else None


def guard_refusal() -> str | None:
    """The one-line refusal an operator needs to see, or ``None`` when nothing was refused."""
    try:
        resolve_anthropic_credential()
    except SubscriptionGuardError as exc:
        return str(exc)
    except Exception:  # noqa: BLE001
        return None
    return None


def sdk_importable() -> bool:
    """True when the optional ``anthropic`` package can be imported.

    Any import-time failure (ImportError, but also a broken or partial install raising
    OSError / AttributeError / RuntimeError) reads as "SDK unavailable"; never raises.
    """
    try:
        import anthropic  # noqa: F401  (lazy: the package is optional)

        return True
    except Exception:  # noqa: BLE001 — a package that cannot be imported is, for this layer, not installed
        return False


# ------------------------------------------------------------------- budgets


def timeout_s() -> float:
    """Drafting-route request timeout in seconds (``LLM_TIMEOUT_S``, default 20, 1..300)."""
    return _env_number("LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S, float, minimum=MIN_TIMEOUT_S, maximum=MAX_TIMEOUT_S)


def reasoning_timeout_s() -> float:
    """Reasoning-route request timeout in seconds (``LLM_REASONING_TIMEOUT_S``, default 60, 1..300)."""
    return _env_number(
        "LLM_REASONING_TIMEOUT_S", DEFAULT_REASONING_TIMEOUT_S, float, minimum=MIN_TIMEOUT_S, maximum=MAX_TIMEOUT_S
    )


def max_retries() -> int:
    """SDK retry count for 429/5xx/connection errors (``LLM_MAX_RETRIES``, default 1; 0..5)."""
    return _env_number("LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES, int, minimum=0, maximum=MAX_MAX_RETRIES)


def monthly_budget_usd() -> float:
    """``LLM_MONTHLY_BUDGET_USD`` (default 20). 0 means "no local ceiling"."""
    return _env_number("LLM_MONTHLY_BUDGET_USD", DEFAULT_MONTHLY_BUDGET_USD, float, minimum=0.0, maximum=1_000_000.0)


def _env_number(name: str, default: Any, cast: Any, *, minimum: float, maximum: float) -> Any:
    """Parse an env number; unparsable, non-finite or outside ``[minimum, maximum]`` reads as ``default``."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return default
    return value


# -------------------------------------------------------------------- prices


def load_prices(path: Path | str | None = None) -> dict[str, Any]:
    """Read ``config/llm_prices.yaml`` (``LLM_PRICES_PATH`` overrides). Never raises.

    Cached on (path, mtime, size) so editing the file takes effect without a restart and a
    missing or malformed file reads as "no prices known" — which makes ``est_cost_usd``
    NULL, never a guess.
    """
    target = Path(path) if path else Path((os.getenv("LLM_PRICES_PATH") or "").strip() or DEFAULT_PRICES_PATH)
    try:
        stat = target.stat()
        key = (str(target), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    with _PRICES_LOCK:
        if _PRICES_CACHE["key"] == key:
            return _PRICES_CACHE["data"]
    try:
        import yaml

        loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        models = loaded.get("models") if isinstance(loaded, dict) else None
        data = {str(name): dict(entry) for name, entry in (models or {}).items() if isinstance(entry, dict)}
    except Exception:  # noqa: BLE001 — a broken price file must not break the LLM path
        data = {}
    with _PRICES_LOCK:
        _PRICES_CACHE["key"] = key
        _PRICES_CACHE["data"] = data
    return data


def price_for(model: str) -> dict[str, Any] | None:
    """The price row for ``model``, or ``None`` when the table does not price it."""
    return load_prices().get(model)


def estimate_cost_usd(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None = 0,
) -> float | None:
    """USD estimate for one call, or ``None`` for an unpriced model.

    Prices in the YAML are per million tokens. Cache reads bill at the model's
    ``cache_read`` rate when the table gives one and at the INPUT rate when it does not —
    an over-estimate, which is the safe direction for a ceiling that stops spending.
    """
    row = price_for(model or "")
    if not row:
        return None
    try:
        in_rate = float(row.get("input") or 0.0)
        out_rate = float(row.get("output") or 0.0)
        cached = row.get("cache_read")
        cache_rate = float(cached) if cached is not None else in_rate
        cost = (
            (int(input_tokens or 0) * in_rate)
            + (int(output_tokens or 0) * out_rate)
            + (int(cache_read_tokens or 0) * cache_rate)
        ) / 1_000_000.0
        return round(cost, 8)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- spend circuit


def current_period(now: datetime | None = None) -> str:
    """The billing month as ``YYYY-MM`` in UTC — the limit resets at 00:00 UTC on the 1st."""
    moment = now or datetime.now(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def is_spend_limit_error(exc: BaseException) -> bool:
    """True for the 429 that means "monthly spend limit reached", not ordinary rate limiting.

    Matched without importing the SDK: HTTP 429 plus ``error_code`` ==
    ``enforced_spend_limit_reached`` in the error body (dict or object), with the raw text
    as a last resort for a transport that hands us only a message.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status not in (429, "429"):
        return False
    body = getattr(exc, "body", None)
    for candidate in (body, getattr(body, "error", None) if body is not None else None):
        if isinstance(candidate, dict):
            if candidate.get("error_code") == SPEND_LIMIT_ERROR_CODE:
                return True
            inner = candidate.get("error")
            if isinstance(inner, dict) and inner.get("error_code") == SPEND_LIMIT_ERROR_CODE:
                return True
        elif candidate is not None and getattr(candidate, "error_code", None) == SPEND_LIMIT_ERROR_CODE:
            return True
    return SPEND_LIMIT_ERROR_CODE in str(getattr(exc, "message", "") or "") or SPEND_LIMIT_ERROR_CODE in str(exc)


def note_spend_limit_error(exc: BaseException) -> bool:
    """Open the circuit if ``exc`` is the spend-limit 429. Returns whether it was. Never raises."""
    try:
        if is_spend_limit_error(exc):
            open_spend_cap()
            return True
    except Exception:  # noqa: BLE001 — observing an error must never raise a new one
        return False
    return False


def open_spend_cap(now: datetime | None = None) -> None:
    """Open the circuit for the current billing month."""
    with _SPEND_LOCK:
        _SPEND_CAP["open"] = True
        _SPEND_CAP["period"] = current_period(now)
        _SPEND_CAP["opened_at"] = (now or datetime.now(timezone.utc)).isoformat()


def reset_spend_cap() -> None:
    """Manual reset (the other way out is the next month)."""
    with _SPEND_LOCK:
        _SPEND_CAP["open"] = False
        _SPEND_CAP["period"] = None
        _SPEND_CAP["opened_at"] = None


def spend_cap_open(now: datetime | None = None) -> bool:
    """True while the circuit is open; it closes itself once the billing month rolls over."""
    with _SPEND_LOCK:
        if not _SPEND_CAP["open"]:
            return False
        if _SPEND_CAP["period"] != current_period(now):
            _SPEND_CAP["open"] = False
            _SPEND_CAP["period"] = None
            _SPEND_CAP["opened_at"] = None
            return False
        return True


def spend_cap_state() -> dict[str, Any]:
    """Public view of the circuit: open flag, the month it belongs to, when it opened."""
    open_now = spend_cap_open()
    with _SPEND_LOCK:
        return {"open": open_now, "period": _SPEND_CAP["period"], "opened_at": _SPEND_CAP["opened_at"]}


def month_spend_usd(session: Any, *, operator_id: str | None = None, now: datetime | None = None) -> float:
    """Summed ``llm_calls.est_cost_usd`` for the current UTC month. Unpriced (NULL) rows add 0."""
    from sqlalchemy import func, select

    from noc_agents.db.models import LlmCallRow

    moment = now or datetime.now(timezone.utc)
    start = datetime(moment.year, moment.month, 1)
    stmt = select(func.coalesce(func.sum(LlmCallRow.est_cost_usd), 0.0)).where(LlmCallRow.ts >= start)
    if operator_id:
        stmt = stmt.where(LlmCallRow.operator_id == operator_id)
    return float(session.scalar(stmt) or 0.0)


def budget_state(session: Any, *, operator_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Where this month's spend sits against ``LLM_MONTHLY_BUDGET_USD``.

    ``state`` is ``ok`` below 80 %, ``warn`` from 80 % to under 100 %, ``stop`` at or above
    100 %. A budget of 0 disables the local ceiling and always reads ``ok``.
    """
    budget = monthly_budget_usd()
    spent = month_spend_usd(session, operator_id=operator_id, now=now)
    fraction = (spent / budget) if budget > 0 else 0.0
    if budget <= 0:
        state = "ok"
    elif fraction >= 1.0:
        state = "stop"
    elif fraction >= BUDGET_WARN_FRACTION:
        state = "warn"
    else:
        state = "ok"
    return {
        "state": state,
        "spent_usd": round(spent, 6),
        "budget_usd": budget,
        "fraction": round(fraction, 6),
        "period": current_period(now),
    }


def spend_gate(session: Any, *, operator_id: str | None = None, now: datetime | None = None) -> str | None:
    """``None`` when a call may be made, otherwise the ``llm_calls.fallback_reason`` to record.

    ``spend_cap`` = the provider's own limit tripped; ``budget_exhausted`` = our ceiling.
    """
    if spend_cap_open(now):
        return FALLBACK_REASON_SPEND_CAP
    if budget_state(session, operator_id=operator_id, now=now)["state"] == "stop":
        return FALLBACK_REASON_BUDGET
    return None


# -------------------------------------------------------------------- status


def llm_status() -> dict[str, Any]:
    """Public status for ``GET /api/v1/llm/status``. Contains no secret material.

    Deliberately UNCHANGED in shape: ``main.py`` returns this dict straight to the API and
    two existing tests pin it. The provider/guard/spend/retention fields live in
    ``llm_port_status()`` so adding them is an API decision, not a side effect of this wave.
    """
    return {
        "enabled": llm_enabled(),
        "sdk_installed": sdk_importable(),
        "credential_present": credential_present(),
        "complex_model": MODEL_REASONING,
        "standard_model": MODEL_DRAFTING,
    }


def llm_port_status() -> dict[str, Any]:
    """Everything ``llm_status()`` reports plus the §7.0.9 port fields. No secret material.

    ``retention`` is ``"standard"`` unless ``LLM_ZDR_CONFIRMED`` is true — and that flag is
    an assertion by the operator, recorded in docs/COMPLIANCE.md with its Anthropic
    reference, not something this code can verify.
    """
    provider = llm_provider()
    return {
        **llm_status(),
        "provider": provider,
        "credential_kind": credential_kind(),
        "auth_token_allowed": allow_auth_token(),
        "oauth_profile_ignored": oauth_profile_present(),
        "guard_refusal": guard_refusal(),
        "base_url": openai_compat_base_url() if provider == PROVIDER_OPENAI_COMPAT else None,
        "local_model": openai_compat_model() if provider == PROVIDER_OPENAI_COMPAT else None,
        "spend_cap_open": spend_cap_open(),
        "spend_cap_period": spend_cap_state()["period"],
        "monthly_budget_usd": monthly_budget_usd(),
        "fallback_reason": llm_unavailable_reason(),
        "zdr_confirmed": zdr_confirmed(),
        "retention": "zdr_asserted" if zdr_confirmed() else "standard",
    }


def llm_unavailable_reason() -> str | None:
    """Why no model call can be made right now, or ``None`` when one can. Never raises.

    Ordered by what an operator should fix first. Budget exhaustion is not here: it needs a
    database session — see ``spend_gate``.
    """
    if not llm_enabled():
        return "disabled"
    if spend_cap_open():
        return FALLBACK_REASON_SPEND_CAP
    provider = llm_provider()
    if provider == PROVIDER_NONE:
        return "provider_none"
    if provider == PROVIDER_OPENAI_COMPAT:
        return None if openai_compat_base_url() else "no_base_url"
    refusal = guard_refusal()
    if refusal:
        return "subscription_guard"
    if not credential_present():
        return "no_credential"
    if not sdk_importable():
        return "sdk_missing"
    return None


# ------------------------------------------------------------------ factories


def build_anthropic_client() -> Any | None:
    """Build the SDK client under the subscription guard, or ``None``. Never raises.

    The credential is passed EXPLICITLY (``api_key=`` / ``auth_token=``) so the SDK cannot
    reach for anything else in the environment — that explicitness is the guard. Clients
    are memoised per (class, timeout, retries, credential fingerprint); the fingerprint is
    a truncated hash so a rotated key rebuilds the client and the value is never stored.
    """
    try:
        resolved = resolve_anthropic_credential()
        if resolved is None or not sdk_importable():
            return None
        kind, value = resolved
        import anthropic

        fingerprint = _fingerprint(value)
        key = (anthropic.Anthropic, timeout_s(), max_retries(), kind, fingerprint)
        with _CLIENT_LOCK:
            client = _CLIENT_CACHE.get(key)
            if client is None:
                credential = {"api_key": value} if kind == "api_key" else {"auth_token": value}
                client = anthropic.Anthropic(**credential, timeout=key[1], max_retries=key[2])
                _CLIENT_CACHE.clear()  # settings changed (or first build): keep exactly one client
                _CLIENT_CACHE[key] = client
        return client
    except SubscriptionGuardError:
        # One line, no secret, and the layer degrades to templates. The operator sees the
        # same message on GET /api/v1/llm/status via llm_port_status()["guard_refusal"].
        return None
    except Exception:  # noqa: BLE001 — an unusable client must degrade to the template path
        return None


def get_llm(settings: Any | None = None) -> Any | None:
    """Return an ``anthropic.Anthropic`` client, or ``None`` when the assist layer is off.

    This is the RAW-client factory the existing assist path uses (it calls
    ``client.beta.messages.parse``). ``get_llm_port()`` is the provider-neutral entry point;
    this one answers only for ``LLM_PROVIDER=anthropic``.

    ``settings`` is accepted for callers that already hold the app settings; the switches
    themselves are environment variables so a key on its own can never enable the layer.
    Never raises: any failure to build the client reads as "unavailable".
    """
    try:
        if not llm_enabled() or llm_provider() != PROVIDER_ANTHROPIC or spend_cap_open():
            return None
        return build_anthropic_client()
    except Exception:  # noqa: BLE001 — an unusable client must degrade to the template path
        return None


def get_llm_port(settings: Any | None = None) -> Any | None:
    """Return an ``LlmPort`` for the configured provider, or ``None``. Never raises.

    ``None`` means "use the deterministic template" and is the answer whenever the layer is
    off, the provider is ``none``, the credential or base URL does not resolve, the
    subscription guard refuses, or the spend circuit is open. Adapters are imported lazily
    so neither the SDK nor an HTTP client is touched by importing this module.
    """
    try:
        if not llm_enabled() or spend_cap_open():
            return None
        provider = llm_provider()
        if provider == PROVIDER_ANTHROPIC:
            client = build_anthropic_client()
            if client is None:
                return None
            from noc_agents.llm.anthropic_adapter import AnthropicAdapter

            return AnthropicAdapter(client)
        if provider == PROVIDER_OPENAI_COMPAT:
            base_url = openai_compat_base_url()
            if not base_url:
                return None
            from noc_agents.llm.openai_compat_adapter import OpenAiCompatAdapter

            return OpenAiCompatAdapter(base_url=base_url, model=openai_compat_model())
        return None
    except Exception:  # noqa: BLE001 — an unusable port must degrade to the template path
        return None


def _fingerprint(value: str) -> str:
    """A short, one-way tag for a credential. Never reversible, never logged whole."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
