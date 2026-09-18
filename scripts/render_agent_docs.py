"""Render ``docs/AGENTS_MCP_LLM.md`` from the agent registry (Phase 0 item (c)).

The document is generated in full -- prose sections included -- so that a reviewer can
diff the committed file against the registry and see the whole truth in one place.
``agent_catalog()`` is the only dynamic input; everything else is a constant in this file.

Usage (no project install needed; ``src`` is put on ``sys.path`` the way tests/conftest.py does):

    C:\\Python313\\python.exe scripts\\render_agent_docs.py           # print to stdout
    C:\\Python313\\python.exe scripts\\render_agent_docs.py --write   # rewrite docs/AGENTS_MCP_LLM.md
    C:\\Python313\\python.exe scripts\\render_agent_docs.py --check   # CI: committed file == fresh render?

Exit codes:  0 = OK   1 = --check found a difference (or the file is missing)   2 = bad usage

Determinism is a hard requirement: the same registry must produce byte-identical output on
every machine and every run. No clock, no locale, no set iteration, no dict ordering games --
every list below comes from ``agent_catalog()`` in registry order or from an explicit ``sorted``
with an explicit key. The file is always written with ``encoding="utf-8"`` and ``newline="\\n"``
and read back the same way, because on Windows a silent CRLF round-trip would make ``--check``
fail on a file nobody touched.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # same trick as tests/conftest.py: run without installing

from noc_agents.orchestrator.registry import agent_catalog  # noqa: E402  (needs the sys.path line above)

DOC_PATH = ROOT / "docs" / "AGENTS_MCP_LLM.md"
REGEN_COMMAND = "python scripts/render_agent_docs.py --write"
SOURCE_OF_TRUTH = "src/noc_agents/orchestrator/registry.py"

# ---------------------------------------------------------------------------------------------
# Static content.
#
# VERDICTS_TABLE is copied verbatim from SUPER_PROMPT_NOC_V2.md section 7.1.1 ("Verdicts (copy
# into docs/AGENTS_MCP_LLM.md)"), source links included. Do not paraphrase it here: the whole
# point is that the repository carries the same wording, and the same evidence, as the spec.
# ---------------------------------------------------------------------------------------------
VERDICTS_TABLE = """\
| Wanted | MCP server reality | Verdict | Source |
|---|---|---|---|
| WhatsApp send | No Meta-official server. Community `lharries/whatsapp-mcp` connects through the WhatsApp Web multidevice API via whatsmeow (breaching WhatsApp ToS: "bulk messaging, auto-messaging", "non-personal use", reverse engineering), stores message history locally in SQLite under `whatsapp-bridge/store/` with no documented encryption; its README warns it is "subject to the lethal trifecta" | **DO NOT ADOPT.** Cloud API adapter (§7.9.4). | https://github.com/lharries/whatsapp-mcp · https://www.whatsapp.com/legal/terms-of-service |
| SMS (Africa's Talking) | No official server; sole repo has 1 commit, 1 star, no licence | **DO NOT ADOPT.** SDK/HTTP adapter (§7.9.2). | https://github.com/brian-mwangi-developer/africastalking-mcp |
| Twilio MCP | Twilio-Labs **alpha**; its docs warn against mixing community servers | not for production sends | https://github.com/twilio-labs/mcp |
| Email send | Official Gmail MCP (`https://gmailmcp.googleapis.com/mcp/v1`): 10 tools, **no send tool**, Developer Preview | **Keep the SMTP adapter.** | https://developers.google.com/workspace/gmail/api/reference/mcp |
| Excel/Sheets | Official Sheets MCP writes but is Developer Preview; `excel-mcp-server` community v0.1.8 pins `fastmcp<3` | **Not needed** (openpyxl is a dependency; §7.9.3). | https://developers.google.com/workspace/sheets/api/guides/configure-mcp-server · https://github.com/haris-musa/excel-mcp-server |
| Weather | OpenWeather official remote MCP `https://mcp.openweathermap.org/mcp` (streamable-HTTP, six operations, protocol versions through 2026-07-28; 1,000 free One Call credits/day; server card names `org.openweathermap` and `websiteUrl: https://openweathermap.org/` only — the **legal operating entity is UNVERIFIED**; confirm from the accepted agents.openweathermap.org terms before the reg 41(2) register names a recipient) | **Adopt as the teaching example** (no personal data). Production weather stays on plain Open-Meteo HTTP. | https://mcp.openweathermap.org/mcp/server-card · https://agents.openweathermap.org/v1/products |
| NOC systems (Grafana, Zabbix, NetBox, Jira DC, PagerDuty…) | Vendor/community servers exist (brief §8.1) | **Declare cards; connect via stdio only when installed; never expose publicly.** | brief §8.1 |"""

MATURITY_LEGEND = (
    ("ga", "generally available; the vendor supports it"),
    ("preview", "vendor-published but explicitly pre-GA (Developer Preview, public preview); tool names can change without notice"),
    ("alpha", "vendor-published and explicitly experimental"),
    ("community", "third-party project; read the source and pin a version before trusting it"),
    ("reference", "an example server from the MCP project itself, meant to be read more than deployed"),
)

TRANSPORT_LEGEND = (
    ("stdio", "a local process this repository would launch itself; nothing listens on a port and no traffic leaves the host"),
    ("streamable_http", "HTTP(S) to a server we run (or the vendor runs inside our network)"),
    ("remote", "HTTP(S) to somebody else's SaaS endpoint"),
)

RESIDENCY_LEGEND = (
    ("local", "stdio or a private endpoint on our own infrastructure"),
    ("kenya", "hosted inside Kenya"),
    ("abroad", "SaaS outside Kenya — every argument that leaves must be redacted first, and the recipient belongs on the Data Protection Act reg 41(2) register"),
)

ACCESS_LEGEND = (
    ("read_only", "the model may call these tools and read the result"),
    ("write_behind_hitl", "the card also declares tools that change something in another system; the model never calls those, the orchestrator does, and only after a human approves the matching HITL task"),
)

# Model tiers as they appear in the registry today, with the routing constants that name them
# in src/noc_agents/llm/client.py. "none" is not a model: it means the agent has no assist
# endpoint at all and is pure Python.
TIER_NOTES = {
    "none": (
        "No LLM at all. The agent is deterministic Python; there is no assist endpoint to call "
        "and no prompt anywhere in its path."
    ),
    "claude-fable-5-1": (
        "`MODEL_REASONING` in `src/noc_agents/llm/client.py` — the reasoning route (incident "
        "analysis, root-cause hypothesis, supervisor recommendation). Thinking is always on for "
        "this model and thinking tokens count against `max_tokens`, which is why the route gets "
        "its own longer budget (`LLM_REASONING_TIMEOUT_S`, default 60 s, against 20 s for drafting). "
        "It is also the more expensive tier per token, so it is used on the fewest agents."
    ),
    "claude-opus-5": (
        "`MODEL_DRAFTING` (and `MODEL_FALLBACK`) in `src/noc_agents/llm/client.py` — the drafting "
        "route: executive brief, broadcast wording, handover prose. Short, structured output under "
        "the 20 s `LLM_TIMEOUT_S` budget, and the model the reasoning route falls back to when it "
        "errors or refuses."
    ),
}


# ---------------------------------------------------------------------------------------------
# Small helpers. Everything that reaches a Markdown table goes through ``cell``.
# ---------------------------------------------------------------------------------------------
def cell(value: Any) -> str:
    """Render a value as one Markdown table cell: no raw pipes, no newlines, never empty."""
    text = str(value).replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").strip()
    return text or "—"


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def code_list(names: list[str]) -> str:
    """Backticked, comma-separated; order is the registry's, which is the documented order."""
    return ", ".join(f"`{n}`" for n in names) if names else "—"


def plain_list(names: list[str]) -> str:
    return ", ".join(names) if names else "—"


def exposed(namespace: str, tool: str) -> str:
    """The name the model actually sees: the registry owns the namespace prefix."""
    return f"{namespace}_{tool}"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def legend(title: str, items: tuple[tuple[str, str], ...]) -> list[str]:
    return [f"**{title}**", ""] + [f"- `{key}` — {text}" for key, text in items] + [""]


# ---------------------------------------------------------------------------------------------
# Sections.
# ---------------------------------------------------------------------------------------------
def section_header() -> list[str]:
    return [
        "<!-- GENERATED FILE — DO NOT EDIT BY HAND. -->",
        f"<!-- Regenerate with: {REGEN_COMMAND} -->",
        "",
        "# NOC agents, their MCP cards, and the LLM layer",
        "",
        "**This file is generated. Do not edit it by hand — your edit will be overwritten and CI will "
        f"fail first.** Run `{REGEN_COMMAND}` after any change to the registry.",
        "",
        f"The source of truth is `{SOURCE_OF_TRUTH}` (read through `agent_catalog()`, the same function "
        "behind `GET /api/v1/agents`). `scripts/render_agent_docs.py` renders every section below, prose "
        "included, so the committed file can be compared byte for byte with a fresh render; "
        "`tests/unit/test_agent_docs_render.py` does exactly that on every run of the suite.",
        "",
    ]


def section_orientation(catalog: list[dict]) -> list[str]:
    cards = sum(len(entry["mcp"]) for entry in catalog)
    servers = sorted({card["server"] for entry in catalog for card in entry["mcp"]})
    in_graph = sum(1 for entry in catalog if entry["in_graph"])
    return [
        "## 1. What this document is",
        "",
        "A NOC (network operations centre) run for a Kenyan mobile operator. An alarm arrives, and a "
        "chain of agents normalises it, enriches it from the CMDB, scores its severity, opens a ticket, "
        "assigns a field engineer, notifies the people who need to know, and keeps watching the SLA clock. "
        "Each step is one agent.",
        "",
        f"This document describes all **{len(catalog)} agents** ({in_graph} of them wired into the incident "
        f"workflow graph today), the **{cards} MCP cards** they declare across **{len(servers)} distinct "
        "servers**, and the LLM layer that a few of them can optionally use. Three things a reader "
        "usually wants first:",
        "",
        "- **Nothing here connects to anything yet.** The MCP cards are declarations — see section 3.",
        "- **The application runs with no Anthropic credentials at all.** `LLM_ENABLED` is `false` by "
        "default and every LLM path has a deterministic template fallback — see sections 7 and 8.",
        "- **Writes are gated.** A card that can change another system names the human-approval task type "
        "that guards it; the model is never handed the write tool.",
        "",
    ]


def section_summary(catalog: list[dict]) -> list[str]:
    rows = []
    for entry in catalog:
        nodes = plain_list(entry["node_ids"]) if entry["in_graph"] else "— (not in the graph)"
        rows.append([
            cell(entry["name"]),
            cell(entry["mission"]),
            cell(entry["criticality"]),
            cell(entry["model_tier"]),
            cell(entry["trigger"]),
            cell(entry["autonomy"]),
            cell(nodes),
            cell(len(entry["mcp"])),
        ])
    return [
        f"## 2. The {len(catalog)} agents at a glance",
        "",
        *table(
            ["Agent", "Mission", "Criticality", "Model tier", "Trigger", "Autonomy", "Graph nodes", "MCP cards"],
            rows,
        ),
        "",
        "`fail_closed` means an exception in that agent fails the whole incident run; `fail_soft` means it "
        "fails only that step and the run continues. Autonomy is `A0` (proposes, a human commits), `A1` (acts, a human can "
        "intervene) or `A2` (acts unattended). Trigger is what starts the agent: an `EVENT` (an incoming "
        "alarm), a `SCHEDULE` (the Phase 1 scheduler) or a `REQUEST` (a human asks). Full detail per agent "
        "in section 6.",
        "",
    ]


def section_cards_are_and_are_not() -> list[str]:
    return [
        "## 3. What these MCP cards are — and what they are not",
        "",
        "Every MCP card in this document is **declarative documentation as of Phase 0**. Nothing in this "
        "repository connects to any MCP server, opens a socket, spawns a stdio server or sends a single "
        "byte to one. The cards exist so that the decision — *would we connect this, and what would it be "
        "allowed to do?* — is written down, reviewed and version-controlled **before** any runtime exists, "
        "instead of being improvised later by whoever wires the first server up.",
        "",
        "The runtime lands in **Phase 7**, behind `MCP_RUNTIME_ENABLED=false`. Until then the cards are "
        "enforced only as import-time invariants in the registry (a name collision, a malformed namespace "
        "or a write tool without a human-approval gate fails at startup, deliberately loudly) and as unit "
        "tests in `tests/unit/test_mcp_cards.py`.",
        "",
        "`verified=True` on a card means exactly one thing: **the URL was fetched and read on 2026-09-16 "
        "and the tool names were copied from that page.** It does not mean the server was contacted over "
        "MCP, that it was authenticated against, that its tools were called, or that it works. No server "
        "in this document has ever been exercised live. `verified=False` marks a card written from "
        "documentation that could not be confirmed that way — treat its tool names as provisional.",
        "",
        "Each card is written against MCP specification revision **2026-07-28**, which says there SHOULD "
        "always be a human in the loop able to deny a tool invocation, and that tool annotations MUST be "
        "treated as untrusted unless the server is trusted "
        "(https://modelcontextprotocol.io/specification/2026-07-28/server/tools). That is why `write_tools` "
        "are structurally separated from the tools the model can see, and why `defer_loading` is on by "
        "default: tool-selection accuracy degrades once a model is shown more than roughly 30–50 tools, "
        "and tool definitions dominate the context window long before that "
        "(https://www.anthropic.com/engineering/advanced-tool-use).",
        "",
        "### How to read a card",
        "",
        *legend("transport", TRANSPORT_LEGEND),
        *legend("maturity", MATURITY_LEGEND),
        *legend("residency", RESIDENCY_LEGEND),
        *legend("access", ACCESS_LEGEND),
        "**auth env vars** are environment-variable *names* only — no value from any of these variables "
        "appears in this repository, in this document, or in any API response. A card with no fixed "
        "variable (OAuth, for instance) explains its scheme in the auth note instead.",
        "",
        "**read tools / write tools** are printed with the namespace prefix the registry owns, which is "
        "the name a model would actually see (`graf_query_prometheus`, not `query_prometheus`). The "
        "`(namespace, tool)` pair is unique across the whole registry — see appendix A.",
        "",
    ]


def section_verdicts() -> list[str]:
    return [
        "## 4. Adoption verdicts: which MCP servers we refuse, and why",
        "",
        "Copied verbatim from the build spec, section 7.1.1. This is the record of what was investigated "
        "and rejected, so that nobody re-litigates it from memory six months from now. The short version: "
        "for anything that *sends a message to a human being*, this project uses a plain adapter (SMTP, "
        "an HTTP SDK) rather than an MCP server — because the official servers cannot send, and the "
        "servers that can send are community projects that would breach a platform's terms of service.",
        "",
        VERDICTS_TABLE,
        "",
    ]


def section_llm(catalog: list[dict]) -> list[str]:
    tiers: dict[str, list[str]] = {}
    for entry in catalog:
        tiers.setdefault(entry["model_tier"], []).append(entry["name"])
    # Present "none" first, then the model ids alphabetically: deterministic, and it reads
    # from "no LLM" outwards, which is also the project's default posture.
    ordered = sorted(tiers, key=lambda tier: (tier != "none", tier))

    lines = [
        "## 7. The LLM layer",
        "",
        "The LLM is an **assist layer**, never a dependency. `model_tier` on an agent names the model its "
        "assist endpoint asks for *first*; it does not mean the agent needs a model to do its job. Every "
        "agent below has a deterministic template path that produces a complete, sendable result with no "
        "model in the loop, and that path is what runs by default.",
        "",
        "### 7.1 Tiers present in the registry",
        "",
        *table(
            ["Model tier", "Agents", "Count"],
            [[cell(f"`{tier}`"), cell(plain_list(sorted(tiers[tier]))), cell(len(tiers[tier]))] for tier in ordered],
        ),
        "",
    ]
    for tier in ordered:
        lines += [f"**`{tier}`** — {TIER_NOTES[tier]}", ""]
    lines += [
        "### 7.2 How the layer is gated",
        "",
        "`get_llm()` in `src/noc_agents/llm/client.py` returns a client only when **all three** of these "
        "hold, and returns `None` (never raises) otherwise:",
        "",
        "1. **`LLM_ENABLED`** is truthy (`1`, `true`, `yes`, `on`). The default is `false`.",
        "2. **A credential is present** — a non-empty `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`. "
        "`credential_present()` only tests it for emptiness; the value itself is read by the SDK, and is "
        "never logged, returned or copied anywhere in this codebase.",
        "3. **The `anthropic` package imports.** It is an optional dependency, and any import-time failure "
        "— not just `ImportError`, but a broken or partial install — reads as \"SDK unavailable\".",
        "",
        "Because `get_llm()` returns `None` rather than raising, a missing key, a missing package and a "
        "disabled flag are the same thing to every caller: the template path runs. The consequence worth "
        "stating plainly is that **a key on its own cannot switch the layer on** — `LLM_ENABLED` must be "
        "set deliberately as well.",
        "",
        "`GET /api/v1/llm/status` reports `enabled`, `sdk_installed`, `credential_present`, `complex_model` "
        "and `standard_model` — booleans and model ids, never secret material. Request budgets are bounded "
        "on both routes (`LLM_TIMEOUT_S`, `LLM_REASONING_TIMEOUT_S`, `LLM_MAX_RETRIES`) so a hung request "
        "cannot hold an assist slot indefinitely.",
        "",
        "One discrepancy worth knowing about while reading `.env.example`: it documents "
        "`LLM_ALLOW_AUTH_TOKEN=false`, implying a subscription token is honoured only when that flag is "
        "set, but `credential_present()` accepts `ANTHROPIC_AUTH_TOKEN` today without consulting the flag. "
        "The spec's G13 guard (explicit `api_key=`, no on-disk OAuth profile) is not implemented in this "
        "phase. Section 8 explains why that distinction matters.",
        "",
    ]
    return lines


def section_licensing() -> list[str]:
    return [
        "## 8. Licensing: two lanes, and they do not substitute for each other",
        "",
        "These are two different products with two different agreements, and conflating them is the most "
        "common way a project like this ends up out of compliance:",
        "",
        "**Lane 1 — building this repository.** A Claude **Max subscription** covers a developer using "
        "Claude Code to write, refactor, review and test the code in this repository. That is a human "
        "using an assistant to do engineering work.",
        "",
        "**Lane 2 — the application calling Claude at runtime.** A subscription does **not** grant the "
        "application API access. For this NOC system to call Claude while it is running — drafting an "
        "executive brief, analysing an incident — it needs an **API key from the Claude Console** "
        "(`ANTHROPIC_API_KEY`), billed separately from the subscription. Anthropic's guidance is explicit: "
        "\"Developers building products or services that interact with Claude's capabilities … should use "
        "API key authentication through Claude Console\" (https://code.claude.com/docs/en/legal-and-compliance).",
        "",
        "Practically, that means subscription credentials — an OAuth profile left on a developer's machine "
        "by Claude Code, or a subscription token pasted into `ANTHROPIC_AUTH_TOKEN` — must never be what "
        "powers the backend. The two lanes are billed differently, governed differently, and metered "
        "differently.",
        "",
        "**And the part that keeps this simple: the application runs fully without any key.** "
        "`LLM_ENABLED=false` is the default, every LLM path has a deterministic template fallback, and the "
        "entire test suite runs with the key explicitly blanked. The LLM improves the wording of some "
        "outputs; it is not load-bearing for a single incident-lifecycle step.",
        "",
    ]


def section_agent(index: int, entry: dict) -> list[str]:
    nodes = plain_list(entry["node_ids"]) if entry["in_graph"] else "not wired into the graph"
    lines = [
        f"### 6.{index} {entry['name']}",
        "",
        f"*{entry['mission']}*",
        "",
        *table(
            ["Field", "Value"],
            [
                ["criticality", cell(entry["criticality"])],
                ["model tier", cell(f"`{entry['model_tier']}`")],
                ["in the workflow graph", cell(f"{yes_no(entry['in_graph'])} — {nodes}")],
                ["run kind (`agent_runs.graph_name`)", cell(f"`{entry['run_kind']}`")],
                ["trigger", cell(entry["trigger"])],
                ["autonomy", cell(entry["autonomy"])],
                ["card version", cell(entry["version"])],
                ["skills", cell(code_list(entry["skills"]))],
                ["tags", cell(plain_list(entry["tags"]))],
                ["native tools", cell(code_list(entry["tools"]))],
                ["data it may see", cell(plain_list(entry["data_may_see"]))],
                ["data it must never see", cell(plain_list(entry["data_must_not_see"]))],
                ["MCP cards", cell(len(entry["mcp"]))],
            ],
        ),
        "",
    ]
    if not entry["mcp"]:
        lines += [
            "**MCP cards: none.** This agent talks to no external server at all — by design, not by "
            "omission.",
            "",
        ]
        return lines

    lines += [
        "**MCP cards**",
        "",
        *table(
            ["Namespace", "Server", "Transport", "Maturity", "Vendor-official", "Residency", "Access"],
            [
                [
                    cell(f"`{card['namespace']}`"),
                    cell(card["server"]),
                    cell(card["transport"]),
                    cell(card["maturity"]),
                    cell(yes_no(card["vendor_official"])),
                    cell(card["residency"]),
                    cell(plain_list(card["access"])),
                ]
                for card in entry["mcp"]
            ],
        ),
        "",
    ]
    for card in entry["mcp"]:
        lines += card_block(card)
    return lines


def card_block(card: dict) -> list[str]:
    reads = [exposed(card["namespace"], t) for t in card["tools"]]
    writes = [exposed(card["namespace"], t) for t in card["write_tools"]]
    if writes:
        gate = (
            f"`{card['hitl_task_type']}` — the orchestrator calls these only after a human approves that "
            "HITL task; the model is never offered them"
        )
    else:
        gate = "not applicable — this card declares no write tools"
    verified = (
        "yes — URL fetched and read 2026-09-16; the server was never exercised live"
        if card["verified"]
        else "no — written from documentation that was not confirmed by fetching the URL; treat the tool names as provisional"
    )
    rows = [
        ["purpose", cell(card["purpose"])],
        ["url", cell(card["url"])],
        ["transport", cell(card["transport"])],
        ["maturity", cell(card["maturity"])],
        ["vendor-official", cell(yes_no(card["vendor_official"]))],
        ["residency", cell(card["residency"])],
        ["access", cell(plain_list(card["access"]))],
        [f"read tools ({len(reads)})", cell(code_list(reads))],
        [f"write tools ({len(writes)})", cell(code_list(writes))],
        ["HITL gate", cell(gate)],
        ["auth env vars", cell(code_list(card["auth_env"]))],
        ["auth note", cell(card["auth_note"])],
        ["verified", cell(verified)],
        ["defer_loading", cell(yes_no(card["defer_loading"]))],
        ["MCP spec revision", cell(card["spec_version"])],
    ]
    return [
        f"#### `{card['namespace']}` — {card['server']}",
        "",
        *table(["Property", "Value"], rows),
        "",
    ]


def section_agents(catalog: list[dict]) -> list[str]:
    lines = [
        "## 6. The agents in detail",
        "",
        "One subsection per agent, in registry order — which is also the order "
        "`GET /api/v1/agents` returns them in.",
        "",
    ]
    for index, entry in enumerate(catalog, start=1):
        lines += section_agent(index, entry)
    return lines


def section_matrix(catalog: list[dict]) -> list[str]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for entry in catalog:
        for card in entry["mcp"]:
            for tool in card["tools"]:
                rows.append((card["namespace"], tool, "read", entry["name"], card["server"], card["residency"]))
            for tool in card["write_tools"]:
                rows.append((card["namespace"], tool, "write (HITL)", entry["name"], card["server"], card["residency"]))
    rows.sort(key=lambda row: (row[0], row[1]))
    reads = sum(1 for row in rows if row[2] == "read")
    return [
        "## Appendix A — the full `(namespace, tool)` matrix",
        "",
        f"Every tool name declared anywhere in the registry: **{len(rows)} pairs** ({reads} readable by a "
        f"model, {len(rows) - reads} write tools behind a HITL gate), sorted by namespace then tool.",
        "",
        "Uniqueness of the `(namespace, tool)` pair is an **import-time invariant**: "
        f"`{SOURCE_OF_TRUTH}` asserts it when the module loads, so a duplicate is a startup failure rather "
        "than a runtime ambiguity about which server a call was meant for. That is why two agents sharing "
        "one server (Grafana, Zabbix, PagerDuty, Slack, Gmail, Elastic, Confluence, Qdrant) always carry "
        "*disjoint* tool lists — the card is per agent, not per server.",
        "",
        *table(
            ["Exposed name", "Namespace", "Tool", "Kind", "Declared by", "Server", "Residency"],
            [
                [
                    cell(f"`{exposed(namespace, tool)}`"),
                    cell(f"`{namespace}`"),
                    cell(f"`{tool}`"),
                    cell(kind),
                    cell(agent),
                    cell(server),
                    cell(residency),
                ]
                for namespace, tool, kind, agent, server, residency in rows
            ],
        ),
        "",
    ]


def section_footer() -> list[str]:
    return [
        "---",
        "",
        f"Generated from `{SOURCE_OF_TRUTH}` by `scripts/render_agent_docs.py`. "
        f"To change anything above, change the registry and run `{REGEN_COMMAND}`.",
    ]


# ---------------------------------------------------------------------------------------------
# Render / write / check.
# ---------------------------------------------------------------------------------------------
def render() -> str:
    """The whole document as one string. Pure: no clock, no filesystem, no environment."""
    catalog = agent_catalog()
    lines: list[str] = []
    lines += section_header()
    lines += section_orientation(catalog)
    lines += section_summary(catalog)
    lines += section_cards_are_and_are_not()
    lines += section_verdicts()
    lines += ["## 5. Where to look next", "",
              "Section 6 is the per-agent detail; appendix A is every tool name in one table; sections 7 "
              "and 8 cover the LLM layer and the licensing split. Workflow topology, the fail-closed / "
              "fail-soft contract and \"how to add an agent\" live in `docs/ORCHESTRATOR.md`.", ""]
    lines += section_agents(catalog)
    lines += section_llm(catalog)
    lines += section_licensing()
    lines += section_matrix(catalog)
    lines += section_footer()
    return "\n".join(lines) + "\n"


def write(path: Path = DOC_PATH) -> str:
    """Write the document. Explicit utf-8 + '\\n' so Windows cannot inject CRLF."""
    text = render()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return text


def read_committed(path: Path = DOC_PATH) -> str:
    """Read the committed file with newline translation OFF, so a CRLF file reads as CRLF."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def display(path: Path) -> str:
    """Repo-relative path when the file is inside the repo, absolute otherwise (tests use tmp dirs)."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def diff(committed: str, fresh: str, path: Path = DOC_PATH) -> str:
    return "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile=f"{path.name} (committed)",
            tofile=f"{path.name} (freshly rendered)",
            n=2,
        )
    )


def check(path: Path = DOC_PATH) -> int:
    """0 when the committed file matches a fresh render, 1 when it does not."""
    fresh = render()
    if not path.exists():
        print(f"FAIL: {path} does not exist.", file=sys.stderr)
        print(f"      Run:  {REGEN_COMMAND}", file=sys.stderr)
        return 1
    committed = read_committed(path)
    if committed == fresh:
        print(f"OK: {display(path)} is up to date with {SOURCE_OF_TRUTH}.")
        return 0
    print(f"FAIL: {display(path)} is STALE — it does not match the registry.", file=sys.stderr)
    if "\r\n" in committed and "\r\n" not in fresh:
        print("      The committed file has CRLF line endings; this document is written with LF.", file=sys.stderr)
    print(f"      Run:  {REGEN_COMMAND}", file=sys.stderr)
    print("", file=sys.stderr)
    print(diff(committed, fresh, path), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # The document and the diff contain em dashes. A redirected Windows stream defaults to the
    # locale encoding (cp1252), which would mangle them -- or, on stdout, raise. Say utf-8 once,
    # and pin '\n' so a redirect cannot turn the rendered document into CRLF either.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", newline="\n")
            except (ValueError, OSError):  # pytest's captured streams, or a closed handle
                pass
    if len(args) > 1 or (args and args[0] not in ("--write", "--check", "--help", "-h")):
        print(f"usage: python scripts/render_agent_docs.py [--write | --check]\n  got: {' '.join(args)}", file=sys.stderr)
        return 2
    if args and args[0] in ("--help", "-h"):
        print("usage: python scripts/render_agent_docs.py [--write | --check]")
        print("  (no flag)  render the document to stdout")
        print(f"  --write    write {display(DOC_PATH)}")
        print("  --check    exit 1 if the committed file differs from a fresh render")
        return 0
    if args and args[0] == "--check":
        return check()
    if args and args[0] == "--write":
        text = write()
        print(f"WROTE: {display(DOC_PATH)} ({len(text.splitlines())} lines) from {SOURCE_OF_TRUTH}.")
        return 0
    sys.stdout.write(render())  # no flag: the document goes to stdout, unchanged
    return 0


if __name__ == "__main__":
    sys.exit(main())
