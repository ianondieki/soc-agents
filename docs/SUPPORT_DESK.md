# Support desk: multi-agent complaint registration and management

The most widely deployed agent pattern in industry, built for a Kenyan operator's customer complaints and
wired into the NOC it sits beside. A customer registers a complaint online. A **triage agent** reads it and
routes it. A **resolver agent** answers from the knowledge base, citing the article it used. An **action
agent** fixes the account through tools (refunds, M-PESA reversals, bundle re-credits, ticket updates,
linking the complaint to a live network incident). Hard cases **escalate to a person**, with the reason.
An **eval suite** measures the desk on a labelled set, headlined by the **resolution rate** and the
**wrong-escalation rate**.

This file is the contract between the backend (`src/noc_agents/support/`, `api/routers/support.py`), the
eval suite (`tests/eval/support_eval.py`) and the UI (`/support`, `/complain`). Change it here first.

## Flow

```
 customer (web form /complain, or seeded channels: sms, app, call centre, social)
   |
   v
 intake      normalise text, mask the MSISDN, detect language (en / sw / mixed), dedupe repeats
   |
   v
 triage      category, urgency, sentiment, risk flags, confidence  ->  route
   |                 |                      |
   | resolver        | action               | human
   v                 v                      v
 resolver         action agent           escalation
 BM25 over the    tool calls under       queue for a person with a reason code;
 KB; answers      policy limits; a       the customer gets an honest holding reply
 only when        call over a limit      and a reference
 grounded,        becomes an approval
 else escalates   for a person
   \                 |                     /
    +--------- reply to the customer, trace of every step ---------+
```

Every agent step is recorded as a **step** with a plain-English summary, the structured detail, and its
duration, so the UI can show exactly why a complaint went where it went.

## Determinism and the LLM

The desk runs fully deterministic by default (keyword and phrase rules with weights for triage, BM25 for
retrieval, policy tables for tools and escalation), which is what CI and the eval gates run. With
`LLM_ENABLED=true` the existing LLM port (`llm/client.get_llm_port`) may (a) break a low-confidence triage
tie and (b) polish the resolver's reply, which must stay grounded in the cited article; any LLM failure
falls back to the deterministic path. The eval CLI takes `--llm` for a nightly comparison; tests never call
a real model.

## Categories, routes, statuses

- `category`: `network` | `data_bundles` | `mpesa` | `billing` | `sim_and_fraud` | `device_settings` |
  `roaming` | `account` | `other`
- `urgency`: `low` | `normal` | `high` | `critical`
- `sentiment`: `calm` | `frustrated` | `angry`
- `route`: `resolver` | `action` | `human`
- `status`: `answered` (resolver replied) | `action_taken` (tool succeeded) | `awaiting_approval` (a tool
  call over its limit waits for a person) | `escalated` (waiting for a person) | `in_progress` (a person
  claimed it) | `resolved` (a person closed it) | `closed`
- `outcome`: `auto_resolved` | `action_completed` | `escalated` | `human_resolved` | `null`

## Escalation policy (config/support/policy.yaml)

A complaint goes to a person when any of these holds; the first matching rule is the `reason_code`:

| reason_code | When |
|---|---|
| `fraud_or_sim_swap` | SIM swap, account takeover, "someone is using my M-PESA", unknown PIN change |
| `legal_or_regulator` | a lawyer, court, "Communications Authority", CA, CAK, ODPC, a formal demand |
| `threat_or_safety` | threats, self-harm, harassment |
| `needs_verification` | an M-PESA reversal asked for on the public form **without** the 10-character transaction code: a person verifies the transfer with the customer first, because the form cannot prove the caller owns the number. Not a safety reason. Nothing reads the account and no tool call is planned. (Policy decision of 2026-10-04.) |
| `over_refund_limit` | a refund above the auto limit (default KES 500) or a reversal above KES 5,000 or older than 24h |
| `repeat_unresolved` | the same customer's third complaint in 7 days on the same category |
| `angry_high_value` | angry sentiment on a high-value account |
| `low_confidence` | triage confidence below 0.55 |
| `not_grounded` | the resolver's best article scores below the grounding threshold |
| `tool_failed` | a tool call failed or was refused by policy and no safe alternative exists |

## Tools (action agent)

All tools run against demo fixtures (`config/support/accounts.yaml`), never a real system.

| tool | does | auto limit |
|---|---|---|
| `lookup_account` | reads the customer's plan, balance, recent transactions and bundles | always |
| `issue_refund` | refunds airtime or a charge | up to KES 500, else approval |
| `reverse_mpesa` | reverses a wrong-number transfer by its 10-character code | within 24h and up to KES 5,000, recipient has not withdrawn, **and the customer's own message quoted the code**; over a limit: approval; no code: `needs_verification` |
| `recredit_bundle` | re-credits a data bundle that expired early or failed to apply | one per 30 days |
| `link_incident` | finds an open NOC incident for the town or region named and links it | always |
| `update_ticket` | sets status and appends a note | always |
| `reset_network_settings` | sends the device configuration SMS (APN, MMS) | always |

## API

Base path `/api/v1/support`. JSON. Times are ISO-8601 UTC. Feature flag `SUPPORT_DESK_ENABLED`
(default `true` in this demo); when false every route answers 404.

```ts
type Complaint = {
  id: string;                 // uuid
  ref: string;                // "CMP-000123"
  created_at: string; updated_at: string;
  channel: "web" | "sms" | "app" | "call_centre" | "social";
  customer: { name: string | null; msisdn_masked: string; account_ref: string | null };
  language: "en" | "sw" | "mixed";
  subject: string;            // first line or a generated summary, <= 90 chars
  body: string;
  category: Category; urgency: Urgency; sentiment: Sentiment;
  route: "resolver" | "action" | "human";
  status: Status; outcome: Outcome | null;
  confidence: number;         // triage confidence 0..1
  escalation: { reason_code: string; reason: string; at: string; claimed_by: string | null } | null;
  reply: string | null;       // the customer-facing reply
  citations: { article_id: string; title: string; score: number }[];
  linked_incident: { id: string; incident_number: string; title: string; status: string } | null;
  link_strength: "site" | "county" | "wide_area" | "person" | null;  // how it was linked (docs/CLOSE_THE_LOOP.md); null in the public view
  sla_due_at: string;
};
type Step = {
  seq: number;
  agent: "intake" | "triage" | "resolver" | "action" | "escalation" | "human";
  action: string;             // machine word: "classified", "retrieved", "answered", "called_tool", ...
  summary: string;            // one plain sentence for the UI
  detail: Record<string, unknown>;
  duration_ms: number;
  at: string;
};
type ToolCall = {
  id: string; tool: string; args: Record<string, unknown>; result: Record<string, unknown> | null;
  status: "ok" | "refused" | "needs_approval" | "approved" | "rejected" | "failed";
  policy: string | null;      // the rule that allowed, limited or refused it, in words
  at: string; decided_by: string | null;
};
type Message = { id: string; author: "customer" | "agent" | "staff"; name: string | null; body: string; at: string };
```

| Method and path | Body | Answer |
|---|---|---|
| `POST /complaints` | `{ body, msisdn, name?, subject?, channel?, account_ref? }` | `201 { complaint, steps, tool_calls, messages }` (runs the whole pipeline) |
| `GET /complaints` | query `status`, `route`, `category`, `q`, `limit` (default 50) | `{ items: Complaint[], counts: { by_status, by_route, by_category } }` newest first |
| `GET /complaints/{id}` | | `{ complaint, steps, tool_calls, messages }` |
| `POST /complaints/{id}/claim` | | the detail; a person takes an escalated case (`in_progress`) |
| `POST /complaints/{id}/resolve` | `{ reply, note? }` | the detail; `resolved`, reply sent |
| `POST /complaints/{id}/actions/{tool_call_id}/approve` | | the detail; the tool runs, status follows |
| `POST /complaints/{id}/actions/{tool_call_id}/reject` | `{ reason }` | the detail; case goes to `escalated` |
| `GET /kb` | | `{ articles: { id, title, category, summary, body, updated_at }[] }` |
| `GET /kb/search?q=` | | `{ results: { article_id, title, score, snippet }[] }` |
| `GET /metrics` | query `hours` (0 = all time) | `{ total, auto_resolved, action_completed, escalated, human_resolved, awaiting_approval, resolution_rate, escalation_rate, median_handle_ms, by_category: Record<Category, number> }` |
| `GET /evals/latest` | | `EvalReport`, or 404 when none has run |
| `POST /evals/run` | | `EvalReport` (runs the golden set in-process, deterministic) |
| `POST /demo/seed` | | `{ created: number }`: about a dozen realistic complaints through the pipeline |

RBAC follows `api/deps.py`: reads for the operations floor, writes (claim, resolve, approve) for
`OPERATIONS`; `POST /complaints` is the public registration form and needs no role (rate-limited per
MSISDN). Realtime events through the hub: `support.created`, `support.escalated`, `support.updated`.

## Evals

```ts
type Metrics = {
  resolution_rate: number | null;        // headline
  wrong_escalation_rate: number | null;  // headline
  missed_escalation_rate: number | null;
  safety_missed_escalation_rate: number | null;  // added: the metric the safety gate reads
  escalation_reason_accuracy: number | null;     // added: right person for the right reason
  containment_rate: number | null;
  triage_accuracy: number | null;
  routing_accuracy: number | null;
  grounded_answer_rate: number | null;
  tool_accuracy: number | null;
  p50_ms: number;
};  // null: no case to measure (empty denominator), never 0
type EvalReport = {
  run_id: string; ran_at: string; mode: "deterministic" | "llm";
  dataset: { name: string; version: string; size: number; excluded: number;
             split: "all" | "dev" | "validation" | "holdout" | "dev+validation" };
    // split: what the headline below is over. A full run says "holdout" (the blind split) and size is its
    // scored cases; excluded counts contested cases that are loaded but never scored. version covers both
    // golden files.
  metrics: Metrics;
  gates: { metric: string; op: ">=" | "<=" | "=="; threshold: number; value: number | null; passed: boolean;
           note: string | null }[];        // note: why a gate failed on a null value (no evidence is a failure)
  passed: boolean;
  confusion: { labels: ["resolver", "action", "human"]; matrix: number[][] };  // rows = expected, cols = actual
  by_category: { category: string; n: number; resolution_rate: number | null;     // null: no case to measure
                 wrong_escalation_rate: number | null; triage_accuracy: number | null }[];
  failures: { case_id: string; text: string; kind: "wrong_escalation" | "missed_escalation" | "wrong_route" |
              "wrong_category" | "wrong_article" | "wrong_tool" | "unresolved";
              expected: Record<string, unknown>; actual: Record<string, unknown> }[];
  by_split: { dev?: Metrics; validation?: Metrics; holdout?: Metrics };  // added: every split that ran
};
```

Definitions (the eval module's docstring repeats them):

- **Resolution rate** = cases the desk resolved correctly without a person / cases the gold set marks
  resolvable (gold route `resolver` or `action`). Correct means the right route and, for `resolver`, a
  cited article in the gold set's accepted list, or for `action`, the gold tool called with valid
  arguments and succeeding.
- **Wrong-escalation rate** = escalations of cases the gold set marks resolvable / all escalations the desk
  made. It answers: of the cases sent to a person, how many did not need one?
- **Missed-escalation rate** = gold `human` cases the desk kept / gold `human` cases. Missed escalations
  are the dangerous error, so the gate is 0 for the safety reasons (`fraud_or_sim_swap`,
  `legal_or_regulator`, `threat_or_safety`).
- **Containment rate** = cases closed without a person / all cases.

A rate with an empty denominator is `null`, never a number, and a gate that reads `null` **fails** with a
`note`: a split with no safety case has not shown a safety-missed rate of 0, it has shown nothing.

Default gates (`config/support/policy.yaml`): resolution rate >= 0.80, wrong-escalation rate <= 0.10,
missed-escalation rate on safety cases == 0, triage accuracy >= 0.85. Two uses, kept apart on purpose:

- **The regression gate** (asserted by pytest, `tests/unit/test_support_evals.py`) is the default gates on
  **dev + validation combined** (`--split dev+validation`, `dataset.split: "dev+validation"`): the sets the
  desk is developed against. It fails CI when a change breaks what the desk already handled.
- **The holdout is reported, never asserted.** A full run (`POST /evals/run`, the CLI without `--split`)
  runs every case, carries every split in `by_split`, and takes the blind **holdout** as the headline
  (`dataset.split: "holdout"`). Its gates appear in the report as information. A held-out set that becomes
  a CI target stops being held out: the moment a failing holdout case is fixed to make a build green, the
  set measures fit, not generalisation. The holdout numbers are the honest ones, and they stay honest only
  while nothing is tuned against them.

### Methodology: three splits, two of them written blind

The desk is keyword rules, and the starter set was written by the same hand as the lexicon, which is why
its first score was 1.0 everywhere. The golden set therefore has three splits, in two files, with
different jobs and different provenance:

| split | file | n | written by | status |
|---|---|---|---|---|
| `dev` | `tests/fixtures/support_eval/golden.jsonl` | 129 | the backend author (39 starter cases) and the eval author, beside the rules | tuned against; grows with every round |
| `validation` | `golden.jsonl` (was `test`) | 86 | the eval author, **blind**: from the contract, the policy and `accounts.yaml` only, before reading `vocab.py`/`triage.py` or any output | frozen, scored, then *seen* (its failures were read in round 1), so it is a development set now |
| `holdout` | `tests/fixtures/support_eval/holdout_blind.jsonl` | 95 (91 scored) | **a different model, with no access to the code**, from the contract, `policy.yaml`, `accounts.yaml` and the article titles; 80 cases carry the author's `note` | scored once before and once after round 2, never tuned against |

Round 1 (validation blind, dev-only tuning) and round 2 (holdout blind, dev + validation tuning) followed
the same discipline: write the held-out cases first, label them from the contract and the policy, freeze
them, record the *before* number, tune on the development sets only with general mechanisms
(Kiswahili/Sheng normalisation, number words, phrase patterns, contrast weights, the multi-issue
capability below), never a rule keyed to a sentence, then record the *after* number once. The validation
split's ids, texts and labels are unchanged since its freeze except the five code-less reversal requests
relabelled for the `needs_verification` policy (recorded in a comment in the file).

**Adjudications on the holdout** (orchestrator, 2026-10-04; the only label-level changes allowed):

- Four cases the author marked CONTESTABLE (`h-020`, `h-067`: a payment status check answered by
  `lookup_account`; `h-054`, `h-068`: a note on an existing ticket by `update_ticket`) are marked
  `"contested": true` in the file and **excluded from every number** (`dataset.excluded: 4`): the contract
  gives no rule that makes a bookkeeping tool the decisive one. Their labels stay as written.
- The author labelled outages in Nakuru, Kayole, Rongai, Nyali/Mombasa, Westlands and Machakos as
  `link_incident`, assuming each town has an open incident. The eval's template database now holds, beside
  the rain-storm hubs (Nakuru, Eldoret, Thika, Embakasi East, Nairobi East), four **eval-only open incidents**
  for Westlands, Ongata Rongai, Nyali and Machakos (`evals.EVAL_EXTRA_INCIDENTS`; the live demo database is
  untouched), and Rongai was added to the operator profile's Nairobi West coverage areas, which is where the
  gazetteer learns its towns. Two dev cases and two unit tests that meant "a town with no open incident"
  moved from Mombasa to Western-Nyanza towns, the one region the eval leaves without a ticket.
- Holdout labels the eval author reads as contradicting the contract, **left unchanged** and listed here:
  `h-048` names the Communications Authority (as a landmark) in an outage report and is labelled
  `link_incident`, but the contract's `legal_or_regulator` rule names the Authority without an "in passing"
  exception; `h-085` asks for a SIM swap (3G to 4G) and is labelled a resolver case, but the contract's
  `fraud_or_sim_swap` rule names "SIM swap" without qualification, and an unverified caller asking for a
  swap on a number is the classic takeover vector; `h-052` and `h-084` (a reversal whose recipient has
  withdrawn) are labelled `tool_failed`, while the tools table says "recipient has not withdrawn; else
  approval", which makes the reason `over_refund_limit` (route and tool agree either way).

**Composition** (dataset version `75e2eb4b020e`; contested cases not counted):

| category | dev | val | hold | all |   | route / tool | dev | val | hold | all |   | reason / language | dev | val | hold | all |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| network | 27 | 16 | 16 | 59 |   | resolver | 49 | 31 | 32 | 112 |   | fraud_or_sim_swap | 6 | 5 | 5 | 16 |
| data_bundles | 17 | 8 | 7 | 32 |   | action | 35 | 22 | 20 | 77 |   | legal_or_regulator | 5 | 4 | 4 | 13 |
| mpesa | 25 | 22 | 23 | 70 |   | human | 45 | 33 | 39 | 117 |   | threat_or_safety | 5 | 3 | 4 | 12 |
| billing | 24 | 14 | 16 | 54 |   | reverse_mpesa | 12 | 10 | 10 | 32 |   | needs_verification | 4 | 5 | 5 | 14 |
| sim_and_fraud | 10 | 9 | 9 | 28 |   | recredit_bundle | 9 | 5 | 5 | 19 |   | over_refund_limit | 6 | 6 | 6 | 18 |
| device_settings | 6 | 4 | 4 | 14 |   | issue_refund | 9 | 6 | 7 | 22 |   | repeat / angry_high_value | 3 / 3 | 1 / 1 | 2 / 3 | 6 / 7 |
| roaming | 5 | 3 | 3 | 11 |   | link_incident | 11 | 7 | 6 | 24 |   | low_conf / not_grounded / tool_failed | 5 / 4 / 4 | 3 / 2 / 3 | 2 / 4 / 4 | 10 / 10 / 11 |
| account | 7 | 4 | 4 | 15 |   | reset_network_settings | 4 | 3 | 2 | 9 |   | en | 63 | 43 | 37 | 143 |
| other | 8 | 6 | 9 | 23 |   | | | | | |   | sw (incl. Sheng) | 44 | 26 | 36 | 106 |
| **total** | **129** | **86** | **91** | **306** |   | | | | | |   | mixed | 22 | 17 | 18 | 57 |

Every split holds the three safety reasons and the hard negatives: "lawyer", "fraud", "court", "police" and
"CA" in benign contexts; refunds of KES 480 and 520 (limit 500); reversals at 23 h and 25 h (window 24 h); a
recipient who already withdrew; a third repeat on a *different* category; angry customers on silver and on
gold/platinum accounts; polite SIM-swap reports; pasted M-PESA confirmation SMS; lower-case codes; amounts
as "1.5k", "Ksh 1,500", "12k", "elfu moja na mia tano", "hamsini bob"; two issues in one message; towns
with and without an open incident.

**Adding a case.** Append one JSON line to `golden.jsonl`, in the split it belongs to (dev lines before the
`# ---- validation split` marker, validation lines after it). **Never add to, edit or relabel
`holdout_blind.jsonl`**: a new blind holdout is written by someone who has not seen the code when the
current one has been seen. Write the case as a customer would, label it from the contract and the policy
(never from what the desk does), use a real MSISDN from `accounts.yaml` when the case needs account context,
and keep the consistency rules (`route` is `human` exactly when `escalation_reason` is set; `safety` exactly
for the three safety reasons; `tool` for action cases and tool-driven escalations; `article_ids` lists
every acceptable article). The loader refuses a contradictory line. `python tests/eval/support_eval.py
--compare` shows the three splits side by side with the regression gate, the holdout's gates and the top
failure kinds; `--no-failures` prints a blind measurement (numbers, never the failing cases).

**Latest numbers** (deterministic, 2026-10-04, round 2). The holdout *before* is the first and only
measurement taken before this round's tuning (its failures unread); *after* is the single measurement at
the end. Dev and validation are development sets; their *after* numbers show fit, not generalisation.

| metric | holdout before | holdout after | dev after | validation after | gate |
|---|---|---|---|---|---|
| resolution_rate | 0.712 | **0.750** | 1.000 | 1.000 | >= 0.80: regression passes, holdout **below** |
| wrong_escalation_rate | 0.292 | **0.250** | 0.000 | 0.000 | <= 0.10: regression passes, holdout **above** |
| safety_missed_escalation_rate | 0.077 | **0.077** | 0.000 | 0.000 | == 0: regression passes, holdout **misses 1 of 13** |
| triage_accuracy | 0.901 | **0.890** | 1.000 | 1.000 | >= 0.85: both pass |
| missed_escalation_rate | 0.128 | 0.077 | 0.000 | 0.000 | |
| routing_accuracy | 0.791 | 0.835 | 1.000 | 1.000 | |
| tool_accuracy | 0.767 | 0.867 | 1.000 | 1.000 | |
| escalation_reason_accuracy | 0.692 | 0.718 | 1.000 | 0.939 | |

Round 1, for the record (validation blind, before -> after dev-only tuning): resolution 0.528 -> 0.906,
wrong-escalation 0.426 -> 0.156, safety-missed 0.167 -> 0.000, triage 0.814 -> 0.965.

What the holdout still shows (22 of 91 cases; read once, after the round, for this paragraph only): most
wrong escalations are `low_confidence` on single-issue Kiswahili messages whose words the lexicon does not
know yet ("mistari ya Biblia", "sina mtandao kabisa", "court interpreter"), and risk words used
figuratively or in a request ("Hii ni fraud tupu" about a bundle, "STOLEN 50 BOB", "naogopa" about future
misuse, a requested 3G-to-4G "SIM swap"); the one missed safety case is a threat phrased without any word
the threat lexicon knows ("someone there will regret it"); two hypothetical questions about reversals
("if one sends M-PESA to a wrong number...") are treated as reversal requests and stop at
`needs_verification`. None of these was fixed: they are the next round's development material once a
fresh holdout exists.

## Decisions the contract left open

Recorded by the backend implementation (`src/noc_agents/support/`); each is a reading of the
contract above, not a change to it, except where marked **refinement**.

**Routes and statuses**

- `route` on a stored complaint is where the case **ended**: `resolver` (answered), `action` (a tool
  fixed it) or `human` (a person owns it). Triage's first choice is in the triage step's `detail.route`.
  This is the route the eval compares with the golden label. A case a person approved stays `human`
  with status `action_taken`.
- When the action agent cannot identify a safe target (no bundle that expired early, no refundable
  charge, no open incident for the place named) the **resolver answers instead** (`answered`, route
  `resolver`). `tool_failed` is kept for a target that was identified and then refused or failed (already
  reversed, not on this account, a second re-credit inside 30 days, the same transfer or charge already
  waiting for approval on another case). A reversal is identified only by a code the customer typed
  (`needs_verification` otherwise, see the public form below).
- A call over its limit is recorded as `needs_approval` and the case is `awaiting_approval`. When another
  rule sends the case to a person (repeat, angry high-value, low confidence...), a call that *would* have
  succeeded is **held**: recorded as `needs_approval` with the policy "held for a person (<reason>)", not
  run, and one approval away once a person has claimed the case (approve answers 409 on an unclaimed
  `escalated` case).
- **A tie-break never moves money.** When the category came from the LLM tie-break and the planned call
  moves money (`reverse_mpesa`, `issue_refund`, `recredit_bundle`), the call is held and the case escalates
  with `low_confidence`; the model may choose which article answers, never a payment.
- Approving re-runs the tool with the approval (validation and idempotency still apply). If it can no
  longer complete (for example the transfer was reversed on another ticket) the call is `refused` and
  the case goes back to `escalated` with `tool_failed`. Resolving a case supersedes (rejects) any call
  still waiting for approval. `escalated` always means *in the queue, unclaimed*: rejecting a call or a
  failed approval clears `claimed_by`.
- **Concurrency.** Every human action (claim, resolve, approve, reject) takes SQLite's write lock
  (`BEGIN IMMEDIATE`) before it reads the case and re-reads it under the lock; the pipeline takes it
  before the action agent plans. Two simultaneous approvals run the tool once (the second gets 409), two
  claims make one owner, and two complaints about one transfer park one reversal.
- Nothing sets `closed` yet: the contract defines no route for it.

**Intake and the public form**

- An identical complaint (same MSISDN, same normalised text) within 2 minutes returns the complaint
  already on file with **200** instead of 201; nothing new is created. A caller without a support read
  role gets only that case's `ref` and `status` (every other key empty; the masked number is their own):
  the first submission may have come from someone else, with their name on it.
- Rate limit: 5 complaints per MSISDN and 30 per client address per 10 minutes
  (`config/support/policy.yaml`), in-process, memory hard-capped (oldest keys evicted first); over either,
  **429** with `Retry-After`.
- **Every intake is unverified**, so the reply never reveals anything the caller did not type: it states
  what was done and echoes only the code, amount or bundle name the customer's own message contained
  ("we have refunded the charge to the airtime balance of +254 7•• ••• 567"); an account-derived escalation
  reason (`over_refund_limit`, `repeat_unresolved`, `angry_high_value`, `tool_failed`) is told to the
  customer as the policy's `account_review_reason`. Replies greet by the name the caller gave, never the
  account holder's.
- A caller without a support read role (anonymous, once `AUTH_DISABLED=false`) gets the **public view**,
  built from facts the caller holds rather than filtered from the staff trace: the steps are a fixed outline
  (received, sorted, then answered / fixed / passed to a person); the only tool call is the one that fixed
  the case, by name and status; `awaiting_approval` reads `escalated`; an account-derived reason reads
  `account_review` (a public-only code, never stored); `customer` holds only what the caller typed. Staff
  see the full trace, with the **verified** account holder and account reference; the reference a caller
  typed is kept in the intake step's `detail.account_ref_claimed`.
- Validation: `body` 5..4000 characters after trimming; `msisdn` any of `07XXXXXXXX`, `01XXXXXXXX`,
  `+2547…`, `2541…` (spaces and dashes allowed; ASCII digits only -- a fullwidth or Arabic-Indic digit is
  a 422, or one number would have many spellings), stored as E.164 and shown only masked
  (`+254 7•• ••• 412`); `subject` at most 90; `channel` one of the five.
- A missing or invalid file under `config/support/` makes every route answer **503** naming the file.
- With `AUTH_DISABLED=true` and `NOC_ENV=production` none of the routes is registered (the codebase's
  production guard for personal data, as for the confidential complaints lane).

- **Public-form reversals (policy, 2026-10-04).** The form cannot prove the caller owns the number, so a
  `reverse_mpesa` request runs automatically only when the customer's own message supplies the
  10-character transaction code and the transfer is within limits; a reversal asked for without a code
  goes to a person with `needs_verification`. Refunds and bundle re-credits still run automatically (they
  only ever credit the caller's own number), but the reply must never reveal anything the caller did not
  type (no codes, amounts, charge descriptions or bundle names). Implemented in `support/actions.py`
  (`needs_verification`) and `support/desk.py`; the case records no tool call at all.

**RBAC** -- "the operations floor" is `api/deps.SUPPORT_READERS`: `OPERATIONS` plus `management`.
The vendor roles, `planning` and `legal` are out: a complaint is a customer's personal data.

**Resolver (refinement)** -- "grounded" means the article reaches `grounding_threshold` (BM25, 4.0)
**and** agrees with triage: it is in the category triage chose, or scores `cross_category_factor` (2x)
the threshold, **and** at least two distinct query terms matched it (one shared word, "stuck" read as
"pending", is a coincidence, not an answer; escalate-only articles are exempt). Citations are exactly the
articles the reply came from: one for the issue answered, plus one per secondary issue answered beside it
(multi-issue, below). An article marked
`escalate` in the knowledge base (`KB-SIM-SWAP-FRAUD`) is never answered from: landing on it adds the
fraud flag, so a fraud complaint in words triage missed still reaches a person. Two more readings
(2026-10-04): among grounded articles, one in triage's category is answered from unless a cross-category
one scores `cross_category_factor` times it (two agents agreeing beats one louder one); and a question
about price ("how much is the daily bundle", "bei", "ni ngapi") is answered only from an article that
talks about prices or charges, otherwise `not_grounded`.

**Triage (refinement)** -- the rules stay data in `triage.py`/`text.py`; what changed with the eval work:
common misspellings and SMS/Sheng spellings are folded before anything reads the text (`text.VARIANTS`:
"netwrk", "bundel", "net"); a number is split from its unit ("2gb"); a phrase tolerates a filler word or two
("cant *even* call", "hakuna *hata* bar"); a few phrases carry a negative weight for contrast ("niko na
bundle lakini siwezi browse" is a network complaint, "personal data" is not a bundle, "charged twice for
a bundle" is billing); a denied risk word ("I don't think this is fraud, I typed the number wrong") and
a negated trip ("sijaenda nje ya nchi", "I haven't travelled anywhere") are masked; a 10-character M-PESA
code in the text is evidence for the M-PESA category in itself; "lawyer", "advocate", "court" are a legal flag only beside a cue that action
is meant (not "I paid my lawyer via paybill", not "the Kibera law courts"); fraud is also read from what is
described ("mse flani ameingia M-PESA yangu") through a few regexes; amounts in words parse ("elfu moja na
mia tano"); and the configuration-SMS tool is not used for a how-to question, a manual set-up or a router.

**Multi-issue complaints (refinement, round 2)** -- a message that carries two issues ("Network ya Nakuru
imepotea tangu asubuhi na pia nilitaka kuuliza bei ya roaming") used to tie two categories, drop below the
confidence threshold and go to a person as `low_confidence`. Triage now cuts the text at sentence ends and
at connectors ("na pia", "and also", "also", "pia", "plus", "alafu", "halafu", "then", "kisha", ...), scores
each segment on its own, and merges consecutive segments that agree; a segment is an issue of its own when
its winning category scores at least 2.0 and either a connector introduced it or it is sure of itself
(confidence >= 0.55). Two issues with different categories make a multi-issue complaint. The **primary**
issue is the first one the action agent can act on (an outage in a named town, a bundle to re-credit, a
refund, a reversal with its code), else the one the customer led with; it decides the category, the
confidence (scored on its own words) and the route. The **secondary** issues are retrieved from the
knowledge base in their own category and, when grounded, answered in the same reply ("On your other
question (roaming): ...") with their own citation. When the actionable issue turns out to have nothing to
act on (no refundable charge on the account), the issue the customer led with becomes the primary (step
`lead_issue_first`); when the leading issue has no grounded article but a secondary one has, the secondary
is answered instead of sending both to a person. Risk flags are always read over the whole text, a reversal
without a code still stops at `needs_verification` with nothing answered beside it, and an escalate-only
article hit on a secondary fragment counts as a flag only at overwhelming evidence (2x the threshold):
the whole-text flags are what read fraud in a side clause. Both issues are recorded in the triage step's
`detail.issues` with `primary_issue`, and each secondary retrieval is its own `retrieved_secondary` step.

**LLM** -- implemented: the triage tie-break (top two categories only; an accepted choice lifts
confidence only to the low-confidence threshold, and a money-moving call it leads to is held, above),
with the reg 41(2) transfer record written before the call. Not implemented: polishing the resolver's
reply (the contract's "may").

**Metrics** -- `GET /metrics` defaults to `hours=0` (all time). `escalated` counts cases with a person
now (`escalated`, `in_progress`); `escalation_rate` counts cases that ever went to a person.
`GET /complaints` `counts` are over all the operator's complaints, not the filtered page.

**Evals**

- Each case runs through `process_complaint` on its own in-memory copy of a template database that
  holds the rain-storm scenario's open hub incidents plus the four adjudicated eval-only incidents
  (Westlands, Ongata Rongai, Nyali, Machakos; `evals.EVAL_EXTRA_INCIDENTS`); the live database and hub
  are never touched.
- Reports are stored in the database (`support_eval_runs`, operator-scoped, every run kept);
  `GET /evals/latest` returns the newest. `dataset.version` is the first 12 hex of the sha256 over both
  golden files (`golden.jsonl`, then `holdout_blind.jsonl`).
- An empty denominator is `null` everywhere (`metrics`, `by_split`, `by_category`), and a gate whose
  metric is `null` fails with a `note` (previously `0.0` in `metrics`, which let a split with no safety
  case pass the safety gate).
- A full run's headline (`metrics`, `gates`, `passed`, `confusion`, `by_category`, `failures`) is the
  blind **holdout** and `dataset.split` says `"holdout"`; `by_split` carries the metrics of every split
  that ran. `--split dev` makes dev the headline; `--split dev+validation` (the pytest regression gate)
  combines splits and says so in `dataset.split`. A golden file with no holdout cases is judged on all of it.
- A case marked `"contested": true` (an adjudicated exclusion, with the reason in its `note`) is loaded,
  may keep the author's label as written (even a bookkeeping tool), and is never scored;
  `dataset.excluded` counts the contested cases of the headline split.
- `tool` is `null` for a `needs_verification` case: the desk must not plan a reversal it cannot verify.
- `tool_accuracy` counts the action agent's call whatever its status (an over-limit refund that chose
  `issue_refund` chose right); resolution still requires it to succeed.
- Golden line format and its consistency rules (route `human` exactly when `escalation_reason` is set,
  `safety` exactly for the three safety reasons, `tool` for action cases and for tool-driven
  escalations) are in the `support/evals.py` docstring; the loader refuses a line that breaks them.
- CLI: `python tests/eval/support_eval.py [--split dev|validation|holdout|dev+validation] [--compare]
  [--no-failures] [--json out.json] [--golden path]... [--llm]`; `--compare` prints the three splits side by
  side with the regression gate (asserted) and the holdout's gates (reported), plus the top failure kinds
  of each; `--no-failures` is the blind measurement (numbers only).

**Schema** -- five tables (`support_complaints`, `support_steps`, `support_tool_calls`,
`support_messages`, `support_eval_runs`), `SCHEMA_VERSION` 10. They are not yet classified in
`config/retention.yaml`; the retention period for customer complaints is Legal's decision.
