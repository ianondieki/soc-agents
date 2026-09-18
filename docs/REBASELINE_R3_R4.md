# Re-baseline R3 / R4 — the transactional outbox

**Register entries:** `SUPER_PROMPT_NOC_V2.md` §2.1 R3 and R4. **Mechanism:** §7.0.2 (outbox), §5.3.7
(BroadcastCommsAgent), §5.3.9 (ShiftLedgerAgent). **Phase:** 1.

**What changed in the product.** Before this change the SMTP call and the Excel append ran
*inside* the SQLite write transaction of `run_incident_lifecycle` (verified defects #6/#8/#17: a
failure after the send rolled the incident back but not the email, and the rolled-back INC number
was reused). Now every outbound effect is an `outbox` row committed with the incident and
transmitted afterwards by `orchestrator.outbox.drain_once`. In the API, the demo and the tests the
drain runs synchronously right after the commit (`OUTBOX_SYNC_DRAIN`, default on), so the same
observable things still happen — just after the commit, never inside it.

**Scope of the test change.** Exactly one test file, `tests/integration/test_golden_sequence.py`,
six edits, all listed below. No other assertion in the suite was edited. Two tests outside the
register break and are *not* re-baselined here — see "Not re-baselined" at the end.

**Targeted verification (2026-09-17):** `tests/unit/test_outbox.py tests/integration/test_golden_sequence.py
tests/integration/test_runner_failures.py tests/integration/test_hitl_decisions.py tests/system/test_contracts.py
tests/unit/test_ledger_root.py` → **69 passed, 2 failed**; the 2 failures are the out-of-register pair
described at the end. The same set was 40 passed before the change (`test_outbox.py` adds 31).

---

## R3 — BROADCAST and LEDGER step-row literals

The step row must say what the node actually did. The node no longer sends or appends; it renders
and enqueues. Nothing here keeps the old `send_sms` / `send_email` / `append_excel_row` names alive.

| # | Assertion (test_golden_sequence.py) | Old literal | New literal | Why this is a true consequence of moving the side effect out of the transaction |
|---|---|---|---|---|
| 1 | `TOOLS_SHARED["LEDGER"]` (both golden runs) | `[{"name": "append_excel_row", "ok": True, "latency_ms": 8}]` | `[{"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None}]` | The LEDGER node no longer opens the workbook: it renders the row cells and queues one `EXCEL_ROW` outbox row; the dispatcher appends the xlsx after commit under a file lock (§5.3.9). The only tool the node calls is `outbox.enqueue`. |
| 2 | `TOOLS_AUTO_RUN["BROADCAST"]` | `[{"name": "send_sms", "ok": True, "latency_ms": 4}, {"name": "send_email", "ok": True, "latency_ms": 50, "error": None}]` | `[{"name": "render_sms", "ok": True, "latency_ms": 1}, {"name": "render_email", "ok": True, "latency_ms": 1}, {"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None}]` | No send happens in the node any more; it renders the per-channel payloads (`render_sms_payload`, `render_email_payload` in `services/notify.py`) and queues them. The entries name the three things the node did, in the order it did them (§5.3.7). |
| 3 | `by_node["BROADCAST"].output_summary` | `.startswith("notified ['RNIO', 'FIELD_ENGINEER']; email_mode=mock to=[]; RNIO=")` | `== "queued 3 outbox rows for ['RNIO', 'FIELD_ENGINEER']; email=PENDING"` | "notified" and `email_mode=mock to=[]` described a *send outcome*, which the node can no longer know at step time. The count is one SMS row per audience (2) plus one EMAIL row per incident (1) = 3, matching the §5.3.7 form. `email=PENDING` is the EMAIL row's status at enqueue on the auto path (`HELD` is the gated form). The `RNIO=` tail is dropped with the old form. |
| 4 | `by_node["BROADCAST"].rationale` | `"No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)"` | `"P4 auto-send under L2_GUARDED (approved_by=policy:L2_GUARDED); dispatcher transmits after commit"` | The old rationale *was* the SMTP adapter's result string, which now only exists at dispatch time. The node's own reason for queuing is the autonomy policy that let the priority through, recorded as `approved_by` on the outbox rows. The mock detail string is not lost: it survives verbatim on the `email.sent` payload (row 7 below). |

LEDGER `output_summary` (the `ledger_YYYY-MM-DD_SHIFT.xlsx` file name, `LEDGER_FILE_RE`) and
`rationale` (`"Shift failure ledger row appended for supervisor scan"`) are **unchanged** and pass:
§5.3.9 fixes only the tools literal; the `ShiftLedgerRow` is still appended inside the node, and
the sync drain has written the file by the time the `.exists()` assertion runs.

## R4 — `email.sent` position and WorkNote order

This breaks in the **default suite, flags off**. With `EMAIL_ENABLED=false` the adapter still
returns a mock `EmailResult`; the mock send, its `email.sent` event and its WorkNote now all
happen in the post-commit drain instead of inside the BROADCAST node.

| # | Assertion (`test_golden_full_lifecycle_auto_broadcast`) | Old literal | New literal | Why this is a true consequence |
|---|---|---|---|---|
| 5 | position of `email.sent` in `hub._history` | `broadcast_started < full.index(emails[0]) < broadcast_done` | `run_finished = full.index(next(e for e in events if e["type"] == "agent.run.finished"))` then `full.index(emails[0]) > run_finished` | The drain runs after `run_incident_lifecycle` returns, i.e. after `agent.run.finished` and `incident.created` were published. The event can no longer sit between the BROADCAST step's start and completion because nothing is transmitted between them. |
| 6 | `[(n.author, n.source) for n in notes]` | `[("BroadcastCommsAgent", "email"), ("WorklogMonitorAgent", "agent")]` | `[("WorklogMonitorAgent", "agent"), ("BroadcastCommsAgent", "email")]` | The MONITOR note is written during the run (before commit); the email note is written by the drain's outcome transaction (after commit). The query has no `ORDER BY`, so SQLite returns rowid order = insertion order. |

The `email.sent` **payload** literal is untouched (row 7).

---

## What did not move — verified one by one

| # | Item | How it was verified | Result |
|---|---|---|---|
| 7 | `email.sent` payload literal, including `"detail": "No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)"`, `run_id=null`, envelope keys | Assertion unchanged in the golden auto test; also pinned in `test_outbox.py::test_crash_between_commit_and_drain_sends_exactly_once` | passes |
| 8 | The 26 run-scoped event literals `GOLDEN_FULL_HITL` / `GOLDEN_FULL_AUTO`: types, order, per-run `seq` | Lists not edited (26 entries each, `email.sent` in neither); `[_shape(e) for e in events] == GOLDEN_FULL_*` unchanged and passing in both golden tests and in `test_runner_failures.py` | unchanged |
| 9 | 12 nodes and 11 edges | `tests/system/test_contracts.py::test_workflow_nodes_edges_contract` (file untouched) | passes |
| 10 | `len(ShiftLedgerRow) == 1` | Assertion unchanged in the golden HITL test; the DB row is still written inside the node (also asserted in `test_outbox.py::test_locked_workbook_is_the_dispatchers_problem_not_the_runs`) | passes |
| 11 | `tests/system/test_contracts.py` | Not edited (mtime unchanged); all 12 tests pass | untouched |
| 12 | HITL-run BROADCAST literals (`"broadcasts drafted; waiting HITL"`, its rationale, `draft_broadcast` tool) | Not edited; the gated path of the node is byte-identical | unchanged (see "Not re-baselined" 2) |
| 13 | Durability spy `durable_at_announce == [True]`, input summaries, audit rows, HITL task/broadcast side tables | All unchanged and passing | unchanged |
| 14 | `tests/integration/test_hitl_decisions.py` (13 tests), `tests/unit/test_ledger_root.py` (4) | Not edited; all pass. The approve path now queues approved rows and drains after the route's commit through a once-only `after_commit` listener, so `"PENDING_HITL" not in statuses` and the `hitl.approved` event order hold as before | pass |

## Proof that no email leaves inside the transaction

`tests/unit/test_outbox.py::test_no_email_leaves_inside_the_transaction` configures real SMTP
credentials in the environment, replaces `smtplib.SMTP` with a probe, and runs `process_event`.
At the instant `send_message` is called the probe records:

* `session.in_transaction()` on the writing Session → **False** (the claim was committed and the
  dispatcher runs with no transaction open);
* the number of `IncidentRow`s visible from a **second** Session → **1** (the incident was already
  durable when the mail left);
* the EMAIL outbox row's status from that second Session → **CLAIMED** (the claim itself was
  committed before transmission, which is what makes a crash mid-send recoverable by the lease).

It also asserts exactly one SMTP call, that the outbox row ends `SENT` with `provider="smtp"`,
and that `email.sent` is published after `agent.run.finished`. The other acceptance items of
§7.0.2 are covered in the same file: duplicate `enqueue` → one row; `drain_once` twice → one send;
crash between commit and drain → sent exactly once on the next drain (by a new session);
`REJECTED_UNAPPROVED` for an unapproved channel row and no transmission; a stale CLAIMED row is
reclaimed after the 120 s lease while a fresh claim is respected; transient errors back off with
jitter up to `max_attempts` and then `FAILED`, programming errors are `DEAD` at once, SMTP 5xx
quoted by the adapter is permanent and 4xx transient; `release_held`; the HITL release; the
handover email.

---

## Not re-baselined — needs the owner's decision

1. **`tests/integration/test_runner_failures.py::test_fail_soft_ledger_error_keeps_the_run_going` and
   `::test_ledger_agent_absorbs_locked_workbook` fail** (AttributeError at
   `monkeypatch.setattr("noc_agents.agents.ledger.write_excel_row", ...)`). Both monkeypatch the
   exact call §5.3.9 moves out of the node, so they cannot pass honestly once the node stops
   writing the file; they are not in §2.1, so they were left as they are.
   * The first tests the runner's fail-soft isolation and still can: retarget the monkeypatch to
     `noc_agents.agents.ledger.ledger_row_cells` (the node's first call, before the DB row is added).
     Every other assertion in it — LEDGER `FAILED`, tool name `append_excel_row` (it comes from the
     registry profile's `tools[0]`, which this change does not touch), no `ShiftLedgerRow`, the
     event shapes — holds unchanged. One-line change.
   * The second's premise ("the OSError is handled INSIDE the agent") is exactly what §5.3.9 removes.
     Its behaviour now lives in the dispatcher and is covered by
     `test_outbox.py::test_locked_workbook_is_the_dispatchers_problem_not_the_runs` (LEDGER step
     `SUCCEEDED`, DB row written, `EXCEL_ROW` row `PENDING` with `attempts=1` and the
     `PermissionError` as `last_error`, appended on the next drain). Recommend deleting it under a
     new §2.1 entry, or rewriting it to those assertions.
2. **HITL-run BROADCAST literals.** §5.3.7's output form `"queued N outbox rows for [...]; email=HELD|PENDING"`
   implies the gated path should also create `HELD` rows during the run and record the new tools.
   §2.1 R3 enumerates only the auto-run literals, so the gated path was left byte-identical: no
   outbox rows are created while a task is open; on approval `release_broadcasts_after_hitl`
   queues approved rows from the `PENDING_HITL` drafts. `release_held` is implemented and
   unit-tested for the HELD flow. If sanctioned, the literals that would change are
   `TOOLS_HITL_RUN["BROADCAST"]` (`draft_broadcast` → the three entries of row 2) and
   `by_node["BROADCAST"].output_summary` (`"broadcasts drafted; waiting HITL"` →
   `"queued 5 outbox rows for ['RNIO', 'FIELD_ENGINEER', 'MSP', 'MANAGEMENT']; email=HELD"`).
3. **`OUTBOX_SYNC_DRAIN` defaults to on.** §7.0.2 reads as if the default were off and the test
   conftest set it on; `tests/conftest.py` is outside this change, and an off default with no
   scheduler thread yet would silently stop the API sending anything. Flip the default when the
   §7.0.3 scheduler owns the drain.
4. **Nominal `latency_ms` constants** in the new tool entries follow the existing convention (every
   agent pins a constant so the golden literal is deterministic); measuring them is a separate
   decision.
5. **Handover idempotency.** Each `POST /api/v1/shifts/handover` still sends (per-request key), as
   before. A per-shift key would de-duplicate re-clicks; not chosen without a decision.
6. **`BroadcastRow.status == "QUEUED"`** is a new value in that column's vocabulary (between
   enqueue and dispatch). The timeline shows it verbatim.
