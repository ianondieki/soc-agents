"""The desk's configuration in one object: policy, knowledge base, accounts, places, timezone.

Every agent takes what it needs from a :class:`SupportContext` instead of loading files
itself, so a test or the eval runner can hand the desk a different policy or fixture without
patching module globals. :func:`default_context` is what the API uses: the shipped files under
``config/support/`` (each parsed once per process) and the active operator's regions.

The lane's feature flag, :func:`support_desk_enabled`, lives here too, beside the configuration
it switches on.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import yaml

from noc_agents.config import AppSettings, get_settings
from noc_agents.support.accounts import AccountBook, load_accounts
from noc_agents.support.kb import KnowledgeBase, load_kb
from noc_agents.support.places import Gazetteer
from noc_agents.support.policy import SupportPolicy, load_policy

log = logging.getLogger(__name__)

ENABLED_ENV = "SUPPORT_DESK_ENABLED"
_FALSE = {"0", "false", "no", "off"}


def support_desk_enabled() -> bool:
    """``SUPPORT_DESK_ENABLED`` -- default **true** in this demo. Read at call time, never at import.

    Unlike the Phase 4/5 lanes (all default off) the desk ships on: it is a showcase feature
    with no outbound side effects -- tools act on fixtures and nothing is ever sent. Any of
    ``0/false/no/off`` turns every route into a 404.
    """
    return (os.getenv(ENABLED_ENV) or "true").strip().lower() not in _FALSE


@dataclass(frozen=True)
class SupportContext:
    policy: SupportPolicy
    kb: KnowledgeBase
    accounts: AccountBook
    gazetteer: Gazetteer
    timezone: str


_GAZETTEERS: dict[str, Gazetteer] = {}


def gazetteer_for(settings: AppSettings) -> Gazetteer:
    """The active operator's gazetteer, built once per operator per process."""
    operator = settings.operator
    if operator.operator_id not in _GAZETTEERS:
        _GAZETTEERS[operator.operator_id] = Gazetteer.from_regions(operator.regions)
    return _GAZETTEERS[operator.operator_id]


class SupportConfigError(RuntimeError):
    """A file under ``config/support/`` is missing or invalid. The API answers 503 with this message:
    the desk cannot run without its policy, and a 500 per route would hide which file is wrong."""


def default_context(settings: AppSettings | None = None) -> SupportContext:
    """The shipped configuration for the active (or given) operator profile.

    Raises :class:`SupportConfigError` naming the file when one cannot be read or does not
    validate (the loaders cache only successes, so the next request after a fix succeeds).
    """
    settings = settings or get_settings()
    loaded = {}
    for name, loader in (("policy.yaml", load_policy), ("knowledge_base.yaml", load_kb), ("accounts.yaml", load_accounts)):
        try:
            loaded[name] = loader()
        except (OSError, yaml.YAMLError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            message = f"config/support/{name} could not be loaded ({type(exc).__name__}: {first_line})"
            log.error("support desk: %s", message)
            raise SupportConfigError(message) from exc
    return SupportContext(
        policy=loaded["policy.yaml"],
        kb=loaded["knowledge_base.yaml"],
        accounts=loaded["accounts.yaml"],
        gazetteer=gazetteer_for(settings),
        timezone=settings.operator.timezone,
    )
