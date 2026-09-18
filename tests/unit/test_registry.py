"""The agent registry is the single source for the workflow graph and the /agents catalog."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from noc_agents.graph.workflow_nodes import WORKFLOW_EDGES, WORKFLOW_NODES
from noc_agents.orchestrator.contract import FAIL_CLOSED, FAIL_SOFT
from noc_agents.orchestrator.registry import (
    AGENT_PROFILES,
    NODE_CARDS,
    PROFILES_BY_NAME,
    agent_catalog,
    profile_for,
    workflow_edges,
    workflow_nodes,
)

SRC = Path(__file__).resolve().parents[2] / "src"
AGENTS_DIR = SRC / "noc_agents" / "agents"

EXPECTED_NODES = [
    {"id": "INGEST", "label": "Ingest", "agent": "IngestCorrelationAgent"},
    {"id": "CORRELATE", "label": "Correlate", "agent": "IngestCorrelationAgent"},
    {"id": "ENRICH", "label": "Enrich", "agent": "EnrichmentAgent"},
    {"id": "SEVERITY", "label": "Severity", "agent": "SeverityImpactAgent"},
    {"id": "TICKET", "label": "Ticket", "agent": "TicketingAgent"},
    {"id": "ASSIGN", "label": "Assign", "agent": "DispatchAssignmentAgent"},
    {"id": "HITL", "label": "HITL Gate", "agent": "SupervisorAgent"},
    {"id": "BROADCAST", "label": "Broadcast", "agent": "BroadcastCommsAgent"},
    {"id": "EXEC_BRIEF", "label": "Exec Brief", "agent": "ExecutiveBriefingAgent"},
    {"id": "LEDGER", "label": "Shift Ledger", "agent": "ShiftLedgerAgent"},
    {"id": "RECURRENCE", "label": "Recurrence", "agent": "RecurrenceProblemAgent"},
    {"id": "MONITOR", "label": "Monitor", "agent": "WorklogMonitorAgent"},
]
EXPECTED_EDGES = [
    {"source": a["id"], "target": b["id"]} for a, b in zip(EXPECTED_NODES, EXPECTED_NODES[1:])
]
EXPECTED_CATALOG = [
    ("SupervisorAgent", "Routes lifecycle and HITL gates"),
    ("IngestCorrelationAgent", "Normalize + dedupe alarms"),
    ("EnrichmentAgent", "Site/region CMDB + user estimate"),
    ("SeverityImpactAgent", "P1–P4 + HUB floors + M-PESA tag"),
    ("TicketingAgent", "Unique INC + narrative fields"),
    ("DispatchAssignmentAgent", "FE vs MSP matrix"),
    ("BroadcastCommsAgent", "RNIO/FE/MSP notifications"),
    ("ExecutiveBriefingAgent", "Exec brief to cut phone spam"),
    ("ShiftLedgerAgent", "Excel shift ledger"),
    ("RecurrenceProblemAgent", "Chronic site problems"),
    ("WorklogMonitorAgent", "Notes + SLA watch"),
    ("ShiftHandoverAgent", "Day/night handover package"),
]
FAIL_CLOSED_NODES = {"INGEST", "CORRELATE", "ENRICH", "SEVERITY", "TICKET", "ASSIGN", "HITL", "BROADCAST"}


def test_workflow_nodes_and_edges_come_from_the_registry():
    assert WORKFLOW_NODES == workflow_nodes() == EXPECTED_NODES
    assert WORKFLOW_EDGES == workflow_edges() == EXPECTED_EDGES
    assert len(WORKFLOW_NODES) == 12 and len(WORKFLOW_EDGES) == 11


def test_agent_catalog_keeps_the_existing_contract_and_adds_only_new_keys():
    catalog = agent_catalog()
    assert [(a["name"], a["mission"]) for a in catalog] == EXPECTED_CATALOG
    assert all(a["status"] == "ready" for a in catalog)
    # §2.1 R1 re-baseline (Phase 0): the eight v2 keys (version, skills, tags, run_kind, trigger, autonomy,
    # data_may_see, data_must_not_see) and the filled `mcp` cards were ADDED; every pre-existing key and value
    # is byte-identical to the 9-key dicts pinned before, and the first three keys stay name, mission, status.
    assert catalog[0] == {
        "name": "SupervisorAgent",
        "mission": "Routes lifecycle and HITL gates",
        "status": "ready",
        "node_ids": ["HITL"],
        "criticality": "fail_closed",
        "model_tier": "claude-fable-5-1",
        "in_graph": True,
        "tools": ["create_hitl_task"],
        "mcp": [],
        "version": "1.0",
        "skills": ["incident.gate.evaluate", "hitl.task.create", "channel.render"],
        "tags": ["lifecycle", "gate"],
        "run_kind": "incident_lifecycle",
        "trigger": "EVENT",
        "autonomy": "A0",
        "data_may_see": ["network", "counts", "timestamps", "role_tokens", "redacted_text"],
        "data_must_not_see": ["msisdn", "customer", "cdr", "location_trace", "mpesa", "raw_names"],
    }
    assert catalog[1]["node_ids"] == ["INGEST", "CORRELATE"]
    assert catalog[1]["tools"] == ["find_open_by_fingerprint", "link_parent_hub", "normalize_event"]
    # §2.1 R1 re-baseline (Phase 0) — see the note above catalog[0].
    assert catalog[-1] == {
        "name": "ShiftHandoverAgent",
        "mission": "Day/night handover package",
        "status": "ready",
        "node_ids": [],
        "criticality": "fail_soft",
        "model_tier": "claude-opus-5",
        "in_graph": False,
        "tools": ["build_handover", "send_handover_email"],
        "mcp": [
            {
                "server": "Gmail MCP server",
                "url": "https://developers.google.com/workspace/gmail/api/reference/mcp",
                "transport": "remote",
                "vendor_official": True,
                "maturity": "preview",
                "access": ["read_only"],
                "auth_env": [],
                "auth_note": "https://gmailmcp.googleapis.com/mcp/v1; OAuth 2.0 (Workspace Developer Preview); 10 tools, NO send tool",
                "verified": True,
                "purpose": "Read the outgoing shift's drafted notes and labelled handover mail; sending stays on the SMTP adapter.",
                "namespace": "gmail",
                "tools": ["get_message", "list_drafts", "list_labels"],
                "write_tools": [],
                "hitl_task_type": None,
                "defer_loading": True,
                "spec_version": "2026-07-28",
                "residency": "abroad",
            },
            {
                "server": "Microsoft Work IQ Mail MCP server",
                "url": "https://learn.microsoft.com/en-us/microsoft-agent-365/tooling-servers-overview",
                "transport": "remote",
                "vendor_official": True,
                "maturity": "preview",
                "access": ["read_only"],
                "auth_env": [],
                "auth_note": "Entra ID OAuth through a registered public client; tenant URL https://agent365.svc.cloud.microsoft/agents/tenants/{tenantId}/servers/mcp_MailTools; needs a Microsoft 365 Copilot licence; Microsoft may rename preview tools",
                "verified": True,
                "purpose": "Search the shift mailbox for vendor promises and open threads to carry into the handover package.",
                "namespace": "wiq",
                "tools": [
                    "mcp_MailTools_graph_mail_searchMessages",
                    "mcp_MailTools_graph_mail_getMessage",
                    "mcp_MailTools_graph_mail_listSent",
                ],
                "write_tools": [],
                "hitl_task_type": None,
                "defer_loading": True,
                "spec_version": "2026-07-28",
                "residency": "abroad",
            },
            {
                "server": "Atlassian Rovo MCP Server (Confluence)",
                "url": "https://github.com/atlassian/atlassian-mcp-server",
                "transport": "remote",
                "vendor_official": True,
                "maturity": "ga",
                "access": ["read_only"],
                "auth_env": [],
                "auth_note": "https://mcp.atlassian.com/v2/mcp; OAuth 2.1 or API token; tools discovered on demand",
                "verified": True,
                "purpose": "List handover pages and open Confluence tasks for the carry-forward section.",
                "namespace": "conf",
                "tools": ["listConfluenceContent", "listConfluenceTasks", "getConfluenceTask"],
                "write_tools": [],
                "hitl_task_type": None,
                "defer_loading": True,
                "spec_version": "2026-07-28",
                "residency": "abroad",
            },
            {
                "server": "mcp-server-qdrant",
                "url": "https://github.com/qdrant/mcp-server-qdrant",
                "transport": "stdio",
                "vendor_official": True,
                "maturity": "preview",
                "access": ["read_only"],
                "auth_env": ["QDRANT_URL", "QDRANT_API_KEY"],
                "auth_note": "QDRANT_READ_ONLY=true; COLLECTION_NAME scopes one collection per card",
                "verified": True,
                "purpose": "Declared for shift-memo recall; no tool exposed yet because qd_qdrant-find is owned by RecurrenceProblemAgent and qdrant-store needs the Phase 2 APPROVE_HANDOVER gate.",
                "namespace": "qd",
                "tools": [],
                "write_tools": [],
                "hitl_task_type": None,
                "defer_loading": True,
                "spec_version": "2026-07-28",
                "residency": "local",
            },
        ],
        "version": "1.0",
        "skills": ["handover.build", "handover.email.send"],
        "tags": ["advisory", "comms", "request"],
        "run_kind": "handover",
        "trigger": "REQUEST",
        "autonomy": "A1",
        "data_may_see": ["network", "counts", "timestamps", "role_tokens", "redacted_text"],
        "data_must_not_see": ["msisdn", "customer", "cdr", "location_trace", "mpesa", "raw_names"],
    }


def test_every_card_is_well_formed():
    assert len({c.node_id for c in NODE_CARDS}) == len(NODE_CARDS)
    for card in NODE_CARDS:
        assert card.agent in PROFILES_BY_NAME, card.node_id
        assert callable(card.run) and callable(card.input_summary), card.node_id
    for profile in AGENT_PROFILES:
        assert profile.criticality in {FAIL_CLOSED, FAIL_SOFT}, profile.name
    assert {c.node_id for c in NODE_CARDS if profile_for(c).criticality == FAIL_CLOSED} == FAIL_CLOSED_NODES


@pytest.mark.parametrize(
    "module",
    [
        "noc_agents.orchestrator.registry",
        "noc_agents.orchestrator.contract",
        "noc_agents.orchestrator.runner",
        "noc_agents.agents.ticket",
        "noc_agents.graph.pipeline",
        "noc_agents.graph.workflow_nodes",
    ],
)
def test_fresh_interpreter_imports(module):
    """Inside pytest every module is already cached; only a subprocess proves there is no import cycle."""
    code = f"import sys; sys.path.insert(0, {str(SRC)!r}); import {module}"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_agent_modules_do_not_import_the_orchestrator_or_pipeline():
    """Agents depend on the contract only; importing the runner/registry/pipeline would be circular."""
    forbidden = ("graph.pipeline", "orchestrator.runner", "orchestrator.registry")
    for path in sorted(AGENTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, f"{path.name} imports {name}"
