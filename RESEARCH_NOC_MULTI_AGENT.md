# Deep Research: Multi-Agent NOC Incident Escalation & Management

**Project codename:** `kenya-noc-agents` / `noc-agents`  
**Context:** **Kenya** mobile-network NOC — **primary profile Safaricom PLC**, **secondary profile Airtel Kenya** (Telkom optional later). Major site/HUB outages, RNIO + field engineers + MSPs (Egypro, ATC, Camusat, …), P1–P4 impact, EAT day/night handover, Human-in-the-Loop NOC.  
**Date:** 2026-07-16 (updated: Kenya dual-operator tuning)  
**Goal of this doc:** Ground a production-grade **super prompt** for an LLM code-generation session, after multi-loop comparison of industry leaders.

---

## 0. Kenya telecom market framing (product grounding)

| Rank | Operator | Product role |
|------|----------|--------------|
| **1** | **Safaricom PLC** | **Default profile.** Market leader by subscribers and site footprint; highest outage visibility; demo narratives, seed sites, and thresholds default here. Optional **M-PESA corridor risk** tag on CORE / major Nairobi HUB incidents (business-impact flag only). |
| **2** | **Airtel Kenya** | **Secondary profile.** Challenger MNO; same codebase, different YAML: lower absolute user thresholds for P-labels, `ATL-INC` prefixes, often **ATC-weighted** passive/power assignment in demos. |
| 3 | Telkom Kenya | Future thin profile only — not required for MVP. |

**Ecosystem (assignment & tickets):** towercos / passive partners (e.g. ATC Kenya, Atlas Towers in market narrative), field MSPs (**Egypro**, **Camusat**, **ATC**), regional **RNIO** + **FE** pools.

**Geography (ops regions in config):** `NBI` Nairobi Metro, `CKA` Central/Mt Kenya, `CST` Coast, `RVA` Rift Valley, `WST` Western/Nyanza, `EST` Eastern/NE — timezone **`Africa/Nairobi` (EAT)**.

**Typical Kenya failure modes to model:** grid + genset/fuel, rectifier/battery countdown, MW hop, fibre cut/vandalism, HUB cascade to child sites, shelter temp, seasonal access (rains).

**Why two profiles matter:** one multi-agent product should not hardcode a single MNO; Safaricom-scale thresholds and public heat differ from Airtel’s estate size and partner mix, but **agent roster and lifecycle stay identical**.

---

## 1. Problem Statement (from operator reality)

### Pain today (manual NOC)

| Pain | What happens | Cost |
|------|----------------|------|
| Major network outage | RNIO + field engineers must be **broadcast-notified** by NOC | Manual, slow, inconsistent |
| Impact classification | Service-affecting failures classified by users affected: **P4 < 50,000**, then P3/P2/P1 for larger multitudes | Ad-hoc judgment, politics, late reclassification |
| Executive pressure | Top ranks call NOC repeatedly asking **cause of failure** | Context switching, incomplete answers, fatigue |
| Ticketing | Unique incident ID; assign Field Engineer **or** MSP (Egypro, ATC, Camusat, …); fill UI fields by hand | Latency, incomplete tickets, wrong assignee |
| Work tracking | MSP/FE notes updated until closure | Missed SLAs, lost narrative |
| Shift log | Excel of shift failures maintained manually | Incomplete audit trail |
| Handover | Mail to incoming day/night shift: what to watch, who owns it | Tribal knowledge; dropouts at shift change |
| Recurrence | Same HUB/site fails repeatedly without problem management | Chronic degradation, no systemic fix |

### Target state

**Multi-agent system does the work.** NOC staff become **Human-in-the-Loop (HITL)** governors: approve high-blast actions, override priority, authorize executive comms, close disputed tickets. Agents own detection enrichment, severity, ticketing, assignment, note chasing, shift log, handover mail, and recurring-problem surveillance.

---

## 2. Competitive / Partner Landscape

### 2.1 Telecom-native multi-agent NOC

| Player | Model | Specialized agents / patterns | HITL stance | Relevance to this project |
|--------|--------|-------------------------------|-------------|---------------------------|
| **TM Forum Incident Co-Pilot** (Catalyst C24.0.636; Cisco Crosswork write-up) | Multi-agent + RAG + telecom domain knowledge; TMF open APIs (e.g. TMF724 Incident) | **Incident Agent** (RCA, tickets, repair recs), **Net-query Agent** (device/CLI abstraction), **Healing / Optimization** agents; MoP-driven troubleshooting | Explicit: co-pilot, engineer still in charge | **Highest domain fit** — ticketing + RCA narrative + field/OSS integration |
| **Cisco Crosswork Multi-Agentic AI Framework** | Containerized agent platform, knowledge graph, MCP, LangChain/LangGraph | Net-query, performance report, troubleshooting, config drift, toxic factor | Progressive autonomy toward white/dark NOC | Architecture reference for agent lifecycle + MCP tools |
| **IBM Telco NOC Agent** (AWS Marketplace) | Supervisor/worker multi-agent for telco NOC | Real-time incident analysis, sleeping cell detection, modular workers | Production NOC assistant | Telco-specific supervisor pattern |
| **Sage IT / ONAP-style agentic NOC** | Observe–reason–act–learn | Domain agents + self-healing loops | Gradual autonomy | Conceptual closed-loop |

**Takeaway for us:** Mirror **Incident + Ticketing + Healing narrative + Net-query style enrichment**, not pure SRE “restart pod” playbooks. Use **supervisor + specialized workers**. Prefer **explainable recommendations** for trust (TIM reported ~90% faster narrative writing via Co-Pilot).

### 2.2 Enterprise incident / on-call platforms

| Player | Strength | AI / agent posture | Gap vs telecom NOC site ops |
|--------|----------|--------------------|------------------------------|
| **PagerDuty** | On-call, escalation policies, event intelligence | End-to-end AI agent suite (SRE agent, lifecycle agents); ServiceNow elevation of critical tickets; impact-based routing | Strong for digital services; weaker for **tower/HUB/MSP field dispatch** and shift Excel culture unless customized |
| **ServiceNow ITSM / ITOM** | System of record, CMDB, major incident workflows | Agentic triage + automation; often paired with PagerDuty | Excellent ticket model; needs custom telco CI model (site, HUB, sector, BSC/RNC, fiber path) |
| **incident.io** | Slack-native major incident command | AI automates large % of response, RCA, PR gen | ChatOps-first; less native to RF/transmission MSP workflows |
| **Rootly** | Incident workflow engine | AI grouping/prioritization + workflows | Same: great for product eng, adapt for field |
| **FireHydrant** | Incident process consistency | AI summaries (enterprise) | Process template source |
| **ilert** | Agentic IR guide + product | L1 co-pilot → L2 act-with-approval → L3 guardrailed autonomy | **Best maturity model for HITL rollout** |

**Takeaway:** Borrow **escalation policy + severity + stakeholder comms** from PagerDuty/incident.io; keep **ServiceNow-class ticket as system of record** (or local equivalent). Adopt **ilert L1→L3 autonomy ladder** so NOC trust is progressive.

### 2.3 Multi-agent IR reference implementations

| Source | Stack | Agent roster | Pattern to copy |
|--------|-------|--------------|-----------------|
| **Booz Allen** multi-agent IR | AWS Bedrock + **LangGraph** + MCP | **Supervisor** → Contextualization, Observability, Network Investigation, Evaluation; final write-back to ticket | Parallel worker fan-out at ticket creation; engineer gets enriched ticket first |
| **IBM watsonx + ServiceNow + Neo4j** | Graph topology + orchestration | RCA + recovery closed loop | Knowledge graph for site/HUB dependency impact |
| **Crafted / n8n style** | Workflow + LLM | Ingest → enrich → Jira/Slack escalate | Practical integration glue for SMS/email/Excel |
| Open examples (e.g. noc-ai-agent on GitHub) | LLM + MCP alert processing | Alert → incident pipeline | Scaffold only |

**Takeaway:** **LangGraph supervisor–worker graph** is the production-default orchestration choice for complex, stateful, branching incident lifecycles. CrewAI is fine for fast role prototypes; AutoGen/MAF if Microsoft-stack enterprise mandate.

### 2.4 AIOps correlation layer (feeds the agents)

| Player | Role | Why it matters |
|--------|------|----------------|
| **BigPanda** | Event correlation, noise reduction, incident intelligence | Collapses flood of alarms into one actionable situation before agents ticket |
| **Moogsoft / APEX AIOps** | Situations, dedup, probable cause | Same; telecom-friendly noise stories in market |
| Native EMS/NMS (Ericsson, Huawei, Nokia, Zabbix, Netcool, etc.) | Raw alarms | **Primary ingest** in real NOC |

**Takeaway:** Agents should **consume correlated “situations”**, not raw alarm storms. Implement a **Correlation/Ingest Agent** with pluggable adapters (webhook, Kafka, file, mock for demo).

### 2.5 Framework comparison (code generation target)

| Framework | Orchestration | Best for | Production notes | Recommendation for this project |
|-----------|---------------|----------|------------------|----------------------------------|
| **LangGraph** | Stateful directed graph, conditional edges, checkpoints | Complex lifecycle, HITL interrupts, retries | Industry leader for production stateful agents | **Primary** |
| **CrewAI** | Role-based crews | Fast role demos | Great for PoC of agent personas | Optional thin prototype layer |
| **AutoGen / Microsoft Agent Framework** | Conversational multi-agent | M365/Azure estates | Use if org is Microsoft-locked | Secondary |
| **n8n / Temporal** | Deterministic workflow | Email, Excel, SMS, SLA timers | Pair with LLM agents for side-effects | **Integration backbone** for non-LLM steps |

---

## 3. Mapping Industry Patterns → Your NOC Workflow

| Your real workflow | Industry analog | Agent ownership |
|--------------------|-----------------|-----------------|
| Broadcast outage to RNIO + FE | Major incident stakeholder notification (PagerDuty/incident.io) | **Comms / Broadcast Agent** |
| Classify P4 (<50k) … P1 | Business-impact priority (ServiceNow + evaluation agent) | **Severity & Impact Agent** |
| Stop executive phone spam | Status page + proactive RCA narrative (TIM Co-Pilot story) | **Executive Briefing / Status Agent** |
| Ticket major HUB/site failures | Incident Co-Pilot trouble ticket + Booz Allen ticket write-back | **Ticketing Agent** |
| Assign FE or MSP (Egypro, ATC, Camusat) | Assignment group / vendor routing | **Dispatch & Assignment Agent** |
| Fill ticket UI fields | Form automation / ITSM API | **Ticketing Agent** (API-first; UI automation only if no API) |
| Track MSP notes → closure | Incident updates / work notes | **Worklog Monitor Agent** |
| Shift Excel log | Shift report / performance report agent | **Shift Ledger Agent** |
| Handover mail night↔day | On-call handoff notes | **Shift Handover Agent** |
| Recurring problematic sites | Problem management + pattern memory | **Problem / Recurrence Agent** |
| NOC only approves | ilert L1–L3 + Cisco white NOC | **HITL Gateway + Supervisor** |

---

## 4. Recommended Target Architecture

```
                    ┌─────────────────────────────┐
                    │  Alarm / Situation Sources  │
                    │  NMS, EMS, BigPanda-like,   │
                    │  manual NOC intake, API     │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │     SUPERVISOR ORCHESTRATOR │
                    │   (LangGraph state machine) │
                    └──────────────┬──────────────┘
           ┌───────────┬───────────┼───────────┬───────────┐
           ▼           ▼           ▼           ▼           ▼
     Ingest/Corr  Severity   Ticketing   Dispatch    Comms
     Recurrence   Worklog    ShiftLog    Handover    ExecBrief
           │           │           │           │           │
           └───────────┴───────────┴─────┬─────┴───────────┘
                                         ▼
                              ┌────────────────────┐
                              │  HITL NOC Console  │
                              │  approve / override│
                              └─────────┬──────────┘
                                        ▼
                    Tickets | SMS/Email | Excel | Slack/Teams | Audit log
```

### Severity model (operator-configurable)

| Priority | Users affected (default) | Broadcast scope | HITL |
|----------|--------------------------|-----------------|------|
| **P4** | < 50,000 | RNIO + local FE; no exec blast | Auto ticket optional |
| **P3** | 50,000 – 249,999 | RNIO + FE + MSP lead | Auto ticket; notify NOC |
| **P2** | 250,000 – 999,999 | Broad + management distribution list | **HITL confirm** severity & message |
| **P1** | ≥ 1,000,000 **or** HUB / multi-HUB / core / national impact | Full major-incident protocol | **Mandatory HITL** before external exec wording |

> Thresholds must be **config YAML**, not hard-coded — different markets differ.

### Site criticality boost

- **HUB / aggregation / BSC/RNC / core / fiber backbone** failures force **minimum P2** (or P1 if multi-site cascade), even if early user-count estimate is low.
- Always **auto-ticket** major site failures (user requirement).

### Assignment rules (MSP example)

| Failure domain | Default assignee pool |
|----------------|----------------------|
| Tower / passive / power / diesel | ATC / Camusat (config) |
| Active radio / BTS / node | Field Engineer (region) or radio MSP |
| Fiber / transmission | Egypro / Tx MSP (config) |
| Unknown | NOC queue → HITL assign |

---

## 5. Autonomy Ladder (do not skip)

Aligned with ilert L1–L3 and Cisco progressive dark-NOC:

1. **L1 Co-pilot:** Agents draft tickets, severity, broadcasts, Excel rows, handover mails; human clicks Approve.
2. **L2 Guardrailed act:** Auto-create tickets for HUB failures; auto-notify RNIO/FE for P3–P4; HITL only for P1/P2 wording and MSP reassignment.
3. **L3 Conditional autonomy:** Auto-chase MSP notes, auto-escalate SLA breaches, auto-send shift handover; HITL only on conflicts/unknown topology.

**Start generating code for L1 + L2 with feature flags.**

---

## 6. Non-Functional Requirements (must land in prompt)

- Full **audit trail** of every agent decision (who/what/why/confidence).
- **Idempotent** ticket creation (dedupe by site+alarm fingerprint+time window).
- **SLA timers** per priority (ack, first note, restore, close).
- **Secrets** via env; no credentials in prompts.
- **Mock adapters** for demo without live Safaricom systems.
- **Explainability:** every severity and assignment includes human-readable rationale.
- **Timezone-aware** shifts (e.g. Africa/Nairobi).
- **Excel** via openpyxl/xlsxwriter; email via SMTP or provider API; SMS optional stub.

---

## 7. Iteration Log (prompt refinement loops)

### Loop 1 — Naive “build multi-agent NOC”
- Too vague; no severity model; no MSP; no HITL; no shift Excel.

### Loop 2 — Add user personas & tickets
- Added P1–P4, MSPs, ticket number; still missing recurrence, executive comms, correlation.

### Loop 3 — Industry alignment
- Injected TM Forum Co-Pilot roles, Booz Allen supervisor, ilert autonomy, PagerDuty escalation concepts.

### Loop 4 — Operator specificity
- HUB auto-ticket, shift handover email content, Excel shift ledger, MSP note tracking, RNIO broadcast.

### Loop 5 — Code-gen quality
- Forced stack choices, folder structure, schemas, tests, mock data, acceptance criteria, phased MVP.

### Loop 6 — Safety & production
- Idempotency, audit, feature flags, no destructive network remediations without HITL, secrets hygiene.

### Loop 7 — Final super prompt
- Single self-contained specification suitable as sole input to a coding LLM (Cursor/Claude/GPT/Grok).

### Loop 8 — Kenya dual-operator tuning (Safaricom → Airtel)
- Explicit Kenya market rank: Safaricom primary, Airtel secondary.
- Operator YAML profiles: prefixes `SFC-INC` / `ATL-INC`, different P-thresholds, MSP weighting, distribution lists.
- Kenya regions (NBI/CKA/CST/RVA/WST/EST), EAT shifts, Kenyan failure narratives.
- M-PESA corridor risk tag (Safaricom only); Airtel demo path (`make demo-airtel`).
- Neutral product naming (no false official affiliation).

### Loop 9 — Whole-team multi-agent Mission Control UI
- UI elevated from thin CRUD to **first-class product**: React SPA + dark NOC theme.
- **Live agent workflow graph** on every incident (React Flow / equivalent) backed by `AgentRun` / `AgentRunStep` + WebSocket/SSE.
- Surfaces: Mission Control, Wallboard (TV), Incident Workspace, Agent Observatory, Global Workflow Map, HITL Inbox (claim), Shift Desk, Problems, Audit, Exec Briefs.
- Personas: NOC analyst, shift supervisor, duty manager, RNIO, MSP viewer, automation admin — RBAC matrix.
- Dual-browser acceptance: whole shift sees the multi-agent system work on the glass; HITL claim propagates live.
- Demo inject panel so demos happen without leaving the UI.

---

## 8. Sources consulted (research base)

Industry materials informing this document include: TM Forum Incident Co-Pilot / Cisco Crosswork Multi-Agentic AI Framework; PagerDuty AI agent suite & ServiceNow integration patterns; incident.io / Rootly / FireHydrant comparisons; ilert Agentic Incident Management Guide (L1–L3); Booz Allen multi-agent IR (LangGraph + Bedrock + MCP); IBM telco NOC agent & agentic IR; BigPanda/Moogsoft AIOps correlation roles; LangGraph vs CrewAI vs AutoGen production comparisons (2026).

---

*Next artifact: `SUPER_PROMPT_NOC_MULTI_AGENT.md` — feed this to the code-generation LLM.*
