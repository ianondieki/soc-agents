"""Agent registry: the single source for the workflow topology and the agent catalog.

``AGENT_PROFILES`` has one entry per agent (order == ``GET /api/v1/agents``);
``NODE_CARDS`` has one entry per workflow node (order == execution order and
``WORKFLOW_NODES``). To add an agent: write one module in ``noc_agents.agents`` that
exposes ``run(state, ctx)`` and ``input_summary(state, ctx)``, add a ``NodeCard`` here
and, if the agent name is new, an ``AgentProfile``. Nothing is written twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from noc_agents.agents import (
    assign,
    broadcast,
    correlate,
    enrich,
    exec_brief,
    hitl,
    ingest,
    ledger,
    monitor,
    recurrence,
    severity,
    ticket,
)
from noc_agents.domain.enums import HitlTaskType
from noc_agents.orchestrator.contract import FAIL_CLOSED, FAIL_SOFT, IncidentState, RunContext, StepResult


@dataclass(frozen=True)
class McpRequirement:
    """An external MCP server an agent can use (declarative; nothing here connects to it)."""

    server: str
    url: str
    transport: str  # "stdio" | "streamable_http" | "remote"
    vendor_official: bool  # published by the product's own vendor (else community/reference)
    maturity: str  # "ga" | "preview" | "alpha" | "community" | "reference"
    access: tuple[str, ...]  # non-empty subset of ("read_only", "write_behind_hitl")
    auth_env: tuple[str, ...] = ()  # real environment variable names only
    auth_note: str = ""  # free text for OAuth / API-key schemes with no fixed env var
    verified: bool = False  # URL fetched and checked; never exercised live
    purpose: str = ""
    # v2 additions (§5.2)
    namespace: str = ""  # short prefix the registry owns, e.g. "owm", "graf", "zbx"; tools exposed as f"{namespace}_{tool}"
    tools: tuple[str, ...] = ()  # tool names (without namespace) the model MAY see (read-only)
    write_tools: tuple[str, ...] = ()  # tools only the orchestrator calls after a HITL approval
    hitl_task_type: str | None = None  # REQUIRED when write_tools is non-empty
    defer_loading: bool = True  # Anthropic tool-search deferral default
    spec_version: str = "2026-07-28"  # MCP spec revision the card was written against
    residency: str = "local"  # "local" (stdio/private) | "kenya" | "abroad" (SaaS; needs reg 41(2) record)

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "url": self.url,
            "transport": self.transport,
            "vendor_official": self.vendor_official,
            "maturity": self.maturity,
            "access": list(self.access),
            "auth_env": list(self.auth_env),
            "auth_note": self.auth_note,
            "verified": self.verified,
            "purpose": self.purpose,
            "namespace": self.namespace,
            "tools": list(self.tools),
            "write_tools": list(self.write_tools),
            "hitl_task_type": self.hitl_task_type,
            "defer_loading": self.defer_loading,
            "spec_version": self.spec_version,
            "residency": self.residency,
        }


@dataclass(frozen=True)
class AgentProfile:
    name: str  # exact string used by RunTracker and /agents
    mission: str  # /agents mission text
    criticality: str  # FAIL_CLOSED | FAIL_SOFT — applies to every node of this agent
    model_tier: str = "none"  # model the agent's assist endpoint requests first; "none" = no endpoint
    tools: tuple[str, ...] = ()  # tool names the agent reports today; [0] names the fail-soft tool
    mcp: tuple[McpRequirement, ...] = ()
    # v2 additions (A2A AgentCard/AgentSkill vocabulary; documentation + /agents only)
    version: str = "1.0"
    skills: tuple[str, ...] = ()  # e.g. ("incident.status.read", "sla.clock.read")
    tags: tuple[str, ...] = ()  # e.g. ("lifecycle", "advisory", "scheduled")
    run_kind: str = "incident_lifecycle"  # agent_runs.graph_name this agent writes when out of band
    trigger: str = "EVENT"  # "EVENT" | "SCHEDULE" | "REQUEST" | "EVENT+SCHEDULE"
    autonomy: str = "A0"  # "A0" | "A1" | "A2"
    data_may_see: tuple[str, ...] = ()  # allowlisted field classes: "network", "counts", "timestamps", "role_tokens", "redacted_text", "coordinates"
    data_must_not_see: tuple[str, ...] = ("msisdn", "customer", "cdr", "location_trace", "mpesa", "raw_names")


@dataclass(frozen=True)
class NodeCard:
    node_id: str  # "INGEST" ... "MONITOR"
    label: str  # WORKFLOW_NODES label
    agent: str  # AgentProfile.name
    run: Callable[[IncidentState, RunContext], StepResult]
    input_summary: Callable[[IncidentState, RunContext], str]  # HITL needs cfg.autonomy_level, hence ctx
    reads: tuple[str, ...] = ()  # documentation only: IncidentState fields the agent reads
    writes: tuple[str, ...] = ()  # documentation only: IncidentState fields the agent writes


# ---------------------------------------------------------------------------------------------
# MCP cards (§7.1.2, declarative — nothing here connects to anything).
#
# One server-facts dict per server; each agent that uses the server gets its own card with a
# DISJOINT tool list (the import-time assert below requires (namespace, tool) to be unique across
# every profile). ``verified=True`` means only that the ``url`` is in Appendix D and was fetched on
# 2026-09-16 (tool names copied from the page); no server was exercised live. ``auth_env`` holds
# environment-variable NAMES only. ``residency="abroad"`` marks SaaS outside Kenya (reg 41(2)).
# ---------------------------------------------------------------------------------------------
_GRAFANA: dict[str, Any] = dict(
    server="mcp-grafana",
    url="https://github.com/grafana/mcp-grafana",
    transport="stdio",
    vendor_official=True,
    maturity="preview",
    auth_env=("GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
    verified=True,
    namespace="graf",
    residency="local",
)
_ZABBIX: dict[str, Any] = dict(
    server="zabbix-mcp-server",
    url="https://github.com/initMAX/zabbix-mcp-server",
    transport="streamable_http",
    vendor_official=False,
    maturity="community",
    auth_env=("ZABBIX_URL", "ZABBIX_API_TOKEN"),
    auth_note="per-server read_only=true in the server's TOML config; token referenced as ${ZABBIX_API_TOKEN}",
    verified=True,
    namespace="zbx",
    residency="local",
)
_ELASTIC: dict[str, Any] = dict(
    server="Elastic Agent Builder MCP server",
    url="https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/mcp-server",
    transport="streamable_http",
    vendor_official=True,
    maturity="ga",
    auth_env=("KIBANA_URL", "ELASTIC_API_KEY"),
    auth_note="API-key header against {KIBANA_URL}/api/agent_builder/mcp; GA on Serverless and Stack 9.3+, preview on 9.2",
    verified=True,
    namespace="es",
    residency="local",
)
_PAGERDUTY: dict[str, Any] = dict(
    server="PagerDuty MCP Server (hosted)",
    url="https://support.pagerduty.com/main/docs/pagerduty-mcp-server",
    transport="remote",
    vendor_official=True,
    maturity="ga",
    auth_env=("PAGERDUTY_API_KEY",),
    auth_note="https://mcp.pagerduty.com/mcp; 'Authorization: Token token=<key>' or an OAuth bearer token",
    verified=True,
    namespace="pd",
    residency="abroad",
)
_SLACK: dict[str, Any] = dict(
    server="Slack MCP Server",
    url="https://docs.slack.dev/ai/slack-mcp-server/",
    transport="remote",
    vendor_official=True,
    maturity="ga",
    auth_env=("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"),
    auth_note="https://mcp.slack.com/mcp; confidential OAuth 2.0 client (user token), streamable HTTP",
    verified=True,
    namespace="slack",
    residency="abroad",
)
_GMAIL: dict[str, Any] = dict(
    server="Gmail MCP server",
    url="https://developers.google.com/workspace/gmail/api/reference/mcp",
    transport="remote",
    vendor_official=True,
    maturity="preview",
    auth_env=(),
    auth_note="https://gmailmcp.googleapis.com/mcp/v1; OAuth 2.0 (Workspace Developer Preview); 10 tools, NO send tool",
    verified=True,
    namespace="gmail",
    residency="abroad",
)
_ROVO_CONFLUENCE: dict[str, Any] = dict(
    server="Atlassian Rovo MCP Server (Confluence)",
    url="https://github.com/atlassian/atlassian-mcp-server",
    transport="remote",
    vendor_official=True,
    maturity="ga",
    auth_env=(),
    auth_note="https://mcp.atlassian.com/v2/mcp; OAuth 2.1 or API token; tools discovered on demand",
    verified=True,
    namespace="conf",
    residency="abroad",
)
_QDRANT: dict[str, Any] = dict(
    server="mcp-server-qdrant",
    url="https://github.com/qdrant/mcp-server-qdrant",
    transport="stdio",
    vendor_official=True,
    maturity="preview",
    auth_env=("QDRANT_URL", "QDRANT_API_KEY"),
    auth_note="QDRANT_READ_ONLY=true; COLLECTION_NAME scopes one collection per card",
    verified=True,
    namespace="qd",
    residency="local",
)

# run_kind / trigger below describe the v2 TARGET roster of §5.1, not today's graph: WorklogMonitorAgent
# is still the MONITOR graph node until Phase 1 moves it to the scheduler (graph_name="monitor"), and
# ShiftHandoverAgent becomes its own run (graph_name="handover") in Phase 2. Autonomy is the first token
# of the §5.1 column. None of these fields is read by the runner.
AGENT_PROFILES: tuple[AgentProfile, ...] = (
    AgentProfile(
        "SupervisorAgent",
        "Holds every P1 and P2 message until a named person approves it.",
        FAIL_CLOSED,
        "claude-fable-5-1",
        ("create_hitl_task",),
        mcp=(),  # humans only (§5.3.1)
        skills=("incident.gate.evaluate", "hitl.task.create", "channel.render"),
        tags=("lifecycle", "gate"),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps", "role_tokens", "redacted_text"),
    ),
    AgentProfile(
        "IngestCorrelationAgent",
        "Reads each alarm and folds repeats and child sites into a ticket already open.",
        FAIL_CLOSED,
        "none",
        ("normalize_event", "find_open_by_fingerprint", "link_parent_hub"),
        mcp=(
            McpRequirement(
                **_GRAFANA,
                access=("read_only",),
                purpose="Query Prometheus datasources and list OnCall alert groups to confirm an alarm before correlating it.",
                tools=("query_prometheus", "list_alert_groups", "list_datasources", "query_loki_logs"),
            ),
            McpRequirement(
                server="prometheus-mcp-server",
                url="https://github.com/pab1it0/prometheus-mcp-server",
                transport="stdio",
                vendor_official=False,
                maturity="community",
                access=("read_only",),
                auth_env=("PROMETHEUS_URL", "PROMETHEUS_TOKEN"),
                verified=True,
                purpose="Run instant and range PromQL queries against the NOC Prometheus to size an alarm burst.",
                namespace="prom",
                tools=("execute_query", "execute_range_query", "list_metrics", "get_metric_metadata", "get_targets"),
                residency="local",
            ),
            McpRequirement(
                **_ZABBIX,
                access=("read_only",),
                purpose="Read current Zabbix problems, events, triggers and host inventory to normalise incoming alarms.",
                tools=("problem_get", "event_get", "trigger_get", "host_get", "hostgroup_get", "item_get", "history_get"),
            ),
            McpRequirement(
                server="Datadog MCP Server",
                url="https://docs.datadoghq.com/bits_ai/mcp_server/",
                transport="remote",
                vendor_official=True,
                maturity="preview",
                access=("read_only",),
                auth_env=("DD_API_KEY", "DD_APP_KEY"),
                auth_note="OAuth or API+application key pair; SaaS outside Kenya — reg 41(2) record and redaction before any argument leaves",
                verified=True,
                purpose="Search Datadog monitors, events, logs and hosts for corroborating signals on a site alarm.",
                namespace="dd",
                tools=("search_datadog_monitors", "search_datadog_events", "get_datadog_metric", "search_datadog_logs", "search_datadog_hosts"),
                residency="abroad",
            ),
            McpRequirement(
                **_ELASTIC,
                access=("read_only",),
                purpose="Search the alarm and syslog indices for the raw events behind an alarm fingerprint.",
                tools=("platform.core.search", "platform.core.list_indices", "platform.core.get_index_mapping"),
            ),
        ),
        skills=("alarm.normalize", "alarm.dedupe", "incident.correlate"),
        tags=("lifecycle",),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps"),
    ),
    AgentProfile(
        "EnrichmentAgent",
        "Looks up the site and its region, and estimates how many subscribers are affected.",
        FAIL_CLOSED,
        "none",
        ("lookup_site", "estimate_users_affected", "classify_tt"),
        mcp=(
            McpRequirement(
                server="netbox-mcp-server",
                url="https://github.com/netboxlabs/netbox-mcp-server",
                transport="stdio",
                vendor_official=True,
                maturity="preview",
                access=("read_only",),
                auth_env=("NETBOX_URL", "NETBOX_TOKEN"),
                auth_note="read-only by default; no plugin surface",
                verified=True,
                purpose="Look up site, device and circuit records in NetBox to enrich an incident with CMDB facts.",
                namespace="nbx",
                tools=("get_objects", "get_object_by_id", "get_changelogs"),
                residency="local",
            ),
            McpRequirement(
                server="ServiceNow MCP Server Console",
                url="https://www.servicenow.com/community/now-assist-articles/mcp-server-console-faq/ta-p/3550125",
                transport="remote",
                vendor_official=True,
                maturity="preview",
                access=("read_only",),
                auth_env=(),
                auth_note="OAuth 2.0 authorization-code grant via the instance's Machine Identity Console (inbound integration); streamable HTTP; tools are published by the operator on the Console",
                verified=False,
                purpose="Read CMDB configuration items published by the operator on the instance's MCP Server Console.",
                namespace="snow",
                tools=("cmdb_ci_lookup",),
                residency="abroad",
            ),
        ),
        skills=("site.lookup", "impact.users.estimate", "tt.classify"),
        tags=("lifecycle",),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "coordinates"),
    ),
    AgentProfile(
        "SeverityImpactAgent",
        "Sets P1–P4 from subscribers affected; a HUB is at least P2; flags M‑PESA at risk.",
        FAIL_CLOSED,
        "none",
        ("priority_engine",),
        mcp=(),
        skills=("priority.compute",),
        tags=("lifecycle", "deterministic"),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps"),
    ),
    AgentProfile(
        "TicketingAgent",
        "Opens the ticket under a new INC number, fills in every field and starts the SLA clocks.",
        FAIL_CLOSED,
        "claude-fable-5-1",
        ("create_incident",),
        mcp=(
            McpRequirement(
                server="servicenow-mcp (community)",
                url="https://github.com/osomai/servicenow-mcp",
                transport="stdio",
                vendor_official=False,
                maturity="community",
                access=("read_only", "write_behind_hitl"),
                auth_env=("SERVICENOW_INSTANCE_URL", "SERVICENOW_USERNAME", "SERVICENOW_PASSWORD"),
                auth_note="SERVICENOW_AUTH_TYPE=basic|oauth|api_key; MCP_TOOL_PACKAGE limits the loaded tools; the backing instance is SaaS",
                verified=False,
                purpose="Mirror a NOC incident into the ServiceNow incident table once a human approves the sync.",
                namespace="snw",
                tools=("list_incidents",),
                write_tools=("create_incident", "update_incident", "add_comment", "resolve_incident"),
                hitl_task_type="APPROVE_TICKET_SYNC",
                residency="local",
            ),
            McpRequirement(
                server="Atlassian Rovo MCP Server (Jira)",
                url="https://github.com/atlassian/atlassian-mcp-server",
                transport="remote",
                vendor_official=True,
                maturity="ga",
                access=("read_only", "write_behind_hitl"),
                auth_env=(),
                auth_note="https://mcp.atlassian.com/v2/mcp; OAuth 2.1 or API token; tools discovered on demand",
                verified=True,
                purpose="Mirror a NOC incident into Jira Cloud once a human approves the sync.",
                namespace="jira",
                tools=("getJiraIssue", "searchJiraIssuesUsingJql", "listJiraIssueTransitions"),
                write_tools=("createJiraIssue", "editJiraIssue", "transitionJiraIssue", "addOrEditJiraIssueComment"),
                hitl_task_type="APPROVE_TICKET_SYNC",
                residency="abroad",
            ),
            McpRequirement(
                server="mcp-atlassian (Jira Data Center)",
                url="https://github.com/sooperset/mcp-atlassian",
                transport="stdio",
                vendor_official=False,
                maturity="community",
                access=("read_only",),
                auth_env=("JIRA_URL", "JIRA_PERSONAL_TOKEN"),
                auth_note="READ_ONLY_MODE=true on the server; personal access token for Jira Data Center",
                verified=False,
                purpose="Read existing Jira Data Center issues so a new NOC ticket can reference the on-prem record.",
                namespace="jiradc",
                tools=("jira_get_issue", "jira_search"),
                residency="local",
            ),
        ),
        skills=("incident.create", "incident.number.allocate", "sla.clock.set", "incident.narrative.draft"),
        tags=("lifecycle",),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps", "redacted_text"),
    ),
    AgentProfile(
        "DispatchAssignmentAgent",
        "Chooses the vendor or field engineer by region and fault type.",
        FAIL_CLOSED,
        "none",
        ("assign_incident",),
        mcp=(
            McpRequirement(
                **_PAGERDUTY,
                access=("read_only", "write_behind_hitl"),
                purpose="Read on-call schedules and escalation policies, and page a responder only after a human approves.",
                tools=("browse_schedules", "browse_escalation_policies", "browse_users", "browse_teams"),
                write_tools=("manage_incidents",),
                hitl_task_type="APPROVE_PAGE",
            ),
            McpRequirement(
                **_GRAFANA,
                access=("read_only",),
                purpose="Read Grafana OnCall schedules and the current on-call users when choosing a field engineer.",
                tools=("list_oncall_schedules", "get_oncall_shift", "get_current_oncall_users", "list_oncall_teams", "list_oncall_users"),
            ),
        ),
        skills=("assignment.route", "msp.lane.match"),
        tags=("lifecycle",),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "timestamps", "role_tokens"),
    ),
    AgentProfile(
        "BroadcastCommsAgent",
        "Drafts the SMS and email to the regional office, field engineer and vendor.",
        FAIL_CLOSED,
        "claude-opus-5",
        ("draft_broadcast", "send_sms", "send_email"),
        mcp=(
            McpRequirement(
                **_SLACK,
                access=("read_only", "write_behind_hitl"),
                purpose="Read the NOC channel and post an approved incident notification to it.",
                tools=("slack_read_channel", "slack_read_thread", "slack_list_user_channels"),
                write_tools=("slack_send_message",),
                hitl_task_type="APPROVE_BROADCAST",
            ),
            McpRequirement(
                **_GMAIL,
                access=("read_only",),
                purpose="Read replies on a notification thread; sending stays on the SMTP adapter because the server has no send tool.",
                tools=("search_threads", "get_thread"),
            ),
        ),
        skills=("broadcast.draft", "sms.send", "email.send"),
        tags=("lifecycle", "comms"),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A1",
        data_may_see=("network", "counts", "timestamps", "role_tokens", "redacted_text"),
    ),
    AgentProfile(
        "ExecutiveBriefingAgent",
        "Writes the exec brief that management reads instead of phoning the NOC.",
        FAIL_SOFT,
        "claude-opus-5",
        ("upsert_status_brief",),
        mcp=(
            McpRequirement(
                **_SLACK,
                access=("read_only",),
                purpose="Search executive-channel history and read the status canvas before drafting a brief.",
                tools=("slack_search_messages", "slack_read_canvas"),
            ),
            McpRequirement(
                **_ROVO_CONFLUENCE,
                access=("read_only",),
                purpose="Find and read the Confluence status page that the executive brief summarises.",
                tools=("searchConfluence", "getConfluenceContent"),
            ),
        ),
        skills=("brief.draft", "brief.upsert"),
        tags=("lifecycle", "advisory", "comms"),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A1",
        data_may_see=("network", "counts", "timestamps", "redacted_text"),
    ),
    AgentProfile(
        "ShiftLedgerAgent",
        "Writes each new ticket's row in the Excel shift ledger.",
        FAIL_SOFT,
        "none",
        ("append_excel_row",),
        mcp=(
            McpRequirement(
                server="Google Sheets MCP server",
                url="https://developers.google.com/workspace/sheets/api/guides/configure-mcp-server",
                transport="remote",
                vendor_official=True,
                maturity="preview",
                access=("read_only", "write_behind_hitl"),
                auth_env=(),
                auth_note="https://sheetsmcp.googleapis.com/mcp/v1; OAuth 2.0 (Workspace Developer Preview); no append tool — a row is insert_dimension + update_values",
                verified=True,
                purpose="Mirror the shift ledger into a shared Google Sheet once a human approves the sync.",
                namespace="sheets",
                tools=("get_spreadsheet", "get_values"),
                write_tools=("update_values", "insert_dimension"),
                hitl_task_type="APPROVE_LEDGER_SYNC",
                residency="abroad",
            ),
            McpRequirement(
                server="excel-mcp-server",
                url="https://github.com/haris-musa/excel-mcp-server",
                transport="stdio",
                vendor_official=False,
                maturity="community",
                access=("read_only",),
                auth_env=("EXCEL_FILES_PATH",),
                auth_note="no credentials; EXCEL_FILES_PATH scopes the workbooks; v0.1.8 pins fastmcp<3",
                verified=True,
                purpose="Read the ledger workbook back for reconciliation; the append itself stays on openpyxl.",
                namespace="xlsx",
                tools=("read_data_from_excel", "get_workbook_metadata"),
                residency="local",
            ),
            McpRequirement(
                server="filesystem (MCP reference server)",
                url="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
                transport="stdio",
                vendor_official=False,
                maturity="reference",
                access=("read_only",),
                auth_env=(),
                auth_note="no credentials; the only allowed directory is data/shift_ledgers, passed as a CLI argument",
                verified=False,
                purpose="List and read the ledger files under data/shift_ledgers and nothing else.",
                namespace="fs",
                tools=("read_text_file", "list_directory", "get_file_info", "list_allowed_directories"),
                residency="local",
            ),
        ),
        skills=("ledger.row.append",),
        tags=("lifecycle", "records"),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps", "role_tokens"),
    ),
    AgentProfile(
        "RecurrenceProblemAgent",
        "Counts repeat faults at the site and opens or updates its problem record.",
        FAIL_SOFT,
        "claude-fable-5-1",
        ("count_recurrence", "create_or_update_problem"),
        mcp=(
            McpRequirement(
                **_ELASTIC,
                access=("read_only",),
                purpose="Aggregate alarm history per site and failure domain with ES|QL to detect chronic problems.",
                tools=("platform.core.execute_esql", "platform.core.generate_esql", "platform.core.get_document_by_id"),
            ),
            McpRequirement(
                server="MCP Toolbox for Databases",
                url="https://github.com/googleapis/mcp-toolbox",
                transport="stdio",
                vendor_official=False,
                maturity="ga",
                access=("read_only",),
                auth_env=(),
                auth_note="sources and fixed-SQL tools live in tools.yaml; the unrestricted execute_sql prebuilt tool is never loaded",
                verified=True,
                purpose="Run the fixed, parameterised recurrence queries declared in tools.yaml against the incident database.",
                namespace="tbx",
                tools=("incidents_by_site_window", "problems_by_signature"),
                residency="local",
            ),
            McpRequirement(
                **_QDRANT,
                access=("read_only",),
                purpose="Find prior problem records similar to the current incident signature.",
                tools=("qdrant-find",),
            ),
        ),
        skills=("recurrence.count", "problem.upsert"),
        tags=("lifecycle", "advisory"),
        run_kind="incident_lifecycle",
        trigger="EVENT",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps"),
    ),
    AgentProfile(
        "WorklogMonitorAgent",
        "Watches the work notes and SLA clocks, and chases a vendor that goes silent.",
        FAIL_SOFT,
        "claude-opus-5",
        ("flag_sla_watch",),
        mcp=(
            McpRequirement(
                **_GRAFANA,
                access=("read_only",),
                purpose="Watch the state of a specific alert group and annotations while an incident is open.",
                tools=("get_alert_group", "get_annotations", "list_incidents", "get_incident"),
            ),
            McpRequirement(
                **_ZABBIX,
                access=("read_only",),
                purpose="Poll active problems, host status and SLA indicators to notice a silent restore or breach.",
                tools=("problem_active_get", "host_status_get", "alert_get", "sla_get", "sla_getsli", "maintenance_get"),
            ),
            McpRequirement(
                **_PAGERDUTY,
                access=("read_only",),
                purpose="Read PagerDuty incident notes and log entries to chase an unacknowledged page.",
                tools=("browse_incidents", "browse_activity"),
            ),
        ),
        skills=("incident.status.read", "sla.clock.read", "worklog.chase"),
        tags=("scheduled", "advisory"),
        run_kind="monitor",
        trigger="SCHEDULE",
        autonomy="A0",
        data_may_see=("network", "counts", "timestamps", "role_tokens", "redacted_text"),
    ),
    AgentProfile(
        "ShiftHandoverAgent",
        "Builds the day or night shift handover; nothing is sent until a person approves it.",
        FAIL_SOFT,
        "claude-opus-5",
        ("build_handover", "send_handover_email"),
        mcp=(
            McpRequirement(
                **_GMAIL,
                access=("read_only",),
                purpose="Read the outgoing shift's drafted notes and labelled handover mail; sending stays on the SMTP adapter.",
                tools=("get_message", "list_drafts", "list_labels"),
            ),
            McpRequirement(
                server="Microsoft Work IQ Mail MCP server",
                url="https://learn.microsoft.com/en-us/microsoft-agent-365/tooling-servers-overview",
                transport="remote",
                vendor_official=True,
                maturity="preview",
                access=("read_only",),
                auth_env=(),
                auth_note="Entra ID OAuth through a registered public client; tenant URL https://agent365.svc.cloud.microsoft/agents/tenants/{tenantId}/servers/mcp_MailTools; needs a Microsoft 365 Copilot licence; Microsoft may rename preview tools",
                verified=True,
                purpose="Search the shift mailbox for vendor promises and open threads to carry into the handover package.",
                namespace="wiq",
                tools=("mcp_MailTools_graph_mail_searchMessages", "mcp_MailTools_graph_mail_getMessage", "mcp_MailTools_graph_mail_listSent"),
                residency="abroad",
            ),
            McpRequirement(
                **_ROVO_CONFLUENCE,
                access=("read_only",),
                purpose="List handover pages and open Confluence tasks for the carry-forward section.",
                tools=("listConfluenceContent", "listConfluenceTasks", "getConfluenceTask"),
            ),
            McpRequirement(
                **_QDRANT,
                access=("read_only",),
                purpose="Declared for shift-memo recall; no tool exposed yet because qd_qdrant-find is owned by RecurrenceProblemAgent and qdrant-store needs the Phase 2 APPROVE_HANDOVER gate.",
                tools=(),
            ),
        ),
        skills=("handover.build", "handover.email.send"),
        tags=("advisory", "comms", "request"),
        run_kind="handover",
        trigger="REQUEST",
        autonomy="A1",
        data_may_see=("network", "counts", "timestamps", "role_tokens", "redacted_text"),
    ),
)

NODE_CARDS: tuple[NodeCard, ...] = (
    NodeCard("INGEST", "Ingest", "IngestCorrelationAgent", ingest.run, ingest.input_summary,
             reads=("event",), writes=("fingerprint",)),
    NodeCard("CORRELATE", "Correlate", "IngestCorrelationAgent", correlate.run, correlate.input_summary,
             reads=("event", "fingerprint")),
    NodeCard("ENRICH", "Enrich", "EnrichmentAgent", enrich.run, enrich.input_summary,
             reads=("event",), writes=("users", "site_name", "county", "is_hub", "tt")),
    NodeCard("SEVERITY", "Severity", "SeverityImpactAgent", severity.run, severity.input_summary,
             reads=("event", "users"), writes=("sev",)),
    NodeCard("TICKET", "Ticket", "TicketingAgent", ticket.run, ticket.input_summary,
             reads=("event", "fingerprint", "users", "site_name", "county", "is_hub", "tt", "sev"),
             writes=("incident", "sla_ack_due", "sla_restore_due", "outage_start")),
    NodeCard("ASSIGN", "Assign", "DispatchAssignmentAgent", assign.run, assign.input_summary,
             reads=("event", "incident", "outage_start", "sla_restore_due"), writes=("incident",)),
    NodeCard("HITL", "HITL Gate", "SupervisorAgent", hitl.run, hitl.input_summary,
             reads=("incident", "sev"), writes=("incident", "waiting_hitl", "email_body", "sms_body")),
    NodeCard("BROADCAST", "Broadcast", "BroadcastCommsAgent", broadcast.run, broadcast.input_summary,
             reads=("incident", "waiting_hitl", "email_body", "sms_body")),
    NodeCard("EXEC_BRIEF", "Exec Brief", "ExecutiveBriefingAgent", exec_brief.run, exec_brief.input_summary,
             reads=("incident",)),
    NodeCard("LEDGER", "Shift Ledger", "ShiftLedgerAgent", ledger.run, ledger.input_summary,
             reads=("incident",)),
    NodeCard("RECURRENCE", "Recurrence", "RecurrenceProblemAgent", recurrence.run, recurrence.input_summary,
             reads=("incident", "fingerprint"), writes=("incident",)),
    NodeCard("MONITOR", "Monitor", "WorklogMonitorAgent", monitor.run, monitor.input_summary,
             reads=("incident",)),
)

PROFILES_BY_NAME: dict[str, AgentProfile] = {p.name: p for p in AGENT_PROFILES}

# Import-time sanity checks: a typo here should fail at startup, not mid-run.
assert all(c.agent in PROFILES_BY_NAME for c in NODE_CARDS), "NodeCard names an unknown agent"
assert len({c.node_id for c in NODE_CARDS}) == len(NODE_CARDS), "duplicate node_id in NODE_CARDS"
# v2 MCP-card checks (§5.2): a name collision or a write tool without a gate is a startup failure, deliberately loud.
_all_tools = [(m.namespace, t) for p in AGENT_PROFILES for m in p.mcp for t in (*m.tools, *m.write_tools)]
assert len(_all_tools) == len(set(_all_tools)), "duplicate (namespace, tool) across AGENT_PROFILES"
assert all(re.fullmatch(r"[a-z][a-z0-9]{1,7}", m.namespace) for p in AGENT_PROFILES for m in p.mcp), "namespace missing or malformed"
assert all(
    (not m.write_tools) or ("write_behind_hitl" in m.access and m.hitl_task_type in {t.value for t in HitlTaskType})
    for p in AGENT_PROFILES for m in p.mcp
), "write_tools require write_behind_hitl access and a real HITL task type"
assert all(m.transport == "stdio" or m.url.startswith("https://") for p in AGENT_PROFILES for m in p.mcp), "remote MCP URLs must be https"
assert all(p.trigger in {"EVENT", "SCHEDULE", "REQUEST", "EVENT+SCHEDULE"} for p in AGENT_PROFILES)
assert all(p.autonomy in {"A0", "A1", "A2"} for p in AGENT_PROFILES)


def profile_for(card: NodeCard) -> AgentProfile:
    return PROFILES_BY_NAME[card.agent]


def workflow_nodes() -> list[dict]:
    return [{"id": c.node_id, "label": c.label, "agent": c.agent} for c in NODE_CARDS]


def workflow_edges() -> list[dict]:
    return [{"source": a.node_id, "target": b.node_id} for a, b in zip(NODE_CARDS, NODE_CARDS[1:])]


def agent_catalog() -> list[dict]:
    """GET /api/v1/agents. name/mission/status are the existing contract; the rest is additive."""
    return [
        {
            "name": p.name,
            "mission": p.mission,
            "status": "ready",
            "node_ids": [c.node_id for c in NODE_CARDS if c.agent == p.name],
            "criticality": p.criticality,
            "model_tier": p.model_tier,
            "in_graph": any(c.agent == p.name for c in NODE_CARDS),
            "tools": sorted(p.tools),
            "mcp": [m.as_dict() for m in p.mcp],
            "version": p.version,
            "skills": list(p.skills),
            "tags": list(p.tags),
            "run_kind": p.run_kind,
            "trigger": p.trigger,
            "autonomy": p.autonomy,
            "data_may_see": list(p.data_may_see),
            "data_must_not_see": list(p.data_must_not_see),
        }
        for p in AGENT_PROFILES
    ]
