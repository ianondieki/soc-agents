<!-- GENERATED FILE — DO NOT EDIT BY HAND. -->
<!-- Regenerate with: python scripts/render_agent_docs.py --write -->

# NOC agents, their MCP cards, and the LLM layer

**This file is generated. Do not edit it by hand — your edit will be overwritten and CI will fail first.** Run `python scripts/render_agent_docs.py --write` after any change to the registry.

The source of truth is `src/noc_agents/orchestrator/registry.py` (read through `agent_catalog()`, the same function behind `GET /api/v1/agents`). `scripts/render_agent_docs.py` renders every section below, prose included, so the committed file can be compared byte for byte with a fresh render; `tests/unit/test_agent_docs_render.py` does exactly that on every run of the suite.

## 1. What this document is

A NOC (network operations centre) run for a Kenyan mobile operator. An alarm arrives, and a chain of agents normalises it, enriches it from the CMDB, scores its severity, opens a ticket, assigns a field engineer, notifies the people who need to know, and keeps watching the SLA clock. Each step is one agent.

This document describes all **12 agents** (11 of them wired into the incident workflow graph today), the **29 MCP cards** they declare across **20 distinct servers**, and the LLM layer that a few of them can optionally use. Three things a reader usually wants first:

- **Nothing here connects to anything yet.** The MCP cards are declarations — see section 3.
- **The application runs with no Anthropic credentials at all.** `LLM_ENABLED` is `false` by default and every LLM path has a deterministic template fallback — see sections 7 and 8.
- **Writes are gated.** A card that can change another system names the human-approval task type that guards it; the model is never handed the write tool.

## 2. The 12 agents at a glance

| Agent | Mission | Criticality | Model tier | Trigger | Autonomy | Graph nodes | MCP cards |
|---|---|---|---|---|---|---|---|
| SupervisorAgent | Routes lifecycle and HITL gates | fail_closed | claude-fable-5-1 | EVENT | A0 | HITL | 0 |
| IngestCorrelationAgent | Normalize + dedupe alarms | fail_closed | none | EVENT | A0 | INGEST, CORRELATE | 5 |
| EnrichmentAgent | Site/region CMDB + user estimate | fail_closed | none | EVENT | A0 | ENRICH | 2 |
| SeverityImpactAgent | P1–P4 + HUB floors + M-PESA tag | fail_closed | none | EVENT | A0 | SEVERITY | 0 |
| TicketingAgent | Unique INC + narrative fields | fail_closed | claude-fable-5-1 | EVENT | A0 | TICKET | 3 |
| DispatchAssignmentAgent | FE vs MSP matrix | fail_closed | none | EVENT | A0 | ASSIGN | 2 |
| BroadcastCommsAgent | RNIO/FE/MSP notifications | fail_closed | claude-opus-5 | EVENT | A1 | BROADCAST | 2 |
| ExecutiveBriefingAgent | Exec brief to cut phone spam | fail_soft | claude-opus-5 | EVENT | A1 | EXEC_BRIEF | 2 |
| ShiftLedgerAgent | Excel shift ledger | fail_soft | none | EVENT | A0 | LEDGER | 3 |
| RecurrenceProblemAgent | Chronic site problems | fail_soft | claude-fable-5-1 | EVENT | A0 | RECURRENCE | 3 |
| WorklogMonitorAgent | Notes + SLA watch | fail_soft | claude-opus-5 | SCHEDULE | A0 | MONITOR | 3 |
| ShiftHandoverAgent | Day/night handover package | fail_soft | claude-opus-5 | REQUEST | A1 | — (not in the graph) | 4 |

`fail_closed` means an exception in that agent fails the whole incident run; `fail_soft` means it fails only that step and the run continues. Autonomy is `A0` (proposes, a human commits), `A1` (acts, a human can intervene) or `A2` (acts unattended). Trigger is what starts the agent: an `EVENT` (an incoming alarm), a `SCHEDULE` (the Phase 1 scheduler) or a `REQUEST` (a human asks). Full detail per agent in section 6.

## 3. What these MCP cards are — and what they are not

Every MCP card in this document is **declarative documentation as of Phase 0**. Nothing in this repository connects to any MCP server, opens a socket, spawns a stdio server or sends a single byte to one. The cards exist so that the decision — *would we connect this, and what would it be allowed to do?* — is written down, reviewed and version-controlled **before** any runtime exists, instead of being improvised later by whoever wires the first server up.

The runtime lands in **Phase 7**, behind `MCP_RUNTIME_ENABLED=false`. Until then the cards are enforced only as import-time invariants in the registry (a name collision, a malformed namespace or a write tool without a human-approval gate fails at startup, deliberately loudly) and as unit tests in `tests/unit/test_mcp_cards.py`.

`verified=True` on a card means exactly one thing: **the URL was fetched and read on 2026-09-16 and the tool names were copied from that page.** It does not mean the server was contacted over MCP, that it was authenticated against, that its tools were called, or that it works. No server in this document has ever been exercised live. `verified=False` marks a card written from documentation that could not be confirmed that way — treat its tool names as provisional.

Each card is written against MCP specification revision **2026-07-28**, which says there SHOULD always be a human in the loop able to deny a tool invocation, and that tool annotations MUST be treated as untrusted unless the server is trusted (https://modelcontextprotocol.io/specification/2026-07-28/server/tools). That is why `write_tools` are structurally separated from the tools the model can see, and why `defer_loading` is on by default: tool-selection accuracy degrades once a model is shown more than roughly 30–50 tools, and tool definitions dominate the context window long before that (https://www.anthropic.com/engineering/advanced-tool-use).

### How to read a card

**transport**

- `stdio` — a local process this repository would launch itself; nothing listens on a port and no traffic leaves the host
- `streamable_http` — HTTP(S) to a server we run (or the vendor runs inside our network)
- `remote` — HTTP(S) to somebody else's SaaS endpoint

**maturity**

- `ga` — generally available; the vendor supports it
- `preview` — vendor-published but explicitly pre-GA (Developer Preview, public preview); tool names can change without notice
- `alpha` — vendor-published and explicitly experimental
- `community` — third-party project; read the source and pin a version before trusting it
- `reference` — an example server from the MCP project itself, meant to be read more than deployed

**residency**

- `local` — stdio or a private endpoint on our own infrastructure
- `kenya` — hosted inside Kenya
- `abroad` — SaaS outside Kenya — every argument that leaves must be redacted first, and the recipient belongs on the Data Protection Act reg 41(2) register

**access**

- `read_only` — the model may call these tools and read the result
- `write_behind_hitl` — the card also declares tools that change something in another system; the model never calls those, the orchestrator does, and only after a human approves the matching HITL task

**auth env vars** are environment-variable *names* only — no value from any of these variables appears in this repository, in this document, or in any API response. A card with no fixed variable (OAuth, for instance) explains its scheme in the auth note instead.

**read tools / write tools** are printed with the namespace prefix the registry owns, which is the name a model would actually see (`graf_query_prometheus`, not `query_prometheus`). The `(namespace, tool)` pair is unique across the whole registry — see appendix A.

## 4. Adoption verdicts: which MCP servers we refuse, and why

Copied verbatim from the build spec, section 7.1.1. This is the record of what was investigated and rejected, so that nobody re-litigates it from memory six months from now. The short version: for anything that *sends a message to a human being*, this project uses a plain adapter (SMTP, an HTTP SDK) rather than an MCP server — because the official servers cannot send, and the servers that can send are community projects that would breach a platform's terms of service.

| Wanted | MCP server reality | Verdict | Source |
|---|---|---|---|
| WhatsApp send | No Meta-official server. Community `lharries/whatsapp-mcp` connects through the WhatsApp Web multidevice API via whatsmeow (breaching WhatsApp ToS: "bulk messaging, auto-messaging", "non-personal use", reverse engineering), stores message history locally in SQLite under `whatsapp-bridge/store/` with no documented encryption; its README warns it is "subject to the lethal trifecta" | **DO NOT ADOPT.** Cloud API adapter (§7.9.4). | https://github.com/lharries/whatsapp-mcp · https://www.whatsapp.com/legal/terms-of-service |
| SMS (Africa's Talking) | No official server; sole repo has 1 commit, 1 star, no licence | **DO NOT ADOPT.** SDK/HTTP adapter (§7.9.2). | https://github.com/brian-mwangi-developer/africastalking-mcp |
| Twilio MCP | Twilio-Labs **alpha**; its docs warn against mixing community servers | not for production sends | https://github.com/twilio-labs/mcp |
| Email send | Official Gmail MCP (`https://gmailmcp.googleapis.com/mcp/v1`): 10 tools, **no send tool**, Developer Preview | **Keep the SMTP adapter.** | https://developers.google.com/workspace/gmail/api/reference/mcp |
| Excel/Sheets | Official Sheets MCP writes but is Developer Preview; `excel-mcp-server` community v0.1.8 pins `fastmcp<3` | **Not needed** (openpyxl is a dependency; §7.9.3). | https://developers.google.com/workspace/sheets/api/guides/configure-mcp-server · https://github.com/haris-musa/excel-mcp-server |
| Weather | OpenWeather official remote MCP `https://mcp.openweathermap.org/mcp` (streamable-HTTP, six operations, protocol versions through 2026-07-28; 1,000 free One Call credits/day; server card names `org.openweathermap` and `websiteUrl: https://openweathermap.org/` only — the **legal operating entity is UNVERIFIED**; confirm from the accepted agents.openweathermap.org terms before the reg 41(2) register names a recipient) | **Adopt as the teaching example** (no personal data). Production weather stays on plain Open-Meteo HTTP. | https://mcp.openweathermap.org/mcp/server-card · https://agents.openweathermap.org/v1/products |
| NOC systems (Grafana, Zabbix, NetBox, Jira DC, PagerDuty…) | Vendor/community servers exist (brief §8.1) | **Declare cards; connect via stdio only when installed; never expose publicly.** | brief §8.1 |

## 5. Where to look next

Section 6 is the per-agent detail; appendix A is every tool name in one table; sections 7 and 8 cover the LLM layer and the licensing split. Workflow topology, the fail-closed / fail-soft contract and "how to add an agent" live in `docs/ORCHESTRATOR.md`.

## 6. The agents in detail

One subsection per agent, in registry order — which is also the order `GET /api/v1/agents` returns them in.

### 6.1 SupervisorAgent

*Routes lifecycle and HITL gates*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `claude-fable-5-1` |
| in the workflow graph | yes — HITL |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `incident.gate.evaluate`, `hitl.task.create`, `channel.render` |
| tags | lifecycle, gate |
| native tools | `create_hitl_task` |
| data it may see | network, counts, timestamps, role_tokens, redacted_text |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 0 |

**MCP cards: none.** This agent talks to no external server at all — by design, not by omission.

### 6.2 IngestCorrelationAgent

*Normalize + dedupe alarms*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `none` |
| in the workflow graph | yes — INGEST, CORRELATE |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `alarm.normalize`, `alarm.dedupe`, `incident.correlate` |
| tags | lifecycle |
| native tools | `find_open_by_fingerprint`, `link_parent_hub`, `normalize_event` |
| data it may see | network, counts, timestamps |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 5 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `graf` | mcp-grafana | stdio | preview | yes | local | read_only |
| `prom` | prometheus-mcp-server | stdio | community | no | local | read_only |
| `zbx` | zabbix-mcp-server | streamable_http | community | no | local | read_only |
| `dd` | Datadog MCP Server | remote | preview | yes | abroad | read_only |
| `es` | Elastic Agent Builder MCP server | streamable_http | ga | yes | local | read_only |

#### `graf` — mcp-grafana

| Property | Value |
|---|---|
| purpose | Query Prometheus datasources and list OnCall alert groups to confirm an alarm before correlating it. |
| url | https://github.com/grafana/mcp-grafana |
| transport | stdio |
| maturity | preview |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (4) | `graf_query_prometheus`, `graf_list_alert_groups`, `graf_list_datasources`, `graf_query_loki_logs` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` |
| auth note | — |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `prom` — prometheus-mcp-server

| Property | Value |
|---|---|
| purpose | Run instant and range PromQL queries against the NOC Prometheus to size an alarm burst. |
| url | https://github.com/pab1it0/prometheus-mcp-server |
| transport | stdio |
| maturity | community |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (5) | `prom_execute_query`, `prom_execute_range_query`, `prom_list_metrics`, `prom_get_metric_metadata`, `prom_get_targets` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `PROMETHEUS_URL`, `PROMETHEUS_TOKEN` |
| auth note | — |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `zbx` — zabbix-mcp-server

| Property | Value |
|---|---|
| purpose | Read current Zabbix problems, events, triggers and host inventory to normalise incoming alarms. |
| url | https://github.com/initMAX/zabbix-mcp-server |
| transport | streamable_http |
| maturity | community |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (7) | `zbx_problem_get`, `zbx_event_get`, `zbx_trigger_get`, `zbx_host_get`, `zbx_hostgroup_get`, `zbx_item_get`, `zbx_history_get` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `ZABBIX_URL`, `ZABBIX_API_TOKEN` |
| auth note | per-server read_only=true in the server's TOML config; token referenced as ${ZABBIX_API_TOKEN} |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `dd` — Datadog MCP Server

| Property | Value |
|---|---|
| purpose | Search Datadog monitors, events, logs and hosts for corroborating signals on a site alarm. |
| url | https://docs.datadoghq.com/bits_ai/mcp_server/ |
| transport | remote |
| maturity | preview |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (5) | `dd_search_datadog_monitors`, `dd_search_datadog_events`, `dd_get_datadog_metric`, `dd_search_datadog_logs`, `dd_search_datadog_hosts` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `DD_API_KEY`, `DD_APP_KEY` |
| auth note | OAuth or API+application key pair; SaaS outside Kenya — reg 41(2) record and redaction before any argument leaves |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `es` — Elastic Agent Builder MCP server

| Property | Value |
|---|---|
| purpose | Search the alarm and syslog indices for the raw events behind an alarm fingerprint. |
| url | https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/mcp-server |
| transport | streamable_http |
| maturity | ga |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (3) | `es_platform.core.search`, `es_platform.core.list_indices`, `es_platform.core.get_index_mapping` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `KIBANA_URL`, `ELASTIC_API_KEY` |
| auth note | API-key header against {KIBANA_URL}/api/agent_builder/mcp; GA on Serverless and Stack 9.3+, preview on 9.2 |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.3 EnrichmentAgent

*Site/region CMDB + user estimate*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `none` |
| in the workflow graph | yes — ENRICH |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `site.lookup`, `impact.users.estimate`, `tt.classify` |
| tags | lifecycle |
| native tools | `classify_tt`, `estimate_users_affected`, `lookup_site` |
| data it may see | network, counts, coordinates |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 2 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `nbx` | netbox-mcp-server | stdio | preview | yes | local | read_only |
| `snow` | ServiceNow MCP Server Console | remote | preview | yes | abroad | read_only |

#### `nbx` — netbox-mcp-server

| Property | Value |
|---|---|
| purpose | Look up site, device and circuit records in NetBox to enrich an incident with CMDB facts. |
| url | https://github.com/netboxlabs/netbox-mcp-server |
| transport | stdio |
| maturity | preview |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (3) | `nbx_get_objects`, `nbx_get_object_by_id`, `nbx_get_changelogs` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `NETBOX_URL`, `NETBOX_TOKEN` |
| auth note | read-only by default; no plugin surface |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `snow` — ServiceNow MCP Server Console

| Property | Value |
|---|---|
| purpose | Read CMDB configuration items published by the operator on the instance's MCP Server Console. |
| url | https://www.servicenow.com/community/now-assist-articles/mcp-server-console-faq/ta-p/3550125 |
| transport | remote |
| maturity | preview |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (1) | `snow_cmdb_ci_lookup` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | OAuth 2.0 authorization-code grant via the instance's Machine Identity Console (inbound integration); streamable HTTP; tools are published by the operator on the Console |
| verified | no — written from documentation that was not confirmed by fetching the URL; treat the tool names as provisional |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.4 SeverityImpactAgent

*P1–P4 + HUB floors + M-PESA tag*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `none` |
| in the workflow graph | yes — SEVERITY |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `priority.compute` |
| tags | lifecycle, deterministic |
| native tools | `priority_engine` |
| data it may see | network, counts, timestamps |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 0 |

**MCP cards: none.** This agent talks to no external server at all — by design, not by omission.

### 6.5 TicketingAgent

*Unique INC + narrative fields*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `claude-fable-5-1` |
| in the workflow graph | yes — TICKET |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `incident.create`, `incident.number.allocate`, `sla.clock.set`, `incident.narrative.draft` |
| tags | lifecycle |
| native tools | `create_incident` |
| data it may see | network, counts, timestamps, redacted_text |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 3 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `snw` | servicenow-mcp (community) | stdio | community | no | local | read_only, write_behind_hitl |
| `jira` | Atlassian Rovo MCP Server (Jira) | remote | ga | yes | abroad | read_only, write_behind_hitl |
| `jiradc` | mcp-atlassian (Jira Data Center) | stdio | community | no | local | read_only |

#### `snw` — servicenow-mcp (community)

| Property | Value |
|---|---|
| purpose | Mirror a NOC incident into the ServiceNow incident table once a human approves the sync. |
| url | https://github.com/osomai/servicenow-mcp |
| transport | stdio |
| maturity | community |
| vendor-official | no |
| residency | local |
| access | read_only, write_behind_hitl |
| read tools (1) | `snw_list_incidents` |
| write tools (4) | `snw_create_incident`, `snw_update_incident`, `snw_add_comment`, `snw_resolve_incident` |
| HITL gate | `APPROVE_TICKET_SYNC` — the orchestrator calls these only after a human approves that HITL task; the model is never offered them |
| auth env vars | `SERVICENOW_INSTANCE_URL`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` |
| auth note | SERVICENOW_AUTH_TYPE=basic\|oauth\|api_key; MCP_TOOL_PACKAGE limits the loaded tools; the backing instance is SaaS |
| verified | no — written from documentation that was not confirmed by fetching the URL; treat the tool names as provisional |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `jira` — Atlassian Rovo MCP Server (Jira)

| Property | Value |
|---|---|
| purpose | Mirror a NOC incident into Jira Cloud once a human approves the sync. |
| url | https://github.com/atlassian/atlassian-mcp-server |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only, write_behind_hitl |
| read tools (3) | `jira_getJiraIssue`, `jira_searchJiraIssuesUsingJql`, `jira_listJiraIssueTransitions` |
| write tools (4) | `jira_createJiraIssue`, `jira_editJiraIssue`, `jira_transitionJiraIssue`, `jira_addOrEditJiraIssueComment` |
| HITL gate | `APPROVE_TICKET_SYNC` — the orchestrator calls these only after a human approves that HITL task; the model is never offered them |
| auth env vars | — |
| auth note | https://mcp.atlassian.com/v2/mcp; OAuth 2.1 or API token; tools discovered on demand |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `jiradc` — mcp-atlassian (Jira Data Center)

| Property | Value |
|---|---|
| purpose | Read existing Jira Data Center issues so a new NOC ticket can reference the on-prem record. |
| url | https://github.com/sooperset/mcp-atlassian |
| transport | stdio |
| maturity | community |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (2) | `jiradc_jira_get_issue`, `jiradc_jira_search` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `JIRA_URL`, `JIRA_PERSONAL_TOKEN` |
| auth note | READ_ONLY_MODE=true on the server; personal access token for Jira Data Center |
| verified | no — written from documentation that was not confirmed by fetching the URL; treat the tool names as provisional |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.6 DispatchAssignmentAgent

*FE vs MSP matrix*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `none` |
| in the workflow graph | yes — ASSIGN |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `assignment.route`, `msp.lane.match` |
| tags | lifecycle |
| native tools | `assign_incident` |
| data it may see | network, timestamps, role_tokens |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 2 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `pd` | PagerDuty MCP Server (hosted) | remote | ga | yes | abroad | read_only, write_behind_hitl |
| `graf` | mcp-grafana | stdio | preview | yes | local | read_only |

#### `pd` — PagerDuty MCP Server (hosted)

| Property | Value |
|---|---|
| purpose | Read on-call schedules and escalation policies, and page a responder only after a human approves. |
| url | https://support.pagerduty.com/main/docs/pagerduty-mcp-server |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only, write_behind_hitl |
| read tools (4) | `pd_browse_schedules`, `pd_browse_escalation_policies`, `pd_browse_users`, `pd_browse_teams` |
| write tools (1) | `pd_manage_incidents` |
| HITL gate | `APPROVE_PAGE` — the orchestrator calls these only after a human approves that HITL task; the model is never offered them |
| auth env vars | `PAGERDUTY_API_KEY` |
| auth note | https://mcp.pagerduty.com/mcp; 'Authorization: Token token=<key>' or an OAuth bearer token |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `graf` — mcp-grafana

| Property | Value |
|---|---|
| purpose | Read Grafana OnCall schedules and the current on-call users when choosing a field engineer. |
| url | https://github.com/grafana/mcp-grafana |
| transport | stdio |
| maturity | preview |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (5) | `graf_list_oncall_schedules`, `graf_get_oncall_shift`, `graf_get_current_oncall_users`, `graf_list_oncall_teams`, `graf_list_oncall_users` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` |
| auth note | — |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.7 BroadcastCommsAgent

*RNIO/FE/MSP notifications*

| Field | Value |
|---|---|
| criticality | fail_closed |
| model tier | `claude-opus-5` |
| in the workflow graph | yes — BROADCAST |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A1 |
| card version | 1.0 |
| skills | `broadcast.draft`, `sms.send`, `email.send` |
| tags | lifecycle, comms |
| native tools | `draft_broadcast`, `send_email`, `send_sms` |
| data it may see | network, counts, timestamps, role_tokens, redacted_text |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 2 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `slack` | Slack MCP Server | remote | ga | yes | abroad | read_only, write_behind_hitl |
| `gmail` | Gmail MCP server | remote | preview | yes | abroad | read_only |

#### `slack` — Slack MCP Server

| Property | Value |
|---|---|
| purpose | Read the NOC channel and post an approved incident notification to it. |
| url | https://docs.slack.dev/ai/slack-mcp-server/ |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only, write_behind_hitl |
| read tools (3) | `slack_slack_read_channel`, `slack_slack_read_thread`, `slack_slack_list_user_channels` |
| write tools (1) | `slack_slack_send_message` |
| HITL gate | `APPROVE_BROADCAST` — the orchestrator calls these only after a human approves that HITL task; the model is never offered them |
| auth env vars | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` |
| auth note | https://mcp.slack.com/mcp; confidential OAuth 2.0 client (user token), streamable HTTP |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `gmail` — Gmail MCP server

| Property | Value |
|---|---|
| purpose | Read replies on a notification thread; sending stays on the SMTP adapter because the server has no send tool. |
| url | https://developers.google.com/workspace/gmail/api/reference/mcp |
| transport | remote |
| maturity | preview |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (2) | `gmail_search_threads`, `gmail_get_thread` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | https://gmailmcp.googleapis.com/mcp/v1; OAuth 2.0 (Workspace Developer Preview); 10 tools, NO send tool |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.8 ExecutiveBriefingAgent

*Exec brief to cut phone spam*

| Field | Value |
|---|---|
| criticality | fail_soft |
| model tier | `claude-opus-5` |
| in the workflow graph | yes — EXEC_BRIEF |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A1 |
| card version | 1.0 |
| skills | `brief.draft`, `brief.upsert` |
| tags | lifecycle, advisory, comms |
| native tools | `upsert_status_brief` |
| data it may see | network, counts, timestamps, redacted_text |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 2 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `slack` | Slack MCP Server | remote | ga | yes | abroad | read_only |
| `conf` | Atlassian Rovo MCP Server (Confluence) | remote | ga | yes | abroad | read_only |

#### `slack` — Slack MCP Server

| Property | Value |
|---|---|
| purpose | Search executive-channel history and read the status canvas before drafting a brief. |
| url | https://docs.slack.dev/ai/slack-mcp-server/ |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (2) | `slack_slack_search_messages`, `slack_slack_read_canvas` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` |
| auth note | https://mcp.slack.com/mcp; confidential OAuth 2.0 client (user token), streamable HTTP |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `conf` — Atlassian Rovo MCP Server (Confluence)

| Property | Value |
|---|---|
| purpose | Find and read the Confluence status page that the executive brief summarises. |
| url | https://github.com/atlassian/atlassian-mcp-server |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (2) | `conf_searchConfluence`, `conf_getConfluenceContent` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | https://mcp.atlassian.com/v2/mcp; OAuth 2.1 or API token; tools discovered on demand |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.9 ShiftLedgerAgent

*Excel shift ledger*

| Field | Value |
|---|---|
| criticality | fail_soft |
| model tier | `none` |
| in the workflow graph | yes — LEDGER |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `ledger.row.append` |
| tags | lifecycle, records |
| native tools | `append_excel_row` |
| data it may see | network, counts, timestamps, role_tokens |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 3 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `sheets` | Google Sheets MCP server | remote | preview | yes | abroad | read_only, write_behind_hitl |
| `xlsx` | excel-mcp-server | stdio | community | no | local | read_only |
| `fs` | filesystem (MCP reference server) | stdio | reference | no | local | read_only |

#### `sheets` — Google Sheets MCP server

| Property | Value |
|---|---|
| purpose | Mirror the shift ledger into a shared Google Sheet once a human approves the sync. |
| url | https://developers.google.com/workspace/sheets/api/guides/configure-mcp-server |
| transport | remote |
| maturity | preview |
| vendor-official | yes |
| residency | abroad |
| access | read_only, write_behind_hitl |
| read tools (2) | `sheets_get_spreadsheet`, `sheets_get_values` |
| write tools (2) | `sheets_update_values`, `sheets_insert_dimension` |
| HITL gate | `APPROVE_LEDGER_SYNC` — the orchestrator calls these only after a human approves that HITL task; the model is never offered them |
| auth env vars | — |
| auth note | https://sheetsmcp.googleapis.com/mcp/v1; OAuth 2.0 (Workspace Developer Preview); no append tool — a row is insert_dimension + update_values |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `xlsx` — excel-mcp-server

| Property | Value |
|---|---|
| purpose | Read the ledger workbook back for reconciliation; the append itself stays on openpyxl. |
| url | https://github.com/haris-musa/excel-mcp-server |
| transport | stdio |
| maturity | community |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (2) | `xlsx_read_data_from_excel`, `xlsx_get_workbook_metadata` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `EXCEL_FILES_PATH` |
| auth note | no credentials; EXCEL_FILES_PATH scopes the workbooks; v0.1.8 pins fastmcp<3 |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `fs` — filesystem (MCP reference server)

| Property | Value |
|---|---|
| purpose | List and read the ledger files under data/shift_ledgers and nothing else. |
| url | https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem |
| transport | stdio |
| maturity | reference |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (4) | `fs_read_text_file`, `fs_list_directory`, `fs_get_file_info`, `fs_list_allowed_directories` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | no credentials; the only allowed directory is data/shift_ledgers, passed as a CLI argument |
| verified | no — written from documentation that was not confirmed by fetching the URL; treat the tool names as provisional |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.10 RecurrenceProblemAgent

*Chronic site problems*

| Field | Value |
|---|---|
| criticality | fail_soft |
| model tier | `claude-fable-5-1` |
| in the workflow graph | yes — RECURRENCE |
| run kind (`agent_runs.graph_name`) | `incident_lifecycle` |
| trigger | EVENT |
| autonomy | A0 |
| card version | 1.0 |
| skills | `recurrence.count`, `problem.upsert` |
| tags | lifecycle, advisory |
| native tools | `count_recurrence`, `create_or_update_problem` |
| data it may see | network, counts, timestamps |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 3 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `es` | Elastic Agent Builder MCP server | streamable_http | ga | yes | local | read_only |
| `tbx` | MCP Toolbox for Databases | stdio | ga | no | local | read_only |
| `qd` | mcp-server-qdrant | stdio | preview | yes | local | read_only |

#### `es` — Elastic Agent Builder MCP server

| Property | Value |
|---|---|
| purpose | Aggregate alarm history per site and failure domain with ES\|QL to detect chronic problems. |
| url | https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/mcp-server |
| transport | streamable_http |
| maturity | ga |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (3) | `es_platform.core.execute_esql`, `es_platform.core.generate_esql`, `es_platform.core.get_document_by_id` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `KIBANA_URL`, `ELASTIC_API_KEY` |
| auth note | API-key header against {KIBANA_URL}/api/agent_builder/mcp; GA on Serverless and Stack 9.3+, preview on 9.2 |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `tbx` — MCP Toolbox for Databases

| Property | Value |
|---|---|
| purpose | Run the fixed, parameterised recurrence queries declared in tools.yaml against the incident database. |
| url | https://github.com/googleapis/mcp-toolbox |
| transport | stdio |
| maturity | ga |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (2) | `tbx_incidents_by_site_window`, `tbx_problems_by_signature` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | sources and fixed-SQL tools live in tools.yaml; the unrestricted execute_sql prebuilt tool is never loaded |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `qd` — mcp-server-qdrant

| Property | Value |
|---|---|
| purpose | Find prior problem records similar to the current incident signature. |
| url | https://github.com/qdrant/mcp-server-qdrant |
| transport | stdio |
| maturity | preview |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (1) | `qd_qdrant-find` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `QDRANT_URL`, `QDRANT_API_KEY` |
| auth note | QDRANT_READ_ONLY=true; COLLECTION_NAME scopes one collection per card |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.11 WorklogMonitorAgent

*Notes + SLA watch*

| Field | Value |
|---|---|
| criticality | fail_soft |
| model tier | `claude-opus-5` |
| in the workflow graph | yes — MONITOR |
| run kind (`agent_runs.graph_name`) | `monitor` |
| trigger | SCHEDULE |
| autonomy | A0 |
| card version | 1.0 |
| skills | `incident.status.read`, `sla.clock.read`, `worklog.chase` |
| tags | scheduled, advisory |
| native tools | `flag_sla_watch` |
| data it may see | network, counts, timestamps, role_tokens, redacted_text |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 3 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `graf` | mcp-grafana | stdio | preview | yes | local | read_only |
| `zbx` | zabbix-mcp-server | streamable_http | community | no | local | read_only |
| `pd` | PagerDuty MCP Server (hosted) | remote | ga | yes | abroad | read_only |

#### `graf` — mcp-grafana

| Property | Value |
|---|---|
| purpose | Watch the state of a specific alert group and annotations while an incident is open. |
| url | https://github.com/grafana/mcp-grafana |
| transport | stdio |
| maturity | preview |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (4) | `graf_get_alert_group`, `graf_get_annotations`, `graf_list_incidents`, `graf_get_incident` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` |
| auth note | — |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `zbx` — zabbix-mcp-server

| Property | Value |
|---|---|
| purpose | Poll active problems, host status and SLA indicators to notice a silent restore or breach. |
| url | https://github.com/initMAX/zabbix-mcp-server |
| transport | streamable_http |
| maturity | community |
| vendor-official | no |
| residency | local |
| access | read_only |
| read tools (6) | `zbx_problem_active_get`, `zbx_host_status_get`, `zbx_alert_get`, `zbx_sla_get`, `zbx_sla_getsli`, `zbx_maintenance_get` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `ZABBIX_URL`, `ZABBIX_API_TOKEN` |
| auth note | per-server read_only=true in the server's TOML config; token referenced as ${ZABBIX_API_TOKEN} |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `pd` — PagerDuty MCP Server (hosted)

| Property | Value |
|---|---|
| purpose | Read PagerDuty incident notes and log entries to chase an unacknowledged page. |
| url | https://support.pagerduty.com/main/docs/pagerduty-mcp-server |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (2) | `pd_browse_incidents`, `pd_browse_activity` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `PAGERDUTY_API_KEY` |
| auth note | https://mcp.pagerduty.com/mcp; 'Authorization: Token token=<key>' or an OAuth bearer token |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

### 6.12 ShiftHandoverAgent

*Day/night handover package*

| Field | Value |
|---|---|
| criticality | fail_soft |
| model tier | `claude-opus-5` |
| in the workflow graph | no — not wired into the graph |
| run kind (`agent_runs.graph_name`) | `handover` |
| trigger | REQUEST |
| autonomy | A1 |
| card version | 1.0 |
| skills | `handover.build`, `handover.email.send` |
| tags | advisory, comms, request |
| native tools | `build_handover`, `send_handover_email` |
| data it may see | network, counts, timestamps, role_tokens, redacted_text |
| data it must never see | msisdn, customer, cdr, location_trace, mpesa, raw_names |
| MCP cards | 4 |

**MCP cards**

| Namespace | Server | Transport | Maturity | Vendor-official | Residency | Access |
|---|---|---|---|---|---|---|
| `gmail` | Gmail MCP server | remote | preview | yes | abroad | read_only |
| `wiq` | Microsoft Work IQ Mail MCP server | remote | preview | yes | abroad | read_only |
| `conf` | Atlassian Rovo MCP Server (Confluence) | remote | ga | yes | abroad | read_only |
| `qd` | mcp-server-qdrant | stdio | preview | yes | local | read_only |

#### `gmail` — Gmail MCP server

| Property | Value |
|---|---|
| purpose | Read the outgoing shift's drafted notes and labelled handover mail; sending stays on the SMTP adapter. |
| url | https://developers.google.com/workspace/gmail/api/reference/mcp |
| transport | remote |
| maturity | preview |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (3) | `gmail_get_message`, `gmail_list_drafts`, `gmail_list_labels` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | https://gmailmcp.googleapis.com/mcp/v1; OAuth 2.0 (Workspace Developer Preview); 10 tools, NO send tool |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `wiq` — Microsoft Work IQ Mail MCP server

| Property | Value |
|---|---|
| purpose | Search the shift mailbox for vendor promises and open threads to carry into the handover package. |
| url | https://learn.microsoft.com/en-us/microsoft-agent-365/tooling-servers-overview |
| transport | remote |
| maturity | preview |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (3) | `wiq_mcp_MailTools_graph_mail_searchMessages`, `wiq_mcp_MailTools_graph_mail_getMessage`, `wiq_mcp_MailTools_graph_mail_listSent` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | Entra ID OAuth through a registered public client; tenant URL https://agent365.svc.cloud.microsoft/agents/tenants/{tenantId}/servers/mcp_MailTools; needs a Microsoft 365 Copilot licence; Microsoft may rename preview tools |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `conf` — Atlassian Rovo MCP Server (Confluence)

| Property | Value |
|---|---|
| purpose | List handover pages and open Confluence tasks for the carry-forward section. |
| url | https://github.com/atlassian/atlassian-mcp-server |
| transport | remote |
| maturity | ga |
| vendor-official | yes |
| residency | abroad |
| access | read_only |
| read tools (3) | `conf_listConfluenceContent`, `conf_listConfluenceTasks`, `conf_getConfluenceTask` |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | — |
| auth note | https://mcp.atlassian.com/v2/mcp; OAuth 2.1 or API token; tools discovered on demand |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

#### `qd` — mcp-server-qdrant

| Property | Value |
|---|---|
| purpose | Declared for shift-memo recall; no tool exposed yet because qd_qdrant-find is owned by RecurrenceProblemAgent and qdrant-store needs the Phase 2 APPROVE_HANDOVER gate. |
| url | https://github.com/qdrant/mcp-server-qdrant |
| transport | stdio |
| maturity | preview |
| vendor-official | yes |
| residency | local |
| access | read_only |
| read tools (0) | — |
| write tools (0) | — |
| HITL gate | not applicable — this card declares no write tools |
| auth env vars | `QDRANT_URL`, `QDRANT_API_KEY` |
| auth note | QDRANT_READ_ONLY=true; COLLECTION_NAME scopes one collection per card |
| verified | yes — URL fetched and read 2026-09-16; the server was never exercised live |
| defer_loading | yes |
| MCP spec revision | 2026-07-28 |

## 7. The LLM layer

The LLM is an **assist layer**, never a dependency. `model_tier` on an agent names the model its assist endpoint asks for *first*; it does not mean the agent needs a model to do its job. Every agent below has a deterministic template path that produces a complete, sendable result with no model in the loop, and that path is what runs by default.

### 7.1 Tiers present in the registry

| Model tier | Agents | Count |
|---|---|---|
| `none` | DispatchAssignmentAgent, EnrichmentAgent, IngestCorrelationAgent, SeverityImpactAgent, ShiftLedgerAgent | 5 |
| `claude-fable-5-1` | RecurrenceProblemAgent, SupervisorAgent, TicketingAgent | 3 |
| `claude-opus-5` | BroadcastCommsAgent, ExecutiveBriefingAgent, ShiftHandoverAgent, WorklogMonitorAgent | 4 |

**`none`** — No LLM at all. The agent is deterministic Python; there is no assist endpoint to call and no prompt anywhere in its path.

**`claude-fable-5-1`** — `MODEL_REASONING` in `src/noc_agents/llm/client.py` — the reasoning route (incident analysis, root-cause hypothesis, supervisor recommendation). Thinking is always on for this model and thinking tokens count against `max_tokens`, which is why the route gets its own longer budget (`LLM_REASONING_TIMEOUT_S`, default 60 s, against 20 s for drafting). It is also the more expensive tier per token, so it is used on the fewest agents.

**`claude-opus-5`** — `MODEL_DRAFTING` (and `MODEL_FALLBACK`) in `src/noc_agents/llm/client.py` — the drafting route: executive brief, broadcast wording, handover prose. Short, structured output under the 20 s `LLM_TIMEOUT_S` budget, and the model the reasoning route falls back to when it errors or refuses.

### 7.2 How the layer is gated

`get_llm()` in `src/noc_agents/llm/client.py` returns a client only when **all three** of these hold, and returns `None` (never raises) otherwise:

1. **`LLM_ENABLED`** is truthy (`1`, `true`, `yes`, `on`). The default is `false`.
2. **A credential is present** — a non-empty `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`. `credential_present()` only tests it for emptiness; the value itself is read by the SDK, and is never logged, returned or copied anywhere in this codebase.
3. **The `anthropic` package imports.** It is an optional dependency, and any import-time failure — not just `ImportError`, but a broken or partial install — reads as "SDK unavailable".

Because `get_llm()` returns `None` rather than raising, a missing key, a missing package and a disabled flag are the same thing to every caller: the template path runs. The consequence worth stating plainly is that **a key on its own cannot switch the layer on** — `LLM_ENABLED` must be set deliberately as well.

`GET /api/v1/llm/status` reports `enabled`, `sdk_installed`, `credential_present`, `complex_model` and `standard_model` — booleans and model ids, never secret material. Request budgets are bounded on both routes (`LLM_TIMEOUT_S`, `LLM_REASONING_TIMEOUT_S`, `LLM_MAX_RETRIES`) so a hung request cannot hold an assist slot indefinitely.

One discrepancy worth knowing about while reading `.env.example`: it documents `LLM_ALLOW_AUTH_TOKEN=false`, implying a subscription token is honoured only when that flag is set, but `credential_present()` accepts `ANTHROPIC_AUTH_TOKEN` today without consulting the flag. The spec's G13 guard (explicit `api_key=`, no on-disk OAuth profile) is not implemented in this phase. Section 8 explains why that distinction matters.

## 8. Licensing: two lanes, and they do not substitute for each other

These are two different products with two different agreements, and conflating them is the most common way a project like this ends up out of compliance:

**Lane 1 — building this repository.** A Claude **Max subscription** covers a developer using Claude Code to write, refactor, review and test the code in this repository. That is a human using an assistant to do engineering work.

**Lane 2 — the application calling Claude at runtime.** A subscription does **not** grant the application API access. For this NOC system to call Claude while it is running — drafting an executive brief, analysing an incident — it needs an **API key from the Claude Console** (`ANTHROPIC_API_KEY`), billed separately from the subscription. Anthropic's guidance is explicit: "Developers building products or services that interact with Claude's capabilities … should use API key authentication through Claude Console" (https://code.claude.com/docs/en/legal-and-compliance).

Practically, that means subscription credentials — an OAuth profile left on a developer's machine by Claude Code, or a subscription token pasted into `ANTHROPIC_AUTH_TOKEN` — must never be what powers the backend. The two lanes are billed differently, governed differently, and metered differently.

**And the part that keeps this simple: the application runs fully without any key.** `LLM_ENABLED=false` is the default, every LLM path has a deterministic template fallback, and the entire test suite runs with the key explicitly blanked. The LLM improves the wording of some outputs; it is not load-bearing for a single incident-lifecycle step.

## Appendix A — the full `(namespace, tool)` matrix

Every tool name declared anywhere in the registry: **99 pairs** (87 readable by a model, 12 write tools behind a HITL gate), sorted by namespace then tool.

Uniqueness of the `(namespace, tool)` pair is an **import-time invariant**: `src/noc_agents/orchestrator/registry.py` asserts it when the module loads, so a duplicate is a startup failure rather than a runtime ambiguity about which server a call was meant for. That is why two agents sharing one server (Grafana, Zabbix, PagerDuty, Slack, Gmail, Elastic, Confluence, Qdrant) always carry *disjoint* tool lists — the card is per agent, not per server.

| Exposed name | Namespace | Tool | Kind | Declared by | Server | Residency |
|---|---|---|---|---|---|---|
| `conf_getConfluenceContent` | `conf` | `getConfluenceContent` | read | ExecutiveBriefingAgent | Atlassian Rovo MCP Server (Confluence) | abroad |
| `conf_getConfluenceTask` | `conf` | `getConfluenceTask` | read | ShiftHandoverAgent | Atlassian Rovo MCP Server (Confluence) | abroad |
| `conf_listConfluenceContent` | `conf` | `listConfluenceContent` | read | ShiftHandoverAgent | Atlassian Rovo MCP Server (Confluence) | abroad |
| `conf_listConfluenceTasks` | `conf` | `listConfluenceTasks` | read | ShiftHandoverAgent | Atlassian Rovo MCP Server (Confluence) | abroad |
| `conf_searchConfluence` | `conf` | `searchConfluence` | read | ExecutiveBriefingAgent | Atlassian Rovo MCP Server (Confluence) | abroad |
| `dd_get_datadog_metric` | `dd` | `get_datadog_metric` | read | IngestCorrelationAgent | Datadog MCP Server | abroad |
| `dd_search_datadog_events` | `dd` | `search_datadog_events` | read | IngestCorrelationAgent | Datadog MCP Server | abroad |
| `dd_search_datadog_hosts` | `dd` | `search_datadog_hosts` | read | IngestCorrelationAgent | Datadog MCP Server | abroad |
| `dd_search_datadog_logs` | `dd` | `search_datadog_logs` | read | IngestCorrelationAgent | Datadog MCP Server | abroad |
| `dd_search_datadog_monitors` | `dd` | `search_datadog_monitors` | read | IngestCorrelationAgent | Datadog MCP Server | abroad |
| `es_platform.core.execute_esql` | `es` | `platform.core.execute_esql` | read | RecurrenceProblemAgent | Elastic Agent Builder MCP server | local |
| `es_platform.core.generate_esql` | `es` | `platform.core.generate_esql` | read | RecurrenceProblemAgent | Elastic Agent Builder MCP server | local |
| `es_platform.core.get_document_by_id` | `es` | `platform.core.get_document_by_id` | read | RecurrenceProblemAgent | Elastic Agent Builder MCP server | local |
| `es_platform.core.get_index_mapping` | `es` | `platform.core.get_index_mapping` | read | IngestCorrelationAgent | Elastic Agent Builder MCP server | local |
| `es_platform.core.list_indices` | `es` | `platform.core.list_indices` | read | IngestCorrelationAgent | Elastic Agent Builder MCP server | local |
| `es_platform.core.search` | `es` | `platform.core.search` | read | IngestCorrelationAgent | Elastic Agent Builder MCP server | local |
| `fs_get_file_info` | `fs` | `get_file_info` | read | ShiftLedgerAgent | filesystem (MCP reference server) | local |
| `fs_list_allowed_directories` | `fs` | `list_allowed_directories` | read | ShiftLedgerAgent | filesystem (MCP reference server) | local |
| `fs_list_directory` | `fs` | `list_directory` | read | ShiftLedgerAgent | filesystem (MCP reference server) | local |
| `fs_read_text_file` | `fs` | `read_text_file` | read | ShiftLedgerAgent | filesystem (MCP reference server) | local |
| `gmail_get_message` | `gmail` | `get_message` | read | ShiftHandoverAgent | Gmail MCP server | abroad |
| `gmail_get_thread` | `gmail` | `get_thread` | read | BroadcastCommsAgent | Gmail MCP server | abroad |
| `gmail_list_drafts` | `gmail` | `list_drafts` | read | ShiftHandoverAgent | Gmail MCP server | abroad |
| `gmail_list_labels` | `gmail` | `list_labels` | read | ShiftHandoverAgent | Gmail MCP server | abroad |
| `gmail_search_threads` | `gmail` | `search_threads` | read | BroadcastCommsAgent | Gmail MCP server | abroad |
| `graf_get_alert_group` | `graf` | `get_alert_group` | read | WorklogMonitorAgent | mcp-grafana | local |
| `graf_get_annotations` | `graf` | `get_annotations` | read | WorklogMonitorAgent | mcp-grafana | local |
| `graf_get_current_oncall_users` | `graf` | `get_current_oncall_users` | read | DispatchAssignmentAgent | mcp-grafana | local |
| `graf_get_incident` | `graf` | `get_incident` | read | WorklogMonitorAgent | mcp-grafana | local |
| `graf_get_oncall_shift` | `graf` | `get_oncall_shift` | read | DispatchAssignmentAgent | mcp-grafana | local |
| `graf_list_alert_groups` | `graf` | `list_alert_groups` | read | IngestCorrelationAgent | mcp-grafana | local |
| `graf_list_datasources` | `graf` | `list_datasources` | read | IngestCorrelationAgent | mcp-grafana | local |
| `graf_list_incidents` | `graf` | `list_incidents` | read | WorklogMonitorAgent | mcp-grafana | local |
| `graf_list_oncall_schedules` | `graf` | `list_oncall_schedules` | read | DispatchAssignmentAgent | mcp-grafana | local |
| `graf_list_oncall_teams` | `graf` | `list_oncall_teams` | read | DispatchAssignmentAgent | mcp-grafana | local |
| `graf_list_oncall_users` | `graf` | `list_oncall_users` | read | DispatchAssignmentAgent | mcp-grafana | local |
| `graf_query_loki_logs` | `graf` | `query_loki_logs` | read | IngestCorrelationAgent | mcp-grafana | local |
| `graf_query_prometheus` | `graf` | `query_prometheus` | read | IngestCorrelationAgent | mcp-grafana | local |
| `jira_addOrEditJiraIssueComment` | `jira` | `addOrEditJiraIssueComment` | write (HITL) | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jira_createJiraIssue` | `jira` | `createJiraIssue` | write (HITL) | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jira_editJiraIssue` | `jira` | `editJiraIssue` | write (HITL) | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jira_getJiraIssue` | `jira` | `getJiraIssue` | read | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jira_listJiraIssueTransitions` | `jira` | `listJiraIssueTransitions` | read | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jira_searchJiraIssuesUsingJql` | `jira` | `searchJiraIssuesUsingJql` | read | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jira_transitionJiraIssue` | `jira` | `transitionJiraIssue` | write (HITL) | TicketingAgent | Atlassian Rovo MCP Server (Jira) | abroad |
| `jiradc_jira_get_issue` | `jiradc` | `jira_get_issue` | read | TicketingAgent | mcp-atlassian (Jira Data Center) | local |
| `jiradc_jira_search` | `jiradc` | `jira_search` | read | TicketingAgent | mcp-atlassian (Jira Data Center) | local |
| `nbx_get_changelogs` | `nbx` | `get_changelogs` | read | EnrichmentAgent | netbox-mcp-server | local |
| `nbx_get_object_by_id` | `nbx` | `get_object_by_id` | read | EnrichmentAgent | netbox-mcp-server | local |
| `nbx_get_objects` | `nbx` | `get_objects` | read | EnrichmentAgent | netbox-mcp-server | local |
| `pd_browse_activity` | `pd` | `browse_activity` | read | WorklogMonitorAgent | PagerDuty MCP Server (hosted) | abroad |
| `pd_browse_escalation_policies` | `pd` | `browse_escalation_policies` | read | DispatchAssignmentAgent | PagerDuty MCP Server (hosted) | abroad |
| `pd_browse_incidents` | `pd` | `browse_incidents` | read | WorklogMonitorAgent | PagerDuty MCP Server (hosted) | abroad |
| `pd_browse_schedules` | `pd` | `browse_schedules` | read | DispatchAssignmentAgent | PagerDuty MCP Server (hosted) | abroad |
| `pd_browse_teams` | `pd` | `browse_teams` | read | DispatchAssignmentAgent | PagerDuty MCP Server (hosted) | abroad |
| `pd_browse_users` | `pd` | `browse_users` | read | DispatchAssignmentAgent | PagerDuty MCP Server (hosted) | abroad |
| `pd_manage_incidents` | `pd` | `manage_incidents` | write (HITL) | DispatchAssignmentAgent | PagerDuty MCP Server (hosted) | abroad |
| `prom_execute_query` | `prom` | `execute_query` | read | IngestCorrelationAgent | prometheus-mcp-server | local |
| `prom_execute_range_query` | `prom` | `execute_range_query` | read | IngestCorrelationAgent | prometheus-mcp-server | local |
| `prom_get_metric_metadata` | `prom` | `get_metric_metadata` | read | IngestCorrelationAgent | prometheus-mcp-server | local |
| `prom_get_targets` | `prom` | `get_targets` | read | IngestCorrelationAgent | prometheus-mcp-server | local |
| `prom_list_metrics` | `prom` | `list_metrics` | read | IngestCorrelationAgent | prometheus-mcp-server | local |
| `qd_qdrant-find` | `qd` | `qdrant-find` | read | RecurrenceProblemAgent | mcp-server-qdrant | local |
| `sheets_get_spreadsheet` | `sheets` | `get_spreadsheet` | read | ShiftLedgerAgent | Google Sheets MCP server | abroad |
| `sheets_get_values` | `sheets` | `get_values` | read | ShiftLedgerAgent | Google Sheets MCP server | abroad |
| `sheets_insert_dimension` | `sheets` | `insert_dimension` | write (HITL) | ShiftLedgerAgent | Google Sheets MCP server | abroad |
| `sheets_update_values` | `sheets` | `update_values` | write (HITL) | ShiftLedgerAgent | Google Sheets MCP server | abroad |
| `slack_slack_list_user_channels` | `slack` | `slack_list_user_channels` | read | BroadcastCommsAgent | Slack MCP Server | abroad |
| `slack_slack_read_canvas` | `slack` | `slack_read_canvas` | read | ExecutiveBriefingAgent | Slack MCP Server | abroad |
| `slack_slack_read_channel` | `slack` | `slack_read_channel` | read | BroadcastCommsAgent | Slack MCP Server | abroad |
| `slack_slack_read_thread` | `slack` | `slack_read_thread` | read | BroadcastCommsAgent | Slack MCP Server | abroad |
| `slack_slack_search_messages` | `slack` | `slack_search_messages` | read | ExecutiveBriefingAgent | Slack MCP Server | abroad |
| `slack_slack_send_message` | `slack` | `slack_send_message` | write (HITL) | BroadcastCommsAgent | Slack MCP Server | abroad |
| `snow_cmdb_ci_lookup` | `snow` | `cmdb_ci_lookup` | read | EnrichmentAgent | ServiceNow MCP Server Console | abroad |
| `snw_add_comment` | `snw` | `add_comment` | write (HITL) | TicketingAgent | servicenow-mcp (community) | local |
| `snw_create_incident` | `snw` | `create_incident` | write (HITL) | TicketingAgent | servicenow-mcp (community) | local |
| `snw_list_incidents` | `snw` | `list_incidents` | read | TicketingAgent | servicenow-mcp (community) | local |
| `snw_resolve_incident` | `snw` | `resolve_incident` | write (HITL) | TicketingAgent | servicenow-mcp (community) | local |
| `snw_update_incident` | `snw` | `update_incident` | write (HITL) | TicketingAgent | servicenow-mcp (community) | local |
| `tbx_incidents_by_site_window` | `tbx` | `incidents_by_site_window` | read | RecurrenceProblemAgent | MCP Toolbox for Databases | local |
| `tbx_problems_by_signature` | `tbx` | `problems_by_signature` | read | RecurrenceProblemAgent | MCP Toolbox for Databases | local |
| `wiq_mcp_MailTools_graph_mail_getMessage` | `wiq` | `mcp_MailTools_graph_mail_getMessage` | read | ShiftHandoverAgent | Microsoft Work IQ Mail MCP server | abroad |
| `wiq_mcp_MailTools_graph_mail_listSent` | `wiq` | `mcp_MailTools_graph_mail_listSent` | read | ShiftHandoverAgent | Microsoft Work IQ Mail MCP server | abroad |
| `wiq_mcp_MailTools_graph_mail_searchMessages` | `wiq` | `mcp_MailTools_graph_mail_searchMessages` | read | ShiftHandoverAgent | Microsoft Work IQ Mail MCP server | abroad |
| `xlsx_get_workbook_metadata` | `xlsx` | `get_workbook_metadata` | read | ShiftLedgerAgent | excel-mcp-server | local |
| `xlsx_read_data_from_excel` | `xlsx` | `read_data_from_excel` | read | ShiftLedgerAgent | excel-mcp-server | local |
| `zbx_alert_get` | `zbx` | `alert_get` | read | WorklogMonitorAgent | zabbix-mcp-server | local |
| `zbx_event_get` | `zbx` | `event_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |
| `zbx_history_get` | `zbx` | `history_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |
| `zbx_host_get` | `zbx` | `host_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |
| `zbx_host_status_get` | `zbx` | `host_status_get` | read | WorklogMonitorAgent | zabbix-mcp-server | local |
| `zbx_hostgroup_get` | `zbx` | `hostgroup_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |
| `zbx_item_get` | `zbx` | `item_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |
| `zbx_maintenance_get` | `zbx` | `maintenance_get` | read | WorklogMonitorAgent | zabbix-mcp-server | local |
| `zbx_problem_active_get` | `zbx` | `problem_active_get` | read | WorklogMonitorAgent | zabbix-mcp-server | local |
| `zbx_problem_get` | `zbx` | `problem_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |
| `zbx_sla_get` | `zbx` | `sla_get` | read | WorklogMonitorAgent | zabbix-mcp-server | local |
| `zbx_sla_getsli` | `zbx` | `sla_getsli` | read | WorklogMonitorAgent | zabbix-mcp-server | local |
| `zbx_trigger_get` | `zbx` | `trigger_get` | read | IngestCorrelationAgent | zabbix-mcp-server | local |

---

Generated from `src/noc_agents/orchestrator/registry.py` by `scripts/render_agent_docs.py`. To change anything above, change the registry and run `python scripts/render_agent_docs.py --write`.
