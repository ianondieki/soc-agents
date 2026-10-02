# Where this project is — 1 October 2026

A plain-language snapshot for the owner. The detailed, row-by-row tracker is
`docs/CONFORMANCE.md`; the build specification is `SUPER_PROMPT_NOC_V2.md`.

## In one paragraph

`soc-agents` is a working multi-agent NOC prototype. An alarm posted to the API runs through
twelve agents in order (ingest, correlate, enrich, severity, ticket, assign, HITL gate,
broadcast, executive brief, shift ledger, recurrence, monitor), each step recorded with its
reasoning, its tool calls and its timing. The React UI shows the whole shift what the agents
did and lets a person approve what leaves the building. Everything runs offline on SQLite
with mock adapters; the LLM, e-mail, SMS and every external connection are off by default
and switch on with environment flags. The suite is green (3,208 tests), the UI builds, and
`bash scripts/run_all.sh` starts the whole thing on one port.

## What exists and works today

| Area | State | Where to look |
|---|---|---|
| 12-agent incident lifecycle, deterministic, one transaction per alarm | **Built**, golden-sequence tested | `src/noc_agents/orchestrator/`, `src/noc_agents/agents/` |
| Correlation: duplicates merge, child sites fold under their HUB major | Built | `agents/correlate.py` |
| P1–P4 severity with HUB/CORE floors and the M-PESA corridor tag | Built, config-driven | `config/operators/safaricom.yaml` |
| Region × domain MSP assignment matrix (Egypro, Tetranet, ATC, Camusat…) | Built | `services/assignment.py` |
| Approvals page: claim, read both renderings, approve or reject with a reason | Built | `frontend/src/pages/HitlInbox.tsx` |
| Transactional outbox: nothing e-mailed or texted from inside a transaction | Built | `orchestrator/outbox.py` |
| Scheduler for unattended jobs (SLA chase, pollers, housekeeping) | Built, **off** by default (`SCHEDULER_ENABLED`) | `scheduler/loop.py` |
| Live WebSocket stream of every agent step to the UI | Built | `realtime/` |
| Shift ledger (Excel) and day/night handover (gated) | Built | `services/ledger.py`, `services/handover.py` |
| Weather / CAP / flood early-warning lane | Built, off by default (`WEATHER_ENABLED`) | `pollers/`, `adapters/` |
| Vendor scorecards, PIRs, regulatory clocks, maintenance, contracts, complaints, capacity | Built behind flags (`*_ENABLED`), each with tests | `api/routers/`, `services/` |
| Advisory memory ("earlier at this site") | Built, off by default (`MEMORY_ENABLED`) | `memory/`, `services/memory.py` |
| Optional LLM drafting (Anthropic or a local Ollama) with redaction and a spend cap | Built, **off** by default (`LLM_ENABLED`) | `llm/` |
| Role-based access (nine roles) | Built, inert in the demo (`AUTH_DISABLED=true`) | `api/auth.py`, `api/deps.py` |
| **Showcase layer (this session)**: productivity rollup, agent rail, Showcase page, guided demo | Built, tested | see below |

## What was added in this session

1. **`GET /api/v1/metrics/productivity`** — adds up what the agents did (alarms, tickets,
   absorbed duplicates, steps, approvals, broadcasts, ledger rows, briefs) and multiplies the
   completed steps by the operator profile's estimate of the same work done by hand
   (`productivity.toil_minutes` in `config/operators/*.yaml`). The inputs are reported next
   to every number. `tests/unit/test_productivity.py` pins the shape and the arithmetic.
2. **The agent rail** (`frontend/src/components/AgentRail.tsx`) — one alarm's path through the
   twelve agents with timing, confidence and the human-wait state on every hop, and the
   reasoning one click away. On Mission Control it follows the newest alarm live; on the
   incident workspace it shows the run that opened the ticket (the old view showed the newest
   run, which for a HUB major is a two-step merge, so the ticket looked unfinished).
3. **`/showcase`** — the page for managers: the live rail, the numbers, the twelve steps
   before/after, how the agents sit on the existing platform, the autonomy ladder, what is
   never automated, where the minutes go, and how to add an agent.
4. **Guided demo** — a five-step presenter panel (top bar button) that runs the storm and
   walks the audience through ticket, approval, handover and the numbers.
5. Grouped navigation with a phone menu; the Agent observatory and Workflow map now show
   real throughput per agent and per hop.
6. `bash scripts/run_all.sh` / `make demo-up` for Linux and macOS (the PowerShell script
   remains); `scripts/screenshots.py` captures every route at 375 and 1440 px.
7. Five tests that failed on a fresh Linux machine were made environment-independent (see
   the commit message `test: make five suite failures environment-independent`); none hid a
   product defect.
8. **Design pass (2 October).** Two independent critiques (code review and screenshot
   evidence at a 1280 × 650 laptop) found every screen fighting itself: bold, monospace,
   uppercase labels, chips and five colours on the same row. The UI now has one type scale,
   sentence-case headings, chips only for state, drawn icons, one name per state
   ("waiting for a decision"), a six-item top bar, a sidebar that fits a 650 px window, one
   page head on every screen, loading/error/empty states on every list, self-hosted fonts,
   and an Audit trail that groups each alarm's steps by run (the API now returns `run_id`
   and `node` per audit row). Verified with Playwright at 1280 × 650, 1440 × 900 and
   375 × 812: no overflow, no console errors, zero critical or serious axe findings.
9. **Consistency pass (2 October, second round).** Heading levels never skip (axe is clean at
   every impact on every route); one primitive per job (segmented control, loading skeletons,
   error-with-retry, static rows, key/value lists); hop names from one source; loading never
   looks empty and a failed poll says so; the Showcase page cut from six copies of the twelve
   steps to one and from 4,362 px to about 2,000 px at 1280 wide; the approval card shows the
   SMS above its sticky decision bar on a laptop; the phone layouts stack rows instead of
   scrolling tables sideways.
10. **Demo pass (2 October, third round).** Two critiques ran the demo as a presenter would,
   from an empty board. The storm no longer starts by itself; the rail replays each new
   ticket run hop by hop and never jumps; a decided card shows its receipt where the eye
   is and focus moves to the next card; the guide is a bar under the top bar instead of a
   panel that covered what it described; waiting hops read as decided after the decision;
   the live layer keeps a small store so pages stop re-rendering during a storm; the
   server gzips, the build ships one chunk per route, the fonts are preloaded. Lighthouse
   (mobile profile) on the four main routes: performance 48–68 → 96–99, layout shift
   0.4 → 0, LCP 4–5 s → about 2 s.

## What is not done (honest list)

- **Nothing has been signed off by the floor.** `docs/SIGNOFF.md` is a blank form on purpose:
  shadow shifts, the SMS sandbox send, the first scorecard period and the breach drill are
  the product owner's to schedule.
- **No real external channel is wired.** E-mail works through Gmail SMTP when configured;
  SMS, WhatsApp, ServiceNow, Jira, PagerDuty and the other 29 declared tool connections are
  declarations with gates, not live integrations (`docs/AGENTS_MCP_LLM.md`).
- **The LLM is optional and off.** Every message is a template unless `LLM_ENABLED=true` and
  a Console API key or local Ollama is configured; even then the model only drafts.
- **The toil minutes are estimates.** They are the floor's own guesses written into the
  profile so the arithmetic is auditable; a stopwatch study would replace them.
- Phase 6+ of the spec (WhatsApp, social signals, individual metrics, MCP runtime) is
  deferred by paperwork or procurement, per `docs/CONFORMANCE.md` section D.

## How to run it

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
(cd frontend && npm install)
bash scripts/run_all.sh          # builds the UI, serves API + UI on http://127.0.0.1:8000
```

Windows: `powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1`.
Tests: `.venv/bin/python -m pytest -q` (about seven minutes). UI typecheck and bundle:
`cd frontend && npm run build`.

## How to present it

`docs/MANAGER_DEMO.md` is the ten-minute script. Short version: open Mission Control, press
**Guided demo**, follow the five steps, finish on `/showcase`.

## Suggested next steps

1. Run one shadow shift on the floor and record it in `docs/SIGNOFF.md`.
2. Replace the toil estimates with a half-day stopwatch study of three analysts.
3. Wire the first real adapter behind its flag (the Africa's Talking SMS sandbox is the
   cheapest; the ticketing system is the most valuable).
4. Decide the open owner questions in `docs/DECISIONS.md` (LLM provider, English-only
   messages, scorecard bands).
