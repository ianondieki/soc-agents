# The orchestrator

**Who this is for:** an engineer who knows Python but has never seen this repository and has
not built an agentic system before. By the end you should be able to read a run in the
database, explain why a failure did or did not destroy a ticket, and add a new agent without
breaking the pinned tests.

**What this document is not:** it is not the per-agent reference. Which tools, models and MCP
servers each agent declares lives in `docs/AGENTS_MCP_LLM.md`, which is generated from
`agent_catalog()` (`src/noc_agents/orchestrator/registry.py:175`). This document is about the
*mechanism* — the contract, the invariants and the decisions — which does not change when a
profile gains a field.

**"Agent" here does not mean "LLM".** Every one of the twelve hot-path nodes is deterministic
Python. `RunContext.llm` exists and the hot path never uses it
(`src/noc_agents/orchestrator/contract.py:100`). An agent in this codebase is a unit of
*accountability* — one named actor, one audited step, one criticality class — not a unit of
model inference.

---

## 1. The hot path in one screen

One alarm goes in, one incident row comes out. `POST /api/v1/events` calls `process_event`
(`src/noc_agents/graph/pipeline.py:36`), which lazily imports and calls
`run_incident_lifecycle` (`src/noc_agents/orchestrator/runner.py:52`). That function walks
`NODE_CARDS` in order and commits once.

| # | Node | Agent | Criticality |
|---|---|---|---|
| 1 | `INGEST` | IngestCorrelationAgent | fail-closed |
| 2 | `CORRELATE` | IngestCorrelationAgent | fail-closed |
| 3 | `ENRICH` | EnrichmentAgent | fail-closed |
| 4 | `SEVERITY` | SeverityImpactAgent | fail-closed |
| 5 | `TICKET` | TicketingAgent | fail-closed |
| 6 | `ASSIGN` | DispatchAssignmentAgent | fail-closed |
| 7 | `HITL` | SupervisorAgent | fail-closed |
| 8 | `BROADCAST` | BroadcastCommsAgent | fail-closed |
| 9 | `EXEC_BRIEF` | ExecutiveBriefingAgent | fail-soft |
| 10 | `LEDGER` | ShiftLedgerAgent | fail-soft |
| 11 | `RECURRENCE` | RecurrenceProblemAgent | fail-soft |
| 12 | `MONITOR` | WorklogMonitorAgent | fail-soft |

Derived from `NODE_CARDS` (`registry.py:128-154`) joined to `AGENT_PROFILES.criticality`
(`registry.py:83-126`) through `profile_for` (`registry.py:163`). The same set is asserted
independently in `tests/unit/test_registry.py:57,103` — if you change a profile's criticality,
that assertion is where it will bite.

Twelve nodes, eleven edges (`workflow_edges` zips the tuple against itself,
`registry.py:171-172`), eleven distinct agents in the graph. The twelfth profile,
`ShiftHandoverAgent`, has no `NodeCard`: `agent_catalog()` reports it with
`node_ids: []` and `in_graph: False` (`registry.py:182,185`; pinned at
`tests/unit/test_registry.py:83-93`). It is the worked example of an agent that exists without
being on the hot path — see §6.

---

## 2. The contract: what an agent is here

An agent is **one module in `src/noc_agents/agents/` that exposes exactly two functions**:

```python
def input_summary(state: IncidentState, ctx: RunContext) -> str: ...
def run(state: IncidentState, ctx: RunContext) -> StepResult: ...
```

`input_summary` returns a short string recorded on the step row *before* the agent runs, so the
audit trail shows what the agent was looking at even if it then blew up. It takes `ctx` because
at least one agent needs config to describe its input: `HITL` returns `ctx.cfg.autonomy_level`
(`agents/hitl.py:15-16`). Keep these short and stable — the golden test pins all twelve
literally (`tests/integration/test_golden_sequence.py:148-175`).

`run` does the work: it reads earlier agents' output off `state`, assigns its own fields back
onto `state`, writes rows through `ctx.session`, and returns a `StepResult`.

### The four prohibitions

Stated in the module docstring at `contract.py:14-18` and enforced by test and by habit:

| Prohibition | Why | Check |
|---|---|---|
| **Never commit.** No `session.commit()` in any agent. | The whole run is one transaction; a mid-run commit makes the fail-closed rollback unable to undo anything before it. | `grep -rn "session.commit" src/noc_agents/agents/` returns nothing. |
| **Never publish realtime events.** No `hub.publish_sync` in any agent. | Event ordering is the runner's contract with the UI, pinned event-for-event by the golden test. | Same grep: no hits. |
| **Never touch the tracker — except `TICKET`.** | `ticket.py:93` calls `ctx.tracker.bind_incident(...)` because the incident does not exist until then; from that point every event carries the incident id and number. It is the single deliberate exception. | `grep -rn "ctx.tracker" src/noc_agents/agents/` returns exactly one line. |
| **Never import the runner, registry or pipeline.** | Those import the agents; the reverse would be a cycle. Agents depend on `contract.py` only, which is why `contract.py` imports SQLAlchemy and config under `TYPE_CHECKING` (`contract.py:30-36`). | `tests/unit/test_registry.py:124-130` greps every agent module for the forbidden names; `:117` re-imports the orchestrator in a fresh subprocess to prove there is no cycle. |

**Failure is reported by raising.** There is no "return an error" convention in the hot path.
The agent raises; the runner decides from the registry whether that kills the run or only the
step. Note the consequence: `StepResult(status=FAILED)` is written by the *runner*
(`runner.py:168`), not by an agent. In the hot path no agent ever constructs it. (The
out-of-band assist lane does — `src/noc_agents/llm/assist.py:231` — because it has no runner
above it.)

An agent may still absorb a failure it understands. `LEDGER` catches `OSError` from the Excel
workbook, marks its tool entry `ok: False`, writes the DB ledger row anyway and **succeeds**
(`agents/ledger.py:32-35`). That is a product decision inside the agent, distinct from the
runner's fail-soft isolation, and both behaviours are tested separately
(`tests/integration/test_runner_failures.py:198-231` vs `:162-196`).

### `StepResult`

`contract.py:53-63`. Everything the runner needs to close the step row and publish its event.

| Field | Meaning |
|---|---|
| `status` | `SUCCEEDED` / `WAITING_HITL` / `SHORT_CIRCUIT` / `FAILED`. See the table below. |
| `output_summary` | What happened, one line. Persisted in full; truncated to 160 chars in the websocket payload (`instrumentation.py:120`). |
| `rationale` | *Why* it happened, in words a NOC supervisor at 3 a.m. can act on. Persisted and broadcast **in full** — never truncated (`tests/integration/test_golden_sequence.py:548`). |
| `tools` | Plain dicts, built literally by the agent, e.g. `{"name": "send_email", "ok": True, "latency_ms": 50, "error": None}`. Stored as-is in `tools_called_json`; there is deliberately no helper class (`contract.py:48-50`). |
| `confidence` | `float | None`; defaults to `0.9`, which is `RunTracker.complete_step`'s own default. `ENRICH` returns `0.85` and `SEVERITY` `0.95`; a runner-generated fail-soft step gets `None`. |
| `incident`, `event_type`, `event_payload` | `SHORT_CIRCUIT` only — the row to return and the terminal event to publish after the commit. |

### When each status is used

| Status | Returned by | Runner behaviour | Effect on the run row |
|---|---|---|---|
| `SUCCEEDED` | the default; nine of twelve nodes on a clean run | record the step, continue | run finishes `SUCCEEDED` |
| `WAITING_HITL` | `HITL` when `needs_hitl()` is true (`agents/hitl.py:74`), and `BROADCAST` when it sees `state.waiting_hitl` (`agents/broadcast.py:17`) | record the step with that status, **continue to the end** | `state.waiting_hitl` makes the run finish `WAITING_HITL` (`runner.py:94`) |
| `SHORT_CIRCUIT` | `CORRELATE` only, on merge or cascade-child (`agents/correlate.py:49,92`) | record the step as **`SUCCEEDED`**, break the loop, return `result.incident` | run finishes `SUCCEEDED` with `incident_id` set to the *existing* incident |
| `FAILED` | never by a hot-path agent; constructed by `_soft_failure` (`runner.py:164-174`) | record the step, continue | does not change the run status |

Two things about `WAITING_HITL` catch people out. First, it does **not** stop the walk — the
run continues through `EXEC_BRIEF`, `LEDGER`, `RECURRENCE` and `MONITOR` and commits normally.
Second, `HITL` and `BROADCAST` are still **fail-closed** nodes; returning `WAITING_HITL` is a
normal outcome, not a soft failure.

### `IncidentState` and `RunContext`

`IncidentState` (`contract.py:66-89`) is the shared scratchpad — the thing the twelve agents
have instead of messages. Each field is annotated with the node that writes it. `NodeCard.reads`
and `NodeCard.writes` (`registry.py:79-80`) document the same information declaratively; they
are documentation only, nothing enforces them at runtime, so keep them honest by hand.

`RunContext` (`contract.py:92-104`) is the injected dependency bundle: `session`, `settings`,
`tracker`, `run`, and the unused `llm`. It is never persisted. `ctx.cfg` is a shortcut to
`settings.operator` and is what agents actually use.

---

## 3. Runner invariants

Read `run_incident_lifecycle` (`runner.py:52-129`) top to bottom. Five invariants hold.

### 3.1 The walk is an ordered `for` loop over `NODE_CARDS`

`runner.py:74`. There is no router, no scheduler, no graph engine. Execution order **is** tuple
order in the registry, which is also `WORKFLOW_NODES` order in the UI
(`graph/workflow_nodes.py:6`) and `/api/v1/agents` order. One list, three consumers.

Per node: `_input_summary` (`runner.py:76`), `tracker.start_step` (`:77`, which increments
`seq`, flushes the step row and publishes `agent.step.started`), `_run_step` (`:81`), then
`_record` (`:87`, which closes the step row, writes an `AuditRow` and publishes
`agent.step.completed`).

### 3.2 One transaction, and the commit is the last guarded statement

`session.commit()` sits at `runner.py:95`, inside the `try` opened at `:73` whose comment says
so explicitly. Nothing in the loop commits. Everything — the run row, every step row, every
audit row, the incident, the HITL task, the broadcasts, the brief, the ledger row, the work
note, the incident-number sequence bump — lands or does not land together.

The commit is *last* inside the guard on purpose. Put anything after it and inside it, and a
failure in that code would call `_fail_closed`, which starts with `session.rollback()`
(`runner.py:224`) — rolling back a transaction that has already committed does nothing, so you
would publish a `FAILED` event for a run whose rows are durable. Hence the split: the guarded
region ends at the commit; everything after it is the **post-commit epilogue**
(`runner.py:100-129`), where "the rows are durable, so an error here is reported as-is."

The epilogue publishes the one terminal event — `incident.created`, `incident.merged` or
`incident.cascade_child` — and returns the row. The golden test enforces the ordering directly:
it spies on `EventHub.publish_sync` and asserts that at the moment `incident.created` is
published, the incident is already readable through a **second, independent** `Session`
(`tests/integration/test_golden_sequence.py:314-326`). An announcement means a durable row.

**The one thing to know here:** `agent.step.*` events are published *during* the transaction,
from `RunTracker` (`instrumentation.py:67,109`). Only the terminal `incident.*` event waits for
the commit. So on a fail-closed failure the UI has already seen steps that no longer exist —
`tests/integration/test_runner_failures.py:109-111` asserts exactly that: four completed steps
were announced, and afterwards only the one `FAILED` step row survives. This is deliberate
(live progress on the wallboard) and is on the v2 roadmap to change.

### 3.3 `WAITING_HITL` suspends; a human resumes it

There is no suspended coroutine and no resume of the walk. "Suspension" is a **row state**.

1. `HITL` opens a `HitlTaskRow` with `task_type="APPROVE_BROADCAST"` and drafts every
   `BroadcastRow` as `PENDING_HITL` (`agents/hitl.py:35-71`), then sets `state.waiting_hitl`.
2. `BROADCAST` sees the flag and sends nothing (`agents/broadcast.py:15-21`).
3. The run completes all twelve nodes and `finish_run("WAITING_HITL")` commits. Note that
   `current_node` is deliberately **left at `MONITOR`** for this status
   (`instrumentation.py:130-131`) — the golden test pins
   `(run.status, run.current_node) == ("WAITING_HITL", "MONITOR")`
   (`test_golden_sequence.py:340`).
4. Later, `POST /api/v1/hitl/{task_id}/approve` (`src/noc_agents/main.py:655`) releases the held
   drafts through `release_broadcasts_after_hitl` (`graph/pipeline.py:43`) and closes the run
   with `_finish_waiting_run(session, inc, "SUCCEEDED")` (`main.py:606-618`).
   `POST .../reject` (`main.py:704`) cancels the pending drafts and closes the same run as
   **`CANCELLED`** with `error_summary = f"HITL rejected: {reason}"` (`main.py:718-720`).

So the "resume" is a second, short transaction in an HTTP route that finds the incident's
latest `WAITING_HITL` run and finalises it. Step rows are left exactly as they were.

### 3.4 `SHORT_CIRCUIT` ends the walk at `CORRELATE`

Only `CORRELATE` returns it, for the two cases where no new ticket should exist:

- **Merge** — an open incident with the same fingerprint inside the correlation window
  (default 15 minutes). The duplicate gets a work note; the existing incident is returned.
  Terminal event `incident.merged` (`agents/correlate.py:35-56`).
- **Cascade child** — a non-HUB site whose parent HUB already has an open major. The parent's
  `child_sites_down` is incremented and a note added; the *parent* is returned. Terminal event
  `incident.cascade_child` (`agents/correlate.py:75-106`).

The runner records the step as `SUCCEEDED`, not `SHORT_CIRCUIT` (`runner.py:84`) — the status
vocabulary on the wire stays small. Because this happens before `run.incident_id` is set, the
audit row for that step carries `entity_id = ""` (`instrumentation.py:102`); both the comment at
`runner.py:83` and `test_golden_sequence.py:584-588` call this out. A short-circuited run is two
steps long and still commits: the merge test proves it by reading the merge work note back
through a fresh session (`test_golden_sequence.py:598-600`).

### 3.5 Facts survive the rollback, ORM objects do not

Before anything can fail, the runner copies the run id, operator id and start time into a frozen
`_RunFacts` dataclass (`runner.py:68,191-197`). After `session.rollback()` the `AgentRunRow`
object is expunged and untrustworthy, so the fail-closed path rebuilds the row from those plain
values. Same trick for the step: `seq`, `started_at` and `input_summary` are read off the row
*before* the rollback (`runner.py:220-223`).

---

## 4. Fail-closed vs fail-soft

Criticality is a property of the **agent profile**, so it applies to every node that agent owns
(`registry.py:66`). `_is_fail_soft` looks it up per card (`runner.py:135-136`).

| Node | Agent | Criticality | A raise here means |
|---|---|---|---|
| `INGEST` | IngestCorrelationAgent | **fail-closed** | no ticket exists |
| `CORRELATE` | IngestCorrelationAgent | **fail-closed** | no ticket exists |
| `ENRICH` | EnrichmentAgent | **fail-closed** | no ticket exists |
| `SEVERITY` | SeverityImpactAgent | **fail-closed** | no ticket exists |
| `TICKET` | TicketingAgent | **fail-closed** | no ticket exists |
| `ASSIGN` | DispatchAssignmentAgent | **fail-closed** | ticket is discarded |
| `HITL` | SupervisorAgent | **fail-closed** | ticket is discarded |
| `BROADCAST` | BroadcastCommsAgent | **fail-closed** | ticket is discarded |
| `EXEC_BRIEF` | ExecutiveBriefingAgent | fail-soft | no exec brief; run continues |
| `LEDGER` | ShiftLedgerAgent | fail-soft | no ledger row; run continues |
| `RECURRENCE` | RecurrenceProblemAgent | fail-soft | no problem record; run continues |
| `MONITOR` | WorklogMonitorAgent | fail-soft | no SLA note; run continues |

The line sits after `BROADCAST` for one reason: everything up to and including the external
notification is what makes a ticket *correct and announced*. A half-ticket that was assigned to
nobody, or whose broadcast half-sent, is worse than no ticket — the alarm is still in the
upstream system and can be replayed. Everything after `BROADCAST` is derived reporting: losing
it costs a supervisor a report, not an outage.

### What actually happens

**Fail-closed** — `_run_step` re-raises (`runner.py:154-155`), the `except` at `runner.py:96`
calls `_fail_closed` and then `raise` re-raises to the caller (HTTP 500 —
`tests/integration/test_runner_failures.py:299-309` proves the app keeps serving afterwards).
`_fail_closed` (`runner.py:208-277`) does four things in order:

1. `session.rollback()` (`:224`) — discards the run, all steps, all audit rows, the incident,
   the incident-number sequence bump, notes, everything.
2. Opens a **fresh transaction** containing exactly two rows that describe the failure
   (`:225-259`): the `AgentRunRow` rebuilt from `_RunFacts` with `status=FAILED`,
   `incident_id=None`, `current_node=<node>`, `error_summary="<ExcType>: <message>"` truncated
   to 2000 chars (`_error_text`, `:200-201`); and one `AgentRunStepRow` for the failing node,
   `status=FAILED`, `tools_called=[]`, `confidence=None`. The step row is skipped when `card` is
   `None`, which means the failure was in `finish_run` or the commit, after the loop (`:218`).
3. If even *that* fails, it rolls back and swallows — deliberately, so a broken database does
   not mask the original exception (`:260-261`).
4. Publishes `agent.run.finished` with `status: FAILED` and an **additive `node` key** that
   successful runs do not carry (`:262-277`; pinned at
   `tests/integration/test_runner_failures.py:110-119`).

Net effect: the failure is fully auditable and nothing else survives. The canonical test asserts
`_steps(run) == [(5, "TICKET", "TicketingAgent", "FAILED")]` — seq 5, because the tracker had
already counted four steps that no longer exist — and `IncidentRow` table empty
(`test_runner_failures.py:99-106`). The rolled-back incident number is reused on the next event
(`:124-125`).

**Fail-soft** — `_run_step` catches, and `_soft_failure` (`runner.py:164-174`) turns the
exception into a `StepResult(status=FAILED)` with `output_summary="<NODE> failed (fail-soft);
run continued"`, the exception text as `rationale`, `confidence=None`, and one synthetic tool
entry `{"name": <tool>, "ok": False, "latency_ms": ..., "error": ...}`. The tool name is
`profile.tools[0]` — which is why `AgentProfile.tools` documents "`[0]` names the fail-soft
tool" (`registry.py:68`) — falling back to `node_id.lower()` when the profile declares none.
The run continues, later nodes run normally, and the commit happens as usual
(`test_runner_failures.py:162-196`).

`input_summary` gets the same treatment: a raise there on a fail-soft card yields an empty input
summary and a soft failure without even calling `run` (`runner.py:139-146`, tested at
`test_runner_failures.py:262-274`). On a fail-closed card it propagates.

### `_session_broken`: the escalation

```python
def _session_broken(session: Session, exc: Exception) -> bool:
    """A DB error, or a session left pending-rollback by a failed flush, cannot be absorbed fail-soft."""
    return isinstance(exc, SQLAlchemyError) or not session.is_active
```
`runner.py:159-161`, used at `runner.py:154`.

Fail-soft means *continue in the same transaction*. If the exception was a `SQLAlchemyError`, or
if a failed flush has left the session in "pending rollback" (`session.is_active` false), then
continuing is impossible — every later statement would raise `PendingRollbackError` and the
commit would fail anyway, producing a confusing error that names the wrong cause. So a fail-soft
agent's DB error is **escalated to the fail-closed path**: re-raised, rolled back, recorded as a
`FAILED` run. `tests/integration/test_runner_failures.py:142-156` drives an `OperationalError`
from `MONITOR` (a fail-soft node) and asserts the run is `FAILED` at `MONITOR`, no incident
survives, and `error_summary` starts with `"OperationalError"` — "the real cause, not
`PendingRollbackError`".

**Practical rule:** a fail-soft agent may fail on file I/O, arithmetic, a missing config key or a
bad external call. It may not fail on the database. If your fail-soft agent's only risky
operation is a query, its criticality label is buying you nothing.

---

## 5. What the golden test pins

`tests/integration/test_golden_sequence.py` is a literal transcript of what an operator and the
UI observe. Treat it as the specification, not as a test you can re-baseline for convenience.
It pins, exactly:

- the event sequence for four scenarios: full HITL run, full auto-broadcast run, merge
  short-circuit, cascade short-circuit (`:101-146`);
- for every event: `type`, `seq`, `node`, `agent`, `status`, `incident_number` and whether
  `incident_id` is set (`_shape`, `:230-240`);
- the envelope key set and each type's exact payload key set (`:41-60`, `_check_envelopes`);
- all twelve `input_summary` strings, per scenario (`:148-175`);
- every node's `tools_called` list, literally, including latencies (`:178-207`);
- per-node `confidence` (`0.9`, except `ENRICH` `0.85` and `SEVERITY` `0.95`, `:208`);
- every `output_summary` and `rationale` (`:355-387`);
- the audit rows, including `entity_id == ""` for the four steps before `TICKET` (`:389-405`);
- the side tables: HITL task, eight `PENDING_HITL` broadcasts, one brief, one ledger row, the
  work notes (`:407-430`);
- durability, read through a second `Session` on the same `DATABASE_URL` (`:271-284`).

`tests/unit/test_registry.py:63` separately pins **12 nodes and 11 edges**.

---

## 6. How to add an agent

### The rule first

**A new agent runs out of band, under its own `agent_runs.graph_name`. It is not inserted into
the twelve-node hot path.**

Inserting a node changes the node count, the edge count, every downstream `seq`, and the event
sequence — which means re-baselining the golden test. That is an explicit product decision made
by a human, with a reviewed change to the pinned literals, not something you do while adding a
capability. Adding an out-of-band agent changes nothing that is pinned.

The precedent already exists in the repo: the LLM assist lane creates its own `AgentRunRow` with
`graph_name="llm_assist"` and `trigger="ON_DEMAND"` (`src/noc_agents/llm/assist.py:36-37`),
drives it through the same `RunTracker` (`assist.py:236-268`), and uses node ids `RCA` and
`BRIEF_DRAFT` (`assist.py:38-41`) that are not in `NODE_CARDS`. `/api/v1/runs` accepts a
`graph_name` filter so the two lanes stay separable (`main.py:510-519`), and the incident detail
view prefers the lifecycle run (`main.py:421-430`). `ShiftHandoverAgent` is the other shape: a
profile with no node at all, catalogued with `in_graph: False`.

### The recipe

1. **Write the module.** `src/noc_agents/agents/<name>.py`, exposing `input_summary(state, ctx)`
   and `run(state, ctx)`. Import from `noc_agents.orchestrator.contract` and from services —
   never from `runner`, `registry` or `graph.pipeline`.
2. **Return a `StepResult`.** Fill `output_summary`, `rationale` and `tools`. Write the tool
   dicts literally. Do not commit, do not publish, do not touch the tracker.
3. **Add the `AgentProfile`** to `AGENT_PROFILES` in `registry.py`, appended at the end (the
   tuple's order is `/api/v1/agents`' order, and `tests/unit/test_registry.py:43-56` pins the
   full name/mission list). Set `criticality` honestly — if the agent's risky work is a DB
   query, see the `_session_broken` rule above. Put the tool that should be blamed on a
   fail-soft failure **first** in `tools`. For the current field set, see
   `docs/AGENTS_MCP_LLM.md`, which is generated from `agent_catalog()`.
4. **Decide where it runs.** Default and strongly preferred: out of band. Create an
   `AgentRunRow` with your own `graph_name`, wrap it in a `RunTracker`, `start_step` /
   `complete_step` / `finish_run`, commit in your own short transaction. Copy the shape from
   `assist.py:236-268`. Do **not** add a `NodeCard`.
5. **Only if a human has decided the agent belongs on the hot path:** add the `NodeCard` to
   `NODE_CARDS` at the right position, fill `reads`/`writes` honestly, add any new fields to
   `IncidentState`, and then re-baseline the golden literals in one reviewed change — node
   count, edge count, `seq` numbers, event sequence, input summaries, tools, and the audit-row
   list, in `tests/integration/test_golden_sequence.py` and `tests/unit/test_registry.py`.
6. **Run the suite.** Do not skip this step; the checks below exist precisely to catch a
   half-finished registration.

### What the import-time asserts catch

`registry.py:159-160` runs on every import of the registry, so a mistake fails at process
startup rather than mid-incident at 3 a.m.:

```python
assert all(c.agent in PROFILES_BY_NAME for c in NODE_CARDS), "NodeCard names an unknown agent"
assert len({c.node_id for c in NODE_CARDS}) == len(NODE_CARDS), "duplicate node_id in NODE_CARDS"
```

- A `NodeCard` whose `agent` string does not match an `AgentProfile.name` — a typo, or a profile
  you forgot to add — fails the first assert. Without it, `profile_for` would raise `KeyError`
  inside `_is_fail_soft` mid-run, at which point the runner would be deciding criticality while
  crashing.
- Two cards with the same `node_id` fail the second. Duplicate node ids would silently corrupt
  `graph_status_map` (`graph/workflow_nodes.py:10-15`) and the step-by-node lookups the tests
  and the UI both use.

Not caught at import: a `NodeCard` whose callables are missing or not callable, and profiles
with an invalid `criticality` string. Those are caught by
`tests/unit/test_registry.py:96-103`.

### Which tests will fail, and why

| Change | Test that fails | Why |
|---|---|---|
| Added an `AgentProfile` | `tests/unit/test_registry.py:66` | `EXPECTED_CATALOG` (`:43-56`) pins the exact ordered name/mission list. |
| Added or removed a `NodeCard` | `tests/unit/test_registry.py:60` | `EXPECTED_NODES`/`EXPECTED_EDGES` and `len == 12`/`11` (`:63`). |
| Changed a criticality | `tests/unit/test_registry.py:103` | `FAIL_CLOSED_NODES` (`:57`) is re-derived from the registry and compared. |
| Changed any agent's output text, tools or confidence | `tests/integration/test_golden_sequence.py` | Every literal is pinned; see §5. |
| Inserted a node into the hot path | `test_golden_sequence.py` **and** `test_registry.py` | Node count, edge count, every `seq` after the insertion point, the event sequence, the input-summary list and the audit list all move. |
| An agent imported the runner/registry/pipeline | `tests/unit/test_registry.py:124` | Greps the agent modules for the forbidden names. |
| Introduced an import cycle | `tests/unit/test_registry.py:117` | Imports each orchestrator module in a fresh subprocess. |

---

## 7. Decisions on record

### 7.1 Agent2Agent (A2A) between the in-process agents — considered and rejected

Per spec §4.3. A2A is a Linux Foundation protocol for agents in **different** processes, often
different organisations, to discover each other and exchange tasks over JSON-RPC.

The reasons it is wrong here are structural, not aesthetic:

- **The twelve agents share `IncidentState`.** `SEVERITY` reads the user estimate `ENRICH`
  wrote; `TICKET` reads eight fields written by four earlier agents (`registry.py:138`). A2A's
  own premise is the opposite — opaque agents that *"don't share internal memory, tools, or
  direct resource access"*
  ([a2a-protocol.org](https://a2a-protocol.org/latest/topics/enterprise-ready/)). Adopting it
  in-process means serialising a shared mutable scratchpad across a boundary that exists only
  to be crossed.
- **They share one SQLAlchemy session and one transaction.** `ctx.session` is the same object
  for all twelve (`contract.py:96`). Twelve A2A endpoints would mean twelve sessions.
- **Fail-closed rollback depends on that single transaction.** `_fail_closed` undoes eleven
  agents' writes with one `session.rollback()` (`runner.py:224`). Across process boundaries
  that becomes a distributed-transaction problem, and the answer to "did the ticket survive?"
  stops being "yes or no" and becomes "partly".
- **The SDK's dependency weight buys nothing.** `a2a-sdk 1.1.2` pulls `google-api-core`,
  `protobuf<7,>=5.29.5` and `json-rpc`
  ([pypi](https://pypi.org/pypi/a2a-sdk/json)) to replace a Python function call.

**Where A2A does belong:** at the organisational boundary — an MSP portal, Airtel's NOC or a
regulator's system calling *into* this NOC. Different organisation, own credentials, needs a
discoverable contract. That is later, read-only, behind authentication, hand-written
(~200 lines, no SDK), default-off. Never a send/dispatch/assign skill: software does not decide
to notify the outside world.

### 7.2 LangGraph for the hot path — rejected

Also rejected: LangGraph, the Agent SDK and Managed Agents, for the hot path only.

This is a **fixed, synchronous, deterministic workflow on a synchronous codebase**: twelve nodes
in a known order, eleven edges, one transaction, no dynamic routing, no model deciding what runs
next. A graph framework's value is routing you cannot predict and state you cannot hold; here
the routing is a `for` loop (`runner.py:74`) and the state is a dataclass. Adding a framework
would add an async runtime, a checkpointer that duplicates `agent_runs`/`agent_run_steps`, and a
second place where transaction boundaries are decided.

**Revisit only if dynamic routing is genuinely needed** — an agent choosing the next node at
runtime rather than the registry fixing it (spec line 2732). Until then, the mechanism you can
read in one file beats the one you have to read documentation for.

Note what is *not* rejected: the optional LLM layer already exists out of band
(`src/noc_agents/llm/assist.py`), and MCP is adopted declaratively on the profiles
([modelcontextprotocol.io](https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices)).
The rejection is specifically about replacing the orchestrator.

### 7.3 The A2A `TaskState` vocabulary — adopted now

Per spec §7.2.1. Adopting the *vocabulary* costs nothing today and is what makes an A2A endpoint
cheap later: when a counterparty asks for task status, the translation is a dictionary lookup,
not a redesign of the run model. Mapping to A2A's `TaskState`
([a2a-protocol.org/latest/specification](https://a2a-protocol.org/latest/specification/)):

| This codebase (`RunStatus`, `domain/enums.py:78-84`) | A2A `TaskState` | Produced where |
|---|---|---|
| `RUNNING` | `working` | `runner.py:60`, `instrumentation.py:53` |
| `WAITING_HITL` | `input-required` | `runner.py:94` |
| `SUCCEEDED` | `completed` | `runner.py:92,94`; `main.py:670` on HITL approve |
| `FAILED` | `failed` | `runner.py:235` |
| `CANCELLED` | `canceled` | `main.py:720` |
| HITL rejected | `rejected` | `main.py:704-720` |
| — | `submitted` | unused: a run is `RUNNING` from creation |
| — | `auth-required` | unused: no A2A endpoint exists yet |

`input-required` is the interesting one: it is exactly what `WAITING_HITL` means — the task
cannot proceed without input from outside the system. The fit is not a coincidence; it is why
the mapping is worth adopting before the endpoint exists.

**Discrepancy to resolve before an A2A endpoint is built.** The spec lists `CANCELLED→canceled`
and `HITL rejected→rejected` as two separate rows, but in the code they are the **same state**:
the only place that ever sets a run to `CANCELLED` is the HITL reject route (`main.py:720`);
`grep -rn "CANCELLED" src/` finds no other producer for `AgentRunRow.status`. As written, the
two rules collide. The disambiguator that exists today is `error_summary`, which the reject path
sets to `f"HITL rejected: {reason}"` (`main.py:719`). A concrete resolution, when the endpoint
is written: map `CANCELLED` with an `error_summary` beginning `"HITL rejected:"` to `rejected`,
and any other `CANCELLED` to `canceled` — and note that nothing currently produces the latter.
A cleaner fix is a distinct `RunStatus.REJECTED`, but that is a schema and API change and
therefore a product decision, not a documentation one.

(A second, harmless gap: `RunStatus.PENDING` (`domain/enums.py:79`) is declared and never
assigned to any run.)

---

## 8. Quick reference

| I want to… | Read |
|---|---|
| know what an agent may and may not do | `src/noc_agents/orchestrator/contract.py:1-19` |
| see the execution order | `src/noc_agents/orchestrator/registry.py:128-154` |
| understand a failed run | `src/noc_agents/orchestrator/runner.py:208-277` |
| understand why a step was `FAILED` but the run succeeded | `src/noc_agents/orchestrator/runner.py:149-174` |
| know what the UI receives | `src/noc_agents/graph/instrumentation.py:44-141` |
| know what I am allowed to change | `tests/integration/test_golden_sequence.py` |
| see each agent's tools, model tier and MCP cards | `docs/AGENTS_MCP_LLM.md` (generated) |
