# Phase 4 — accountability and learning

Everything Phase 4 added ships **off**. With the flags unset the system behaves exactly as it
did at the Phase 3 stop line: the routes 404, the scheduled jobs report `enabled: false`, and
no golden literal moved. This document says what is there, what is still wired to nothing, and
what must be true before any of it is switched on.

## What shipped

| Lane | Flag (default) | Surface |
|---|---|---|
| Vendors + stop clocks (§7.6.1) | `SCORECARDS_ENABLED=false` | `vendors`, `incident_clock_events`; `/vendors`, `/incidents/{id}/clock` |
| Regulatory clocks + evidence packs (§7.6.1) | `REGULATORY_ENABLED=false` | `regulatory_notifications`, `evidence_packs`; 6 routes; `regulatory_sweep` job |
| Housekeeping (§5.3.22) | `HOUSEKEEPING_ENABLED=false` **and** `HOUSEKEEPING_APPLY=false` | retention, outbox sweep, redaction scan, daily backup; `housekeeping` job |
| Post-incident reviews (§7.7) | `PIR_ENABLED=false` | `post_incident_reviews`, `pir_action_items`, known-error columns on `problems`; `pir_autoopen` job |
| Regions dashboard (§7.4) | none — read-only, always on | `GET /api/v1/dashboard/regions`, Regions page |
| Memory recall M0 (§7.11) | `MEMORY_ENABLED=false` | `GET /api/v1/memory/sites/{site_id}`, "Earlier at this site" panel. **No new table.** |
| Realtime after-commit fix (§7.0.4) | n/a — a defect fix | `incident.closed` / `incident.reassigned` now buffered until commit |

Schema is at version **5**. The migration is additive and takes a backup before it touches
anything; `data/backups/noc_agents.4-to-5.*.db` is the rollback point for the v5 upgrade.

## Before you switch anything on

**1. ~~A regulatory notice would go to the demo mailbox.~~ FIXED in Phase 5 — now fill in the
address.** `services/notify.resolve_recipients` honours `recipients_ref`, and an unresolvable
ref fails **closed**: the outbox row goes `DEAD` (terminal, no retry — a missing config entry
will not appear during a backoff), `outbox.failed` reaches the wallboard and the incident gets
an error work note. The shipped operator profiles declare
`notification_recipients['regulatory.recipients.CA']` as an **empty list** on purpose: the CA
notification mailbox comes from the operator's own licence correspondence, and an invented
address would read as configured. **Fill it in before `REGULATORY_ENABLED` goes on**, or every
approved notice will refuse at dispatch.

**2. ~~The outbox has no `LLM_CALL` transmitter.~~ FIXED in Phase 5 — but the draft has no
sink.** The transmitter goes through the LLM port, so the G13 subscription guard, the spend
circuit and `llm_calls` recording all apply, and §7.0.10's reg 41(2) paperwork gate is enforced
for a hosted model. `LLM_ENABLED=false` is inert and writes no rows at all.

What it does **not** do is store the drafted text anywhere. There is no column or sink function
for it, and the token→name map is deliberately dropped at enqueue, so a draft would carry
unresolvable `<PERSON_n>` tokens. So today `PIR_ENABLED` + `LLM_ENABLED` buys an audited,
budgeted, recorded call whose output nobody receives. The follow-up is one function in
`services/pir.py` — apply the draft under the blameless validator and a DRAFT/IN_REVIEW status
guard, never overwriting a human's words. The dispatcher deliberately did not invent a
review-editing policy.

**2b. A last-gate redaction backstop now exists, and it found a real weakness.** Redaction does
happen at enqueue (`services/pir.queue_llm_draft`), but **nothing enforced it**: `outbox.enqueue`
takes any payload, so a future producer of an `LLM_CALL` row could have handed the transmitter
raw text and the dispatcher would have posted it abroad. The transmitter now re-scans the
outgoing block with redaction's own patterns and refuses, reporting counts only. It catches
contact identifiers; there is still no NER, so a person named only in free text is not detected.

**3. Housekeeping deletes data, and needs two keys on purpose.**
`HOUSEKEEPING_APPLY=true` **and** `posture.dry_run: false` in `config/retention.yaml`. The YAML
is the document Legal signs off; the env flag is the operator's decision. Neither party can
start deleting operational records alone, and a `.env` copied from a colleague's machine cannot
delete anything. As shipped, `config/retention.yaml` deletes nothing from today's schema — a
5000-day-old incident survives an `apply=True` purge, and that is asserted by a test.

`LICENCE_FLOOR_DAYS = 1095` is in code, not just config: CA licence Condition 12.2 requires
operational records for ≥ 3 years, so `validate_policy()` refuses *any* delete rule on a class
marked `licence_floor: true`, at any age. A policy that fails validation deletes **nothing at
all** — not "the rules that happen to be legal" — because a policy nobody can trust authorises
nothing. Anything unclassified is kept; there is no wildcard.

**4. `sla_terms.yaml` carries placeholders, not commercial terms.**
The `default` P1–P4 block is derived from `cfg.sla_minutes` and is real. `availability_target_pct`,
`credit_shape`, `credit_pct`, `contract_ref` and `active_from` are **not** — they need Supply
Chain and Legal. The two vendor blocks point at synthetic sample contracts and are marked
`contract_is_synthetic: true`. A scorecard built on these must read "defaults, not contract"
and credits stay `PROPOSED`.

**5. The CA QoS seed is deliberately half-empty.**
`data/seed/ca_qos_FY2024_2025.yaml` carries the three published operator-wide scores and the
80 % pass mark. The five cluster names and their per-cluster scores are **not in the spec and
not in the repo**, so `clusters: []` and every `cluster_to_region` value is `null`. The card
therefore reads "operator-wide figure — no cluster mapped to this region yet", and
`granularity` is never `"region"` (pinned by test). Filling the seed in from the published CA
report takes about 30 minutes, once a year, and the code path is already tested.

## Decisions taken, worth a reviewer's eye

**Stop-clock arithmetic is a union, not a sum.** Two overlapping SCC events covering the same
hour deduct that hour once. An event still open at period end is bounded by the period end; a
reversed event deducts nothing. Whole minutes are **truncated**, so a partial minute of stop
clock is not credited to the vendor — the conservative direction. Verified independently
against ten adversarial cases beyond the lane's own tests.

**Discipline counters are operator-side.** `opened_at − started_at` measures how late *our* NOC
recorded the event. It is recorded and reported; it never improves a vendor's number.

**Blamelessness reuses the redaction `NameMap`, not a second list.** Names come from *this
incident's* cast, never a static list. One trap had to be closed: `NameMap` registers any
four-letter name part as an alias, so an incident whose `rnio_name` is literally `"RNIO"` would
have made the validator reject its own suggested remedy.

**PIR tables are never read by the scorecard or individual-metrics jobs.** This is structural,
not a convention — a three-part test enforces it, including a runtime query capture over every
registered scheduled job, so the scorecard job is covered automatically the moment it is
registered, with no edit to the test.

**A regulatory draft cannot reach the dispatcher without a named human approval.** Three layers:
`release_notice()` is the only function that creates a regulatory outbox row and its first act
re-reads the approval; `request_approval()` queues nothing at all (unlike the handover gate,
whose `HELD` row is one `release_held` away from `PENDING`); and `regulatory_notifications` has
no `APPROVED` status for code to mistake for permission. Approving and sending are two separate
acts — no single button both approves and transmits to a regulator.

**Housekeeping never writes `outbox.status`.** Releasing a stale 120-second lease is not
cleanup, it is a re-dispatch — and a row whose SMS already reached the customer would go twice.
Stale rows are counted and logged for a human; `drain_once` reclaims them under its own lease.

**The redaction scan never quotes what it found.** §9.5 forbids MSISDNs in an audit payload, and
a breach record that quotes the breach is a second copy of it that gets exported to a regulator.
Counts and JSON paths only.

## Still wired to nothing

- **`HousekeepingAgent` has no `AgentProfile`.** Adding it makes the catalogue 13 agents, which
  moves two exact-equality assertions *including one in `tests/system/test_contracts.py`* — the
  file that has been byte-identical since before Phase 0. §2.1 R2 anticipates the re-baseline,
  but it is a deliberate act, not a side effect. The job runs fine without it; only `/agents`
  documentation output is affected.
- **`/metrics/summary`** does not yet carry `freshness` (§5.3.22) or the "PIRs awaiting review"
  counter. Both helpers exist, are tested and are operator-scoped; wiring them changes a shape
  `test_contracts.py` asserts on, so each needs its own re-baseline decision.
- **`incidents.vendor_id` is not stamped at ASSIGN time.** Today it is stamped on the first stop
  clock or by `POST /vendors/backfill`. The scorecard job should call
  `backfill_incident_vendor_ids()` before computing.
- **`OperatorConfig` has no `regulatory` or `memory` field**, so a `regulatory:` or `memory:`
  block in an operator YAML is silently dropped by pydantic's `extra="ignore"`. Both services
  read defensively and fall back to spec defaults, so this is a "before the lane goes live"
  item, not a bug today.
- **`CBK_FACTSHEET` refuses to open.** §7.6.1 lists it as a kind but gives it no deadline.
  Rather than invent a statutory number, `open_notification` raises naming the gap. Legal
  supplies it.
- **`work_notes.author` / `.body` are unclassified for retention.** They carry names typed
  free-form and §9.4 lists them in no personal class. Left as "keep, never pseudonymise" rather
  than widening a class unilaterally. Worth raising in the D14 conversation.

## Still owned by people, not code

1. `docs/TEMPLATE_V2_REVIEW.md` — approving `@2` unblocks email under the envelope.
2. SMTP relay + SPF/DKIM, and recipient resolution (blocker 1 above).
3. Real site coordinates.
4. A shadow shift — and per §7.6, **one shadow shift with the first scorecard period before any
   scorecard is PUBLISHED**.
5. Human review of the outbox, operator-scoping and retention changes.
6. The `sla_terms` commercial blocks (Supply Chain) and the `CBK_FACTSHEET` deadline (Legal).
