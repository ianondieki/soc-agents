# Conformance tracker — `SUPER_PROMPT_NOC_V2.md`

The single list of everything the build spec asks for that is **not** yet built and tested.
It exists so that "are we done?" has an answer other than somebody's impression.

**Done means:** every row below is `BUILT`, `BLOCKED-HUMAN` (with a named owner) or
`DEFERRED-BY-SPEC` (with the citation). Nothing may sit at `TODO` or `PARTIAL`.

| Status | Meaning |
|---|---|
| `BUILT` | Code exists **and** a test pins the behaviour. Reading that a function exists is not enough. |
| `IN-FLIGHT` | Being built now. |
| `TODO` | Engineering not started. Nothing but time stands in the way. |
| `BLOCKED-HUMAN` | Cannot be finished by code. Names who. If engineering is *also* outstanding, the row says so. |
| `DEFERRED-BY-SPEC` | The spec itself defers or excludes it. Cites where. |

**Baseline (2026-09-21, audit of ~320 requirements by three read-only auditors, one per spec
slice):** roughly 60 % `BUILT`. Phases 0–2, the weather lane of Phase 3, and Phases 4–5 are
substantially in place; the gaps cluster in (a) holes in shipped code the audit found, (b)
behaviour whose *names* exist but whose behaviour does not, (c) the non-weather half of Phase 3,
and (d) Phases 6–8. Suite at baseline: 1844 passed; `tests/system/test_contracts.py`
byte-identical since before Phase 0.

How the audit was run matters for how far to trust it: auditors were read-only and were told not
to run the suite (three builders were using the machine), so every `BUILT` below means "code
plus a named test that reads as pinning it", verified by reading. The full suite is the check
on that, and it runs at every integration.

---

## A. Holes in code that is already shipped — fix first

These are not missing features. They are reachable today.

| ID | Spec | Finding | Status |
|---|---|---|---|
| A-01 | §2 G4 | **The hot path imported a poller.** `agents/enrich.py` lazily imported `pollers.weather`, which loads the weather adapter and with it the HTTP client, from inside `run_incident_lifecycle`. No socket was ever opened, so the runtime half of G4 stayed green; the grep half the spec also mandates **did not exist**, and found this on its first run. Fixed by splitting the cache reads into a network-free `services/signals.py`. | `BUILT` — `f09a538`, `tests/unit/test_hot_path_imports.py` |
| A-02 | §9.3 | **Alarm ingest is gated; two other routes that run the same 12-node lifecycle are not.** `POST /api/v1/events/batch` and `POST /api/v1/demo/rain-storm` carry no `require_role`. With auth fully enforced, an anonymous caller can still create incidents, runs, audit rows and outbox rows. Same class of hole Phase 2 closed on `POST /events`. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| A-03 | §9.3 | `POST /api/v1/incidents/{id}/notes` is ungated. §9.3 makes notes the *one* thing an MSP coordinator or field engineer may write — which means it must check they are one. A note can also mark an incident restored (`note_declares_restored`). | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| A-04 | §9.3 | List reads are gated (`GET /incidents`, `/runs`), their detail forms are not (`/incidents/{id}`, `/runs/{id}`, `/timeline`, `/workflow`, `/briefs/{id}`, `/problems`, `/sites`). **Needs care, not a blanket fix:** `test_deliberately_open_routes_stay_open_when_auth_is_enforced` pins some routes open on purpose (`/health`, `/profile`, the SPA). Each route needs a decision recorded next to it. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| A-05 | §7.0.5, §7.8.6 | The complaints router refuses to register any route when `AUTH_DISABLED=true` and `NOC_ENV=production`. The **contracts** router has no such guard, and §7.8.6 names contracts explicitly: confidential MSP contract text would be served unauthenticated in exactly the misconfiguration the guard exists for. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| A-06 | App. C | `APPROVE_HANDOVER` is in use as a **bare string** (`services/handover.py:62`), not a `HitlTaskType` member. The §5.2 import-time assert only validates gate names that are enum members, so a typo in that literal is a runtime bug, not a startup failure. `CONFIRM_POWER_NOTICE` and `APPROVE_PERFORMANCE_ACTION` are also absent from the enum. | `BUILT` — `5031665`; the `handover.py` literal became `HitlTaskType.APPROVE_HANDOVER.value` in `b9d11a7` |
| A-07 | §10.3 | `EMAIL_DAILY_CAP` is marked ✱ "enforced in code" by the spec, documented in `.env.example`, and read by nothing. A free Gmail account is suspended for 24 h at 500 messages. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running (with C-06) |
| A-08 | §9.4 | Vendor contact pseudonymisation has never run: the `vendors` retention entry has no `timestamp_column` and the table has no creation timestamp. Recorded with reasoning in `config/retention.yaml`; needs `active_to`-based handling with an explicit NULL guard. | `TODO` |
| A-09 | §2 G8, §9.2 | **The two LLM assist routes bypass the transfer-register gate.** `POST /incidents/{id}/analysis` and `/brief/draft` go through `llm/assist.run_assist`, which writes an `llm.call` audit row *after* the model call but never calls `services/external_calls.record_transfer` and never consults the DPIA/TIA refs that gate every newer hosted call outside demo. The assist layer predates the gate and was never retrofitted. Verified in code: `record_transfer` has exactly four callers (complaints, outbox ×2, contracts). | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| A-10 | §9.6 | **`outbox_dispatch` and `monitor_tick` ignore their own flag when run by hand.** Every other job re-checks its flag; these two do real work under `POST /scheduler/run/{job}` with every flag off. During a freeze, a manual `outbox_dispatch` drains the queue — with `EMAIL_ENABLED=false` it marks queued mail `SENT` with provider `mock`, so it is never delivered. Found by the runbook lane running all ten jobs against a scratch database. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| A-11 | §9.6 | The spec's three-flag kill sequence does not stop mail: `OUTBOX_SYNC_DRAIN` is on by default (`graph/pipeline.py`) and is not in the spec's list; `MCP_RUNTIME_ENABLED` is read by nothing. RUNBOOK §16 documents the four lines that actually work. The `POST /admin/freeze` route (C-11) should set exactly those. | `TODO` (with C-11) |
| A-12 | §7.0.5, §9.2 | An unset `NOC_ENV` means **production** to the transfer gate (`services/external_calls.py`) but **demo** to auth (`api/auth.py`). One variable, two opposite defaults. Each is on the strict side *for its own concern*, and either alignment changes behaviour: auth reading unset as production would unregister the contracts and complaints routes in every demo that leaves `NOC_ENV` unset; the transfer gate reading unset as demo would let a hosted call through with its paperwork recorded as unfiled. Not an engineering call. | `BLOCKED-HUMAN` (product owner: pick the default, or require `NOC_ENV` to be set explicitly) |
| A-13 | §4.6 | The Agent Observatory page draws a FAILED run with the green chip and does not show `error_summary`. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running. Not pinned by a test: see C-29 |
| A-14 | §9.3 | **`/ws/ops` has no authentication.** The ops event feed accepts every WebSocket and replays the last 15 envelopes (incident numbers, sites, run and HITL events) whatever `AUTH_DISABLED` says. Gating every HTTP read while the live feed of the same facts answers anyone is not a control. The frozen contract test pins the feed's shape with auth disabled, so a handshake check that applies only when auth is on leaves it byte-identical. | `TODO` — high |
| A-15 | §9.2, §10.3 | **The assist routes' spend is never counted.** `llm/assist.py` now consults `spend_gate` (A-09), but the gate sums `llm_calls`, and only `llm/port.py` writes that table; the assist path writes an `llm.call` audit row and no `llm_calls` row. So `LLM_MONTHLY_BUDGET_USD` cannot see the two routes most likely to be pressed repeatedly. | `TODO` |
| A-16 | §7.6, §9.6 | **A regulatory notice can be stuck `QUEUED` for ever.** When a drainer dies during a notice's last attempt, `outbox._reclaim_stale` marks the row `FAILED` without `_finalize`, so the notice keeps `QUEUED` with no dispatch record; `release_notice` refuses a `QUEUED` notice and the outbox retry (C-10) correctly refuses regulatory rows. It cannot be automated safely: a lease-expired row may already have transmitted, so any automatic resend risks a second statutory notice. | `TODO` (surface it as "outcome unknown") + **`BLOCKED-HUMAN`** (product owner with Legal: the reconciliation step with the CA before a re-release) |
| A-17 | §9.3 | `/openapi.json`, `/docs` and `/redoc` answer anonymous callers with auth on and publish every lane's route map. Not a §9.3 cell (the review refuted it as a matrix disagreement), so it is recorded as an exposure decision. | `BLOCKED-HUMAN` (product owner: serve the API docs in production or not) |
| A-18 | §7.3 | `adapters/weather.py` (the forecast poller) still has the per-read-only timeout the KMD/flood adapters had before W02: a server that drips bytes is not bounded by a total deadline. | `TODO` |
| A-19 | §9.1 | The `outbox.failed` WS event carries `last_error` unscrubbed; an SMTP refusal quotes the mailbox, and `/ws/ops` is unauthenticated (A-14). The C-10 list route scrubs the same field. | `TODO` |
| A-20 | §9.3 | Round-3 replay: every route was compared with §9.3 and five router families disagreed (vendor roles reading incidents beyond "notes only"; legal on lane-router incident reads; the contracts row; planning / noc_analyst / management on the HITL and handover rows; complaints assign). Being fixed as a class, with `tests/system/test_rbac_matrix.py` making §9.3 executable over every registered route. | `IN-FLIGHT` — built in fix round 4: the matrix maps all 131 routes (118 to 38 permission families, 13 exempt with written reasons), is checked statically and live with auth on, per HITL card type, and fails on any unclassified route; 8 of 8 deliberate breakages caught. Replay and full suite pending. |

## B. Names exist, behaviour does not

The dangerous category: a reviewer grepping for the enum member or the column concludes the
feature is there.

| ID | Spec | Finding | Status |
|---|---|---|---|
| B-01 | §6.5, Phase 2 exit | **HITL escalation ladder.** T+5 unclaimed → nudge; T+15 → duty manager + `hitl.escalated`; T+30 → red on the Wallboard. Nothing exists: no YAML, no `HITL_NUDGE` outbox kind, no template, no event, no `test_hitl_escalation.py`. Every other gate in the system is a refusal; this is the one thing that makes refusing safe, because it is what happens when nobody is looking at the card. | `TODO` — after the `hitl_tasks` rebuild lands |
| B-02 | §3.7 #13, §5.3.4 | Cascade re-evaluation silently changes priority. `services/priority.reevaluate` → `PriorityProposal` → `APPROVE_PRIORITY` does not exist; the enum member does. | `TODO` |
| B-03 | §3.7 #21, §5.3.6 | Unknown lane is assigned silently. `incidents.assignment_confidence` exists and nothing writes it; `APPROVE_ASSIGNMENT` exists and nothing raises it. | `TODO` |
| B-04 | §3.7 #14, §5.3.2 | `incidents.child_site_ids_json` is **read** (`services/alerts.py`) and never **written** (`agents/correlate.py`). So `NocAlert.area.sites_affected` on a cascade parent reports the parent alone, and a storm envelope understates its blast radius — on exactly the P1/P2 messages that reach a human gate. | `TODO` — golden path, needs care |
| B-05 | §3.7 #42, §5.3.5 | `next_update_at` is still `utcnow() + 15 min` at ticket creation (`agents/ticket.py:83`). The monitor re-arms it correctly from `note_interval × region multiplier`; creation does not. The 4 priorities × 6 regions unit test the spec names does not exist. | `TODO` — golden path, needs care |
| B-06 | §5.3.10 | Known-error text surfaces only through the PIR router. The spec's "narrative gains a *Known error PRB…* line" is not wired into `compose_narrative`. | `TODO` — golden path; must be byte-identical with `PIR_ENABLED` off |
| B-07 | §5.3.8, App. A | `incident_briefs.ai_assisted` does not exist, so `APPROVE_EXEC_BRIEF` (P1 brief publication when AI-assisted) has nothing to key on. | `TODO` |
| B-08 | §5.3.12, App. A | `handovers` table is not persisted; the handover task is anchored to a watchlist incident via `entity_type="handover"`. `run_kind="handover"` on the profile is documentation only. | `TODO` — after the `hitl_tasks` rebuild (it removes the need for the anchor) |
| B-09 | App. A, §1.2 M2 | None of the eight additive `broadcasts` columns exist (`envelope_json`, `template_key`, `template_version`, `idempotency_key`, `outbox_id`, `edited_before_send`, `suppress_reason`, `provider_message_id`). They live on `outbox` instead. Metric **M2** ("% incidents needing no retyping") names `broadcasts.edited_before_send` and therefore cannot be computed. Either add them or record the deviation and point M2 at `hitl_tasks.edited`. | `TODO` — decide and record |
| B-10 | §8.9 r2 | **`BROADCAST_SHADOW`, `SMS_SHADOW`, `SCORECARDS_SHADOW` are read by nothing.** Every shadow shift the spec requires runs under one of these flags, so today a shadow shift *cannot be run* — a human-owned exit criterion is blocked on code. | `TODO` — unblocks four human sign-offs |
| B-11 | §6.6 | `delivery_receipts` exists and is unit-tested with hand-inserted rows; nothing writes it. "SENT" is the last thing the system can ever say about a message. No `outbox.<status>` audit rows; no `broadcast.queued/sent/failed/delivered` events. | `TODO` |
| B-12 | §7.7.3 | The PIR LLM draft has a transmitter and no sink: an audited, budgeted, recorded model call whose output nobody receives. One function in `services/pir.py` under the blameless validator and a DRAFT/IN_REVIEW guard. | `TODO` |
| B-13 | §7.3–§7.11 | `OperatorConfig` has no `weather`, `maintenance`, `capacity`, `memory`, `regulatory` or `scorecards` field, so pydantic's `extra="ignore"` silently drops those YAML blocks and six lanes run on code defaults. Every knob the spec describes as "YAML, never code" is currently code-only. | `TODO` |
| B-14 | §5.3.5 | `incidents.vendor_id` is not stamped at ASSIGN; only on the first stop clock or `POST /vendors/backfill`. | `TODO` — the scorecard job backfills before computing, so this is tidiness, not correctness |
| B-15 | §9.1, §7.11 | `validate_no_contacts` — named by the spec and in several code comments as the pre-send check for contact identifiers — **does not exist**. The only pre-send scan is the `LLM_CALL` backstop in the outbox; everything else relies on the daily redaction scan, which runs only when `HOUSEKEEPING_ENABLED=true` (off by default). | `TODO` |
| B-16 | §2 G12 | Raiser ≠ approver never fires on routine broadcasts: pipeline cards are raised by `agent:SupervisorAgent`, handover cards by `agent:ShiftHandoverAgent`, monitor cards record no raiser. So the 403 protects maintenance and regulatory cards only. Arguably correct — no human raised the broadcast — but "a supervisor may not approve a message they edited" is not enforced, because the HITL inbox cannot edit wording at all (the approve API accepts only a priority override; the message is rebuilt from the incident). Record the design, or build edit-then-other-approves. | `TODO` — decide |
| B-17 | §9.3, §7.0.5 | **There is no login route** (`api/auth.py`: "a future login route"). With `AUTH_DISABLED=false` every gated route answers 401, the HITL inbox included, so the system cannot currently be run with authentication on at all. | `BLOCKED-HUMAN` (product owner, **D18**: which auth provider) + `TODO` for the route once decided |
| B-18 | §7.9.2 | SMS rows are recorded `SENT` with provider `mock`: no engineer has ever received an SMS from this system. Correct while C-17 is unbuilt, but the wallboard and the audit trail say `SENT`. | `TODO` (with C-17) |

## C. Spec'd, not started

| ID | Spec | Item | Status |
|---|---|---|---|
| C-01 | §7.6, Lane 4A | Vendor scorecard computation: tables, KPI lines, bands, data-quality gate (`WITHHELD`), `SHADOW` rule, discipline counters, read routes, golden-numbers test | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-02 | §7.6 | `DISPUTE_SCORECARD_LINE` handling (409 on a closed window), `APPROVE_VENDOR_NOTICE` draft, QBR `.xlsx` + vendor pack, `test_scorecard_disputes.py` | `TODO` — after C-01 and the rebuild |
| C-03 | §7.11 | Memory **M1**: `memory_episodes`, `memory_note_fts`, `consolidate_incident`, the consolidate job, backfill script, FTS recall tier, `advisory` bundle, `test_memory_advisory_is_inert.py` | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-04 | §7.11 | Memory `GET /memory/stats` (M16's growth tripwire), `test_memory_scoring.py`, MEM9 untrusted-wrapper, MEM11 < 25 ms at 5,000 episodes, `memory.*` audit actions, retention rows | `TODO` — after C-03. Measured: the advisory costs ~32 VM steps per extra incident at one site (pinned); a site with 5,000 incidents of one fault class answers in ~94 ms median on a loaded machine against MEM11's 25 ms — removing the sort needs an index on `incidents(site_id, …)`, i.e. a migration |
| C-05 | §7.11 | Memory **M3** (bi-temporal `memory_facts`, shift memos, `/memory/memos`, `expire_memory` hard delete) and **M4a** (MSP priors) | `TODO` — after C-03; M4a after C-01 |
| C-06 | §7.9.1 | `send_email(*, to, subject, body, html, headers)` with `to` required; ≤ 100 recipients per message, batched by the dispatcher; `EMAIL_DAILY_CAP` with a fail-soft note at 80 %; `test_email_batching.py` | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-07 | §6.1 | Envelope-level `validate_content(alert)` and the **"no invented cause"** rule (`caused by` / `due to` must name `facts.failure_domain`); violation → deterministic template, `ai_assisted=false`, `fallback_reason`. Must exist *before* anything passes `ai_content`, and that seam is already open. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-08 | §6.2 | `render_statuspage(alert)` | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-09 | §6.3 | `PUT /api/v1/templates/{id}/status` — `TemplateRegistry.set_status` exists and is tested; there is no route | `IN-FLIGHT` — built `59786b7`, reviewed, fixed `082db90`; full suite and tag pending. Compare-and-set on the transition. |
| C-10 | §10.4 | Dead-letter view `GET /api/v1/outbox?status=` and `POST /api/v1/outbox/{id}/retry` | `IN-FLIGHT` — built `59786b7`, reviewed, fixed `082db90`; full suite and tag pending. The retry refuses rows whose producer owns recovery (regulatory, handover, superseded invites and reminders); the review found it would otherwise send the CA a second notice. |
| C-11 | §9.6 | Breach drill: `POST /api/v1/admin/freeze` (setting the four flags of A-11). **Correction:** `ODPC_BREACH_72H` already exists in `db/models_regulatory.py` — but its clock starts at the incident's `failure_time`, whereas DPA s.43's 72 hours run from *becoming aware*, and it answers 503 unless `REGULATORY_ENABLED=true`. The Wallboard chip landed in `93fc4cf`; the RUNBOOK page in `1857c63`. | `TODO` — the freeze route and the awareness-based clock |
| C-12 | §7.3 | `pollers/kmd_cap.py` (KMD CAP RSS) + `test_kmd_cap.py` | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-13 | §7.3 | `pollers/flood.py` (GloFAS discharge tier) + `test_flood.py` | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-14 | §7.3, §10.6 | `scripts/backtest_signals.py` + `test_backtest_signals.py` → `signal_precision_30d` and the "LOW CONFIDENCE" label. The dashboard hard-codes `precision_30d: None` until this exists. | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running |
| C-15 | §7.3 | `GET /api/v1/signals?source=&region_code=&active=`; county→region mapping that rejects an unknown county at startup | `IN-FLIGHT` — committed `b9d11a7`; consolidated re-verification of every review finding running. Product default set by engineering, for the owner to confirm: a CAP alert in force at severity Severe or Extreme lifts a region to at least `WATCH`, never `ALERT` |
| C-16 | §7.3 | KPLC planned-power lane: `pollers/kplc.py`, PDF parser, locality gazetteer, `match_sites`, `planned_power_interruptions` + `_links`, `/power-notices` routes, `CONFIRM_POWER_NOTICE`, `power_notice.new` | `TODO` for everything that does not need a real PDF; **`BLOCKED-HUMAN` (NOC floor)** for the "golden PDF pinned" exit criterion — it needs an actual KPLC interruption notice, and `pdfplumber` is an optional extra that is not installed |
| C-17 | §7.9.2 | `adapters/sms_africastalking.py` in sandbox + `test_sms_adapter.py` (recorded fixtures) | `TODO` for the adapter; **`BLOCKED-HUMAN` (product owner)** for the account and the KES 8,700 sender ID |
| C-18 | §7.10 | Frontend: `PirsPage` + `PirEditor` + `BlamelessHint`; Incident Workspace stop-clock control and regulatory countdown. | `BUILT` — `93fc4cf` (tsc + vite clean). **Scorecards page** built `59786b7`, reviewed, fixed `082db90` (tsc + vite clean); disputes disabled until C-02. |
| C-19 | App. C | WS renderers for `pir.opened`, `regulatory.deadline`, `scheduler.job_failed`, `security.redaction_miss`, `complaint.surge` (the last has no publisher yet — Phase 6). | `BUILT` — `93fc4cf` |
| C-20 | §10.7 | RUNBOOK task pages; `docs/SIGNOFF.md`; `docs/COMPLIANCE.md` | `BUILT` — `1857c63`. Sign-offs and compliance entries are the empty forms, correctly: filling them is §D below. |
| C-28 | App. C, §4.6 | Frontend gaps the PIR/countdown lane recorded: no route lists regulatory notices across incidents (so a `SEND_FAILED` notice cannot reach the Wallboard); no route lists SCC codes (copied into the UI); `/scheduler/status` does not report the configured tick; no read/acknowledge route for redaction misses (the chip cannot be cleared); `incident_number` missing from `/pir` responses. | `TODO` |
| C-29 | §10.5 step 8 | **The frontend has no test runner.** `frontend/package.json` has no test script and no vitest/jest, so a UI regression such as A-13 coming back is caught by nothing (`tsc` and `vite build` both pass on the old green-chip code). Adding one is a new dev dependency and an `npm install` with network access. | `TODO` — pick the runner (vitest fits Vite), then pin A-13, the `/incidents/null` link on incident-less cards, and the stop-clock panel first |
| C-30 | §10.5 step 2 | `pytest -q -m flagged` selects nothing: the marker is registered, no test carries it. The flag-ON fixtures exist as ordinary tests. | `TODO` — mark them |
| C-31 | §10.5 step 7 | `tests/unit/test_m1_no_unapproved_send.py` does not exist; the property is covered across the HITL tests but not in the file the pre-merge checklist names. | `TODO` |
| C-32 | §4.6, §9.3 | Frontend identity: no "who am I" route (`/api/v1/session` returns the demo switcher, not the signed-in principal), and the Settings role switcher offers `rnio`, `msp_viewer`, `automation_admin` (unknown to the backend) while omitting `management`, `admin`, `msp_coordinator`. | `TODO` |
| C-33 | App. C | No `scorecard.published` WS event, so the scorecards page refetches instead of updating live. | `TODO` (with C-02) |
| C-21 | §10.1, §10.2, §10.5 | `tests/eval/` nightly layer + `tests/fixtures/eval/alarm_sequences.yaml`; `scripts/golden_diff.py` + `tests/fixtures/golden/`; `tests/system/test_auth.py` (the full §9.3 403 matrix) | `IN-FLIGHT` — built `59786b7`, reviewed, fixed `082db90`; full suite and tag pending. The nightly LLM eval is the CLI `python tests/eval/draft_eval.py` (pytest forces the LLM off); `golden_diff.py` catches every regression the golden test does, plus two it misses; `tests/unit/test_golden_fixtures_match.py` pins the fixtures to the test's literals; `test_auth.py` landed in `b9d11a7`. |
| C-22 | §10.4 | OpenTelemetry GenAI span attribute names (`gen_ai.request.model`, …); `OTEL_EXPORTER_OTLP_ENDPOINT` is documented and read by nothing | `TODO` |
| C-23 | §7.9.5 | Webhook hardening: `webhook_nonces` replay window, per-IP token bucket → 429, 256 KB body cap, `webhook.rejected` audit rows, `test_webhooks.py` | `TODO` — ships with whichever webhook lands first (C-17 delivery reports) |
| C-24 | App. B | `RETENTION_POLICY_PATH` and `NOC_SEED_V2_DIR` are read by code and absent from `.env.example` | `BUILT` — `b9d11a7` (documentation row; the same edit corrected `.env.example`'s `OUTBOX_SYNC_DRAIN`, which claimed a default of false — the code's default is on) |
| C-25 | §7.9.4, Phase 6 | WhatsApp Cloud adapter in draft-only mode, `opt_in_register`, template mirror | `TODO` for the engineering (ships OFF); **`BLOCKED-HUMAN` (product owner)** to ever turn on — Meta Business verification + 5 approved Utility templates + rate card |
| C-26 | §7.4, Phase 6 | Social signals sidecar: `social_signals`, `complaint_buckets`, surge z-score, purge | `TODO` for the engineering (ships OFF); **`BLOCKED-HUMAN` (Legal/DPO)** DPIA + transparency notice, **(Operator IT)** the contact-centre export |
| C-27 | §7.1, Phase 7 | MCP runtime: `tools/registry.py` (`ToolSpec`, `tools_for_model` — the read-only filter G5 names), `tools/mcp_client.py`, `GET /mcp/status`, the sanitizer / call-guard / SSRF tests | `TODO` — "optional extra" in the spec; the pydantic gate already passed, so nothing blocks it |

## D. Waiting on a person — grouped so each owner can see their own queue

### Product owner
- **A-12**: what an unset `NOC_ENV` means (see the row).
- **Spec conflict, resolved to the stricter reading:** §7.11.4 says memory's site history is readable by "any signed-in role"; §9.3's memory row excludes msp_coordinator and field_engineer. Engineering followed §9.3 (the route serves the same earlier-ticket text as the advisory). Confirm or reverse.
- **Vendor roles are "notes only" (§9.3 row 1)** once auth is on: how does a vendor find the ticket to note on? Own-vendor-scoped reads need a vendor binding on the principal (D18).
- **A P1 is never held by `EMAIL_DAILY_CAP`**, even when it alone exceeds the cap (§7.9.1). With a free Gmail sender a P1 to more than ~40,000 recipients could still trigger the 500/day suspension. Decide whether a ceiling is wanted.
- **KMD CAP severity mapping default:** a Severe or Extreme alert in force lifts a region to at least `WATCH`, never `ALERT` (engineering default).
- **A-16** (stuck regulatory notice) and **A-17** (API docs in production).
- **§9.3 cells engineering had to read (A-20), stricter reading taken each time — confirm or reverse:**
  - Vendor roles are "notes only": with auth on they can post notes but cannot list or open incidents, the workspace, the dashboard or signals (see the vendor-ticket question above).
  - PIR: a bare "publish" is publish only, so supervisors publish but do not edit.
  - The JSON ledger list follows row 4 (ledger download), not row 1: analysts lose it, management gains it.
  - HITL card types §9.3 does not name (exec brief, generic escalation, ticket sync, page, ledger sync) are row 2's supervisors. Analysts can no longer claim (a claim would silence the §6.5 escalation ladder) and so see no HITL queue; should they get a read-only view?
  - Complaints acknowledge/resolve are not named in §9.3; kept for duty manager, management, shift supervisor, admin.
  - Contracts FAQ and query log: §7.8.2 says legal only, §9.3 gives admin "all"; §9.3 followed. Reconcile the spec.
  - `/dashboard/regions` no longer admits vendor roles; a counts-only vendor view would be a product decision.
  - Routes §9.3 has no row for still admit vendor roles (maintenance, capacity, vendor reads); `GET /vendors` may show an MSP coordinator other vendors' contacts.
  - The contract samples' `allowed_roles` (seed data) no longer match the gate: analysts and planning pass the gate but see 0 contracts.
- **25 of 26 §12 decisions are open.** `docs/DECISIONS.md` records D4 only. The ones gating work
  in this tracker: **D3** (flip `ALERT_ENVELOPE_V2`, adopt template `@2`), **D9** (scorecard
  period, dispute window, bands, who adjudicates), **D12** (build individual metrics at all),
  **D15** (significance rule for the CA 24-hour clock), **D17/D18** (LLM and auth provider for a
  real deployment). **D21** and **D23** are ratifications: the §2.1 re-baselines and the memory
  lane were built ahead of the decision and need an explicit yes.
- Approve `docs/TEMPLATE_V2_REVIEW.md` `@2` — gates every email under the envelope.
- Three re-baselines of the frozen `tests/system/test_contracts.py`, each deliberately not taken
  by engineering: `HousekeepingAgent`'s `AgentProfile` (12 → 13 agents, and with it profiles for
  the other nine new agents), and the two `/metrics/summary` additions (`freshness`, "PIRs
  awaiting review" — and the spec's other additive keys: `hitl_queue` by type, `llm` spend,
  `outbox`, `scheduler`).
- Procurement: Africa's Talking account + sender ID; Meta Business verification; Open-Meteo
  commercial quote; Console API credit with a self-imposed spend limit.

### Legal / DPO
- **`docs/COMPLIANCE.md`** exists as the empty form (`1857c63`); every entry is `NOT FILED`. Five
  spec sections name it as the required home for the transparency notice, every DPIA reference
  and the dated ZDR confirmation.
- DPIAs (s.31(5): filed **60 days before** processing) for individual metrics, social signals,
  `PARTY_TOKEN` memory priors, and a hosted LLM on personal data.
- A Transfer Impact Assessment + ODPC standard contractual clauses per cross-border recipient.
  `config/operators/*/transfers.yaml` is waiting for the `tia_ref` / `dpia_ref` values; the demo
  honestly records `DEMO-UNFILED`.
- CA licence class and Condition 9.1/9.2 wording (code and error messages say UNVERIFIED); the
  `CBK_FACTSHEET` statutory deadline (`open_notification` raises rather than invent one).
- Confirm §9.4 retention, including two recorded gaps: `work_notes.author`/`.body` are
  unclassified, and A-08 above.

### NOC floor / floor lead
- **`docs/SIGNOFF.md`** exists as the empty form (`1857c63`). Four shadow shifts are required and none has happened:
  Phase 2 (wording, before `ALERT_ENVELOPE_V2`), Phase 3 (before SMS), Phase 4 (the first
  scorecard period, before anything is PUBLISHED), Phase 6. *Blocked on engineering too — see
  B-10: the shadow flags are not wired.*
- The quarterly §9.6 breach drill (with the DPO).
- Real site coordinates: county, lat/lon, parent hub, riverine, KPLC hints.
- A `resolution_code` census on the live database (about 10 minutes). **Gates memory M2.**
- A real KPLC interruption notice PDF to pin the parser against (C-16).

### Operator IT
- SMTP relay on the operator's domain + SPF/DKIM/DMARC.
- The CA notification mailbox for `notification_recipients['regulatory.recipients.CA']`, and the
  `maintenance.recipients.FE_ONCALL` / `complaints.recipients.MANAGEMENT` lists. All ship as
  empty lists on purpose; every dispatch that needs one is refused until they are filled in.
- The contact-centre complaint export (C-26).

### Supply Chain
- The commercial blocks of `config/sla_terms.yaml` (`availability_target_pct`, `credit_shape`,
  `credit_pct`, `contract_ref`, `active_from`), currently marked `contract_is_synthetic: true`.
  A scorecard built on them must read "defaults, not contract" and credits stay `PROPOSED`.

## E. Deferred or excluded by the spec itself

| Spec | Item | Why |
|---|---|---|
| §12 D4 | Kiswahili rendering | Owner decided English only, 2026-09-17 (`docs/DECISIONS.md`). The `Literal["en","sw"]` seam is kept. |
| §7.6, D12 | `individual_metrics`, `performance_actions`, fairness check, reconsideration path | Phase 6, behind an open owner decision (D12: *build at all?*) **and** a DPIA. Not built ahead of that decision on purpose: this is the one lane where software touches a person's employment, and DPA s.35 / Employment Act s.41 make the design a legal question first. |
| §7.11.8, D24 | Per-person memory priors, `/memory/parties/*` | D24's default is "never — MSP-level priors only for the pilot". The route is correctly absent, which is the spec's own exit criterion. |
| §7.11, D25 | `memory_embeddings`, local embedding tier | Only if `/memory/stats` shows FTS5 missing cases a human found. No budget assigned. |
| §7.11 M2 | Memory playbooks | Gated by the spec on the `resolution_code` census (NOC floor, above). |
| Phase 8 | A2A boundary, `a2a_tasks` | Conditional on a confirmed external counterparty and `AUTH_DISABLED=false` in production. Neither exists. |
| §5.3.19 | PDF contract ingest | Refused with 415; text extraction needs a new dependency. Recorded in `docs/PHASE5.md`. |
| §11 | Cost model figures | §11.3: "Nothing here is measured." The measurement columns (`llm_calls.input_tokens` …) exist. |
| §8 | Tags `v2-phase-1`, `-2`, `-3` | Not recoverable: version control began at the Phase 3 stop line (`c7725c9`), so no commit corresponds to those phase boundaries. `v2-phase-0` and `v2-phase-3-stopline` both mark that first commit. |

## F. Deviations recorded elsewhere (not defects — listed so they are findable)

- Stop-clock union arithmetic, the two-key housekeeping delete, the regulatory `QUEUED` /
  `SEND_FAILED` statuses beyond §7.6.1's four — `docs/PHASE4.md`.
- The contract corpus is 2.3 % of the prompt-stuffing line, so retrieval is built but
  `answer_question` stuffs below the ceiling; `icalendar` unused in favour of stdlib RFC 5545; the
  PIR action-item `PATCH` route §7.7.2 omits — `docs/PHASE5.md`.
- `orchestrator/outbox.py` is both the queue the hot path enqueues into and the dispatcher, so it
  loads `smtplib` by design; G4's grep layer is scoped accordingly —
  `tests/unit/test_hot_path_imports.py`.
- `tzdata` as a hard dependency; MCP gate passed on pydantic 2.13.5 — `docs/RUNBOOK.md`.

---

## Log

| Date | Commit | What moved |
|---|---|---|
| 2026-09-21 | `d83d95b` | `DISPUTE_SCORECARD_LINE`, `APPROVE_VENDOR_NOTICE` card types |
| 2026-09-21 | `f09a538` | A-01 `BUILT`. Tracker created from the three-slice audit. |
| 2026-09-21 | `5031665` | A-06 `BUILT` (Appendix C's 16 HITL members; router stubs). |
| 2026-09-21 | `93fc4cf` | C-18 (PIR, stop clock, countdown), C-19 `BUILT`. |
| 2026-09-21 | `1857c63` | C-20 `BUILT`. Verifying the runbook against running code added A-09..A-13, B-15..B-18, C-28 and corrected C-11. |
| 2026-09-21 | `b9d11a7` | Seven lanes integrated and committed (A-02..A-05, A-07, A-09, A-10, A-13, C-01, C-03, C-06..C-08, C-12..C-15), each after adversarial review and an author fix round; not tagged until the consolidated re-verification returns. A-06 and C-24 `BUILT`. A-12 recorded as an owner decision. Added A-14 (`/ws/ops` unauthenticated), A-15 (assist spend uncounted), C-29 (no frontend test runner). Suite: 2585 passed, 1 skipped, 3 xfailed. |
| 2026-09-22 | `59786b7` | Wave 2 checkpoint: C-09, C-10, C-21, the scorecards page. |
| 2026-09-22 | `082db90` | Fix round 3 plus the wave-2 review fixes. A consolidated re-verification replayed all 69 earlier findings (most FIXED; two fixes had caused regressions, both since fixed), and a replay of round 3 found the round-4 items now in flight. Added A-16..A-20, C-30..C-33 and the owner decisions above. |
