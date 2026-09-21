# Sign-off record — shadow shifts and breach drills

This file is the record the build spec requires before certain features go live
(`SUPER_PROMPT_NOC_V2.md` §8.9 rule 2, §9.6). Each block below is **signed by a named person on
the NOC floor**, not by a developer. It is how the floor, not the developer, decides that what
the system says is right.

**Every block below is EMPTY and NOT DONE.** Nothing in this file has been inspected, run or
signed. Engineering created the form. It filled in no result.

## Rules for whoever fills this in

1. **Sign only what you watched.** A block is signed by the named floor lead (or, for the drill,
   the floor lead and the DPO) who was present for the whole shift or drill.
2. **Never sign for a shift the flags could not gate.** As of 2026-09-21 the shadow flags
   (`BROADCAST_SHADOW`, `SMS_SHADOW`, `SCORECARDS_SHADOW`) are read by no code
   (`docs/CONFORMANCE.md` B-10). Until engineering confirms in writing that the flag for your
   phase is wired, a shadow shift cannot be run. A signed record of a shift that did not happen
   is worse than an empty form.
3. **The developer who built the feature does not sign.** They may attend.
4. **Record failures too.** A shift that found problems is still a shift. Mark it `FAIL`, list
   the issues, and run another one after the fixes. Add a new block below the old one; never
   overwrite a signed block.
5. **Copy the lines from `.env`** into "Flags in force". Do not type them from memory.
6. How to run a shadow shift: `docs/RUNBOOK.md` section 19. How to run the breach drill:
   `docs/RUNBOOK.md` section 16.

## Status

| # | Sign-off | Gates | Status |
|---|---|---|---|
| 1 | Phase 2 — message wording | `ALERT_ENVELOPE_V2` going live (decision D3) | **NOT DONE** — blocked on B-10 |
| 2 | Phase 3 — SMS | `SMS_ENABLED` going live | **NOT DONE** — blocked on B-10, C-17, sender ID |
| 3 | Phase 4 — first scorecard period | any scorecard moving to `PUBLISHED` | **NOT DONE** — blocked on B-10, C-01 |
| 4 | Phase 6 — each channel that leaves the building | WhatsApp / social signals going live | **NOT DONE** — lanes not built |
| 5 | Quarterly breach drill (§9.6) | nothing switches on; this is a standing rehearsal | **NOT DONE** — never rehearsed |

---

## 1. Phase 2 — message wording, before `ALERT_ENVELOPE_V2` goes live

**Status: NOT DONE**

**Why it is required.** Phase 2 changes the words engineers receive. The spec: "one shadow shift
completed and signed (§8.9) before D3 flips `ALERT_ENVELOPE_V2`" (§8, Phase 2 exit criteria).
Decision D3 recommends flipping it "at Phase 2 exit, after `docs/SIGNOFF.md` records the shadow
shift" (§12).

**Blocked by, as of 2026-09-21:**
- `BROADCAST_SHADOW` is read by no code (`docs/CONFORMANCE.md` B-10). This shift cannot be run.
- Template `@2` (`docs/TEMPLATE_V2_REVIEW.md`) is awaiting the product owner's approval.

**Pass criterion (spec §8.9 r2).** The named floor lead is satisfied that the v2 wording is right
for the people who receive it, and every issue found is listed with an owner. The spec gives no
numeric threshold; the judgement is the floor lead's.

| Field | Entry |
|---|---|
| Date of shift | |
| Shift (day / night) and hours (EAT) | |
| Flags in force (copied from `.env`) | |
| Engineering's written confirmation that `BROADCAST_SHADOW` is wired (who, when) | |
| What was compared against what | |
| Incidents covered (numbers) | |
| Number of renderings inspected, by channel | |
| Anything sent outside the NOC by the shadow feature? (must be "no"; how checked) | |

**Issues found**

| # | Incident | Channel / audience | What was wrong | Fix owner | Fixed? |
|---|---|---|---|---|---|
| | | | | | |

**Verdict:** PASS / FAIL — ____

| | |
|---|---|
| Floor lead — name | |
| Role | |
| Date | |
| Signature | |

Product owner decision D3 recorded in `docs/DECISIONS.md` on: ____

---

## 2. Phase 3 — SMS, before `SMS_ENABLED` goes live

**Status: NOT DONE**

**Why it is required.** SMS changes what leaves the building (§8.9 r2: "Phases 3 SMS").

**Blocked by, as of 2026-09-21:**
- `SMS_SHADOW` is read by no code (`docs/CONFORMANCE.md` B-10).
- There is no SMS adapter (`docs/CONFORMANCE.md` C-17). Every SMS row in this build is stored
  as a mock send; nothing is transmitted.
- The Africa's Talking account and registered sender ID (KES 8,700 one-off) are the product
  owner's to procure.

**Engineering precondition, not signed here.** Phase 3 exit: "SMS sandbox send recorded as `SENT`
with a provider id" (§8).

**Pass criterion (spec §8.9 r2).** The named floor lead is satisfied that every SMS the shadow
feature would have sent is correct, readable on a handset, and addressed to the right people,
and every issue is listed with an owner.

| Field | Entry |
|---|---|
| Date of shift | |
| Shift (day / night) and hours (EAT) | |
| Flags in force (copied from `.env`) | |
| Engineering's written confirmation that `SMS_SHADOW` is wired (who, when) | |
| What was compared against what | |
| Incidents covered (numbers) | |
| Number of SMS renderings inspected | |
| Renderings refused by the SMS validator, or with characters outside GSM-7 (list) | |
| Anything transmitted by the shadow feature? (must be "no"; how checked) | |

**Issues found**

| # | Incident | Audience | What was wrong | Fix owner | Fixed? |
|---|---|---|---|---|---|
| | | | | | |

**Verdict:** PASS / FAIL — ____

| | |
|---|---|
| Floor lead — name | |
| Role | |
| Date | |
| Signature | |

---

## 3. Phase 4 — first scorecard period, before anything is PUBLISHED

**Status: NOT DONE**

**Why it is required.** Scorecards decide vendor money (§8.9 r2: "what is scored"). The spec:
"one shadow shift with the first scorecard period before any PUBLISHED" (§8, Phase 4 exit), and
the §7.6.2 shadow rule: the first period for each vendor is computed as `SHADOW`, visible only to
`duty_manager` and `management`, and moves to `PUBLISHED` only after a named human records
`shadow_reviewed_by`. The same rule applies again after any change to `sla_terms.version`.

**Blocked by, as of 2026-09-21:**
- `SCORECARDS_SHADOW` is read by no code (`docs/CONFORMANCE.md` B-10).
- Scorecard computation is being built and is not committed (`docs/CONFORMANCE.md` C-01).
- The commercial terms in `config/sla_terms.yaml` are marked `contract_is_synthetic: true` until
  Supply Chain supplies them. A card built on them reads "defaults, not contract".
- Decision D9 (period, dispute window, bands, who adjudicates) is open.

**Pass criterion.** Spec §7.6.2 and §8 Phase 4 exit, above. The floor lead and duty manager are
satisfied that each vendor's first-period card is arithmetically right against the incidents it
counts, and every card has `shadow_reviewed_by` recorded **before** any card is published.

| Field | Entry |
|---|---|
| Scorecard period | |
| Date(s) inspected | |
| Flags in force (copied from `.env`) | |
| Engineering's written confirmation that `SCORECARDS_SHADOW` is wired (who, when) | |
| `sla_terms.version` in force | |
| Contract terms real or synthetic? | |
| Vendors covered | |
| Card ids inspected, each with `shadow_reviewed_by` recorded (yes / no) | |
| What was compared against what | |
| Any card `WITHHELD`? Why? | |
| Any card published before this block was signed? (must be "no") | |

**Issues found**

| # | Vendor | KPI line | What was wrong | Fix owner | Fixed? |
|---|---|---|---|---|---|
| | | | | | |

**Verdict:** PASS / FAIL — ____

| | |
|---|---|
| Floor lead — name | |
| Role | |
| Date | |
| Signature | |
| Duty manager — name | |
| Date | |
| Signature | |

---

## 4. Phase 6 — each channel that leaves the building

**Status: NOT DONE**

**Why it is required.** §8.9 r2 names Phase 6 among the phases that change what leaves the
building. **Use one copy of this block per channel** (WhatsApp; social signals; any other Phase 6
lane that sends or ingests outside the operator).

**Blocked by, as of 2026-09-21:**
- The Phase 6 lanes are not built: WhatsApp (`docs/CONFORMANCE.md` C-25), social signals (C-26).
- The spec names no `*_SHADOW` flag for Phase 6. `WHATSAPP_DRAFT_ONLY=true` is the nearest
  thing ("compose and store, never transmit"). **Engineering must state in writing which flag
  gates this shift before it is scheduled.**
- Each needs its paperwork first: see `docs/COMPLIANCE.md` (DPIA, transparency notice, transfer
  assessment). A shadow shift does not replace any of it.

**Pass criterion (spec §8.9 r2).** The named floor lead is satisfied that what the channel would
have sent, or taken in, is correct and appropriate, with every issue listed and owned.

| Field | Entry |
|---|---|
| Channel | |
| Date of shift | |
| Shift (day / night) and hours (EAT) | |
| Flags in force (copied from `.env`) | |
| Engineering's written statement of which flag gates this shift (who, when) | |
| Paperwork on file in `docs/COMPLIANCE.md` for this channel (references) | |
| What was compared against what | |
| Anything transmitted outside the operator? (must be "no"; how checked) | |

**Issues found**

| # | Item | What was wrong | Fix owner | Fixed? |
|---|---|---|---|---|
| | | | | |

**Verdict:** PASS / FAIL — ____

| | |
|---|---|
| Floor lead — name | |
| Role | |
| Date | |
| Signature | |

---

## 5. Quarterly breach drill — "Redaction miss / data left the building" (§9.6)

**Status: NOT DONE — never rehearsed.**

**Why it is required.** §9.6: the drill is rehearsed once per quarter, as a calendar item,
recorded here. Budget: about one hour per quarter, floor lead and DPO (§11.2).

**Copy this block for each quarter.** Keep every past quarter below it.

**What the drill rehearses** — the five steps in `docs/RUNBOOK.md` section 16:
1. **Detect** — find the `redaction.miss` audit row.
2. **Kill** — a supervisor stops everything leaving, with no deploy.
3. **Assess** — which rows, which recipient, which fields; the Anthropic retention exposure.
4. **Notify** — the `ODPC_BREACH_72H` clock opened; the call list worked through; any credential
   involved rotated.
5. **Learn** — a post-incident review opened; the rule that missed has a test.

**Known gaps the drill will run into, as of 2026-09-21** (record how you worked around each):
- `POST /api/v1/admin/freeze` is not built (C-11). The kill sequence is four lines of `.env` and
  a restart, not the three the spec lists (RUNBOOK section 16 explains why).
- `POST /api/v1/scheduler/run/outbox_dispatch`, and the demo script, drain the outbox even while
  the kill flags are set.
- There is no pre-send contact check on email or SMS. The daily scan is the only detection, and
  it runs only with `HOUSEKEEPING_ENABLED=true`.
- The Wallboard red chip is being built and is not committed (C-19).

**Pass criteria (from §9.6).** Each of the five steps is carried out by the people the spec names
for it: detection from the audit trail; the kill sequence by a supervisor without a deploy;
assessment by the DPO and duty manager the same day; the 72-hour clock visible; a review opened.
The spec sets no time target; record the times so the next drill can be compared.

| Field | Entry |
|---|---|
| Quarter | |
| Date and start time (EAT) | |
| Where it was run (live system or a copy — and who decided) | |
| Scenario used | |
| Time detection found the miss | |
| Time the kill sequence was complete (all three status routes checked) | |
| Anything drained while frozen? (must be "no") | |
| Rows, recipients and fields identified in assessment | |
| Anthropic retention exposure stated (not retained / 30 days / up to 2 years flagged) | |
| `ODPC_BREACH_72H` clock opened? On which incident? Counting from which time? | |
| Call list reached (who, at what time) | |
| Credential rotated? Which? | |
| Review opened (id) | |
| Freeze lifted at, and queued rows reviewed before lifting? | |

**Issues found**

| # | Step | What went wrong | Fix owner | Fixed? |
|---|---|---|---|---|
| | | | | |

**Verdict:** PASS / FAIL — ____

| | |
|---|---|
| Floor lead — name | |
| Role | |
| Date | |
| Signature | |
| DPO — name | |
| Date | |
| Signature | |
