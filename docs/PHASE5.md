# Phase 5 — scheduling and knowledge

Everything Phase 5 added ships **off**. With the flags unset the routes 404, the four new
scheduled jobs report `enabled: false`, and no golden literal moved. Schema is at version 6.

## What shipped

| Lane | Flag (default) | Surface |
|---|---|---|
| Maintenance scheduling (§7.5) | `MAINTENANCE_ENABLED=false` | `maintenance_plans/tasks/windows`, 15 routes, Maintenance page, rain guard, `maintenance_plan_due` + `maintenance_window_sweep` jobs |
| iCalendar / iMIP (§7.5) | same | `services/ics.py` — RFC 5545 + MIME, stdlib only |
| Contract retrieval + cited answers (§7.8) | `CONTRACTS_ENABLED=false` | `contracts`, `contract_clauses` (+FTS5), `contract_faq`, `contract_queries`; Contracts page + workspace drawer |
| Complaint intake (§7.8) | `COMPLAINTS_ENABLED=false` | `relationship_complaints`, `subject_persons`, `complaints_followup` job |
| Upload hardening (§7.9.5) | `UPLOADS_ENABLED=false` | `services/uploads.py` — streaming 413, sniffed 415 |

## The one that should be fixed first

**`hitl_tasks.incident_id` is `NOT NULL`, and a maintenance window is not an incident.**

Every HITL task's owner is derived by joining `incidents` — which was a deliberate and good
choice while every gated thing *was* an incident (see the reasoning in `api/deps.py`). Phase 5
breaks that assumption: an `APPROVE_SCHEDULE` card is about a programme of work, and an
`APPROVE_MAINTENANCE_WINDOW` card is about a night's outage. Neither has an incident.

The maintenance lane therefore borrows a **scoping anchor**: the window's own `incident_id` if
it has one, else the operator's most recent incident of any status, else it fails closed with
`NoAnchorIncident` → 503 and raises no card at all. That last branch means **on a database with
no incidents, the maintenance lane cannot raise an approval card.**

Nothing here is a security hole — operator scoping still resolves correctly through the anchor,
and the failure mode is a refusal rather than a leak. It is a correctness and comprehensibility
problem: an approval card for Tuesday's generator service is filed against an unrelated incident.

The fix is to make `hitl_tasks.incident_id` nullable and add `hitl_tasks.operator_id`, then
register maintenance windows directly. **This is not an additive migration.** SQLite cannot drop
a `NOT NULL` constraint in place, and `db/migrate.py` deliberately "never drops, renames or
changes types". Doing it properly means a table rebuild — create, copy, swap — inside the
existing backup-first transaction, plus a test that an existing v6 file survives it with every
`hitl_tasks` row and its decision trail intact. That is a change worth its own reviewed commit,
not an integration side effect. Adding `operator_id` alone IS additive and could land first.

## Decisions worth a reviewer's eye

**The contract corpus does not need retrieval yet, and the code says so.** Measured: 96 clauses,
≈ 4,663 tokens — **2.3 %** of the ~200k-token line below which §7.8 (citing Anthropic's own
guidance) says to put the corpus in the prompt and skip RAG. So `answer_question` hands the model
every permitted clause while the allowed corpus is under the ceiling, and falls back to BM25
top-k only above it. Both paths are tested. The measurement is on `GET /contracts/status` and
pinned by a test with a band, so a third real contract moves it. Retrieval was still built —
it is what the lane is for and what the eval measures — but the system does not pretend the
measurement said something it did not.

**The model's prose never reaches the asker.** Cited answers are rendered from validated
verbatim quotes through a fixed template. A paraphrase, a true quote carrying an unsupported
conclusion, a missing citation or an out-of-range citation index all become the refusal path.
This is the direct answer to the Stanford result §7.8 cites: >17 % incorrect answers from
commercial legal AI, including misgrounded citations. Only the Legal-curated FAQ is `official`.

**Measured retrieval quality:** recall@5 0.939, recall@10 0.967, **recall@20 1.000**, MRR 0.873,
zero cross-tenant leaks, over 45 scored golden items. Exit criterion was recall@20 ≥ 0.9.

**The tenant filter cannot be omitted.** `retrieve_clauses(..., *, allowed_contract_ids)` is
keyword-only with no default — omitting it is a `TypeError` — and an empty set raises
`ValueError` rather than matching everything. The route ignores an allow-set supplied in the
request body, so a caller cannot widen their own scope. Contracts are mutually confidential
across MSPs and §7.8 is explicit that an omitted filter returns everything.

**The rain guard is three-valued, and never says CLEAR when it has no data.** `RAIN_CLEAR` is
reachable only with a fresh forecast showing no storm. A storm blocks unless a named human
overrides with a reason, which is audited. No forecast in the MAM/OND rain seasons warns and
flags but does not refuse — the reasoning is recorded in the code: refusing would mean a guard
with zero evidence decides that half the year is closed to maintenance, including the fuel runs
and battery checks that keep sites up *through* the rains. A guard with no data may raise its
hand; it may not make policy. A stale forecast counts as no forecast.

**Two maintenance gates, structurally separate.** `APPROVE_SCHEDULE` signs off the programme;
`APPROVE_MAINTENANCE_WINDOW` signs off going ahead on the night. Seven bypasses are tested and
refused, including a fully approved programme, an approved card for a different window, and the
raiser approving their own. Rescheduling **revokes** approval — "approve Tuesday night" is not
approval for Thursday.

**The planned-window stop clock is a proposal and has no accept helper.** Deliberately: a
convenience wrapper is one refactor away from being called by a job, and stop-clock minutes are
deducted from a vendor's SLA. Accepting goes through the existing human route.

**Uploads: the limit is enforced while streaming.** A cap applied after buffering is not a cap.
The test hands in a generator that would yield 5 GB and fails if the reader asks for more than
the cap's worth of chunks. Content type is sniffed from bytes, not trusted from the header or
the extension; a PDF header at a non-zero offset is rejected *as a polyglot*. A ZIP magic number
is refused in four bytes, so a zip bomb is never opened and its ratio never matters.

**A complaint's subject cannot read it — including when they are `admin`.** One read path, three
predicates in one WHERE clause, and 404 rather than 403, because a 403 confirms that a complaint
about you exists. Manager reminders carry counts and complaint ids and nothing else.

## Known gaps and honest limits

- **Subject access cannot find a complaint that names someone only in free text.** There is no
  NER. The response says so, in the payload, as `unstructured_text_not_searched`.
- **`icalendar` is not installed** and stays an unused optional extra; `services/ics.py` is
  stdlib-only RFC 5545. Its parser is written to the RFC rather than to its own writer, so the
  round-trip test is not circular, and an `icalendar` cross-check runs if the extra is ever added.
- **Vendor contact pseudonymisation has never run.** `config/retention.yaml`'s `vendors` entry
  has no `timestamp_column`, so the generic pass skips it. It cannot simply be filled in: the
  table has no creation timestamp, only a contract window, and measuring from `active_from`
  would scrub the contacts of a *currently engaged* MSP — the numbers the NOC rings at 02:00.
  The gap is recorded in the config with the reasoning. Found while fixing the same class of bug
  on `relationship_complaints`, whose rule pointed at a `created_at` column that does not exist.
- **PDF contract ingest is refused (415)** — text extraction needs a new dependency; deferred.
- **CA licence Condition 9.1 is UNVERIFIED** for this operator's licence class. A `ca_approval_ref`
  is enforced before a REGION or NETWORK window may be scheduled, and the code and the error
  message both say UNVERIFIED. Legal confirms the wording before the flag goes live.
- **Every maintenance interval is secondary-sourced** (NFPA 110, IEEE 1187/1188, TIA-222 are
  paywalled). `maintenance_plans.standard_ref` is NOT NULL and every plan carries the
  "secondary source" note, so the UI cannot present these as verified readings of a standard.
- **The webhook half of §7.9.5 is not built** — replay nonces, the per-IP token bucket, the
  256 KB body cap. It belongs with the webhook routes and needs a table.

## Before any Phase 5 flag goes on

1. `config/operators/*.yaml` needs a `maintenance:` block and the recipient keys
   `maintenance.recipients.FE_ONCALL` and `complaints.recipients.MANAGEMENT`. Both services read
   defensively and fall back to spec defaults, and an unresolvable recipient refuses at dispatch
   rather than landing in the demo inbox — which is correct, but it means invites and reminders
   do not send until these exist.
2. `OperatorConfig` has no `maintenance`, `memory` or `regulatory` field, so those YAML blocks
   are dropped by pydantic's `extra="ignore"`.
3. `docs/RUNBOOK.md` needs "how to enable" entries for both lanes (§8.9 rule 4).
4. Legal confirms CA Condition 9.1, and signs off the contract confidentiality fields.
