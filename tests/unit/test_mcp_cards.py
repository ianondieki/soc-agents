"""Phase 0 acceptance for the declarative MCP cards (spec §7.1.7).

Nothing here connects to a server: the cards are documentation plus import-time invariants, and this file
pins the invariants a reviewer would otherwise have to re-derive by hand.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from noc_agents.domain.enums import HitlTaskType
from noc_agents.orchestrator.registry import AGENT_PROFILES, PROFILES_BY_NAME, McpRequirement, agent_catalog

SRC = Path(__file__).resolve().parents[2] / "src"

ALLOWED_MATURITY = {"ga", "preview", "alpha", "community", "reference"}
ALLOWED_TRANSPORT = {"stdio", "streamable_http", "remote"}
ALLOWED_ACCESS = {"read_only", "write_behind_hitl"}
ALLOWED_RESIDENCY = {"local", "kenya", "abroad"}
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")
# Module-level only: a line starting at column 0 with `import mcp` / `from mcp` (not `mcp_client` etc.).
MODULE_LEVEL_MCP_IMPORT = re.compile(r"^(import|from)\s+mcp\b", re.MULTILINE)

ALL_CARDS: list[tuple[str, McpRequirement]] = [(p.name, m) for p in AGENT_PROFILES for m in p.mcp]
CARD_IDS = [f"{name}/{m.namespace}" for name, m in ALL_CARDS]


def test_there_are_cards_to_check():
    assert ALL_CARDS, "Phase 0 fills cards on the existing twelve profiles"


@pytest.mark.parametrize(("agent", "card"), ALL_CARDS, ids=CARD_IDS)
def test_card_url_is_https_or_stdio(agent, card):
    assert card.transport == "stdio" or card.url.startswith("https://"), (agent, card.namespace, card.url)


@pytest.mark.parametrize(("agent", "card"), ALL_CARDS, ids=CARD_IDS)
def test_card_vocabularies(agent, card):
    assert card.maturity in ALLOWED_MATURITY, (agent, card.namespace, card.maturity)
    assert card.transport in ALLOWED_TRANSPORT, (agent, card.namespace, card.transport)
    assert card.residency in ALLOWED_RESIDENCY, (agent, card.namespace, card.residency)
    assert card.access, (agent, card.namespace, "access must be non-empty")
    assert set(card.access) <= ALLOWED_ACCESS, (agent, card.namespace, card.access)
    assert len(set(card.access)) == len(card.access), (agent, card.namespace, "duplicate access value")
    assert isinstance(card.verified, bool), (agent, card.namespace)
    assert isinstance(card.vendor_official, bool), (agent, card.namespace)
    assert isinstance(card.defer_loading, bool), (agent, card.namespace)
    assert card.purpose.strip(), (agent, card.namespace, "purpose must be non-empty")
    assert card.server.strip(), (agent, card.namespace, "server must be non-empty")
    assert card.spec_version == "2026-07-28", (agent, card.namespace, card.spec_version)


@pytest.mark.parametrize(("agent", "card"), ALL_CARDS, ids=CARD_IDS)
def test_auth_env_holds_environment_variable_names_only(agent, card):
    for name in card.auth_env:
        assert ENV_NAME.match(name), (agent, card.namespace, name)


@pytest.mark.parametrize(("agent", "card"), ALL_CARDS, ids=CARD_IDS)
def test_write_tools_are_gated_and_never_readable(agent, card):
    assert not (set(card.tools) & set(card.write_tools)), (agent, card.namespace, "write tool listed as readable")
    if card.write_tools:
        assert "write_behind_hitl" in card.access, (agent, card.namespace)
        assert card.hitl_task_type in {t.value for t in HitlTaskType}, (agent, card.namespace, card.hitl_task_type)
    else:
        assert card.hitl_task_type is None, (agent, card.namespace, "gate declared without write tools")


def test_namespace_tool_pairs_are_unique_across_profiles():
    pairs = [(m.namespace, t) for _, m in ALL_CARDS for t in (*m.tools, *m.write_tools)]
    dupes = {p for p in pairs if pairs.count(p) > 1}
    assert not dupes, dupes
    assert all(re.fullmatch(r"[a-z][a-z0-9]{1,7}", m.namespace) for _, m in ALL_CARDS)


def test_supervisor_has_no_mcp():
    assert PROFILES_BY_NAME["SupervisorAgent"].mcp == ()


def test_write_capable_gates_exist_in_the_enum():
    # The three §7.1.2 members must land together with the write-capable cards.
    assert {"APPROVE_TICKET_SYNC", "APPROVE_PAGE", "APPROVE_LEDGER_SYNC"} <= {t.value for t in HitlTaskType}
    gates = {m.hitl_task_type for _, m in ALL_CARDS if m.write_tools}
    assert gates == {"APPROVE_TICKET_SYNC", "APPROVE_PAGE", "APPROVE_BROADCAST", "APPROVE_LEDGER_SYNC"}


def test_agent_catalog_is_json_serialisable_and_keeps_the_first_three_keys():
    catalog = agent_catalog()
    json.dumps(catalog)  # raises on any non-serialisable value
    for entry in catalog:
        assert list(entry)[:3] == ["name", "mission", "status"], entry["name"]
        assert set(entry) >= {
            "name", "mission", "status", "node_ids", "criticality", "model_tier", "in_graph", "tools", "mcp",
            "version", "skills", "tags", "run_kind", "trigger", "autonomy", "data_may_see", "data_must_not_see",
        }
        for card in entry["mcp"]:
            assert list(card)[:10] == [
                "server", "url", "transport", "vendor_official", "maturity", "access", "auth_env", "auth_note",
                "verified", "purpose",
            ], (entry["name"], card["namespace"])


def test_profile_vocabularies():
    for p in AGENT_PROFILES:
        assert p.trigger in {"EVENT", "SCHEDULE", "REQUEST", "EVENT+SCHEDULE"}, p.name
        assert p.autonomy in {"A0", "A1", "A2"}, p.name
        assert set(p.data_may_see) <= {"network", "counts", "timestamps", "role_tokens", "redacted_text", "coordinates"}, p.name
        assert not (set(p.data_may_see) & set(p.data_must_not_see)), p.name


def test_no_module_level_mcp_import_under_src():
    """The `mcp` package is optional and fails to import on this machine; it may only be imported inside functions."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if MODULE_LEVEL_MCP_IMPORT.search(text):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders
