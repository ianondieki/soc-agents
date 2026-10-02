# The ten-minute demo for managers

What to show, in what order, and what to say. The product does the work; this script keeps the
presenter from narrating code.

## Before the room fills (5 minutes)

1. Start the stack: `bash scripts/run_all.sh` (Windows: `scripts\run_all.ps1`). It builds the
   UI and serves everything on <http://127.0.0.1:8000>.
2. Open two browser windows side by side: one on Mission Control (`/`), one on the Approvals page
   (`/hitl`). Two windows make the "a person approves it" moment visible.
3. If the board is not empty, go to Settings and note the storm button on Mission Control will
   add to it; a clean start is `rm data/noc_agents.db` before step 1.
4. Press **Guided demo** in the top bar. The panel follows you from page to page.
5. Internet is optional. Without it the UI falls back to the system font; nothing else changes.

## The story in one sentence

A NOC analyst used to spend about forty-five minutes per service-affecting alarm on typing,
matrix lookups and chasing; the agents now do that in under a second, record why, and hand the
decisions that matter to a person.

## Step 1 — Start the storm (2 minutes)

Press **Launch the storm** in the guide (or the storm button in the hero).

Say: *Heavy rain hits Rift, Mt Kenya and Nairobi East. Eleven alarms arrive in twenty seconds —
microwave hops fail and child sites drop under their HUBs. Watch the rail under the KPIs: every
hop is one agent finishing its part of the job.*

Point at:

- the rail lighting up hop by hop with milliseconds under each;
- the live incidents list filling with `INC000001` upwards;
- the Agent activity ticker under the lists, one line per agent step;
- the HITL count rising to four: those are the P2 broadcasts waiting for a person.

The storm absorbs six of the eleven alarms into their parent HUB tickets. That is the
correlation agent refusing to open duplicates.

## Step 2 — Read what the agents decided (2 minutes)

Open the first ticket (the guide's button, or click the row).

Say: *Every field an analyst used to type is already filled: category, technology, outage
start, the responsible MSP, the field engineer, the expected resolution. Two lines explain the
priority and the assignment in the floor's own words.*

Click **Severity** on the rail: the reasoning reads `users=280000→P2; site_type=HUB floor=P2;
mpesa_risk=true`. Click **Assign**: `region=RFT; lane=tx_mw; pool=['TETRANET', 'FIELD_ENGINEER']`.
Point at the lavender **Approval** and **Broadcast** hops: *these are waiting for a decision*.
Point at "2 later alarms folded in": the duplicates the HUB absorbed.

## Step 3 — Approve what matters (2 minutes)

In the second window, the Approvals page.

Say: *A P2 broadcast never leaves without a named person. The card shows the SMS and the e-mail
exactly as they will be sent, with the facts beside them. One person claims it so two
supervisors cannot both act; a reason is required and lands on the audit row.*

Claim, type a reason, approve. Back on Mission Control the HITL count drops and the ticket's
run flips from "waiting for a human" to done. With a Gmail app password configured the e-mail
really arrives; without one it is stored and marked mock.

## Step 4 — Hand the shift over (1 minute)

Open the Shift desk.

Say: *The Excel ledger was written as each ticket opened. The handover lists every open ticket
with its owner, priority and last vendor note. It is generated, reviewed, and only then sent.*

Press **Generate / send handover preview**.

## Step 5 — Show the numbers (3 minutes)

Open `/showcase`.

Say: *Everything on this page is read from the running system.* Then walk down:

- **The three numbers.** Hours saved after the time people spent deciding; alarms into
  tickets with the share folded into an open ticket; decisions made by a person and how many
  are waiting now. Switch between *Last 24 h* and *All time* above them.
- **Latest alarm through the agents.** The same rail as Mission control, hop by hop.
- **What changed for the floor.** The twelve steps in one table: before, now, and the minutes
  a person spends on each by hand, with the per-alarm total.
- **How it sits on the platform.** Nothing is replaced: the NMS feed, the ticket system, the
  CMDB, the mail and SMS gateways and the Excel ledger stay. The agents read and write through
  adapters. Twenty-nine tool connections across twenty systems are already declared, each
  read-only or behind a named approval, none switched on here.
- **People keep the decisions.** The autonomy level is one setting; this deployment runs at
  L2. Beside it, the list of what is never automated.
- **Try it yourself.** One line, one button to the Approvals page.

## Where the numbers come from (say this if asked, and before anyone asks about "AI magic")

- Counts are rows the system already writes: runs, steps, incidents, approvals, broadcasts,
  ledger rows, briefs. `GET /api/v1/metrics/productivity` adds them up; nothing is estimated
  there.
- Minutes are a **model**: `productivity.toil_minutes` in `config/operators/safaricom.yaml`
  says how long an analyst spends on each step by hand (1 min ingest … 8 min ticket … 10 min
  exec brief; 47 min for a full alarm, 4 min for a duplicate). The page shows those inputs and
  multiplies them by completed steps. Decided approvals are charged back at 2 min each.
  Change the YAML and the page recalculates. A stopwatch study replaces the guesses.
- Timings in milliseconds are measured, per step, from the step rows.

## Questions you will get

**Is a language model deciding priorities?** No. The twelve-step path is deterministic code
driven by the operator's YAML; it runs with no model, no network and no credentials, and the
suite proves that on every run. A model is optional (`LLM_ENABLED`), only drafts text after
the ticket is committed, is fed redacted data, has a monthly spend cap, and every draft is
validated or replaced by the template.

**How does it plug into our systems?** Through adapter interfaces — alarm source, ticket
system, CMDB, e-mail, SMS, ledger. The demo ships mocks behind the same interfaces; a real
adapter is a module behind a flag. Writes into another system are never made by a model; the
orchestrator makes them after a person approves the matching card.

**What stays human?** P1 and P2 wording, priority overrides, reassignment disputes, the
handover send, anything that changes a live network element (there is no such tool), and
every write into another system.

**What about personal data and Kenyan law?** Names, numbers and addresses are redacted before
any external call; each transfer outside Kenya is recorded; the design notes in
`docs/COMPLIANCE.md` cover the Data Protection Act posture. The demo data is fictional.

**What does it cost to run?** The demo costs nothing. The spec's pilot estimate (section 11)
is roughly USD 150–300 a month, most of it the hosted model and SMS, and both are optional.

**What would it take to go live?** A shadow shift signed by the floor (`docs/SIGNOFF.md`),
one real adapter, and the owner decisions in `docs/DECISIONS.md`. `docs/STATUS.md` has the
honest list.

## If something goes wrong

- *The browser shows `{"detail":{"code":"not_found","message":"Not found."}}`*: that is not this
  app. Another program owns the port, most often the `second-brain` Bridge API, whose Docker stack
  publishes 127.0.0.1:8000 and answers 404s in exactly those words. Stop it
  (`docker compose -f infra/docker-compose.dev.yml down` in that repo) or start the NOC on another
  port: `PORT=8010 bash scripts/run_all.sh`, or `scripts\run_all.ps1 -Port 8010` on Windows, then
  open http://127.0.0.1:8010. Both scripts now refuse to start on a busy port and say so.
- *`ModuleNotFoundError: No module named 'fastapi'` on start*: the `python` on your PATH is another
  project's virtualenv (the prompt says `(.venv)` but it is not this repo's). Both run scripts now
  check the interpreter first and print the fix; pass the right one explicitly:
  `scripts\run_all.ps1 -Python C:\Python313\python.exe` or `PYTHON=/path/to/python bash scripts/run_all.sh`,
  or install the project into that interpreter with `python -m pip install -e ".[dev]"`.
- *The browser shows `{"detail":"Not Found"}` or a page saying the UI is not built*: the API is
  up but `frontend/dist` is missing. Run `cd frontend && npm install && npm run build`, then
  restart, or use the Vite dev server (`npm run dev`, http://127.0.0.1:5173) while the API runs.
- *Rail does not animate*: the top bar should read "Live". If it says "Reconnecting",
  refresh; the page polls as a fallback and the numbers stay right.
- *Nothing happens on the storm button*: the top bar says "API unreachable" when the API is down; the server log is in the
  terminal running `run_all.sh`.
- *Mission Control already has tickets*: that is fine; the numbers accumulate. For a clean
  board stop the server, delete `data/noc_agents.db`, start again.
- *Quiet mode is on* (button in the top bar): animations are off by design for night shifts.
