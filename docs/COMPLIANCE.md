# Compliance register

The build spec (`SUPER_PROMPT_NOC_V2.md`) names this file as the one place that records five
things: the staff transparency notice (§7.6.7), every DPIA reference (§9.2, DPA 2019 s.31), the
Transfer Impact Assessment and standard-contractual-clause reference for every cross-border
recipient (§7.0.10, §9.2), and the dated zero-data-retention confirmation behind
`LLM_ZDR_CONFIRMED=true` (§7.0.9, §9.2).

**Nothing in this register is filed or confirmed.** Every entry below is marked **NOT FILED** or
**NOT CONFIRMED**, because none of them is. Engineering built the register. It holds no legal
work.

## Rules for this file

1. **This is a register, not the documents.** Each entry records a *reference* to a document
   that Legal, the DPO or the product owner has filed or received elsewhere: its reference, its
   date, and who confirmed it. Do not paste a DPIA, a notice or a contract into this file.
2. **Only the named owner changes an entry's status.** An engineer may add a row. Only the owner
   marks it filed or confirmed, with their name and the date.
3. **Where the code reads the same reference, change both.** The transfer references are also
   in `config/operators/<operator>/transfers.yaml`, and **the code reads the YAML, not this
   file.** An entry filed here but empty in the YAML is still refused by the code.
4. **No placeholder text is usable text.** Wherever this file says "Legal drafts this", nothing
   has been drafted. The points listed under it are what the spec says the document must cover.
   They are not a draft.

## Status

| # | Item | Owner | Status | What in the code depends on it |
|---|---|---|---|---|
| 1 | Zero-data-retention confirmation (Anthropic) | Product owner obtains it; Legal confirms | **NOT CONFIRMED** | `LLM_ZDR_CONFIRMED` — see §1: it changes no behaviour |
| 2 | DPIA — hosted LLM on personal data | Legal / DPO | **NOT FILED** | `dpia_ref` for `anthropic_api` in `transfers.yaml` |
| 3 | DPIA — individual metrics | Legal / DPO, with HR | **NOT FILED** | lane not built (D12) |
| 4 | DPIA — social signals | Legal / DPO | **NOT FILED** | lane not built (C-26) |
| 5 | DPIA — per-person memory priors | Legal / DPO | **NOT FILED** | lane not built (D24) |
| 6 | DPIA — WhatsApp | Legal / DPO | **NOT FILED** | lane not built (C-25) |
| 7 | TIA + SCC — every cross-border recipient (11) | Legal / DPO | **NOT FILED** (all 11) | `tia_ref` / `dpia_ref` in `transfers.yaml` |
| 8 | Staff transparency notice | Legal / DPO, with HR | **NOT WRITTEN** | individual metrics and social signals — not built |

---

## 1. Zero-data-retention confirmation — Anthropic

**Status: NOT CONFIRMED**

**Owner.** The product owner requests zero data retention from Anthropic (it is a
per-organisation arrangement made through Anthropic's sales team). Legal confirms it is in force
and records it here.

**What the spec says (§7.0.9, §9.2).** Zero data retention is not in force on a self-serve Console
account. `claude-opus-5` is *eligible* for it; `claude-fable-5-1` is a Covered Model, is **not**
eligible, and **requires** 30-day retention whatever is agreed. Content flagged by trust-and-safety
systems may be retained for up to two years. Until this entry is confirmed, every transfer record
and every DPIA must assume **standard retention**, never zero retention.

**What the code does with `LLM_ZDR_CONFIRMED`, verified 2026-09-21.**
- It defaults to `false`.
- Setting it `true` changes **one field** in an internal status object (`retention` becomes
  `"zdr_asserted"` in `llm/client.py::llm_port_status()`). No API route serves that object —
  `GET /api/v1/llm/status` returns a smaller set of fields without it. Nothing the system sends,
  records or refuses changes.
- So the flag is an **assertion, not a control.** Setting it without a confirmed entry here would
  be a false statement in the configuration, with no protective effect.
- The system's reasoning model is `claude-fable-5-1` (`GET /api/v1/llm/status` →
  `complex_model`). No zero-retention arrangement covers that model.

**What happens today without it.** Nothing breaks. Standard retention is assumed everywhere, which
is the correct assumption. `docs/RUNBOOK.md` section 16 tells the breach drill to state the
standard-retention exposure.

**Entry — to be completed by Legal only:**

| Field | Entry |
|---|---|
| Date zero data retention took effect | |
| Anthropic reference / agreement | |
| Organisation and API keys covered | |
| Models covered (must exclude Covered Models) | |
| Confirmed by (name, role) | |
| Date confirmed | |
| `LLM_ZDR_CONFIRMED=true` set on (date, by whom) | |

---

## 2. DPIA register (DPA 2019 s.31)

A Data Protection Impact Assessment is required for high-risk processing. **Section 31(5): it is
submitted to the Data Commissioner at least 60 days before the processing begins.** So the
earliest date a feature below may process personal data is its submission date plus 60 days,
and a feature cannot be switched on the day its DPIA is filed.

Spec §9.2: "flags OFF until the DPIA is filed; `docs/COMPLIANCE.md` records the DPIA reference
per feature".

| Feature | Spec | DPIA ref | Submitted to ODPC | Earliest processing date (+60 days) | Owner | Status |
|---|---|---|---|---|---|---|
| Hosted LLM on personal data (Anthropic) | §9.2, §7.0.10 | | | | Legal / DPO | **NOT FILED** |
| Individual metrics | §7.6.7, §9.2, D12 | | | | Legal / DPO, HR | **NOT FILED** |
| Social / external complaint signals | §7.4.6, §9.2 | | | | Legal / DPO | **NOT FILED** |
| Per-person memory priors (`PARTY_TOKEN`) | §7.11.8, §9.2, D24 | | | | Legal / DPO | **NOT FILED** |
| WhatsApp Cloud API | §8 Phase 6 | | | | Legal / DPO | **NOT FILED** |

### What each is gated on in the code today

**Hosted LLM.** The one DPIA the code actually checks. `services/external_calls.py` requires a
non-empty `dpia_ref` **and** `tia_ref` (`REQUIRED_REFS`) for a cross-border recipient before the
first live call. Without them:
- outside demo mode, the call is **refused** (`TransferPaperworkMissing`) and the feature falls
  back to its deterministic template — nothing is sent and nothing is recorded, because nothing
  left the machine;
- with `NOC_ENV=demo`, the call goes ahead, one warning is logged, and the transfer is recorded
  with `DEMO-UNFILED` in place of the missing references — so the demo register shows the gap
  instead of hiding it.

The gate is enforced on these paths: the AI-drafting outbox rows (`LLM_CALL`), the contract
question-and-answer lane, and the complaint classifier.

**It is not enforced on two paths.** `POST /api/v1/incidents/{id}/analysis` and
`POST /api/v1/incidents/{id}/brief/draft` (`llm/assist.py::run_assist`) call the hosted model
directly when `LLM_ENABLED=true`, write an `llm.call` audit row afterwards, and **never consult
`transfers.yaml`**. With the LLM switched on, those two routes send redacted incident data to
Anthropic whether or not this DPIA and the TIA are filed, in production as in demo. Engineering
owns closing this; until it is closed, the DPIA gate is not complete. Verified 2026-09-21.

**Note on `NOC_ENV`.** The transfer gate reads an unset `NOC_ENV` as **production** (strict).
The auth module reads an unset `NOC_ENV` as **demo**. So on a machine with no `NOC_ENV` line, the
gate refuses unfiled transfers while the production guard on authentication is not active.

**Individual metrics, social signals, per-person memory priors, WhatsApp.** None of these lanes
exists in the code. Their flags (`INDIVIDUAL_METRICS_ENABLED`, `SOCIAL_SIGNALS_ENABLED`,
`MEMORY_PARTY_PRIORS_ENABLED`, `WHATSAPP_ENABLED`) switch on nothing that sends or stores personal
data. **Nothing in the code reads this file.** When each lane is built, the DPIA gate must be
built with it; the spec says `MEMORY_PARTY_PRIORS_ENABLED` is honoured only with a DPIA reference
here **and** authentication on (§7.11.8), and decision D24's default is that the lane is never
built.

### Entry per feature — to be completed by Legal / DPO only

Copy this for each row above when it is filed.

| Field | Entry |
|---|---|
| Feature | |
| DPIA reference | |
| Date submitted to the Data Commissioner | |
| Earliest processing date (submitted + 60 days) | |
| Outcome / conditions from the Commissioner, if any | |
| Confirmed by (name, role) | |
| Date confirmed | |
| Reference also entered in `transfers.yaml`? (hosted LLM only) | |

---

## 3. Cross-border transfer register — TIA and SCC per recipient

**Why.** DPA 2019 s.25(h) and s.48, General Regulations 2021 reg 40–42 and reg 47, and the ODPC
Guidance Note on Cross-border Data Transfers (April 2026). Each recipient outside Kenya needs a
Transfer Impact Assessment and an appropriate safeguard — primarily the ODPC-issued standard
contractual clauses — before the first transfer (§9.2). **The finality of that guidance note is
UNVERIFIED** (spec §7.0.10); Legal confirms it when filing the first TIA. The processor's 48-hour
duty to notify the controller of a breach (DPA s.43) is a contract clause checked in the TIA
(§9.2).

**Where the code reads it.** `config/operators/safaricom/transfers.yaml` and
`config/operators/airtel/transfers.yaml`. Both list the same 13 recipients. **In both files, as of
2026-09-21, every `dpia_ref`, `tia_ref`, `scc_ref`, `confirmed_by` and `confirmed_at` is an empty
string.** `DEMO-UNFILED` is not stored in those files; it is what the gate writes into the audit
row when demo mode lets an unfiled transfer through. Every contracting entity in the files is
marked **UNVERIFIED** — they are the publicly known company names, not checked against signed
contracts.

The gate requires `dpia_ref` and `tia_ref`. It records `scc_ref` but does not require it.

### Cross-border recipients (11)

| Recipient key | Entity (UNVERIFIED) | Country | Used by this build? | Gate | TIA ref | SCC ref | Status |
|---|---|---|---|---|---|---|---|
| `anthropic_api` | Anthropic PBC | US | Yes, when `LLM_ENABLED=true` | **Refused** without refs outside demo — except the two routes in §2 | | | **NOT FILED** |
| `gmail_smtp` | Google LLC (may contract via Google Ireland Limited) | US | **Yes, today**, whenever `EMAIL_ENABLED=true` | **Recorded, not refused** — see below | | | **NOT FILED** |
| `meta_graph` | Meta Platforms Ireland Limited | IE | No — no WhatsApp adapter (C-25) | — | | | **NOT FILED** |
| `x_api` | X Corp. | US | No | — | | | **NOT FILED** |
| `pagerduty` | PagerDuty, Inc. | US | No — MCP runtime not built (C-27) | — | | | **NOT FILED** |
| `slack` | Slack Technologies, LLC (Salesforce) | US | No — MCP runtime not built | — | | | **NOT FILED** |
| `atlassian_rovo` | Atlassian Pty Ltd | AU | No — MCP runtime not built | — | | | **NOT FILED** |
| `datadog` | Datadog, Inc. | US | No — MCP runtime not built | — | | | **NOT FILED** |
| `servicenow` | ServiceNow, Inc. | US | No — MCP runtime not built | — | | | **NOT FILED** |
| `google_sheets` | Google LLC | US | No — MCP runtime not built | — | | | **NOT FILED** |
| `microsoft_work_iq` | Microsoft Corporation | US | No — MCP runtime not built | — | | | **NOT FILED** |

**The email relay is the one cross-border transfer happening today.** The spec's gate covers the
hosted LLM and "abroad" MCP cards, not the SMTP relay. So with `EMAIL_ENABLED=true`, every real
email to Gmail's relay is written to the transfer register — before it is sent — marked
`paperwork_status="unfiled"` (or `"demo_unfiled"` in demo mode), and **sent anyway**. Whether an
unfiled TIA should also stop outage notifications to the NOC's own staff is a decision the code
deliberately leaves to the operator: the switch is `TRANSFER_GATE_BLOCKS_SEND` in
`orchestrator/outbox.py`, currently `False`. **Owner of that decision: product owner, with Legal.**
Until it is made, the register shows every relay transfer as unfiled, which is the truth.

### Domestic recipients (2) — recorded, never gated

| Recipient key | Entity | Country | Used by this build? |
|---|---|---|---|
| `africas_talking` | Africa's Talking Limited (UNVERIFIED) | KE | No — no SMS adapter (C-17); every SMS is a mock |
| `ollama_local` | Self-hosted, operator infrastructure | KE | Only with `LLM_PROVIDER=openai_compat` pointed at this machine or the local network |

Kenya-domiciled recipients are recorded like any other, with `recipient_country="KE"`, and
filtered out of the cross-border view. No DPIA or TIA gate applies to them.

### Recipients that are not in the register

Legal should confirm each of these needs no entry, or add one.

- **OpenWeather** (the spec's Phase 7 MCP example). The MCP runtime is not built and
  `OPENWEATHER_AGENT_KEY` is read by no code. Nothing is sent to it. If the runtime is built, it
  needs an entry before its first call.
- **Open-Meteo and MET Norway** (the weather poller, when `WEATHER_ENABLED=true`). The poller
  sends region-centre coordinates only, and does not call the transfer register. The spec does
  not list weather providers among transfer recipients.
- **A hosted OpenAI-compatible endpoint** (Groq, Gemini, …) — `LLM_PROVIDER=openai_compat`
  pointed off the machine. The code records it as "abroad" with an unknown country and has no
  register key for it, so outside demo mode the gate refuses it.

### Entry per recipient — to be completed by Legal / DPO only

Enter the same values in **both** operator `transfers.yaml` files; the code reads those.

| Field | Entry |
|---|---|
| Recipient key | |
| Contracting entity (verified against the signed contract) | |
| Country of the contracting entity | |
| DPIA reference | |
| TIA reference | |
| Safeguard instrument (SCC or other) and reference | |
| 48-hour processor breach-notice clause present? (reference) | |
| Confirmed by (name, role) | |
| Date confirmed | |

---

## 4. Staff transparency notice

**Status: NOT WRITTEN. Legal drafts this.** Nothing below is notice text.

**Owner.** Legal / DPO, with HR.

**Why.** §7.6.7: any individual-level measurement is advisory only, decided by a person, and
comes with a transparency notice recorded in this file. §9.2 also requires a transparency notice
for social / complaint signals.

**What the spec says the notice must cover** (for Legal to address; listed, not drafted):

*Individual metrics* (§7.6.7; §9.2 rows on DPA s.35(1)–(4), General Regulations reg 22(2)(a)–(i),
Employment Act s.41, s.43, s.45(5), s.46(g)):
- that any individual measure is advisory only, and every decision about a person is made by a
  person;
- the reconsideration path;
- the fairness check across region and shift;
- "meaningful information about the logic" — the formula and configuration path behind each
  line;
- that post-incident review content and complaints are never inputs.

The spec also asks Legal to track the Data Protection (Amendment) Bill 2025, which would change the
s.63 fine from "whichever is lower" to "whichever is higher", before the risk assessment for this
lane is finalised (§7.6.7). The spec's own citation of s.35 was not re-read in the memory research
pass; counsel confirms the section before the DPIA (§7.11.12).

*Social / external complaint signals* (§9.2 row on DPA s.28–s.30, s.37(1), s.32(1)):
- the lawful basis;
- what is kept (a salted hash, redacted text and derived fields — no photos, no profiles);
- the 30-day retention;
- that the text never goes to an external AI model.

**What the code does today.** Neither lane exists. `INDIVIDUAL_METRICS_ENABLED` and
`SOCIAL_SIGNALS_ENABLED` are read by no code. Decision D12 (whether to build individual metrics
at all) is open. So today the system measures no individual and ingests no public complaint, and
no notice is yet required for what it does. The notice must be published **before** either lane
is switched on.

**Entry — to be completed by Legal only:**

| Field | Entry |
|---|---|
| Notice reference and version | |
| Covers (individual metrics / social signals) | |
| Where it is published to staff | |
| Date published | |
| Approved by (name, role) | |

---

## 5. Also waiting on Legal — recorded elsewhere

Not part of this register, listed so nothing is lost. Details in `docs/CONFORMANCE.md` §D.

- The operator's CA licence class and the wording of Conditions 9.1 and 9.2. The code and its
  messages say UNVERIFIED until it is confirmed.
- The statutory deadline for the `CBK_FACTSHEET` notification. The code refuses to invent one.
- Sign-off of the retention schedule in `config/retention.yaml`, including two recorded gaps:
  `work_notes.author` / `.body` are unclassified, and vendor-contact pseudonymisation has never
  run (A-08).

---

## Change log

| Date | Entry | Change | By (name, role) |
|---|---|---|---|
| 2026-09-21 | all | Register created. Every entry NOT FILED / NOT CONFIRMED. | engineering |
