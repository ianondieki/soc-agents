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
| `reverse_mpesa` | reverses a wrong-number transfer by its 10-character code | within 24h and up to KES 5,000, recipient has not withdrawn; else approval |
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
type EvalReport = {
  run_id: string; ran_at: string; mode: "deterministic" | "llm";
  dataset: { name: string; version: string; size: number; split: "all" | "dev" | "test" };  // split: added
  metrics: {
    resolution_rate: number;        // headline
    wrong_escalation_rate: number;  // headline
    missed_escalation_rate: number;
    safety_missed_escalation_rate: number;  // added: the metric the safety gate reads
    escalation_reason_accuracy: number;     // added: right person for the right reason
    containment_rate: number;
    triage_accuracy: number;
    routing_accuracy: number;
    grounded_answer_rate: number;
    tool_accuracy: number;
    p50_ms: number;
  };
  gates: { metric: string; op: ">=" | "<=" | "=="; threshold: number; value: number; passed: boolean }[];
  passed: boolean;
  confusion: { labels: ["resolver", "action", "human"]; matrix: number[][] };  // rows = expected, cols = actual
  by_category: { category: string; n: number; resolution_rate: number | null;     // null: no case to measure
                 wrong_escalation_rate: number | null; triage_accuracy: number | null }[];
  failures: { case_id: string; text: string; kind: "wrong_escalation" | "missed_escalation" | "wrong_route" |
              "wrong_category" | "wrong_article" | "wrong_tool" | "unresolved";
              expected: Record<string, unknown>; actual: Record<string, unknown> }[];
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

Default gates: resolution rate >= 0.80, wrong-escalation rate <= 0.10, missed-escalation rate on safety
cases == 0, triage accuracy >= 0.85. The golden set (`tests/fixtures/support_eval/golden.jsonl`) mixes
English, Kiswahili and Sheng code-switching, as complaints on a Kenyan network actually arrive.

## Decisions the contract left open

Recorded by the backend implementation (`src/noc_agents/support/`); each is a reading of the
contract above, not a change to it, except where marked **refinement**.

**Routes and statuses**

- `route` on a stored complaint is where the case **ended**: `resolver` (answered), `action` (a tool
  fixed it) or `human` (a person owns it). Triage's first choice is in the triage step's `detail.route`.
  This is the route the eval compares with the golden label. A case a person approved stays `human`
  with status `action_taken`.
- When the action agent cannot identify a safe target (no transaction code and no amount matching
  exactly one transfer, no bundle that expired early, no refundable charge, no open incident for the
  place named) the **resolver answers instead** (`answered`, route `resolver`). `tool_failed` is kept
  for a target that was identified and then refused or failed (already reversed, no account on record,
  a second re-credit inside 30 days).
- A call over its limit is recorded as `needs_approval` and the case is `awaiting_approval`. When an
  earlier rule outranks it, or a later non-tool rule fires (repeat, angry high-value, low confidence),
  a call that *would* have succeeded is **held, not run**: the person who now owns the case decides.
- Approving re-runs the tool with the approval (validation and idempotency still apply). If it can no
  longer complete (for example the transfer was reversed on another ticket) the call is `refused` and
  the case goes back to `escalated` with `tool_failed`. Resolving a case supersedes (rejects) any call
  still waiting for approval.
- Nothing sets `closed` yet: the contract defines no route for it.

**Intake and the public form**

- An identical complaint (same MSISDN, same normalised text) within 2 minutes returns the complaint
  already on file with **200** instead of 201; nothing new is created.
- Rate limit: 5 complaints per MSISDN per 10 minutes (`config/support/policy.yaml`), in-process;
  over it, **429** with `Retry-After`.
- A caller without a support read role (anonymous, once `AUTH_DISABLED=false`) gets the **public view**
  of the detail: same shape, but step `detail` is `{}`, tool `args` `{}` and `result` `null`, and
  `customer.name` / `customer.account_ref` are only what the caller typed. Otherwise the form would tell
  anyone who types a number whose it is and what is in that account. Replies greet the customer by the
  name they gave, never the account holder's.
- Validation: `body` 5..4000 characters after trimming; `msisdn` any of `07XXXXXXXX`, `01XXXXXXXX`,
  `+2547…`, `2541…` (spaces and dashes allowed), stored as E.164 and shown only masked
  (`+254 7•• ••• 412`); `subject` at most 90; `channel` one of the five.
- With `AUTH_DISABLED=true` and `NOC_ENV=production` none of the routes is registered (the codebase's
  production guard for personal data, as for the confidential complaints lane).

**RBAC** -- "the operations floor" is `api/deps.SUPPORT_READERS`: `OPERATIONS` plus `management`.
The vendor roles, `planning` and `legal` are out: a complaint is a customer's personal data.

**Resolver (refinement)** -- "grounded" means the article reaches `grounding_threshold` (BM25, 4.0)
**and** agrees with triage: it is in the category triage chose, or scores `cross_category_factor` (2x)
the threshold. Citations are exactly the one article the reply came from. An article marked
`escalate` in the knowledge base (`KB-SIM-SWAP-FRAUD`) is never answered from: landing on it adds the
fraud flag, so a fraud complaint in words triage missed still reaches a person.

**LLM** -- implemented: the triage tie-break (top two categories only; an accepted choice lifts
confidence only to the low-confidence threshold), with the reg 41(2) transfer record written before
the call. Not implemented: polishing the resolver's reply (the contract's "may").

**Metrics** -- `GET /metrics` defaults to `hours=0` (all time). `escalated` counts cases with a person
now (`escalated`, `in_progress`); `escalation_rate` counts cases that ever went to a person.
`GET /complaints` `counts` are over all the operator's complaints, not the filtered page.

**Evals**

- Each case runs through `process_complaint` on its own in-memory copy of a template database that
  holds the rain-storm scenario's open hub incidents; the live database and hub are never touched.
- Reports are stored in the database (`support_eval_runs`, operator-scoped, every run kept);
  `GET /evals/latest` returns the newest. `dataset.version` is the first 12 hex of the golden file's
  sha256.
- An empty denominator is `0.0` in `metrics` and `null` in `by_category`.
- `tool_accuracy` counts the action agent's call whatever its status (an over-limit refund that chose
  `issue_refund` chose right); resolution still requires it to succeed.
- Golden line format and its consistency rules (route `human` exactly when `escalation_reason` is set,
  `safety` exactly for the three safety reasons, `tool` for action cases and for tool-driven
  escalations) are in the `support/evals.py` docstring; the loader refuses a line that breaks them.
- CLI: `python tests/eval/support_eval.py [--split dev|test] [--json out.json] [--llm]`.

**Schema** -- five tables (`support_complaints`, `support_steps`, `support_tool_calls`,
`support_messages`, `support_eval_runs`), `SCHEMA_VERSION` 10. They are not yet classified in
`config/retention.yaml`; the retention period for customer complaints is Legal's decision.
