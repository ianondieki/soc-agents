from __future__ import annotations

from noc_agents.orchestrator.registry import workflow_edges, workflow_nodes

# Canonical multi-agent lifecycle topology for UI graph (derived from the agent registry)
WORKFLOW_NODES = workflow_nodes()
WORKFLOW_EDGES = workflow_edges()


def graph_status_map(step_statuses: dict[str, str]) -> list[dict]:
    nodes = []
    for n in WORKFLOW_NODES:
        st = step_statuses.get(n["id"], "pending")
        nodes.append({**n, "status": st})
    return nodes
