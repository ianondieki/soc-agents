# SUPER PROMPT — Kenya Telecom NOC Multi-Agent Incident System  
### Primary operator: **Safaricom PLC** · Secondary profile: **Airtel Kenya**

> **How to use:** Paste everything from `BEGIN SUPER PROMPT` through `END SUPER PROMPT` into a coding LLM as a **single** project-generation instruction.  
> Optionally attach `RESEARCH_NOC_MULTI_AGENT.md` as background.  
> Prefer models with large context and multi-file code generation.

---

## BEGIN SUPER PROMPT

You are a principal engineer and **Kenya telecom NOC architect**. Build a **production-quality, multi-agent software system** that automates Network Operations Center (NOC) incident escalation and management for **Kenyan mobile network operators**, with:

1. **Primary deployment profile: Safaricom PLC** (market leader — largest site footprint, densest urban/rural coverage, M-PESA-adjacent service-impact sensitivity, highest executive/public scrutiny on outages).
2. **Secondary profile: Airtel Kenya** (clear #2 challenger — aggressive coverage build, heavy use of towerco partners such as ATC for many passive builds, different scale thresholds and distribution lists).

The system is **operator-configurable** via YAML profiles (`safaricom.yaml`, `airtel.yaml`) so one codebase serves both, but **all demos, seed data, copy, and default configs must feel like a real Safaricom-style NOC first**. Airtel is a second-class profile you can switch to — not an afterthought stub.

The system replaces most **manual NOC toil**. Human NOC staff remain **Human-in-the-Loop (HITL)** governors only.

Do **not** produce a shallow demo of three chatbots. Produce a **runnable monorepo** with clear architecture, typed domain models, orchestrated agents, mock integrations, tests, and a path to real adapters.

**Important branding rule:** Use realistic Kenyan ops language and fictional-but-plausible internal names. Do **not** claim official affiliation with Safaricom or Airtel. Label the product something neutral, e.g. **`kenya-noc-agents`** or **`nairobi-noc`**, with config `operator: safaricom | airtel`.

---

### 0. Kenya market & ops context (encode this in domain language)

#### 0.1 Operator landscape (Kenya)

| Rank | Operator | Role in this product |
|------|----------|----------------------|
| 1 | **Safaricom PLC** | **Primary profile.** Largest subscriber base and site estate; NOC processes, severity thresholds, MSP matrix, regions, and seed data default here. Outages have high social/media/regulatory visibility. |
| 2 | **Airtel Kenya** | **Secondary profile.** Smaller estate but competitive quality push; often different towerco mix (e.g. ATC-heavy passive); lower absolute user thresholds for the same priority labels. |
| 3 | **Telkom Kenya** (optional later) | Mention in README as future profile only; do not fully implement unless easy. |

Also model ecosystem players that appear on tickets and assignment matrices:

- **Towercos / passive infrastructure:** ATC Kenya, Atlas Towers, and operator-owned or leased sites.
- **Field / managed services MSPs (examples used in routing):** Egypro, Camusat, ATC (as passive/power partner), and generic `FIELD_ENGINEER` regional pools.
- **Transmission / fibre context:** national backbone, metro fibre, last-mile; fibre cuts and power (grid + diesel genset) are first-class failure modes in Kenya.
- **Services impacted language:** voice, data, SMS; for Safaricom profile also flag **M-PESA / mobile money corridor risk** when core, HUB, or major urban sites fail (as *business impact tag*, not a payment integration).

#### 0.2 Geography — Kenya regions (use these, not US/EU regions)

Model **Kenya operational regions** for RNIO / FE on-call and seed sites. Use a practical NOC-style split (names may be normalized in config):

| Region code | Label | Example focus areas (seed) |
|-------------|--------|----------------------------|
| `NBI` | Nairobi Metro | Nairobi CBD, Westlands, Eastlands, Industrial Area, Athi River corridor |
| `CKA` | Central & Mount Kenya | Thika, Nyeri, Meru, Nanyuki |
| `CST` | Coast | Mombasa, Kilifi, Malindi, Kwale, Lamu (sparser) |
| `RVA` | Rift Valley | Nakuru, Eldoret, Naivasha, Kericho |
| `WST` | Western | Kisumu, Kakamega, Kisii, Bungoma |
| `EST` | Eastern / NE | Machakos, Kitui, Garissa, Isiolo (long drive-times — SLA notes matter) |
| `NYZ` | Nyanza (if split from Western) | optional; may fold into `WST` in MVP |

**Timezone:** `Africa/Nairobi` (EAT, UTC+3) — always. Shift handovers use local EAT clocks.

#### 0.3 Site & network object types (Kenya RAN/TX vocabulary)

```text
site_type:
  HUB          # aggregation / major hub — ALWAYS auto-ticket; priority floor
  BTS          # 2G/3G macro site (legacy still present)
  NODEB        # 3G
  ENODEB       # 4G LTE
  GNODEB       # 5G where deployed (esp. Nairobi/Mombasa corridors)
  BSC | RNC    # controllers (legacy-critical)
  CORE         # core network / PS-core / IMS — national impact risk
  TX           # microwave / fibre transmission node
  POWER        # site power plant / rectifier / genset-related CI
  DAS_IBS      # indoor / mall / airport (optional)
  OTHER
```

**Technology tags:** `2G`, `3G`, `4G`, `5G`, `MW` (microwave), `FO` (fibre), `POWER`.

**Common Kenyan alarm / failure narratives (seed + templates):**

- Grid power failure + genset not starting / fuel theft / dry tank  
- Rectifier / battery low voltage → site on battery countdown  
- Microwave hop down / high BER  
- Fibre cut (road works, vandalism) on metro or backbone  
- Transmission cascade from HUB affecting many child sites  
- High temperature / shelter AC failure  
- Flooding / access road impassable (Coast, Western rains)  
- VSWR / RF unit fault  
- Controller (BSC/RNC) or core node degradation  

#### 0.4 People & roles (Kenya NOC language)

| Role | Meaning in product |
|------|---------------------|
| **NOC** | Network Operations Center — HITL governors; night & day shifts |
| **RNIO** | Regional Network / field coordination contact notified on service-affecting events |
| **Field Engineer (FE)** | Operator or contracted field staff by region |
| **MSP** | Managed Service Provider / vendor (Egypro, Camusat, ATC, …) |
| **Shift Supervisor** | Owns Excel ledger accuracy + handover quality |
| **Management / Exec** | Receive proactive briefs on P1/P2 — reduce phone spam into NOC |
| **Dashboard users (whole team)** | Shared **NOC Mission Control UI** — see open work, agent workflow, HITL, handovers (role-scoped) |

#### 0.4b Who uses the UI (whole-team product, not a single-user admin screen)

The multi-agent system is useless if only one engineer “knows what the bots did.” Build a **shared team surface** so the shift can **see, trust, and govern** the agents together:

| Persona | What they need on screen | Default home |
|---------|--------------------------|--------------|
| **NOC Analyst (L1)** | Live queue, agent progress, claim HITL, add notes | Mission Control + HITL Inbox |
| **Shift Supervisor** | Wallboard KPIs, SLA risk, handover compose, autonomy level | Wallboard + Shift Desk |
| **Duty Manager** | P1/P2 only, exec briefs, approve high-blast comms | Exec lens + HITL (P1/P2) |
| **RNIO / FE (read-heavy)** | Incidents in *their region*, owner, last note, access notes | Region filter on Incident Board |
| **MSP coordinator (optional demo role)** | Tickets assigned to their MSP only; post work notes | Vendor lens (read + notes) |
| **NOC Engineer / Automation owner** | Full **agent workflow graph**, tool traces, failures, config | Agent Observatory |
| **Training / new joiner** | Replay of past incident agent path (explainability) | Incident replay |

**Design mantra:** *Every agent decision that used to live in one person’s head must be visible on the glass for the whole shift.*

#### 0.5 Why Safaricom vs Airtel profiles differ

| Dimension | Safaricom (default) | Airtel Kenya |
|-----------|---------------------|--------------|
| Scale | Higher absolute users per urban HUB | Lower absolute thresholds for same P-label |
| Public heat | Higher (market leader + M-PESA narrative) | High on coverage quality; slightly different exec lists |
| Passive / tower mix | Mix of own + partners (e.g. Atlas and others in market stories) | Often **ATC-heavy** for many passive builds — default power/passive MSP weight to ATC |
| Ticket prefix | `SFC-INC-YYYYMMDD-#####` | `ATL-INC-YYYYMMDD-#####` |
| Problem prefix | `SFC-PRB-…` | `ATL-PRB-…` |
| Branding in UI | “Safaricom NOC Agents (demo profile)” | “Airtel Kenya NOC Agents (demo profile)” |
| Business tags | May set `mpesa_risk: true` on CORE/HUB Nairobi | Generally `mpesa_risk: false`; focus voice/data |

---

### 1. Mission (read carefully)

When major network outages occur on the **Kenya mobile network** (especially **site / HUB** failures), the system must:

1. **Ingest & correlate** alarms/events into a single incident candidate (noise reduction) — e.g. collapse child-site floods under a parent HUB TX/power root cause when fingerprint allows.
2. **Classify severity** using service impact (estimated subscribers/users affected) and site criticality:
   - **P4:** below **50,000** users affected on **Safaricom** default (configurable; Airtel profile uses lower thresholds — see config).
   - **P3 / P2 / P1:** progressively larger impact; **P1** = great multitude and/or national / multi-HUB / CORE / multi-region impact.
   - **HUB / aggregation / CORE** failures are **always auto-ticketed** and apply a **priority floor** even if early user counts are low.
3. **Broadcast** notifications to **RNIO** and **field engineers** (and MSP contacts when assigned) with clear **service-affecting** language suitable for Kenya field ops (site ID, region, access notes when known).
4. **Create tickets** with a **unique operator-scoped incident number**, fill all structured fields an agent would fill in a ticketing UI, and **assign** to either:
   - a **Field Engineer** (by Kenya region/domain), or  
   - an **MSP** such as **Egypro**, **ATC**, **Camusat** (config-driven vendor matrix per operator profile).
5. **Track work notes** updated by MSPs/FEs until **ticket closure**, chase silence, escalate SLA risk (account for long drive times in EST/Coast rural).
6. **Update a shift Excel ledger** of failures for the current **EAT** day/night shift (append-only, audit-friendly) — columns a Kenyan shift supervisor would actually scan.
7. **Send shift-handover email** to the incoming **day or night** shift listing failures to watch closely, priority, status, last vendor note, and **who currently owns** each incident.
8. **Detect recurring / problematic** sites or failure signatures (chronic power, repeated MW flaps) and open/update **problem records**.
9. **Deflect executive phone spam:** produce and maintain a living **executive status brief** (cause hypothesis, regions/sites, est. users, M-PESA risk tag if Safaricom, next update time, owner) so top ranks get proactive updates instead of calling NOC.
10. Keep **NOC human** as approver for high-blast actions (P1/P2 external wording, priority overrides, reassignment disputes, anything that changes live network config — **this system does NOT auto-remediate live network elements without explicit HITL + allowlisted tools**).
11. **Expose the entire multi-agent workflow on a team dashboard/UI** so the whole NOC shift (and optional regional/MSP lenses) can **watch agents work in real time**, understand *why* a priority/assignment/broadcast was chosen, act on HITL without leaving the console, and train juniors by replaying agent paths — not by reverse-engineering chat logs.

Success looks like:
- A Kenyan-site alarm flows through the graph; a ticket with `SFC-INC-…` (or `ATL-INC-…`) exists; RNIO/FE/MSP notified; Excel row; notes monitored; handover ready; audit explains decisions in Nairobi NOC English.
- **Simultaneously**, any teammate on the wallboard sees the incident appear, watches nodes light up on the **live agent workflow graph** (Correlate → Enrich → Severity → Ticket → …), opens the incident to read each agent’s rationale/tool calls, and a supervisor can approve HITL from the same glass.

---

### 2. Design principles (non-negotiable)

1. **Supervisor–worker multi-agent pattern** (Booz Allen / TM Forum Incident Co-Pilot style), not one mega-prompt.
2. **Stateful orchestration** with explicit lifecycle states and HITL interrupt points.
3. **Tools over free-text hallucination:** agents call typed tools; LLMs reason; tools execute.
4. **Idempotency & deduplication:** same site + fingerprint + time window must not open duplicate major tickets.
5. **Explainability:** every priority, assignment, and broadcast includes a short rationale string.
6. **Config over hardcode:** operator profile, priorities, MSP matrix, Kenya regions, shift times, distribution lists, SLA minutes live in YAML.
7. **Mock-first adapters:** system runs fully offline with **Kenya seed sites**; real SMS/email/NMS later via same interfaces.
8. **Autonomy ladder via feature flags:**
   - `L1_COPILOT` — draft only, require HITL approve for almost everything.
   - `L2_GUARDED` — auto-ticket HUBs, auto-notify P3–P4, HITL for P1/P2 external comms.
   - `L3_CONDITIONAL` — auto note-chase + auto handover; still no unattended network remediations.
9. **Security:** secrets from env; least privilege; full audit log; no real customer MSISDNs in demos.
10. **Timezone:** `Africa/Nairobi` only for MVP.
11. **Multi-operator ready:** `OPERATOR_PROFILE=safaricom|airtel` selects thresholds, prefixes, MSP weights, branding — without forking the codebase.
12. **UI is a first-class product**, not a thin CRUD afterthought: real-time multi-agent observability for the **whole team**, role-aware views, wallboard mode, incident-level workflow graph, and HITL actions in-context.
13. **Glass-to-action loop:** anything an agent proposes that needs a human must be **one click away** on the dashboard (approve / edit / reject / reassign) with mandatory reason on reject.

---

### 3. Competitive DNA to encode (behaviors, not vendor marketing in UI)

| Source | Behaviors to implement |
|--------|------------------------|
| TM Forum Incident Co-Pilot / Cisco Crosswork | Incident agent + enrichment + ticket creation + transparent recommendations; progressive white/dark NOC |
| Booz Allen multi-agent IR | Supervisor fans out to Contextualization, Investigation, Evaluation workers; write results back onto the ticket; **observable multi-agent front-end** spirit |
| PagerDuty / incident.io | Escalation policies, stakeholder notifications, major-incident style broadcasts, impact-based routing; **shared incident channel UX** |
| ServiceNow-class ITSM | Unique incident number, assignment group/vendor, work notes, SLA clocks, problem linkage; **ticket workspace** |
| ilert autonomy model | L1 advise → L2 act-with-approval → L3 guardrailed autonomy; **visible autonomy mode on UI** |
| BigPanda/Moogsoft AIOps | Correlate/dedupe alarms into one “situation” before ticketing; **situation timeline** |
| TIM Co-Pilot insight | High-quality **incident narrative** for field/exec — auto-written and updated |
| LangGraph / agent ops best practice | Persist **per-node run traces** (input summary, output summary, tools, duration, errors) for UI replay |

---

### 4. Technology stack (implement exactly unless impossible)

**Language:** Python 3.11+ (backend) + modern SPA frontend.

**Core backend:**
- `langgraph` + `langchain-core` for multi-agent orchestration (primary).
- `pydantic` v2 for all domain models and tool I/O.
- `httpx` for HTTP adapters.
- `tenacity` for retries on external calls.
- `structlog` or `loguru` for structured logging.
- `pyyaml` for config.
- `openpyxl` for Excel shift ledger.
- `jinja2` for email/SMS templates.
- `pytest` + `pytest-asyncio` for tests.
- Optional LLM: pluggable via env (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `XAI_API_KEY`) or **local mock LLM** with deterministic structured JSON when no key present. **Must run demos without paid API keys.**

**API:**
- FastAPI backend with REST **and** real-time channel:
  - **WebSocket** (`/ws/ops`) and/or **SSE** (`/api/v1/stream/events`) broadcasting: incident created/updated, agent node started/completed/failed, HITL created/resolved, note added, SLA risk flipped, handover sent.
- Every LangGraph node **must emit** a structured `AgentRunEvent` persisted to DB **and** pushed to the stream (this powers the team UI).

**Frontend (REQUIRED — choose React + Vite + TypeScript; do not ship “API only”):**
- **React 18 + Vite + TypeScript** single-page app under `frontend/`.
- Styling: Tailwind CSS (preferred) or equivalent utility CSS — **dark NOC theme by default** (wallboard-friendly), with light toggle optional.
- Routing: React Router.
- Data: TanStack Query (or similar) for REST; native WebSocket/EventSource client for live updates.
- Graph viz: **React Flow** (`@xyflow/react`) **or** mermaid-rendered SVG for the **Agent Workflow Graph** — pick React Flow if feasible.
- Charts/KPIs: lightweight (recharts or CSS bars) for counts by priority/region.
- Tables: sortable/filterable incident board.
- **No blank pages:** every route in §14 must render with empty states that teach the user what will appear.

**Auth for MVP (team demo, not enterprise IdP):**
- Simple **role switcher + display name** (localStorage / cookie): roles `noc_analyst | shift_supervisor | duty_manager | rnio | msp_viewer | automation_admin`.
- Optional PIN not required; document that production would use SSO/AD.
- UI **hides or disables** actions the role cannot perform (see RBAC matrix in §14).

**Persistence (MVP):**
- SQLite via SQLAlchemy 2.x (Postgres-ready schema).
- Excel under `./data/shift_ledgers/{operator}/`.
- Tables: incidents, notes, audits, hitl_tasks, **agent_runs**, **agent_run_steps**, **workflow_snapshots**, **ui_presence** (optional soft presence).

**Packaging:**
- `pyproject.toml`, `Makefile` (`install`, `run`, `run-ui`, `demo`, `demo-airtel`, `test`).
- `docker-compose.yml` preferred: `api`, `frontend` (or static serve from FastAPI), optional `mailhog`.
- Single command path: `make run` starts API + serves built UI **or** concurrent dev servers documented clearly.

---

### 5. Domain model (Pydantic + SQLAlchemy)

```text
OperatorProfile
  id: safaricom | airtel
  display_name, incident_prefix, problem_prefix, default_locale, branding

AlarmEvent
  id, operator_id, source, raw_payload,
  site_id, site_name, site_type, region_code, county (optional),
  technology[], alarm_code, severity_raw, failure_domain
    (POWER|TRANSMISSION|RADIO|CORE|ACCESS|ENVIRONMENT|UNKNOWN),
  started_at, fingerprint, correlated_group_id

Incident
  id, operator_id,
  incident_number,  # SFC-INC-YYYYMMDD-##### or ATL-INC-YYYYMMDD-#####
  status (NEW|TRIAGED|TICKETED|ASSIGNED|IN_PROGRESS|AWAITING_VENDOR|RESTORED|CLOSED|CANCELLED),
  priority (P1|P2|P3|P4),
  users_affected, service_affecting (bool),
  services_impacted[]  # VOICE, DATA, SMS, MPESA_CORRIDOR (tag), ENTERPRISE
  site_id, site_name, site_type, region_code, county,
  title, description, narrative,
  root_cause_hypothesis, impact_summary, next_update_at,
  assignee_type (FIELD_ENGINEER|MSP|NOC|UNASSIGNED),
  assignee_name, msp_name, fe_name,
  access_notes,  # e.g. "night access restricted", "flooded approach road"
  created_at, updated_at, closed_at, sla_ack_due, sla_restore_due,
  correlation_fingerprint, is_hub_major, recurrence_count, problem_id,
  mpesa_risk (bool),  # Safaricom profile primarily
  autonomy_level_applied, requires_hitl, hitl_state

WorkNote
  id, incident_id, author, author_role (MSP|FE|NOC|AGENT|SYSTEM|RNIO),
  body, created_at, source

Broadcast
  id, incident_id, channel (SMS|EMAIL|SLACK|WEBHOOK),
  audience (RNIO|FIELD_ENGINEER|MSP|MANAGEMENT|SHIFT),
  message, status, sent_at

ShiftLedgerEntry
  id, operator_id, shift_id, shift_type (DAY|NIGHT),
  incident_number, priority, site, site_type, region_code,
  owner, status, last_note_summary, sla_risk, row_written_at

ProblemRecord
  id, operator_id, problem_number, signature, site_id, region_code,
  occurrence_count, first_seen, last_seen,
  status (OPEN|MONITORING|KNOWN_ERROR|RESOLVED),
  summary, linked_incident_ids[], dominant_failure_domain

AuditEvent
  id, ts, operator_id, actor, action, entity_type, entity_id, rationale, payload_json

HitlTask
  id, incident_id, task_type
    (APPROVE_BROADCAST|APPROVE_PRIORITY|APPROVE_ASSIGNMENT|APPROVE_EXEC_BRIEF|GENERIC),
  proposed_payload, status, created_at, resolved_by, resolved_at,
  claimed_by, claimed_at  # team queue: claim to avoid double-approve

# --- Multi-agent observability (powers the team dashboard) ---
AgentRun
  id, incident_id (nullable for system jobs), operator_id,
  graph_name, trigger (EVENT|SCHEDULE|MANUAL|HANDOVER),
  status (PENDING|RUNNING|WAITING_HITL|SUCCEEDED|FAILED|CANCELLED),
  started_at, finished_at, current_node, error_summary

AgentRunStep
  id, run_id, seq, node_name, agent_name,
  status (STARTED|SUCCEEDED|FAILED|SKIPPED|WAITING_HITL),
  started_at, finished_at, duration_ms,
  input_summary, output_summary, rationale,
  tools_called[]  # [{name, ok, latency_ms, error?}]
  confidence (nullable float),
  parent_step_id (nullable)  # for fan-out workers under Supervisor

WorkflowSnapshot
  id, incident_id, run_id, graph_json,  # nodes+edges+status colors for React Flow
  updated_at

RealtimeEvent  # also pushed on WS/SSE; optional persist last N
  id, ts, operator_id, type, incident_id, run_id, payload_json

UiUserSession (demo)
  id, display_name, role, region_filter, msp_filter, last_seen_at
```

**Numbering:** concurrency-safe daily sequences per operator prefix.

**Invariant:** Completing any LangGraph node writes `AgentRunStep` + updates `WorkflowSnapshot` + emits `RealtimeEvent`. If the UI cannot show “which agent is running now,” the backend is incomplete.

---

### 6. Agent roster

| Agent | Responsibility | Kenya-specific notes |
|-------|----------------|----------------------|
| **SupervisorAgent** | Plan, route, merge, HITL decisions | Knows operator profile |
| **IngestCorrelationAgent** | Normalize, fingerprint, dedupe, HUB cascade awareness | Collapse child BTS under HUB power/TX when rules match |
| **EnrichmentAgent** | Site meta, region, MSP coverage, history, user estimate | Mock CMDB of Kenyan sites; drive-time hint by region |
| **SeverityImpactAgent** | P1–P4 + floors + multi-region boost | Different thresholds per Safaricom/Airtel YAML |
| **TicketingAgent** | Create/update ticket fields + narrative | Operator-prefixed INC numbers; Kenya field names |
| **DispatchAssignmentAgent** | FE vs MSP matrix | ATC/Camusat/Egypro vs FE by failure_domain + region |
| **BroadcastCommsAgent** | RNIO/FE/MSP notifications | EAT timestamps; site ID + region in every SMS |
| **ExecutiveBriefingAgent** | Exec brief to kill phone spam | Safaricom: mention M-PESA corridor risk when tagged |
| **WorklogMonitorAgent** | Notes, stall detection, SLA | Longer note intervals OK for remote EST sites in config |
| **ShiftLedgerAgent** | Excel shift sheet | Paths per operator; EAT shift boundaries |
| **ShiftHandoverAgent** | Day↔Night handover mail | Distribution lists per operator profile |
| **RecurrenceProblemAgent** | Chronic sites/problems | Power + MW signatures common in Kenya seeds |
| **AuditAgent** | Decision log | Always on |

---

### 7. Lifecycle state machine (LangGraph)

```text
INGEST → CORRELATE → ENRICH → SEVERITY →
  [duplicate open] → MERGE_UPDATE → END
  else → TICKET → ASSIGN →
    HITL_GATE (conditional) →
    BROADCAST → EXEC_BRIEF → LEDGER →
    MONITOR_LOOP (notes/SLA/recurrence) →
    HANDOVER (scheduled or on-demand) →
    CLOSE
```

**HITL_GATE (default L2):**
- P1 always pending HITL before management/exec broadcast wording.
- P2 pending HITL for expanded management audience.
- Priority override or MSP reassignment if low confidence / rule conflict → HITL.
- HUB auto-ticket at L2 without HITL; assignment disputes may still HITL.
- Any network remediation stub → always HITL.

API: `POST /api/v1/hitl/{task_id}/approve|reject`.

---

### 8. Business rules & config (Kenya-first)

#### 8.1 `config/operators/safaricom.yaml` (DEFAULT)

```yaml
operator_id: safaricom
display_name: "Safaricom PLC (demo profile)"
incident_prefix: "SFC-INC"
problem_prefix: "SFC-PRB"
timezone: Africa/Nairobi
autonomy_level: L2_GUARDED
locale_notes: "Market leader; high public scrutiny; tag M-PESA corridor risk on CORE/major NBI HUB"

priority_thresholds:
  P4_max_users: 49999
  P3_max_users: 249999
  P2_max_users: 999999
  # > P2_max_users => P1

site_type_priority_floor:
  HUB: P2
  CORE: P1
  BSC: P2
  RNC: P2
  TX: P3
  POWER: P3
  ENODEB: P4
  GNODEB: P4
  BTS: P4
  NODEB: P4
  OTHER: P4

# Boost to P1 if any of:
p1_force_rules:
  - multi_region: true
  - site_type_in: [CORE]
  - child_sites_down_gte: 20
  - users_affected_gte: 1000000

correlation:
  window_minutes: 15
  fingerprint_fields: [site_id, alarm_code, failure_domain]

sla_minutes:
  P1: { ack: 5,  restore: 60,  note_interval: 15 }
  P2: { ack: 10, restore: 120, note_interval: 30 }
  P3: { ack: 20, restore: 240, note_interval: 60 }
  P4: { ack: 30, restore: 480, note_interval: 120 }

# Slightly relaxed note chase for hard-to-reach regions
region_sla_note_multiplier:
  EST: 1.5
  CST: 1.25
  NBI: 1.0

assignment_matrix:
  power_passive: [ATC, Camusat]
  transmission_fiber: [Egypro]
  transmission_mw: [Egypro, FIELD_ENGINEER]
  radio_active: [FIELD_ENGINEER]
  core: [NOC]  # then escalate specialist — HITL reassign allowed
  unknown: [NOC]

# Prefer these MSPs when region+domain tie
msp_contacts:
  ATC: { email: "atc.noc.ke@example.com", sms: "+254700000001" }
  Camusat: { email: "camusat.ke@example.com", sms: "+254700000002" }
  Egypro: { email: "egypro.tx.ke@example.com", sms: "+254700000003" }

regions:
  NBI:
    label: "Nairobi Metro"
    rnio: "RNIO-NBI"
    fe_oncall: "FE-NBI-01"
    counties: ["Nairobi", "Kiambu", "Machakos"]  # metro fringe
  CKA:
    label: "Central & Mt Kenya"
    rnio: "RNIO-CKA"
    fe_oncall: "FE-CKA-01"
  CST:
    label: "Coast"
    rnio: "RNIO-CST"
    fe_oncall: "FE-CST-01"
  RVA:
    label: "Rift Valley"
    rnio: "RNIO-RVA"
    fe_oncall: "FE-RVA-01"
  WST:
    label: "Western / Nyanza"
    rnio: "RNIO-WST"
    fe_oncall: "FE-WST-01"
  EST:
    label: "Eastern / North Eastern"
    rnio: "RNIO-EST"
    fe_oncall: "FE-EST-01"

shifts:
  day:
    start: "08:00"
    end: "20:00"
    handover_to: night
    distribution_list: ["safaricom.noc.day@example.com"]
  night:
    start: "20:00"
    end: "08:00"
    handover_to: day
    distribution_list: ["safaricom.noc.night@example.com"]

management_distribution_list:
  - "safaricom.noc.duty.manager@example.com"
  - "safaricom.ran.manager@example.com"

recurrence:
  threshold_count: 3
  lookback_days: 30
  auto_open_problem: true

broadcast:
  channels: [EMAIL, SMS]
  p1_audiences: [RNIO, FIELD_ENGINEER, MSP, MANAGEMENT]
  p2_audiences: [RNIO, FIELD_ENGINEER, MSP, MANAGEMENT]
  p3_audiences: [RNIO, FIELD_ENGINEER, MSP]
  p4_audiences: [RNIO, FIELD_ENGINEER]

mpesa_risk:
  enable: true
  site_types: [CORE, HUB]
  regions: [NBI]  # extendable
```

#### 8.2 `config/operators/airtel.yaml` (SECONDARY)

```yaml
operator_id: airtel
display_name: "Airtel Kenya (demo profile)"
incident_prefix: "ATL-INC"
problem_prefix: "ATL-PRB"
timezone: Africa/Nairobi
autonomy_level: L2_GUARDED
locale_notes: "Challenger MNO; often ATC-weighted passive; lower absolute user thresholds"

priority_thresholds:
  # Lower absolute bands — same P labels, smaller network scale
  P4_max_users: 19999
  P3_max_users: 99999
  P2_max_users: 399999

site_type_priority_floor:
  HUB: P2
  CORE: P1
  BSC: P2
  RNC: P2
  TX: P3
  POWER: P3
  ENODEB: P4
  GNODEB: P4
  BTS: P4
  NODEB: P4
  OTHER: P4

p1_force_rules:
  - multi_region: true
  - site_type_in: [CORE]
  - child_sites_down_gte: 15
  - users_affected_gte: 400000

assignment_matrix:
  power_passive: [ATC, Camusat]   # ATC first for Airtel-leaning passive story
  transmission_fiber: [Egypro]
  transmission_mw: [Egypro, FIELD_ENGINEER]
  radio_active: [FIELD_ENGINEER]
  core: [NOC]
  unknown: [NOC]

msp_contacts:
  ATC: { email: "atc.airtel.ke@example.com", sms: "+254700000011" }
  Camusat: { email: "camusat.airtel.ke@example.com", sms: "+254700000012" }
  Egypro: { email: "egypro.airtel.ke@example.com", sms: "+254700000013" }

regions:
  # Same Kenya region codes; different on-call identities
  NBI: { label: "Nairobi Metro", rnio: "ATL-RNIO-NBI", fe_oncall: "ATL-FE-NBI-01" }
  CKA: { label: "Central & Mt Kenya", rnio: "ATL-RNIO-CKA", fe_oncall: "ATL-FE-CKA-01" }
  CST: { label: "Coast", rnio: "ATL-RNIO-CST", fe_oncall: "ATL-FE-CST-01" }
  RVA: { label: "Rift Valley", rnio: "ATL-RNIO-RVA", fe_oncall: "ATL-FE-RVA-01" }
  WST: { label: "Western / Nyanza", rnio: "ATL-RNIO-WST", fe_oncall: "ATL-FE-WST-01" }
  EST: { label: "Eastern / North Eastern", rnio: "ATL-RNIO-EST", fe_oncall: "ATL-FE-EST-01" }

shifts:
  day:
    start: "08:00"
    end: "20:00"
    handover_to: night
    distribution_list: ["airtel.noc.day@example.com"]
  night:
    start: "20:00"
    end: "08:00"
    handover_to: day
    distribution_list: ["airtel.noc.night@example.com"]

management_distribution_list:
  - "airtel.noc.duty.manager@example.com"

mpesa_risk:
  enable: false

# reuse correlation, recurrence, broadcast structure from safaricom with same keys
```

#### 8.3 Priority engine (unit-test both profiles)

```text
base = users_to_priority(users_affected, profile.thresholds)
floor = site_type_priority_floor[site_type]
final = max_severity(base, floor)  # P1 max
if any p1_force_rules matched: final = P1
if safaricom and mpesa_risk rules matched: tag mpesa_risk=true (does not alone force P1 unless CORE/HUB rules say so)
rationale includes operator_id, users, base, floor, forced rules
```

Order: P4 < P3 < P2 < P1.

#### 8.4 User-impact estimation (mock, documented)

Provide `estimate_users_affected(site, profile)`:

| site_type (illustrative Safaricom seed) | Base estimate |
|-----------------------------------------|---------------|
| CORE | 2_000_000+ or “national” flag |
| HUB (NBI) | 300_000 – 800_000 |
| HUB (other) | 80_000 – 300_000 |
| ENODEB urban | 8_000 – 40_000 |
| BTS rural | 1_000 – 8_000 |

Airtel estimates scale down ~40–60% for similar site classes in seed data. Always label as **estimate**.

---

### 9. Seed data (must feel Kenyan)

Create **≥12 sites** for Safaricom and **≥8** for Airtel, including:

**Safaricom examples (fictional IDs):**
- `SFC-NBI-HUB-01` — Nairobi Metro HUB (Westlands/Industrial corridor) — critical  
- `SFC-NBI-CORE-PS01` — Core-related CI (demo)  
- `SFC-NBI-ENB-CBD07` — CBD eNodeB  
- `SFC-CST-HUB-MSA01` — Mombasa HUB  
- `SFC-CST-ENB-NYL12` — Nyali  
- `SFC-RVA-HUB-NKR01` — Nakuru HUB  
- `SFC-RVA-BTS-ELD44` — Eldoret area  
- `SFC-WST-HUB-KSM01` — Kisumu HUB  
- `SFC-WST-ENB-KKG03` — Kakamega  
- `SFC-CKA-ENB-THK21` — Thika  
- `SFC-EST-BTS-GRS08` — Garissa rural (long access)  
- `SFC-NBI-TX-MW-01` — MW/fibre TX node feeding multiple children  

**Airtel examples:** `ATL-NBI-HUB-01`, `ATL-CST-HUB-MSA01`, `ATL-RVA-ENB-ELD02`, etc.

Include 2–3 **historical incidents** on one power-problematic site for recurrence demo.

---

### 10. Integrations (adapter pattern)

```text
ports/
  AlarmSourcePort, TicketPort, NotificationPort, CmDBPort,
  SpreadsheetPort, OnCallPort, OperatorConfigPort
adapters/
  mock_* (default), smtp_email, file_excel, webhook_alarm_source
  # stubs: future ServiceNow-like, Africa's Talking SMS
```

SMS body limit awareness (~160–320 chars): provide **short SMS** + **long email** templates.

---

### 11. Notification standards (Kenya ops tone)

**SMS (RNIO/FE) — short:**
```text
[{priority}] {incident_number} {site_id} {region_code}
{failure_domain}|est.users {users_affected}
{one_line_summary}
Owner:{assignee} EAT:{time}
Ticket notes for updates — minimize NOC calls.
```

**Email broadcast:**
```text
Subject: [{priority}] {incident_number} | {site_name} ({site_type}) | {region_label} | {operator_display}

Service affecting: YES/NO
Est. users: {users_affected}
Services: {voice/data/sms/mpesa_corridor}
Failure domain: {POWER/TX/...}
Summary: ...
Narrative: 3–6 lines (access, cascade, hypothesis)
Owner: {FE or MSP}
Opened (EAT): ... | Next update (EAT): ...
Do not call NOC for routine status — update ticket / wait for next brief.
```

**Handover email subject:**
```text
[{operator}] [{DAY|NIGHT} shift] NOC Handover {YYYY-MM-DD EAT} — {n_p1} P1 / {n_p2} P2 open
```

Body table columns:  
`Time | INC | Pri | Site | Type | Region | Domain | Owner | Status | Last note | SLA risk | M-PESA risk`

**Executive brief (Safaricom P1 example tone):**
Plain language, regions affected, whether Nairobi metro/core involved, estimated users, whether mobile-money corridor risk is flagged, current owner, next update time. No blame language.

---

### 12. Excel shift ledger columns

```text
Time (EAT) | Incident No | Priority | Site ID | Site Name | Type | Region |
Failure Domain | Est. Users | Owner | MSP | Status | Last Note | SLA Risk | M-PESA Risk | Shift
```

File name pattern:  
`data/shift_ledgers/safaricom/ledger_2026-07-16_NIGHT.xlsx`

---

### 13. API surface (minimum)

```text
# Profile & health
GET    /api/v1/profile
GET    /health

# Ingest
POST   /api/v1/events
POST   /api/v1/events/batch

# Incidents
GET    /api/v1/incidents?priority=&region=&status=&owner=&q=
GET    /api/v1/incidents/{id}
POST   /api/v1/incidents/{id}/notes
GET    /api/v1/incidents/{id}/timeline     # unified: agent steps + notes + broadcasts + hitl
GET    /api/v1/briefs/{incident_id}

# Multi-agent observability (CRITICAL for dashboard)
GET    /api/v1/agents                       # catalog of agents + descriptions + last heartbeat
GET    /api/v1/runs?status=&incident_id=
GET    /api/v1/runs/{run_id}
GET    /api/v1/runs/{run_id}/steps
GET    /api/v1/incidents/{id}/workflow      # React Flow graph_json + step statuses
POST   /api/v1/incidents/{id}/replay        # optional: re-emit historical events for training UI

# HITL team queue
GET    /api/v1/hitl/pending
POST   /api/v1/hitl/{task_id}/claim
POST   /api/v1/hitl/{task_id}/unclaim
POST   /api/v1/hitl/{task_id}/approve
POST   /api/v1/hitl/{task_id}/reject

# Shift & problems & audit
POST   /api/v1/shifts/handover
GET    /api/v1/shifts/current
GET    /api/v1/shifts/ledger
GET    /api/v1/problems
GET    /api/v1/audit?entity_type=&entity_id=

# Wallboard KPIs
GET    /api/v1/metrics/summary              # open by P, hitl pending, sla risk, agents running, by region

# Demo identity (MVP auth)
POST   /api/v1/session                      # {display_name, role, region_filter?, msp_filter?}
GET    /api/v1/session

# Realtime
WS     /ws/ops                              # subscribe; server pushes RealtimeEvent
GET    /api/v1/stream/events                # SSE fallback
```

Env: `OPERATOR_PROFILE=safaricom` (default) or `airtel`.

---

### 14. Team Dashboard / UI — DEEP SPEC (first-class deliverable)

Build a product called in-UI something like **“Kenya NOC Mission Control”** (subtitle: multi-agent incident co-pilot · demo profile Safaricom/Airtel). This is where the **whole team** watches and governs the multi-agent system.

#### 14.0 Product goals

1. **Situational awareness** — What is broken, how bad, who owns it, are we SLA-safe?  
2. **Agent transparency** — Which agents ran, in what order, with what rationale and tools?  
3. **Shared control** — HITL approvals without side-channel WhatsApp/phone.  
4. **Shift continuity** — Ledger + handover visible and sendable.  
5. **Teachability** — Replay path for training; juniors see *why* P2 was chosen.  
6. **Trust** — Failed agent steps are red and actionable, not silent.

#### 14.1 Information architecture (routes)

Implement **all** of these routes in the React SPA:

| Route | Name | Purpose |
|-------|------|---------|
| `/` | **Mission Control** | Default ops home: KPI strip + live incident feed + agent activity ticker + HITL badge |
| `/wallboard` | **Wallboard** | Full-screen, low-chrome, high-contrast TV mode for NOC room; auto-refresh; large P1/P2 cards |
| `/incidents` | **Incident Board** | Filterable table/kanban of all open (and recent closed) incidents |
| `/incidents/:id` | **Incident Workspace** | Deep work surface for one INC (see 14.4) |
| `/agents` | **Agent Observatory** | Fleet view of all agents; health; last runs; error rate |
| `/agents/runs/:runId` | **Run Inspector** | Step list + graph for one graph execution |
| `/workflow` | **Global Workflow Map** | Static+live diagram of the LangGraph topology; click node → agent doc |
| `/hitl` | **HITL Inbox** | Team approval queue with claim/approve/reject |
| `/shift` | **Shift Desk** | Current shift, ledger preview, compose/send handover, watchlist |
| `/problems` | **Problem Board** | Recurring/chronic sites |
| `/audit` | **Audit Explorer** | Searchable decision log |
| `/briefs` | **Exec Briefs** | P1/P2 living briefs (duty manager lens) |
| `/settings` | **Session & Profile** | Role switcher, display name, region filter, operator badge (read-only env profile) |

#### 14.2 Global chrome (every page)

Persistent top bar:
- Operator badge: **Safaricom (demo)** or **Airtel Kenya (demo)**  
- Autonomy level chip: L1 / L2 / L3 (visible; supervisor may toggle if allowed)  
- Clock **EAT**  
- Current shift: DAY/NIGHT  
- Live connection indicator: WS connected / reconnecting  
- Counts: Open P1 · P2 · HITL pending · Agents running · SLA risk  
- User: display name + role dropdown  

Left nav (collapsible): routes above.  
Bottom or side **Activity Ticker**: scrolling last 20 realtime events (“SeverityAgent set SFC-INC-… to P2 — HUB floor”).

#### 14.3 Mission Control (`/`)

Layout (desktop-first, responsive ok):

```text
┌─────────────────────────────────────────────────────────────┐
│ KPI cards: Open | P1 | P2 | HITL | SLA risk | Runs active   │
├──────────────────────────┬──────────────────────────────────┤
│ Live incident stream     │  Agent activity (who is running) │
│ (newest first, color by  │  list of AgentRunStep in flight  │
│  priority)               │  + recently completed            │
├──────────────────────────┼──────────────────────────────────┤
│ HITL needs you (top 5)   │  Watchlist / chronic problems    │
├──────────────────────────┴──────────────────────────────────┤
│ Mini workflow: last incident’s graph thumbnail (click open) │
└─────────────────────────────────────────────────────────────┘
```

Clicking any incident opens **Incident Workspace**.  
When a new event is ingested in another browser tab/demo script, **this page updates live** via WebSocket/SSE without full reload.

#### 14.4 Incident Workspace (`/incidents/:id`) — the heart of multi-agent UX

**Multi-pane layout** (must feel like a real ops console, not a blog post):

**A. Header strip**  
Incident number, priority pill (P1 red … P4 gray), status, site ID/name/type, region, est. users, mpesa_risk chip, owner/MSP, service-affecting flag, SLA clocks (ack/restore) with countdown color.

**B. Center — Narrative & ticket fields**  
Title, description, auto narrative, root cause hypothesis, impact summary, access notes, services impacted. Editable only where role allows (supervisor/analyst).

**C. Right — Live Agent Workflow Graph (REQUIRED)**  
- Nodes = LangGraph stages / agents (Ingest, Correlate, Enrich, Severity, Ticket, Assign, HITL, Broadcast, ExecBrief, Ledger, Monitor, Handover, Close).  
- Edges = control flow.  
- Node states: `pending | running (pulse) | succeeded | failed | skipped | waiting_hitl`.  
- Click node → drawer with that step’s rationale, tools_called, duration, input/output summaries.  
- If Supervisor fanned out workers, show child steps nested or as parallel branches.  
- **This is how the whole team “sees the multi-agentic system work.”**

**D. Bottom tabs**
1. **Unified Timeline** — agent steps + human notes + broadcasts + HITL decisions interleaved (newest or chronological toggle).  
2. **Work Notes** — add note (FE/MSP/NOC).  
3. **Comms** — broadcast drafts/sent (SMS/email bodies).  
4. **Exec Brief** — current brief + history.  
5. **Audit** — raw decisions for this INC.  
6. **Linked Problem** — if recurrence linked.

**E. Action bar**
- Claim/approve/reject HITL if pending  
- Trigger “Refresh enrichment” / “Re-run monitor” (manual graph triggers where safe)  
- Open in wallboard focus mode  

#### 14.5 Wallboard (`/wallboard`)

- Designed for **NOC room TV** (1920×1080): huge type, no dense tables.  
- Top: operator + EAT + shift.  
- Grid of **P1 and P2 cards** only (site, users, owner, minutes open, agent status “Ticketed / Awaiting vendor / HITL”).  
- Side column: HITL waiting count + “Agents running: N”.  
- Subtle animation when priority changes or new P1 appears.  
- Optional query `?region=NBI` for regional wall.  
- Auto-hide mouse cursor after idle (nice-to-have).

#### 14.6 Agent Observatory (`/agents`)

Table/cards for each agent in the roster:
- Name, one-line mission, last run time, success/fail counts (session or 24h), status Idle/Busy/Error.  
- Click → recent runs list → Run Inspector.  
- Banner explaining **Supervisor–worker** model in plain ops language.

#### 14.7 Global Workflow Map (`/workflow`)

- Full topology of the multi-agent graph (not per-incident).  
- Legend of HITL gates and autonomy differences L1/L2/L3.  
- Used for training: “this is how the system thinks.”

#### 14.8 HITL Inbox (`/hitl`)

- Shared team queue (not personal-only).  
- Columns: created EAT, incident, type, priority, proposed summary, claimed by, age.  
- **Claim** locks task to user (prevents two supervisors approving twice).  
- Detail drawer: **side-by-side** proposed message/priority/assignee vs editable fields.  
- Reject **requires reason** (stored on audit + note).  
- After resolve, realtime event removes card for all connected clients.

#### 14.9 Shift Desk (`/shift`)

- Current shift DAY/NIGHT + countdown to handover.  
- Embedded ledger table (same columns as Excel).  
- Watchlist builder (auto: all open P1/P2 + SLA risk + chronic).  
- **Preview handover email** (HTML) → **Send** (mock or SMTP).  
- History of last handovers sent.

#### 14.10 Problem Board & Audit & Briefs

- Problems: signature, site, count, last seen, linked INCs.  
- Audit: filter by actor=agent_name|user, action, free text.  
- Briefs: card list for open P1/P2 with “copy for WhatsApp/email” button (still prefer system email).

#### 14.11 RBAC matrix (enforce in UI; soft-enforce in API)

| Capability | Analyst | Supervisor | Duty Mgr | RNIO | MSP viewer | Automation admin |
|------------|---------|------------|----------|------|------------|------------------|
| View Mission Control | ✓ | ✓ | ✓ | ✓ region | MSP only | ✓ |
| View agent graph/traces | ✓ | ✓ | ✓ | ✓ | limited | ✓ |
| Approve HITL P3–P4 | ✓ | ✓ | ✓ | — | — | ✓ |
| Approve HITL P1–P2 | — | ✓ | ✓ | — | — | ✓ |
| Toggle autonomy L1–L3 | — | ✓ | ✓ | — | — | ✓ |
| Send handover | — | ✓ | ✓ | — | — | ✓ |
| Add work notes | ✓ | ✓ | ✓ | ✓ | ✓ own MSP | ✓ |
| Ingest demo event | ✓ | ✓ | — | — | — | ✓ |
| Wallboard | ✓ | ✓ | ✓ | ✓ | — | ✓ |

MVP may implement RBAC primarily in the frontend with API checks on HITL/handover/autonomy.

#### 14.12 UX / visual design principles

- **Dark NOC theme** default: deep charcoal/navy, priority colors: P1 `#E11D48`, P2 `#F97316`, P3 `#EAB308`, P4 `#64748B`.  
- Density: Mission Control dense; Wallboard sparse.  
- Always show **rationale** near agent conclusions (tooltips ok).  
- Empty states with CTA: “Run `make demo` or inject event from Settings → Demo inject”.  
- **Demo inject panel** (analyst+): form to fire mock Nairobi HUB / rural BTS / recurrence scenarios without CLI — critical for team demos on the glass.  
- Accessibility: contrast WCAG-ish; keyboard for HITL approve.  
- Performance: virtualize long timelines if needed; WS events are small JSON.  
- Mobile: Incident Board + HITL usable on phone for duty manager; wallboard desktop-only ok.

#### 14.13 Multi-user concurrency expectations

- Two browsers, two roles: both see same incident graph update when demo event fires.  
- HITL claim in browser A → browser B shows claimed_by within ~1s.  
- No “refresh to see agent finished” — streaming is mandatory for Mission Control and Incident Workspace graph.

#### 14.14 What “view the multi-agent workflow” means (acceptance language)

A shift supervisor must be able to answer **without opening code or logs**:
1. Which agents have already run for `SFC-INC-…`?  
2. Why is priority P2 not P4?  
3. Why was ATC assigned not Egypro?  
4. Is the system waiting on a human? Who should click?  
5. Did BroadcastAgent succeed or fail SMS?  
6. What should night shift watch?  

If the UI cannot answer these, it fails the product bar.

---

### 15. Repository structure

```text
kenya-noc-agents/
  README.md                 # architecture, UI map, roles, demos, screenshots section
  pyproject.toml
  Makefile
  docker-compose.yml
  config/
    default.yaml
    operators/
      safaricom.yaml
      airtel.yaml
  src/noc_agents/
    main.py
    config.py
    domain/
    db/
    graph/                  # langgraph + step instrumentation middleware
    agents/
    tools/
    ports/
    adapters/
    services/
    realtime/               # broadcaster hub for WS/SSE
    templates/              # email/sms/handover
    api/
      routers/
        incidents.py
        hitl.py
        agents.py
        runs.py
        shifts.py
        metrics.py
        session.py
        stream.py
  frontend/                 # React + Vite + TS — REQUIRED
    package.json
    index.html
    src/
      main.tsx
      App.tsx
      api/
      hooks/useOpsSocket.ts
      components/
        layout/
        kpis/
        IncidentBoard.tsx
        WorkflowGraph.tsx     # React Flow
        Timeline.tsx
        HitlDrawer.tsx
        ActivityTicker.tsx
        PriorityPill.tsx
      pages/
        MissionControl.tsx
        Wallboard.tsx
        IncidentWorkspace.tsx
        AgentObservatory.tsx
        WorkflowMap.tsx
        HitlInbox.tsx
        ShiftDesk.tsx
        Problems.tsx
        Audit.tsx
        Briefs.tsx
        Settings.tsx
      styles/
      mocks/                  # optional MSW for UI-only storybook later
  tests/
    test_priority_safaricom.py
    test_priority_airtel.py
    test_correlation.py
    test_assignment_matrix.py
    test_graph_hub_outage_nairobi.py
    test_recurrence_power.py
    test_handover_eat.py
    test_idempotency.py
    test_operator_prefixes.py
    test_agent_run_instrumentation.py
    test_hitl_claim.py
    test_metrics_summary.py
  data/
    seed/
      safaricom_sites.json
      airtel_sites.json
    shift_ledgers/
  scripts/
    demo_safaricom.py
    demo_airtel.py
    seed_db.py
  docs/
    UI_WALKTHROUGH.md       # click-path for team demo
    AGENT_WORKFLOW.md       # graph legend for training
```

---

### 16. What NOT to build

- No real Safaricom/Airtel proprietary API endpoints or real credentials.  
- No live RAN/CLI remediation in MVP (mock NetQuery only).  
- No dependency on paid LLM for demo.  
- No US-centric regions, area codes, or “pager” culture as primary UX — this is **Kenya NOC + RNIO + MSP**.  
- **No API-only delivery** — the React team dashboard is mandatory.  
- No fake “AI chat bubble” as the only UI — chat may exist as helper, but **Mission Control + Workflow Graph + HITL** are the product.  
- Do not skip tests.

---

### 17. Quality bar

1. `pytest` all green for **both** operator priority configs + agent run instrumentation tests.  
2. `make demo` (Safaricom) prints end-to-end story with `SFC-INC-…` **and** leaves data visible in the UI.  
3. `make demo-airtel` prints `ATL-INC-…` path.  
4. `make run` / `make run-ui`: open browser → Mission Control shows live update when demo event injected.  
5. README with: mermaid backend architecture, **UI sitemap**, role matrix, how to run two-browser HITL claim demo, Kenya region list, operator switch.  
6. Type hints backend; TypeScript frontend strict enough to build.  
7. AuditEvent + AgentRunStep on every agent decision.  
8. `docs/UI_WALKTHROUGH.md` with a 5-minute team demo script.

---

### 18. Implementation order

1. Operator config + domain/DB (including AgentRun/Step) + priority/fingerprint/numbering + tests  
2. Kenya seed data + mock adapters  
3. LangGraph with **mandatory step instrumentation + realtime hub**  
4. Graph happy path ingest→ticket→assign (Nairobi HUB)  
5. REST API for incidents/runs/workflow/metrics  
6. **Frontend shell**: layout, session role switcher, Mission Control wired to API  
7. **WorkflowGraph + Incident Workspace** live updates  
8. Broadcast + ledger + exec brief + HITL inbox (claim/approve)  
9. Wallboard + Shift Desk + handover  
10. Agent Observatory + Global Workflow Map + Problems + Audit  
11. Worklog monitor + recurrence  
12. Demo inject panel + dual-browser polish + Airtel profile + README/UI walkthrough  

**Vertical slice first (must work end-to-end on glass):**  
`UI Demo Inject → Nairobi HUB power fail → live graph nodes animate → SFC-INC on board → HITL card if P1 → approve on UI → comms/ledger visible → wallboard shows card`.

---

### 19. Acceptance scenarios

**A. Safaricom Nairobi HUB major outage**  
Given `SFC-NBI-HUB-01` power failure, est. ~450,000 users, when ingested then:
- `SFC-INC-…` created  
- priority **P1 or P2** (not P4)  
- `mpesa_risk` true if rules match  
- assignee power/passive MSP (ATC or Camusat)  
- RNIO-NBI + FE notified (or drafted at L1)  
- Excel row under safaricom ledger  
- exec brief exists  
- full audit trail  
- **UI:** incident appears on Mission Control without manual refresh; Workflow Graph shows completed Severity/Ticket/Assign steps with rationales  

**B. Idempotency**  
Same fingerprint twice within 15 min → one open incident.

**C. Recurrence**  
Third grid+genset failure on same site in 30 days → `SFC-PRB-…` problem record visible on Problem Board.

**D. Handover**  
Two open incidents → Shift Desk preview + send lists owners, priorities, EAT times, M-PESA column.

**E. HITL**  
L2: P1 management broadcast pending until approve **from HITL Inbox UI**.

**F. Airtel profile**  
`OPERATOR_PROFILE=airtel`, ingest `ATL-NBI-HUB-01` with 50,000 users → Airtel thresholds/prefixes; UI badge shows Airtel; no mpesa_risk.

**G. Multi-region P1 force**  
Cascade affecting NBI + CST HUBs → forced P1 on Safaricom.

**H. Whole-team multi-agent visibility (NEW)**  
With two browser sessions (Supervisor + Analyst):
1. Analyst injects HUB outage from Demo panel.  
2. Both see Activity Ticker events and Mission Control KPI bump.  
3. Both open the same INC; graph nodes progress live.  
4. Supervisor claims HITL; Analyst sees claim.  
5. Supervisor approves; Broadcast step turns green for both.  
6. Either can explain from the UI why priority and MSP were chosen (rationale text present).

**I. Wallboard**  
`/wallboard` shows only P1/P2 cards with owner and age; usable at a glance from 2 meters (large type).

**J. Role lens**  
Session as `rnio` with `region_filter=CST` primarily lists Coast incidents; session as `msp_viewer` + ATC sees ATC-assigned tickets.

---

### 20. Coding style

- Pure functions for priority, fingerprint, shift boundary (EAT).  
- Agents orchestrate; services compute.  
- Explicit errors → incident note + audit + failed AgentRunStep (UI red node).  
- Ops English: “service affecting”, “HUB”, “RNIO”, “genset”, “fibre cut”, “awaiting vendor”.  
- Frontend: small composable components; no god-file App.tsx of 3k lines.  
- Instrument graph nodes once via middleware/wrapper — do not copy-paste logging into every agent.

---

### 21. Deliverable checklist

- [ ] Runnable app with mock LLM  
- [ ] LangGraph multi-agent pipeline with **run/step instrumentation**  
- [ ] Realtime WS and/or SSE  
- [ ] **React team dashboard** (all routes in §14.1)  
- [ ] **Live Agent Workflow Graph** on incident workspace  
- [ ] Mission Control + Wallboard + HITL Inbox (claim) + Shift Desk  
- [ ] Agent Observatory + Global Workflow Map  
- [ ] Demo inject panel for team demos  
- [ ] Role switcher + basic RBAC  
- [ ] All agents present  
- [ ] **Safaricom default + Airtel secondary** YAML profiles  
- [ ] Kenya regions + seed sites  
- [ ] Excel shift ledger (EAT)  
- [ ] Handover email  
- [ ] Recurrence/problems  
- [ ] M-PESA risk tag (Safaricom)  
- [ ] Tests including both operators + instrumentation + HITL claim  
- [ ] `demo` + `demo-airtel`  
- [ ] `docs/UI_WALKTHROUGH.md`  
- [ ] Mermaid architecture + UI sitemap in README  

---

### 22. Final instruction

Generate the **full project** now, file by file, ensuring imports work, demos run, **and the React Mission Control UI is fully usable by a multi-person NOC shift**. Optimize for a **Nairobi-based NOC team** that must **see the multi-agent system work on the glass** during HUB power failures, MSP chases, and night-shift handovers at **Safaricom scale**, with a clean switch to **Airtel Kenya**.  

Tickets, owners, messages, **and the live workflow graph** must look like serious Kenyan telecom ops tooling — not a generic chatbot wrapper with a table glued on.

## END SUPER PROMPT

---

## Companion follow-up prompts (after first generation)

### Iteration A — deepen Kenya realism
> Replace any generic site names with Kenyan counties/cities. Expand failure narratives: genset, fuel, grid (KPLC-style language without claiming integration), fibre vandalism, MW hop, rains/access. Ensure SMS is concise for Safaricom/Airtel field staff. Double-check all timestamps render in EAT.

### Iteration B — Safaricom vs Airtel parity
> Run the same HUB outage scenario under both profiles and print a comparison table: incident number, priority, thresholds applied, MSP chosen, mpesa_risk, distribution lists. Fix any hard-coded Safaricom strings that break Airtel mode. UI operator badge and prefixes must switch cleanly.

### Iteration C — graph hardening
> Checkpointing, notification retries, poison-event dead letter, concurrent-safe daily INC sequences per operator prefix, MONITOR_LOOP for note intervals with region multipliers (EST 1.5×). Ensure every retry/failure appears as AgentRunStep visible in UI.

### Iteration D — HITL + team UX polish
> Side-by-side proposed broadcast vs edit box; reject requires reason; claim locks; autonomy L1/L2/L3 toggle on chrome; dual-browser test script in UI_WALKTHROUGH. Wallboard font sizes for TV. Empty states and Demo Inject panel if missing.

### Iteration E — multi-agent observability excellence
> Add Run Inspector flame-style step timing bars; tool call expanders; “explain this priority” panel that quotes SeverityAgent rationale + threshold config values; failed SMS with one-click resend; graph export PNG for post-incident review packs.

### Iteration F — integrations
> `AfricasTalkingSmsAdapter` stub + `FakeServiceNowAdapter` field map for SFC/ATL prefixes. Document env vars for Kenya SMS short-code later.

### Iteration G — Telkom optional
> Add a thin `telkom.yaml` stub (prefix `TKL-INC`) only if time remains; keep README note that market focus is Safaricom then Airtel.

---

## Tiny elevator prompt (context-constrained only)

```text
Build Python 3.11 multi-agent Kenya telecom NOC system (LangGraph + FastAPI + SQLite + Pydantic) PLUS a React+Vite+TS team dashboard (Mission Control, Wallboard, Incident Workspace with LIVE agent workflow graph, HITL inbox with claim, Shift Desk, Agent Observatory) fed by WebSocket/SSE and AgentRun/Step instrumentation so the whole NOC shift can watch multi-agent work. Default operator Safaricom (SFC-INC, EAT, M-PESA risk on CORE/NBI HUB); secondary Airtel (ATL-INC, lower thresholds, ATC-weighted passive). Agents: correlate, enrich, severity, auto-ticket HUBs, assign FE/MSP (Egypro/ATC/Camusat), broadcast RNIO/FE, track notes, Excel ledger, handover email, recurrence, exec brief. HITL L1/L2/L3. Regions NBI/CKA/CST/RVA/WST/EST. Role switcher. Demo inject on UI. Mock LLM/SMS/email. Tests + make demo/demo-airtel. No live network remediations without HITL. Neutral product name.
```

---

*Tuned for Kenyan market: Safaricom primary, Airtel secondary. Pair with `RESEARCH_NOC_MULTI_AGENT.md`.*
