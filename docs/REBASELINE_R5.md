# Re-baseline R5 — realtime events after commit

**Register entry:** `SUPER_PROMPT_NOC_V2.md` §2.1 R5. **Mechanism:** §7.0.4 (`realtime/commit_hook.py`).
**Phase:** 1.

**What changed in the product.** `graph/instrumentation.py:RunTracker` published its `agent.step.started`,
`agent.step.completed` and `agent.run.finished` events with `hub.publish_sync` **inside** the open
transaction (`instrumentation.py:67,109,133`). On a fail-closed rollback the UI had already been told
that INGEST, CORRELATE, ENRICH and SEVERITY completed, and none of those rows survived. Now the tracker
calls `realtime.commit_hook.buffer_event(session, event)`, which parks the event in
`session.info["events"]`; a class-level `after_commit` listener publishes the buffer in insertion order
(each event exactly once), and `after_rollback` / `after_soft_rollback` discard it. The events the UI
hears are therefore always backed by committed rows. `EventHub` stamps a global monotonic `seq` on each
stored ring-buffer record (a sibling field of the record, never an envelope key), keeps `recent(n)`
unchanged, adds `since(seq)`, and its default history is 2000 (was 100). `/ws/ops?since=N` replays only
the records newer than N as `{"seq": N, "event": <envelope>}`. The live `/ws/ops` frame stays the bare
six-key envelope — see "Decision needed" at the end.

**Scope of the test change.** Exactly one edit in one test file: the durability spy in
`tests/integration/test_golden_sequence.py::test_golden_full_lifecycle_with_hitl`. No other assertion in
the suite was edited. Two pinned assertions outside the register break as a direct consequence of the
mechanism and are **not** re-baselined here — see "Not re-baselined" at the end.

---

## R5 — the durability spy, widened

The spy patches `EventHub.publish_sync` on the class and, for each publish, reads the announced row back
through a **second** Session (uncommitted rows are invisible there).

| | Old | New |
|---|---|---|
| Which events are checked | Only `incident.created` (`if event.type == "incident.created"`) | **Every** event of the run: all 26 golden literals, filtered by `event.run_id == run.id` |
| What "durable" means | The incident row is readable in another Session | `agent.step.started`: the step row with that `run_id`/`seq` exists; `agent.step.completed`: it exists **and** its `status` equals the announced status; `agent.run.finished`: the run row exists and its `status` equals the announced status; `incident.created`: as before |
| Assertion | `assert durable_at_announce == [True]` | `assert [t for t, _ in seen] == [t for (t, *_rest) in GOLDEN_FULL_HITL]` (the 26 types, announced once each, in order) and `assert all(durable for _, durable in seen)` |

Why the old premise had to change: "only `incident.created` is durable at announce" was true because the
`agent.*` events were published from inside the transaction — the spy *could not* have checked them.
After §7.0.4 they are published from `after_commit`, so the check extends to all of them. The new
assertion is strictly stronger: it still requires `incident.created` to be durable (the old `[True]`),
and additionally requires the 25 `agent.*` events to be durable, present once each and in golden order.

---

## What did not move — verified

| Item | How | Result |
|---|---|---|
| The 26 run-scoped event literals, order, per-run `seq` | `test_golden_full_lifecycle_with_hitl`, `..._auto_broadcast`, `test_golden_merge_short_circuit`, `test_golden_cascade_child_short_circuit` — `GOLDEN_*` lists untouched | pass |
| Envelope key set `{type, operator_id, payload, incident_id, run_id, ts}` | `_check_envelopes` in every golden test; `test_events_after_commit.py` asserts `set(e) == ENVELOPE_KEYS` on every stored record and on every WS frame's `event` | pass — `seq` is an attribute of `HistoryRecord`, not a key |
| `recent(10)` (SSE) and `recent(15)` (`/ws/ops` connect) | calls unchanged in `main.py`; `test_recent_callers_are_unaffected`, `test_ws_ops_without_since_replays_recent_15_as_before`, `tests/unit/test_hub.py` | pass |
| `hub._history` read directly as dicts by tests | records are a `dict` subclass with identical keys/equality/JSON | pass |
| Fail-closed path still announces `agent.run.finished{FAILED}` | `runner._fail_closed` publishes it **after** `session.commit()` of the fresh transaction (runner.py:259 → :262); the rollback at :224 discards the buffered ghost events first | all 8 `test_runner_failures.py` tests see the FAILED event; `test_fail_closed_run_announces_only_the_failed_run_it_persisted` proves the row is durable at announce |
| 12 nodes / 11 edges, `tests/system/test_contracts.py` | not opened; run as part of the targeted verification | pass |

---

## Decision needed — §7.0.4 live-frame wrapper vs. the frozen contract

§7.0.4 says the `/ws/ops` frame wraps the envelope as `{"seq": N, "event": <envelope>}` "on the `since`
replay path and the live path alike". The frozen
`tests/system/test_contracts.py::test_ws_ops_replays_recent_and_delivers_live_events` (line 338,
`assert set(msg) == envelope`) pins the **live** frame as the bare six-key envelope; with the live
wrapper on, that test fails. Under the standing rules the frozen contract wins, so what ships is:

| Path | Frame | Why |
|---|---|---|
| `/ws/ops?since=N` replay | `{"seq": N, "event": <envelope>}` | §7.0.4; no frozen test observes it |
| `/ws/ops` replay without `since` | bare envelope (`recent(15)`, unchanged) | §7.0.4 "from `recent(15)` otherwise"; frozen contract |
| `/ws/ops` live | bare envelope (unchanged) | frozen contract, line 338 |

Consequence: a browser can only learn a `seq` from a `since` replay, so resume-by-seq is not yet usable
end to end. The plumbing is in place — `EventHub.subscribe(with_seq=True)` delivers the wrapped frame on
the live path (tested in `test_events_after_commit.py`) — and `main.py:ws_ops` switches with one line once
the contract is re-cut. The frontend (`frontend/src/realtime/renderers.ts:normalizeEvent`) already accepts
both shapes.

## Not re-baselined (outside the register — decision needed)

Both break in the default suite as a direct, intended consequence of §7.0.4. Neither file was edited.

| Test | Old pinned literal | What it observes now | Why |
|---|---|---|---|
| `tests/integration/test_runner_failures.py::test_fail_closed_ticket_error_persists_failed_run`, line 111 | `[_shape(e) for e in events] == GOLDEN_FULL_HITL[:9] + [("agent.run.finished", 5, "TICKET", None, "FAILED", None, False)]` | `[("agent.run.finished", 5, "TICKET", None, "FAILED", None, False)]` | The nine `agent.step.*` events for INGEST..TICKET were the ghost events of the rolled-back transaction — exactly the bug. The FAILED event (lines 114-121) is still present and unchanged. Proposed re-baseline: drop `GOLDEN_FULL_HITL[:9] +`. |
| `tests/integration/test_golden_sequence.py::test_step_payload_truncation_boundaries`, line 542 | `started, completed = _run_events(run.id)` after `start_step` / `complete_step` with **no commit** | `_run_events(run.id) == []` (ValueError on unpack) | The test drives a `RunTracker` directly and never commits; its two events are buffered on the session and, by design, never leave. Proposed re-baseline: add `session.commit()` before reading the events (the truncation assertions themselves are untouched). |
