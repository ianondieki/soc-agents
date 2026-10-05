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
  and the surge's `parent_incident_id` set to the restored incident, so two or more still-down
  reports raise "Possible outage in Kayole" for the NOC.

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
  - `region_code` from the gazetteer;
  - `description` reads "4 customers reported no service in Rongai between 14:05 and 14:31; no network alarm".

  The new ticket is always TOP-LEVEL (no `parent_incident_id`), so `link_incident` links later
  complaints about the place to it instead of letting them feed another surge. When the surge came
  from still-down reports, the relation to the restored incident is kept on the surge
  (`parent_incident_id`, shown on the card as `parent_incident_number`) and in a work note on BOTH
  incidents: "Opened from customer reports after INC000005 was restored; 2 customers say service is
  still down in Nairobi East" on the new ticket, and the same sentence led by the new ticket's
  number on the restored one.

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

## Decisions

What the contract above leaves open, as built (`support/loop.py`, `support/surge.py`, the routes in
`api/routers/support.py`, the hooks in `main.py`). Each is a default someone may want to change.

**Telling customers (section 1)**

1. *Which notice a close records.* When the close is what tells customers (the incident was never
   restored, or only by `VENDOR_NOTE_INFERRED`), the notice and card carry `restore_source="CLOSED"`.
2. *Re-triggering.* A "mark restored" note triggers only when it moves the incident to
   (`RESTORED`, `MARK_RESTORED`) from anything else, including from an inferred restore. A second
   `POST /restore` runs the hook again; it finds nobody new, because a number is skipped when its
   complaint was told about this incident or when any outbox row exists under its key (sent, held
   behind a card, or suppressed).
3. *A rejected notice is final for its numbers.* The contract's key allows one message per number
   per incident, and the suppressed row holds that key, so a later restore or the close sends them
   nothing. The rejection's reason is kept on the notice, on the card and as a
   `followup/restore_notice_rejected` step on each complaint.
4. *One SMS, every complaint.* The SMS names the number's most recent complaint; every complaint
   that number made about the incident is closed (`service_restored`), gets the SMS in its
   conversation and a `followup/told_restored` step. A tool call still waiting for approval on such
   a complaint is marked superseded.
5. *Language and place.* `sw` gets Kiswahili; `en` and `mixed` get English. A customer who named no
   place gets the incident's area: the region's label ("Nairobi East"), else the county, else the
   site's name.
6. *One segment.* The contract's template, with a gazetteer place and the default base URL, is one
   GSM-7 segment in both languages (tested). The text is never shortened to fit: a long
   `SUPPORT_PUBLIC_BASE_URL` may take two segments, and the card's `segments_en`/`segments_sw` say so.
7. *Where the SMS rows point.* They carry the card's id (`hitl_task_id`) and no `incident_id`:
   the broadcast gate's approve (`outbox.release_held`) and reject (`suppress_held_outbox`) act on
   every HELD row of an incident and would otherwise release or suppress the customer notice. The
   payload names the complaint (id, reference, masked number); the number itself never enters the
   outbox, and the key holds a hash of it (`sha256`, 16 hex characters).
8. *Who raises the cards.* Both cards are raised by `agent:SupportFollowup`, so raiser != approver
   is checked but cannot stop the supervisor who restored the incident from approving the message
   about it. Requiring a second person for "tell customers it is back" would leave customers
   uninformed on a one-supervisor night; say so if that is wanted.
9. *The ladder's edges.* An autonomy level the policy does not list waits for every priority, and so
   does a priority outside P1-P4. The ladder and `auto_max_recipients` live in `policy.yaml`
   (`customer_updates`).
10. *The card's texts.* `text_en`/`text_sw` are a real recipient's message in that language, else
    the template filled with the first recipient's place and reference; `place_summary` names up
    to three places by how many customers named them ("Kayole, Umoja, Donholm and 1 more").
11. *Failure and the savepoint.* The commit hook drops every buffered realtime event on any
    rollback, a savepoint's included, so `main._support_followup` puts the route's own events
    (`incident.closed`) back after a failed savepoint. The outbox drain is armed only after the
    savepoint is released: a drain listener also fires on a savepoint release, and would then wait
    on the route's own write lock.
12. *Desk switched off.* With `SUPPORT_DESK_ENABLED=false` the hook does nothing, an approval or a
    rejection of a customer-update card records the decision only (its rows stay HELD), and a
    surge card's decision does not touch the surge.

**Track (section 2)**

13. *The one 404* also covers: a body that is not JSON or not an object, a missing or non-string
    `ref`/`msisdn`, an empty reference, a number the form would not accept, a body over 4 KB, and
    (still-down) a `note` that is not a string or is over 1,000 characters. The reference is
    trimmed and upper-cased; the number may be typed in any spelling the form accepts.
14. *Limits.* The desk's limiter, under keys `track-ip:{address}` (20 per 10 minutes) and
    `track-ref:{operator}:{REF}` (10 per 10 minutes), shared by both Track routes. Every request
    counts, a malformed one included; over the limit is a 429 with `Retry-After` (it reveals
    nothing about the pair). Values in `policy.yaml` (`track`).
15. *A third 409.* A complaint linked to an open incident other than the one it was told about
    (a confirmed surge relinked it), or linked and not told yet, gets "We already know about the
    outage (ticket INC...); we will tell you by SMS when service is back." and
    `can_report_still_down=false`.
16. *"Restored" only once told.* `outage.state` is `restored` only when this customer was told about
    this incident; a restore still waiting on a card, or an inferred one, reads `working`, so the
    Track page never says "it's back" before the SMS does.
17. *Stages*, first match: `with_a_person` (a person owns it: escalated, awaiting approval, in
    progress, a still-down report included), `restored` (told, closed by the restore),
    `outage_known` (linked, not told), `closed`, `fixed` (resolved by a person or fixed by a tool),
    `answered`, `received`. The headline and detail are fixed customer sentences (in `tracked()`);
    a with-a-person detail gives the customer-facing reason (`escalation.customer_facing`, so an
    account-derived reason reads as the generic account review) and the reply-due time.
18. *Timeline* is built from public facts only: received, sorted, linked (ticket number), passed to
    a person, a person replied, service restored and told (only when told), still down, outage
    confirmed (ticket number), closed. *Messages* are the customer's words ("you") and every reply
    or SMS ("us"), never a staff name.
19. *The still-down report* gives a new reply-due time (`now + sla_hours[urgency]`), records the
    customer's note (or "Service is still down in {place}.") with `channel="web"`, adds our holding
    reply, clears the claim and `closure_reason`, and keeps `told_restored_at`. Its work note is
    written by "Support desk" (`author_role="AGENT"`); "k of n told" counts distinct numbers told
    about the incident, k of them having reported still down (this report included).
20. *`still_down_after_restore` is outside the intake reason codes* (`vocab.FOLLOWUP_REASON_CODES`):
    it has no predicate and no place in the escalation order, and the golden set (which must label
    every intake reason) cannot contain it, because no complaint arrives with it. Its customer
    wording is `track.still_down_reason` in `policy.yaml`.

**Surges (section 3)**

21. *Still-down threshold and window.* "Two or more still-down reports" is two distinct numbers
    inside `still_down_window_minutes` (default 120: everyone gets the restore SMS at once, and the
    ones still without service find out over the next hour or two). Still-down reports also count
    towards the ordinary three-number threshold. A surge is `origin="still_down"` when any opening
    member is a still-down report; its parent is the latest report's incident.
22. *Place and region.* A still-down report from a complaint that named no place counts under the
    incident's area (normalised). A place in several regions ("Nairobi", "Kiambu") takes the
    restored incident's region when that is one of them, else the first in sorted order.
23. *Counted once.* An open surge, or a confirmed one whose ticket could not be opened yet, keeps
    collecting; a complaint any surge already holds never counts towards another (so a dismissed
    surge does not reopen on the next complaint); a still-down report counts by its time, so the
    same complaint's report a day later can count again. One open surge per place is also a unique
    key (`operator_id, open_place`) behind the write lock.
24. *Card excerpts* are the six most recent members; a still-down member reads "Still down after
    the restore: {note}".
25. *No `parent_incident_id` on the ticket* (orchestrator's decision, replacing the first build's
    post-ingest link). The synthetic alarm never names a parent: CORRELATE would merge it into the
    restored incident as a cascade child and open no ticket. And the ticket is not made a child
    afterwards either: `link_incident` skips child incidents (by design, for HUB cascades), so every
    later complaint about the place would miss the open ticket and feed yet another surge. The
    relation lives on the surge (`parent_incident_id`) and in the work note on both incidents
    (section 3), each ending "(confirmed by {name})."; a later complaint about the place links to
    the new ticket and is told when it is restored (tested).
26. *The ingest service.* `confirm_surge` calls `graph.pipeline.process_event` (imported at call
    time), the same service `main` uses, so nothing imports `main`. It runs after the approval's
    commit, in the same request, in its own session; the approval answers `{"ok": true}` either way.
    A failure leaves the card APPROVED and the surge `confirmed` with `error`; the retry reuses a
    ticket an earlier attempt opened (same site, since the decision) instead of opening a second,
    and its SMS is still the confirmer's approval (who pressed retry is logged).
27. *Fields the contract does not name.* `site_type="BTS"`, no `users_affected` (the severity
    agent decides; typically P4), times in the operator's timezone, "at 14:05" when the first and
    last complaint share a minute, "1 customer" in the singular.
28. *Telling after a confirm.* One SMS per number (`support-confirmed:{incident_id}:{msisdn_hash}`,
    `requires_hitl=1`, approved by the confirmer). The complaints keep their status; they now wait
    to hear about the new ticket and are told when it is restored. The Kiswahili text ("Tumethibitisha
    hitilafu ya mtandao {place} (tiketi {INC}). Wahandisi wanalishughulikia; tutakujulisha huduma
    itakaporejea.") is ours and wants a native speaker's review.

**Measuring (section 4)**

29. *People, not complaints.* `told`, `waiting_to_hear` and the outage counts (`customers`, `told`,
    `waiting`, `still_down`) are distinct numbers per incident; `repeat_contacts` is the complaints
    beyond each number's first about an incident.
30. *Windows.* `told` and its timings by the SMS time, `still_down_reports` by the report (each one
    is a step, so repeats count), the repeat and outage counts by the complaint's creation,
    confirmed/dismissed surges and `spotted_by_customers` by the decision. `waiting_to_hear`,
    `notices_waiting`, `recipients_waiting` and open surges are the state now, whatever the window.
    `waiting_to_hear` is literal: linked to an incident not yet RESTORED/CLOSED/CANCELLED.
31. *Timings.* Minutes from the incident's `restored_at` (else `closed_at`) to the first time that
    number was told; median and nearest-rank p90, rounded to 0.1; null when nobody was told.
32. *Notice states.* `waiting_for_restore` while the incident is open or only inferred restored;
    `none` when it is restored or closed and no notice was ever written (the desk was off, or the
    hook failed); otherwise the latest notice's state. `GET /outages` and `GET /surges` take an
    optional `limit` (default 50, at most 200); outages include an incident a complaint was told
    about before a relink.

**Schema, cards and demo**

33. *Schema v11.* New tables `support_notices`, `support_surges`, `support_surge_members`;
    nullable columns `support_complaints.{place, closure_reason, told_restored_at,
    told_incident_id, still_down_at}` and `support_messages.channel`; two indexes. `place` is
    filled at intake for every complaint (the eval runner's too, in its throwaway database). The
    support desk's `Complaint` and message shapes are unchanged, so `channel` is stored, not served.
34. *The demo seed* picks, for each open storm incident, a place the real `link_incident` (asked
    read-only) maps to it. The storm's P3 ring node shares county and region with the far bigger
    Embakasi HUB, so no typed place reaches it: it gets two call-centre complaints that name no
    place, linked by a recorded `human/linked_incident` step in the seeding person's name, and its
    restore SMS names the incident's area. The numbers are stable per incident, so a re-seed inside
    two minutes still dedupes.
