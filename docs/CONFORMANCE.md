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
| A-02 | §9.3 | **Alarm ingest is gated; two other routes that run the same 12-node lifecycle are not.** `POST /api/v1/events/batch` and `POST /api/v1/demo/rain-storm` carry no `require_role`. With auth fully enforced, an anonymous caller can still create incidents, runs, audit rows and outbox rows. Same class of hole Phase 2 closed on `POST /events`. | `TODO` |
| A-03 | §9.3 | `POST /api/v1/incidents/{id}/notes` is ungated. §9.3 makes notes the *one* thing an MSP coordinator or field engineer may write — which means it must check they are one. A note can also mark an incident restored (`note_declares_restored`). | `TODO` |
| A-04 | §9.3 | List reads are gated (`GET /incidents`, `/runs`), their detail forms are not (`/incidents/{id}`, `/runs/{id}`, `/timeline`, `/workflow`, `/briefs/{id}`, `/problems`, `/sites`). **Needs care, not a blanket fix:** `test_deliberately_open_routes_stay_open_when_auth_is_enforced` pins some routes open on purpose (`/health`, `/profile`, the SPA). Each route needs a decision recorded next to it. | `TODO` |
| A-05 | §7.0.5, §7.8.6 | The complaints router refuses to register any route when `AUTH_DISABLED=true` and `NOC_ENV=production`. The **contracts** router has no such guard, and §7.8.6 names contracts explicitly: confidential MSP contract text would be served unauthenticated in exactly the misconfiguration the guard exists for. | `TODO` |
| A-06 | App. C | `APPROVE_HANDOVER` is in use as a **bare string** (`services/handover.py:62`), not a `HitlTaskType` member. The §5.2 import-time assert only validates gate names that are enum members, so a typo in that literal is a runtime bug, not a startup failure. `CONFIRM_POWER_NOTICE` and `APPROVE_PERFORMANCE_ACTION` are also absent from the enum. | `TODO` |
| A-07 | §10.3 | `EMAIL_DAILY_CAP` is marked ✱ "enforced in code" by the spec, documented in `.env.example`, and read by nothing. A free Gmail account is suspended for 24 h at 500 messages. | `TODO` (with C-06) |
| A-08 | §9.4 | Vendor contact pseudonymisation has never run: the `vendors` retention entry has no `timestamp_column` and the table has no creation timestamp. Recorded with reasoning in `config/retention.yaml`; needs `active_to`-based handling with an explicit NULL guard. | `TODO` |

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

## C. Spec'd, not started

| ID | Spec | Item | Status |
|---|---|---|---|
| C-01 | §7.6, Lane 4A | Vendor scorecard computation: tables, KPI lines, bands, data-quality gate (`WITHHELD`), `SHADOW` rule, discipline counters, read routes, golden-numbers test | `IN-FLIGHT` |
| C-02 | §7.6 | `DISPUTE_SCORECARD_LINE` handling (409 on a closed window), `APPROVE_VENDOR_NOTICE` draft, QBR `.xlsx` + vendor pack, `test_scorecard_disputes.py` | `TODO` — after C-01 and the rebuild |
| C-03 | §7.11 | Memory **M1**: `memory_episodes`, `memory_note_fts`, `consolidate_incident`, the consolidate job, backfill script, FTS recall tier, `advisory` bundle, `test_memory_advisory_is_inert.py` | `IN-FLIGHT` |
| C-04 | §7.11 | Memory `GET /memory/stats` (M16's growth tripwire), `test_memory_scoring.py`, MEM9 untrusted-wrapper, MEM11 < 25 ms at 5,000 episodes, `memory.*` audit actions, retention rows | `TODO` — after C-03 |
| C-05 | §7.11 | Memory **M3** (bi-temporal `memory_facts`, shift memos, `/memory/memos`, `expire_memory` hard delete) and **M4a** (MSP priors) | `TODO` — after C-03; M4a after C-01 |
| C-06 | §7.9.1 | `send_email(*, to, subject, body, html, headers)` with `to` required; ≤ 100 recipients per message, batched by the dispatcher; `EMAIL_DAILY_CAP` with a fail-soft note at 80 %; `test_email_batching.py` | `TODO` |
| C-07 | §6.1 | Envelope-level `validate_content(alert)` and the **"no invented cause"** rule (`caused by` / `due to` must name `facts.failure_domain`); violation → deterministic template, `ai_assisted=false`, `fallback_reason`. Must exist *before* anything passes `ai_content`, and that seam is already open. | `TODO` |
| C-08 | §6.2 | `render_statuspage(alert)` | `TODO` |
| C-09 | §6.3 | `PUT /api/v1/templates/{id}/status` — `TemplateRegistry.set_status` exists and is tested; there is no route | `TODO` |
| C-10 | §10.4 | Dead-letter view `GET /api/v1/outbox?status=` and `POST /api/v1/outbox/{id}/retry` | `TODO` |
| C-11 | §9.6 | Breach drill: `POST /api/v1/admin/freeze`, the `ODPC_BREACH_72H` notification kind, a Wallboard chip for `security.redaction_miss`, and the RUNBOOK page "Redaction miss / data left the building" | `TODO` |
| C-12 | §7.3 | `pollers/kmd_cap.py` (KMD CAP RSS) + `test_kmd_cap.py` | `TODO` |
| C-13 | §7.3 | `pollers/flood.py` (GloFAS discharge tier) + `test_flood.py` | `TODO` |
| C-14 | §7.3, §10.6 | `scripts/backtest_signals.py` + `test_backtest_signals.py` → `signal_precision_30d` and the "LOW CONFIDENCE" label. The dashboard hard-codes `precision_30d: None` until this exists. | `TODO` |
| C-15 | §7.3 | `GET /api/v1/signals?source=&region_code=&active=`; county→region mapping that rejects an unknown county at startup | `TODO` |
| C-16 | §7.3 | KPLC planned-power lane: `pollers/kplc.py`, PDF parser, locality gazetteer, `match_sites`, `planned_power_interruptions` + `_links`, `/power-notices` routes, `CONFIRM_POWER_NOTICE`, `power_notice.new` | `TODO` for everything that does not need a real PDF; **`BLOCKED-HUMAN` (NOC floor)** for the "golden PDF pinned" exit criterion — it needs an actual KPLC interruption notice, and `pdfplumber` is an optional extra that is not installed |
| C-17 | §7.9.2 | `adapters/sms_africastalking.py` in sandbox + `test_sms_adapter.py` (recorded fixtures) | `TODO` for the adapter; **`BLOCKED-HUMAN` (product owner)** for the account and the KES 8,700 sender ID |
| C-18 | §7.10 | Frontend: `PirsPage` + `PirEditor` + `BlamelessHint`; Scorecards page; Incident Workspace stop-clock control and regulatory countdown. Three complete, tested backend lanes have no screen — a 24-hour regulatory countdown that exists only as JSON is not an operational control. | `TODO` |
| C-19 | App. C | WS events published with no renderer (fall through to the generic one): `complaint.surge`, `pir.opened`, `scheduler.job_failed`, `regulatory.deadline`, `security.redaction_miss`. So "AGENTS OFFLINE" and the red chip never render. | `TODO` (with C-18) |
| C-20 | §10.7 | RUNBOOK: 14 of the 18 named task pages are missing (rotate a key, read a FAILED run, retry/clear the outbox, re-run a poller, why you cannot approve your own broadcast, P1 unapproved, kill `LLM_ENABLED` in an emergency, the freeze sequence, "AGENTS OFFLINE", roll back a phase in 3 ordered steps, export a QBR pack, shadow-shift checklist, what STALE / LOW CONFIDENCE mean, confirm a KPLC notice) | `TODO` |
| C-21 | §10.1, §10.2, §10.5 | `tests/eval/` nightly layer + `tests/fixtures/eval/alarm_sequences.yaml`; `scripts/golden_diff.py` + `tests/fixtures/golden/`; `tests/system/test_auth.py` (the full §9.3 403 matrix) | `TODO` |
| C-22 | §10.4 | OpenTelemetry GenAI span attribute names (`gen_ai.request.model`, …); `OTEL_EXPORTER_OTLP_ENDPOINT` is documented and read by nothing | `TODO` |
| C-23 | §7.9.5 | Webhook hardening: `webhook_nonces` replay window, per-IP token bucket → 429, 256 KB body cap, `webhook.rejected` audit rows, `test_webhooks.py` | `TODO` — ships with whichever webhook lands first (C-17 delivery reports) |
| C-24 | App. B | `RETENTION_POLICY_PATH` and `NOC_SEED_V2_DIR` are read by code and absent from `.env.example` | `TODO` |
| C-25 | §7.9.4, Phase 6 | WhatsApp Cloud adapter in draft-only mode, `opt_in_register`, template mirror | `TODO` for the engineering (ships OFF); **`BLOCKED-HUMAN` (product owner)** to ever turn on — Meta Business verification + 5 approved Utility templates + rate card |
| C-26 | §7.4, Phase 6 | Social signals sidecar: `social_signals`, `complaint_buckets`, surge z-score, purge | `TODO` for the engineering (ships OFF); **`BLOCKED-HUMAN` (Legal/DPO)** DPIA + transparency notice, **(Operator IT)** the contact-centre export |
| C-27 | §7.1, Phase 7 | MCP runtime: `tools/registry.py` (`ToolSpec`, `tools_for_model` — the read-only filter G5 names), `tools/mcp_client.py`, `GET /mcp/status`, the sanitizer / call-guard / SSRF tests | `TODO` — "optional extra" in the spec; the pydantic gate already passed, so nothing blocks it |

## D. Waiting on a person — grouped so each owner can see their own queue

### Product owner
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
- **`docs/COMPLIANCE.md` does not exist** and five spec sections name it as the required home for
  the transparency notice, every DPIA reference and the dated ZDR confirmation.
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
- **`docs/SIGNOFF.md` does not exist.** Four shadow shifts are required and none has happened:
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
