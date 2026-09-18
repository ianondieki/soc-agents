# Kenya NOC Mission Control (`soc-agents`)

Multi-agent **incident escalation & management** system for Kenyan telecom NOC operations.

| Profile | Role |
|---------|------|
| **Safaricom PLC (default)** | Primary — 6 regions, ~7k sites, `INC######` (9-char) tickets, floor MSP matrix |
| **Airtel Kenya** | Secondary YAML profile (`OPERATOR_PROFILE=airtel`) |

> Demo / training product. **Not** an official Safaricom or Airtel system.

## What it does

Agents replace most NOC toil; humans stay **HITL**:

1. Ingest & correlate alarms  
2. Severity P1–P4 (Safaricom: P4 &lt; 50k users; HUB floor P2; CORE → P1)  
3. Auto-ticket major HUB/CORE failures with unique incident numbers  
4. Assign FE or MSP (ATC, Camusat, Egypro)  
5. Broadcast RNIO / FE / MSP (held for HITL on P1/P2 under L2)  
6. Exec brief (cut “what’s the cause?” phone spam)  
7. Shift Excel ledger + day/night handover  
8. Recurrence → problem records  
9. **Team Mission Control UI** with **live multi-agent workflow graph**

## Safaricom regions (ops model)

| Code | Label | Example coverage |
|------|--------|------------------|
**~7,000 sites · 50M+ subscribers · 6 regions · tickets `INC######` (9 chars)**

| Code | Region | Power MSP | TX / Fibre | Radio OEM |
|------|--------|-----------|------------|-----------|
| **NBI_E** | Nairobi East | Egypro / Remote Egypro | Egypro Fibre | Mixed |
| **NBI_W** | Nairobi West | ATC / Camusat | Camusat, Ecta, Adrian, Alan Dick | **Huawei** |
| **MTK** | Mt Kenya | Egypro / Remote Egypro | **Soliton**, Egypro Fibre | Mixed |
| **CST** | Coast | ATC / Camusat | Camusat, Ecta, Adrian… | **Huawei** |
| **RFT** | Rift Valley | **Tetranet** | Camusat / Ecta / Egypro Fibre | Mixed |
| **WNY** | Western-Nyanza | **Tetranet** | Camusat / Ecta / Egypro Fibre | **Nokia+Huawei** |

**Priority:** P4 &lt;50k · P3 &lt;100k · P2 100k–&lt;500k · P1 ≥500k subscribers  

Timezone: **Africa/Nairobi (EAT)**.

## Architecture

```mermaid
flowchart LR
  Alarm --> Ingest --> Correlate --> Enrich --> Severity
  Severity --> Ticket --> Assign --> HITL
  HITL --> Broadcast --> ExecBrief --> Ledger --> Recurrence --> Monitor
  subgraph UI
    MissionControl
    WorkflowGraph
    HITLInbox
    Wallboard
  end
  Ticket --> MissionControl
  HITL --> HITLInbox
```

## Quick start (live Mission Control)

```powershell
# from soc-agents/
python -m pip install -e ".[dev]"
python -m pytest -q

# One-command: rebuild UI + start API (recommended)
powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1
# open http://127.0.0.1:8000
# Empty board auto-starts heavy-rain MW storm; or click the blue storm button
```

Or two terminals (best live WebSocket during dev):

```powershell
# Terminal 1
python -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port 8000

# Terminal 2
cd frontend; npm run dev
# open http://127.0.0.1:5173  — top bar must show API OK + LIVE WS
```

**Empty board:** Mission Control auto-launches the rain/MW cascade once per browser session.  
**Storm content:** Rift + Mt Kenya + Nairobi East microwave hops fail; child sites cascade under HUB majors.

## Tests

| Layer | Path |
|-------|------|
| Unit | `tests/unit/` — priority, assignment, fingerprint, shifts |
| Integration | `tests/integration/` — full pipeline, idempotency, recurrence, handover |
| System | `tests/system/` — FastAPI health, ingest, workflow, HITL, metrics |

```bash
python -m pytest -q
```

## UI map

| Route | Purpose |
|-------|---------|
| `/` | Mission Control KPIs + live incidents + agent ticker |
| `/incidents/:id` | Workspace + **live agent workflow graph** |
| `/hitl` | Shared claim/approve/reject queue |
| `/shift` | Ledger + handover |
| `/wallboard` | TV-friendly P1/P2 |
| `/settings` | Role + **demo inject** (Westlands HUB, Coast, Nyanza, NEA, …) |

## Config

- `config/default.yaml` — active profile  
- `config/operators/safaricom.yaml` — primary (detailed regions)  
- `config/operators/airtel.yaml` — secondary  

```bash
set OPERATOR_PROFILE=airtel   # Windows PowerShell: $env:OPERATOR_PROFILE="airtel"
```

## Docs

- `SUPER_PROMPT_NOC_MULTI_AGENT.md` — generation prompt  
- `RESEARCH_NOC_MULTI_AGENT.md` — industry research  
- `docs/UI_WALKTHROUGH.md` — 5-minute team demo script  
- **`docs/GAP_ANALYSIS_AND_ROADMAP.md`** — flaws found + hardening log + Phase B/C roadmap  

## Gmail demo email

Outage broadcasts can **really email your Gmail** for demos:

1. Create a Google **App Password** (not your normal password)  
2. Set env vars (see `docs/GMAIL_SETUP.md` and `.env.example`):

```powershell
$env:GMAIL_ADDRESS = "you@gmail.com"
$env:GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
$env:DEMO_EMAIL_TO = "you@gmail.com"
$env:EMAIL_ENABLED = "true"   # required since Phase 0 — sending is opt-in
```

3. Restart API → Settings → **Send test email**  
4. Inject P4 for instant mail, or P2 HUB → HITL Approve to send  

Without credentials — **or without `EMAIL_ENABLED=true`** — the system stays on **mock** (tests stay green).

## Hardening highlights (post-MVP)

- Full **NOC TT fields**: category, symptom, site class, technology, battery countdown, vendor TT, resolution  
- **Region × domain MSP** (e.g. Nairobi power→ATC, Coast power→Camusat)  
- **HUB cascade**: child sites with `parent_hub_id` attach to open HUB major  
- **WorklogMonitor** silence/SLA chase: `POST /api/v1/monitor/tick`  
- **Close / reassign / MSP notes** drive real status transitions  
- HITL claim/approve **WebSocket** events for dual-browser shifts  
- Excel ledger timestamps in **EAT**
