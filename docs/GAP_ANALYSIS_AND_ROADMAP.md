# Gap Analysis & Roadmap — Kenya NOC Mission Control

**Audit date:** 2026-07-16  
**Scope:** Full product review after MVP (Safaricom-first multi-agent NOC)  
**Method:** Code inspection, test suite, demo path, ops-fidelity review against real NOC practice  

---

## Severity legend

| Tag | Meaning |
|-----|---------|
| **P0** | Breaks trust or correctness in production-like use |
| **P1** | Major ops gap — system unusable for real shift without workaround |
| **P2** | Important product gap — shipable demo, not floor-ready |
| **P3** | Nice-to-have / scale / polish |

---

## Loop 1 findings (pre-hardening)

### Domain fidelity (Safaricom NOC)

| ID | Sev | Gap | Impact |
|----|-----|-----|--------|
| D1 | P1 | Ticket missing classic NOC fields: outage start (EAT), technology, TT category/symptom, vendor TT ref, battery countdown, restoration code/time, NE name, zone | Agents “fill ticket UI” incompletely |
| D2 | P1 | MSP matrix is domain-only, not **region × domain** (real contracts vary by region) | Wrong vendor on Coast vs Nairobi power |
| D3 | P1 | No **parent HUB → child site** linkage on cascade | Flood of tickets instead of one major + children |
| D4 | P2 | No site class (Gold/Silver/Bronze or Critical/Major/Minor) beyond type | Priority politics incomplete |
| D5 | P2 | M-PESA tag only NBI HUB/CORE — real corridors include other cash-dense hubs | Under-flag risk |
| D6 | P2 | No diesel/fuel/theft narrative codes as first-class enum | Weaker shift language |
| D7 | P3 | Seed site IDs fictional (OK for demo) | Need import of real inventory later |

### Multi-agent / lifecycle

| ID | Sev | Gap | Impact |
|----|-----|-----|--------|
| A1 | P0 | Worklog “monitor” only posts one note — **no SLA silence chase / escalate** | Core user story incomplete |
| A2 | P1 | No restore/close path for MSP/FE notes → closure | Tickets never complete lifecycle |
| A3 | P1 | No reassignment after wrong MSP | HITL half-implemented |
| A4 | P2 | Graph is sequential orchestration, not true concurrent LangGraph workers | Acceptable MVP; not research-grade multi-agent |
| A5 | P2 | No checkpoint/resume if process crashes mid-pipeline | Partial incidents possible |
| A6 | P3 | No confidence calibration from outcomes | Learning loop missing |

### Data / reliability

| ID | Sev | Gap | Impact |
|----|-----|-----|--------|
| R1 | P0 | Incident number sequence not concurrency-safe under load | Duplicate numbers risk |
| R2 | P1 | SQLite schema `create_all` only — **no migrations** for new columns | Existing DB breaks on model change |
| R3 | P1 | Module-level `settings` in `main.py` frozen at import | Profile switch/env change ignored |
| R4 | P2 | Excel ledger date uses UTC not EAT | Wrong shift file name near midnight |
| R5 | P2 | Correlation window ignores parent_hub_id / cascade groups | Weak dedupe for HUB floods |
| R6 | P3 | Audit payload uses `str(dict)` not JSON | Hard to query |

### API / security

| ID | Sev | Gap | Impact |
|----|-----|-----|--------|
| S1 | P1 | No auth — anyone can inject/approve | Demo-only |
| S2 | P1 | RBAC only cosmetic on frontend | Duty manager rules not enforced |
| S3 | P2 | HITL claim/approve does not always push WS events | Dual-browser lag until poll |
| S4 | P2 | No rate limit on `/events` | Abuse/flood |
| S5 | P3 | CORS `*` | Fine for local demo |

### UI / team workflow

| ID | Sev | Gap | Impact |
|----|-----|-----|--------|
| U1 | P1 | `api.addNote` missing; ad-hoc fetch only | Fragile |
| U2 | P1 | No vendor note form with status transition (awaiting vendor → in progress → restored) | MSP tracking weak |
| U3 | P2 | Incident board sort not true P1-first | Ops risk |
| U4 | P2 | Wallboard doesn’t show HITL waiting state on cards | Duty manager blind spot |
| U5 | P2 | No close/restore buttons on workspace | Lifecycle incomplete on glass |
| U6 | P3 | No sound/flash on new P1 | Wallboard polish |

### Testing

| ID | Sev | Gap | Impact |
|----|-----|-----|--------|
| T1 | P1 | No tests for note-chase SLA, close, reassign, region MSP | Regressions likely |
| T2 | P2 | No frontend E2E (Playwright) | UI regressions silent |
| T3 | P2 | System tests don’t assert WS events | Realtime untested |
| T4 | P3 | No load test for concurrent ingest | Scale unknown |

---

## What this hardening pass implements (2026-07-16 loop)

| Gap | Fix |
|-----|-----|
| D1 Ticket fields | `tt_category`, technology, site_class, outage_start, battery countdown, vendor TT, resolution, rationales |
| D2 Region MSP | `msp_region_overrides` (NBI→ATC, CST→Camusat, …) |
| D3 HUB cascade | Child events with `parent_hub_id` link to open HUB major |
| D5 M-PESA regions | Expanded to NBI, CST, NYZ, RVA |
| A1 Worklog chase | `POST /api/v1/monitor/tick` + silence/SLA escalation HITL |
| A2 Close path | Note side-effects → IN_PROGRESS / RESTORED + `POST .../close` |
| A3 Reassign | `POST .../reassign` |
| R1 Numbering | SQLite atomic increment |
| R3 Settings | `_settings()` per request |
| R4 Excel EAT | Ledger filenames/timestamps in Africa/Nairobi |
| S3 HITL WS | claim/approve/reject publish realtime events |
| U1–U5 UI | Notes API, restore checkbox, close/reassign, TT panel, wallboard HITL, monitor button |
| T1 Tests | Region MSP, TT classify, cascade, lifecycle, monitor chase |

Still open for Phase B: auth/SSO, Alembic, real SMS, Playwright E2E, NMS topology.

---

## Recommended next phases (after this pass)

### Phase B — Floor readiness (2–3 weeks)
1. Import real site master (CSV) with parent HUB IDs  
2. Africa’s Talking SMS + corporate SMTP  
3. SSO (Azure AD) + real RBAC middleware  
4. Alembic migrations  
5. Playwright dual-browser HITL test  

### Phase C — Intelligent co-pilot (1–2 months)
1. RAG over historical TT + MoPs  
2. True LangGraph fan-out (enrich ‖ history ‖ topology)  
3. Topology-aware cascade (NMS integration)  
4. Problem management board with CAPA owners  
5. Exec WhatsApp/Teams connector (read-only briefs)  

### Phase D — Dark/white NOC
1. Allowlisted remote actions with dual control  
2. Digital twin dry-run before remediations  
3. Continuous agent evaluation (scorecards)

---

## Acceptance criteria for “super intelligent” bar

A system is *ops-intelligent* only if:

1. A night-shift RNIO trusts the ticket without calling NOC for basics  
2. Wrong MSP is rare and fixable in one HITL click  
3. Silent vendors are auto-escalated before SLA breach  
4. HUB power creates **one** major story, not 40 child tickets  
5. Every agent decision is replayable on the glass  
6. Handover mail matches the Excel ledger without human retyping  

MVP + this hardening moves from (5 partial) toward (1–6). Gaps remaining are Phase B/C.
