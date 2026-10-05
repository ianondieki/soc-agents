# Close the loop: customers hear back, and their complaints become an outage signal

The Support desk answers a complaint and, for an outage, links it to the NOC ticket and promises
"we'll tell you when it's back". Until now nothing kept that promise, the customer had no way to
check on a complaint, and a burst of complaints about a town with no alarm told the NOC nothing.
This lane closes those three gaps. It is the contract between the backend
(`src/noc_agents/support/loop.py`, `surge.py`, the routes in `api/routers/support.py`, hooks in
`main.py`) and the UI (`/track`, the Support desk's Outages tab, two Approvals cards, a panel on the
incident page, the Regions card). Change it here first.

## 1. Tell customers when it is fixed

**Trigger.** Service is back on an incident when a person says so:

| How the incident moved | Customers told? |
|---|---|
| `POST /incidents/{id}/restore` (`restored_source=SUPERVISOR`) | yes |
| a work note with "mark restored" ticked (`MARK_RESTORED`) | yes |
| an NMS clear (`ALARM_CLEAR`, reserved, no producer yet) | yes |
| a work note whose text merely says "restored" (`VENDOR_NOTE_INFERRED`) | **no**: that is a guess, and a customer told "it's back" when it is not calls again angrier. The outage waits for a confirmed restore or the close. |
| `POST /incidents/{id}/close`, when no notice went out yet | yes |

The hook runs inside the NOC route's transaction, in a SAVEPOINT: a support failure is logged and
rolled back on its own and **never** blocks or rolls back the NOC restore or close.

**Who is told.** Every complaint whose `linked_incident_id` is the incident, same operator, not
already told about this incident. One message per phone number per incident, however many times
that number complained (no spam). Complaints a person already resolved are told too: the message
is about the network, not the case.

**Wait for a person or send now** (`config/support/policy.yaml` → `customer_updates`), following
the floor's autonomy ladder:

| Autonomy | Waits for a person when the incident is |
|---|---|
| L1 co-pilot | any priority |
| L2 guarded (default) | P1 or P2 |
| L3 conditional | P1 |

plus: any batch with more than `auto_max_recipients` (default 20) numbers waits, whatever the priority.

- **Send now**: one SMS outbox row per number (`kind="SMS"`, `requires_hitl=0`), idempotency key
  `support-restore:{incident_id}:{msisdn_hash}`. The SMS adapter is a mock: nothing leaves the
  process.
- **Wait**: one `APPROVE_CUSTOMER_UPDATE` card (incident-bound) and the SMS rows enqueued `HELD`
  with `requires_hitl=1` and the card's id. Approve → rows released to PENDING, the complaints
  marked told. Reject (reason required) → rows SUPPRESSED, nobody told, the reason recorded.

**The message.** In the customer's language (`sw` → Kiswahili, otherwise English), one SMS
segment where possible (count with `services/gsm7.py`), naming only the place the customer typed
(or the incident's area when they named none) and their own reference:

- en: `Service is back in {place}. Your complaint {ref} is now closed. Still down? Tell us at {track_url}`
- sw: `Huduma imerejea {place}. Lalamiko lako {ref} limefungwa. Bado haifanyi kazi? Tuambie hapa {track_url}`

There is no inbound SMS channel (the SMS adapter is a mock), so the message never says "reply": the
Track page is where a customer says it is still down.

`{track_url}` is `/track?ref={ref}` on `SUPPORT_PUBLIC_BASE_URL` (default `http://127.0.0.1:8000`).

**After telling.** The complaint gets `status="closed"`, `closure_reason="service_restored"`,
`told_restored_at`, a message (author `agent`, channel `sms`, the exact text) and a step:
`agent="followup"`, `action="told_restored"`, summary like "Told the customer service is back in
Kayole (INC000004 restored 16:40)". `followup` is a new agent in the trace vocabulary.

## 2. Track my complaint (public)

`POST /api/v1/support/track` `{ref, msisdn}` (POST so a phone number never sits in a URL or an
access log). The pair must match a complaint of this operator; anything else, including a bad
ref, a bad number, another operator's ref, or a malformed body, answers the same **404**
`{"detail": "We could not find a complaint with that reference and number."}`. Rate-limited per
address (reuse the desk's limiter: 20 per 10 minutes) and per ref (10 per 10 minutes).

Answer (customer words only, never an account fact, a staff name, a policy line or an internal
reason code):

```ts
type Tracked = {
  ref: string;
  stage: "received" | "answered" | "fixed" | "with_a_person" | "outage_known" | "restored" | "closed";
  headline: string;            // "Engineers are working on the outage in Kayole"
  detail: string | null;       // "We will tell you by SMS when service is back."
  received_at: string;
  reply_due_at: string | null; // set while a person owns it
  outage: { place: string; ticket: string; state: "working" | "restored"; restored_at: string | null } | null;
  timeline: { at: string; text: string }[];   // oldest first: received, sorted, linked, restored, told, closed...
  messages: { at: string; from: "you" | "us"; body: string }[];  // the customer's own words and our replies
  can_report_still_down: boolean;
};
```

**Still down.** `POST /api/v1/support/track/still-down` `{ref, msisdn, note?}` (same matching,
same 404, same limits). Allowed only when the complaint was told "restored" within the last
72 hours and has not reported still-down in the last 24 hours; otherwise 409 with a plain
sentence. Effect:

- the complaint reopens: `status="escalated"`, reason `still_down_after_restore` (new, not a
  safety reason; customer wording: "you told us service is still down, so a person will check
  it"), claim cleared, a customer message, a step `followup/still_down_reported`;
- a work note on the incident: "Customer CMP-000123 reports service is still down in Kayole
  after the restore (2 of 6 told)", `source="support"`;
- realtime `support.still_down`;
- the report counts towards a surge (section 3) for that place, with `origin="still_down"`
  and `parent_incident_id` set, so two or more still-down reports raise "Possible outage
  in Kayole" for the NOC.

Answer: the updated `Tracked`.

## 3. Complaints as an early outage signal

After each complaint the API or the seeder processes (**never** inside the eval runner),
`surge.observe(...)` looks at network complaints that named a place (the gazetteer in
`support/places.py`) and did **not** link to an open incident. When at least `threshold`
distinct numbers (default 3) complain about the same place inside `window_minutes` (default
30), and no open surge exists for that place, it opens a surge and one `CONFIRM_POSSIBLE_OUTAGE`
card (`incident_id=NULL`, `entity_type="support_surge"`, `entity_id=surge.id`). Later complaints
about that place join the open surge and update the card's payload (count, last time).

- **Approve** ("Open a ticket"): after the approval commits, `confirm_surge` runs one synthetic
  alarm through the normal 12-agent ingest (`process_event`, `EventIngest`):
  - `site_id = "CUST-{REGION}-{PLACE}"` and `site_name = "{Place} (customer reports)"`;
  - `alarm_code = "CUSTOMER_REPORTED_OUTAGE"`, `failure_domain = "UNKNOWN"`, `source = "customer_reports"`;
  - `region_code` from the gazetteer, `parent_incident_id` set when the surge came from still-down reports;
  - `description` reads "4 customers reported no service in Rongai between 14:05 and 14:31; no network alarm".

  Every complaint in the surge is then linked to the new incident, gets a step
  (`followup/linked_confirmed_outage`) and is told, already approved by the person who confirmed:
  `We have confirmed an outage in {place} (ticket {INC}). Engineers are on it; we will tell you
  when service is back.` (sw equivalent). If the ingest fails, the surge keeps
  `status="confirmed"` with `error` set, and the Outages tab offers "Try again".
- **Reject** ("Dismiss", reason required): surge `dismissed`; the complaints stay as they were.
- `GET /api/v1/dashboard/regions` fills the existing `complaint_surge` key (always null until
  now) with `{surge_id, place, complaints, numbers, first_at, last_at, card_id}` for the
  region's open surge, else null. This is first-party data customers sent us about their own
  service, not social media, so it needs no new notice. Cards show counts, places and the
  complaint texts to staff, never phone numbers beyond the masked form.

## 4. Measuring the loop

`GET /api/v1/support/loop` (staff, `SUPPORT_READERS`), query `hours` (0 = all time):

```ts
type Loop = {
  waiting_to_hear: number;            // linked to an incident not yet restored, not yet told
  told: number;                       // told "restored" in the window
  told_median_minutes: number | null; // restored_at -> told_restored_at
  told_p90_minutes: number | null;
  notices_waiting: number;            // APPROVE_CUSTOMER_UPDATE cards pending
  recipients_waiting: number;
  still_down_reports: number;
  repeat_contacts: number;            // complaints beyond the first from the same number about the same outage
  outages_with_complaints: number;
  repeat_contacts_per_outage: number | null;
  spotted_by_customers: number;       // incidents opened by a confirmed surge
  surges: { open: number; confirmed: number; dismissed: number };
};
```

`GET /api/v1/support/outages`: one row per incident that has linked complaints, newest first:

```ts
type OutageRow = {
  incident_id: string; incident_number: string; title: string; priority: string; status: string;
  places: string[];                   // places customers named
  restored_at: string | null; restore_source: string | null;
  customers: number; told: number; waiting: number; still_down: number; repeat_contacts: number;
  notice: { state: "none" | "waiting_for_restore" | "sent" | "awaiting_approval" | "rejected";
            card_id: string | null; recipients: number; sent_at: string | null };
  from_customer_reports: boolean;     // opened by a confirmed surge
};
```

`GET /api/v1/support/surges`: `{ id, place, region_code, status, origin, complaints, numbers,
first_at, last_at, card_id, incident_id, incident_number, error, decided_by, decided_at, reason,
complaint_refs: string[] }[]`, newest first. `POST /api/v1/support/surges/{id}/retry` re-runs a
failed confirm (OPERATIONS).

`GET /api/v1/support/incidents/{incident_id}/customers` (staff): `{ customers, told, waiting,
still_down, notice: OutageRow["notice"], complaints: { id, ref, msisdn_masked, status,
told_restored_at, still_down_at }[] }` for the panel on the incident page.

Realtime: `support.customers_told` `{incident_number, count}`, `support.still_down` `{ref,
incident_number, place}`, `support.surge` `{surge_id, place, complaints}`. Card creation goes
through the normal `hitl.created` path.

## 5. Cards on Approvals

`proposed_payload_json`:

- `APPROVE_CUSTOMER_UPDATE`: `{kind:"restore_notice", incident_number, place_summary, recipients,
  languages:{en:n, sw:n}, text_en, text_sw, segments_en, segments_sw, sample:[{ref, msisdn_masked,
  language}] (max 10), restore_source}`. Approve = "Send to {n} customers".
- `CONFIRM_POSSIBLE_OUTAGE`: `{kind:"surge", place, region_code, complaints, numbers, first_at,
  last_at, origin, parent_incident_number, excerpts:[{ref, text (<= 140 chars), at}] (max 6)}`.
  Approve = "Open a ticket and tell them"; Reject = "Dismiss" (reason).

Both are decided by the §9.3 row-2 deciders (`SUPERVISORS`), raiser != approver applies, and both
are classified in `tests/system/test_rbac_matrix.py`.

## 6. Demo

`POST /api/v1/support/demo/seed` also leaves: a few complaints linked to each open storm incident
(including one number that complained twice, for the repeat count), and three complaints from
three numbers about Rongai with no incident, which opens one surge card. Restoring a storm P3 from
the incident page then tells its customers at once; restoring a P2 raises the customer-update card.
