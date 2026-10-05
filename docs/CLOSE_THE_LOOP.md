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
is about the network, not the case. Only STRONG links exist (`link_strength` site, county,
wide_area or person; a weak region-only match is never linked, see Decisions 35), so nobody is
told "it's back" about an outage that was never theirs. At most
`customer_updates.max_sms_per_number_per_day` (4) loop messages go to one number in 24 hours.

**Wait for a person or send now** (`config/support/policy.yaml` → `customer_updates`), following
the floor's autonomy ladder:

| Autonomy | Waits for a person when the incident is |
|---|---|
| L1 co-pilot | any priority |
| L2 guarded (default) | P1 or P2 |
| L3 conditional | P1 |

plus: any batch with more than `auto_max_recipients` (default 20) numbers waits, whatever the priority.

- **Send now**: one SMS outbox row per number (`kind="SMS"`, `requires_hitl=0`), idempotency key
  `support-restore:{notice_id}:{msisdn_hash}` (one per notice attempt, 7.1; the hash is an
  HMAC keyed by `SUPPORT_HASH_KEY`). The SMS adapter is a mock: nothing leaves the process.
- **Wait**: one `APPROVE_CUSTOMER_UPDATE` card (incident-bound) and the SMS rows enqueued `HELD`
  with `requires_hitl=1` and the card's id. Approve → the card's own rows released to PENDING, the
  complaints marked told. Reject (reason required) = "Not now" (7.1): rows SUPPRESSED, nobody told,
  the notice `held_back` with the reason, and the update can be raised again.

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
address (reuse the desk's limiter: 20 per 10 minutes; the direct peer unless
`SUPPORT_TRUSTED_PROXIES` names it), and by FAILED attempts only: per ref (10 per 10 minutes)
and per number (10 per day), so a customer's own look-ups never lock them out. A body over 4 KB
is refused before it is read, with the same 404.

Answer (customer words only, never an account fact, a staff name, a policy line or an internal
reason code):

```ts
type Tracked = {
  ref: string;
  stage: "received" | "answered" | "fixed" | "with_a_person" | "outage_known" | "restored" | "closed";
  headline: string;            // "Engineers are working on the outage in Kayole"
  detail: string | null;       // "We will tell you when service is back."
  received_at: string;
  reply_due_at: string | null; // set while a person owns it
  outage: { place: string; ticket: string; state: "working" | "restored" | "still_down"; restored_at: string | null } | null;
  timeline: { at: string; text: string }[];   // oldest first, in customer words (7.3)
  messages: { at: string; from: "you" | "us"; body: string }[];  // echo-only: no code or amount they did not type
  can_report_still_down: boolean;
};
```

**Still down.** `POST /api/v1/support/track/still-down` `{ref, msisdn, note?}` (same matching,
same 404, same limits). Allowed when the complaint was told "restored" within the last 72 hours,
or its linked ticket closed or was cancelled without telling them (within 72 hours of the close),
and it has not reported still-down in the last 24 hours; otherwise 409 with a plain sentence that
never promises a message that will not come. Effect:

- the complaint reopens: `status="escalated"`, reason `still_down_after_restore` (new, not a
  safety reason; customer wording: "you told us service is still down, so a person will check
  it"), claim cleared, a reply due within `track.still_down_reply_hours` (4), a customer message,
  our reply "You told us service is still down. A member of our team will check and reply by …",
  a step `followup/still_down_reported`;
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
`support/places.py`) and did **not** link to an open incident (a weak, region-only match does not
link, so it counts here). When at least `threshold`
distinct numbers (default 3) complain about the same place inside `window_minutes` (default
30), and no open surge exists for that place, it opens a surge and one `CONFIRM_POSSIBLE_OUTAGE`
card (`incident_id=NULL`, `entity_type="support_surge"`, `entity_id=surge.id`). Later complaints
about that place join the open surge and update the card's payload (count, last time).

- **Approve** ("Open a ticket"): after the approval commits, `confirm_surge` claims the surge
  (`confirmed` → `ingesting`, a compare-and-set). If a real open incident now covers the place
  strongly, the complaints are linked to it instead (`outcome="linked_existing"`, a note on that
  incident, no synthetic ticket; the card showed it as `covering_incident_number`). Otherwise it
  runs one synthetic alarm through the normal 12-agent ingest (`process_event`, `EventIngest`,
  `outcome="ticket_opened"`):
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
  when service is back.` (sw equivalent). If the ingest fails, the surge goes back to
  `status="confirmed"` with `error` set, and the Outages tab offers "Try again" (a retry needs
  that error, and only one claim wins). A surge whose card was decided without the decision
  reaching it (the desk was off) becomes `stale` and lets the next complaint raise a fresh card.
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
  notice: { state: "none" | "waiting_for_restore" | "sent" | "awaiting_approval" | "held_back";
            card_id: string | null; recipients: number; sent_at: string | null;
            reason: string | null; held_by: string | null; held_at: string | null };  // the last three: held_back only
  from_customer_reports: boolean;     // opened by a confirmed surge (not one linked to an existing incident)
};
```

`GET /api/v1/support/surges`: `{ id, place, region_code, status, origin, complaints, numbers,
first_at, last_at, card_id, incident_id, incident_number, error, decided_by, decided_at, reason,
complaint_refs: string[], parent_incident_number, outcome }[]`, newest first; `status` is
`open | ingesting | confirmed | dismissed | stale`, `outcome` `ticket_opened | linked_existing | null`.
`POST /api/v1/support/surges/{id}/retry` re-runs a FAILED confirm (OPERATIONS).

`POST /api/v1/support/incidents/{incident_id}/customer-update` (OPERATIONS, 7.1) raises the
"service is back" update again; it answers the incident's `customers` payload, or 409 with the
reason (still open, a card pending, nobody left to tell).

`GET /api/v1/support/incidents/{incident_id}/customers` (staff): `{ customers, told, waiting,
still_down, notice: OutageRow["notice"], complaints: { id, ref, msisdn_masked, status,
told_restored_at, still_down_at, closure_reason }[], follow_up: {incident_id, incident_number,
status} | null }` for the panel on the incident page. Staff see masked numbers with the last four
digits (`+254 7•• •• 1234`).

Realtime: `support.customers_told` `{incident_number, count}`, `support.still_down` `{ref,
incident_number, place}`, `support.surge` `{surge_id, place, complaints}`. Card creation goes
through the normal `hitl.created` path.

## 5. Cards on Approvals

`proposed_payload_json`:

- `APPROVE_CUSTOMER_UPDATE`: `{kind:"restore_notice", incident_number, place_summary, recipients,
  languages:{en:n, sw:n}, text_en, text_sw, segments_en, segments_sw, sample:[{ref, msisdn_masked,
  language}] (max 10), restore_source, restored_at, restored_by, restore_note (<= 200 chars),
  incident_status}`. Approve = "Send to {n} customers"; Reject = "Not now" (7.1).
- `CONFIRM_POSSIBLE_OUTAGE`: `{kind:"surge", place, region_code, complaints, numbers, first_at,
  last_at, origin, parent_incident_number, covering_incident_number, excerpts:[{ref, text (<= 140
  chars), at}] (max 6), text_en, text_sw, segments_en, segments_sw, languages:{en, sw}, recipients,
  sample:[{ref, msisdn_masked, language}] (max 10)}`. The texts carry the literal slot `{ticket}`
  until a ticket exists (the covering incident's number when there is one).
  Approve = "Open a ticket and tell them"; Reject = "Dismiss" (reason).

Both are decided by the §9.3 row-2 deciders (`SUPERVISORS`), raiser != approver applies, and both
are classified in `tests/system/test_rbac_matrix.py`.

## 6. Demo

`POST /api/v1/support/demo/seed` also leaves: a few complaints linked to each open storm incident
(including one number that complained twice, for the repeat count), and three complaints from
three numbers about Rongai with no incident, which opens one surge card. Restoring a storm P3 from
the incident page then tells its customers at once; restoring a P2 raises the customer-update card.

## 7. Revision 2 (after the UX review): every side is told the truth

Orchestrator decisions; they override sections 1-5 and the Decisions below where they differ.
They are now folded into sections 1-5 and implemented as written here; Decisions 35-49 record the
choices revision 2 and the security review left open.

**7.1 Reject is "Not now", never "never".** Rejecting an `APPROVE_CUSTOMER_UPDATE` card holds the
update back; it does not silence the customers.
- The held SMS rows are SUPPRESSED, the reason is kept, and the customers stay *waiting to hear*.
- The notice's state is `held_back` (replacing `rejected`) with `reason`, `held_by` and `held_at`.
- The update can be raised again:
  - automatically, when the incident is closed after being held back at restore;
  - by a person, with `POST /api/v1/support/incidents/{incident_id}/customer-update` (OPERATIONS).
    This is allowed when the incident is RESTORED or CLOSED, someone is still untold and no card is
    pending. It follows the same ladder, so it sends now or raises a fresh card, and answers with the
    incident's `customers` payload.
- Idempotency keys therefore carry the notice attempt (`support-restore:{notice_id}:{msisdn_hash}`).
  One message per number per incident still holds through `told_restored_at`: a told number is never
  told again.
- Quick reject reasons on the card ("not restored yet", "wrong place", "wording") are honest under
  this rule. The card says: "Holds the update back. The customers stay waiting, and you can send it
  later from the outage."

**7.2 The approver sees what goes out, and why it is safe.**
- The `CONFIRM_POSSIBLE_OUTAGE` payload adds the exact confirmation SMS:
  - `text_en` and `text_sw`;
  - `segments_en` and `segments_sw`;
  - `languages: {en, sw}`;
  - `recipients` (distinct numbers);
  - `sample: [{ref, msisdn_masked, language}]` (max 10).
- The `APPROVE_CUSTOMER_UPDATE` payload adds the evidence that service is back:
  - `restored_at` and `restored_by`;
  - `restore_note` (max 200 chars, the note the restorer wrote);
  - `incident_status`.
- Masked numbers show the last 4 digits (`+254 7•• •• 1234`) everywhere staff see them, so two
  numbers never look like one.

**7.3 Track never tells a customer something untrue.**
- `outage.state` gains `"still_down"`. Once this customer reports still-down, the strip shows that
  instead of a green "Restored".
- An `answered` network complaint with no linked incident gets the headline "We have passed your
  report to our network team" and the detail "If we find an outage in your area, we will link your
  complaint to it and tell you when it is fixed."
- **Late linking** makes that promise keepable. When a new top-level incident opens through ingest:
  - recent unlinked network complaints are linked to it when they are from the last 6 hours
    (`late_link_hours`), from the same operator, and name a place that matches the incident (the same
    matching as `link_incident`);
  - each gets a step `followup/linked_late`, but no SMS. The restore notice reaches them later.
- Timeline lines are written for customers:
  - "We read your complaint and passed it to the network team", not "Sorted as network";
  - our messages are labelled "Kenya NOC Support".
- The still-down reply window is `still_down_reply_hours` (default 4), not the 24-hour default.
  Wording: "You told us service is still down. A member of our team will check and reply by …".
- Nothing on /track or /complain mentions an SMS the customer never received. Intake sends none. The
  reference is on the page they saw after sending.

**7.4 Smaller additions.**
- The surges list adds `parent_incident_number`.
- The incident customers payload adds `follow_up: {incident_id, incident_number, status} | null`,
  the ticket opened from this incident's still-down reports.
- `OutageRow.notice.state` adds `held_back` (above), and keeps `sent`, `awaiting_approval`, `none`
  and `waiting_for_restore`.

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
3. *Superseded by 7.1:* a rejected notice is `held_back`, not final. Keys carry the notice attempt,
   so a fresh attempt is possible; a re-restore leaves a held-back number alone, the close and a
   person raise it again. The reason is kept on the notice (`reason`, `held_by` = `decided_by`,
   `held_at` = `decided_at`), on the card and as a `followup/restore_notice_held_back` step.
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
   outbox, and the key and the payload hold an HMAC of it (Decision 43).
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
14. *Limits* (revised by the security review, Decision 39). The desk's limiter, shared by both
    Track routes: `track-ip:{address}` counts every request (20 per 10 minutes);
    `track-ref:{operator}:{REF}` (10 per 10 minutes) and `track-msisdn:{operator}:{e164}` (10 per
    day) count only FAILED well-formed attempts. Over a limit is a 429 with `Retry-After` (it
    reveals nothing about the pair). Values in `policy.yaml` (`track`).
15. *A third 409.* A complaint linked to an open incident other than the one it was told about
    (a confirmed surge relinked it), or linked and not told yet, gets "We already know about the
    outage (ticket INC...); we will tell you when service is back." and
    `can_report_still_down=false`. Revision 2 adds two more refusals that promise nothing that will
    not come: an unlinked complaint ("we have not linked your complaint to an outage ...") and one
    whose incident is restored but whose update is not out yet ("we are checking that service is
    back in your area ..."). Every refusal is free of the word "SMS".
16. *"Restored" only once told.* `outage.state` is `restored` only when this customer was told about
    this incident; a restore still waiting on a card, or an inferred one, reads `working`, so the
    Track page never says "it's back" before the message does; and once they say still down it reads
    `still_down` (7.3).
17. *Stages*, first match: `with_a_person` (a person owns it: escalated, awaiting approval, in
    progress, a still-down report included), `restored` (told, closed by the restore),
    `closed` with "tell us if it is still down" (the ticket closed or was cancelled without telling
    them, Decision 40), `outage_known` (linked, not told; "we are checking that service is back"
    while the incident is RESTORED), `closed`, `fixed` (resolved by a person or fixed by a tool),
    `answered` (an unlinked network complaint: "We have passed your report to our network team"),
    `received`. The headline and detail are fixed customer sentences (in `tracked()`);
    a with-a-person detail gives the customer-facing reason (`escalation.customer_facing`, so an
    account-derived reason reads as the generic account review) and the reply-due time.
18. *Timeline* is built from public facts only, in customer words (7.3): received, "We read your
    complaint and passed it to the network team" (network) or "We read your complaint", linked
    (ticket number; also a late link), passed to a person, a person replied, service restored and
    "We told you service is back and closed your complaint" (only when told), still down, outage
    confirmed (ticket number), closed. *Messages* are the customer's words ("you") and our replies
    ("us"), never a staff name, under the echo-only rule (Decision 40). The UI labels "us" as
    "Kenya NOC Support"; the API keeps `from: "us"`.
19. *The still-down report* gives a new reply-due time (`now + track.still_down_reply_hours`, 4,
    per 7.3), records the customer's note (or "Service is still down in {place}.") with
    `channel="web"`, adds our reply "You told us service is still down. A member of our team will
    check and reply by …", clears the claim and `closure_reason`, and keeps `told_restored_at`. Its work note is
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
    and its SMS is still the confirmer's approval (who pressed retry is logged). See Decisions 44
    and 49 for the claim and the covering incident.
27. *Fields the contract does not name.* `site_type="BTS"`, no `users_affected` (the severity
    agent decides; typically P4), times in the operator's timezone, "at 14:05" when the first and
    last complaint share a minute, "1 customer" in the singular.
28. *Telling after a confirm.* One SMS per number (`support-confirmed:{incident_id}:{msisdn_hash}`,
    `requires_hitl=1`, approved by the confirmer, within the daily cap). The complaints are linked
    with `link_strength="person"` and keep their status; they now wait
    to hear about the new ticket and are told when it is restored. The Kiswahili text ("Tumethibitisha
    hitilafu ya mtandao {place} (tiketi {INC}). Wahandisi wanalishughulikia; tutakujulisha huduma
    itakaporejea.") is ours and wants a native speaker's review.

**Measuring (section 4)**

29. *People, not complaints.* `told`, `waiting_to_hear` and the outage counts (`customers`, `told`,
    `waiting`, `still_down`) are distinct numbers per incident; `repeat_contacts` is the complaints
    beyond each number's first about an incident. Who was told, and who said still down, come from
    the history (Decision 48), so a relink never takes an earlier tell away.
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
    read-only) maps to it STRONGLY. With link strength (Decision 35) the storm's P3 ring node is
    reached by "Nairobi East", its own site name, so it is seeded like the others; the call-centre
    path (two complaints that name no place, linked by a recorded `human/linked_incident` step,
    `link_strength="person"`) remains for an incident no typed place reaches. The numbers are
    stable per incident, so a re-seed inside two minutes still dedupes.

**Revision 2 and the security review**

35. *Link strength (MAJOR 1 and 2).* `link_incident` grades each open top-level incident against the
    place named: `site` (the place is in the SITE NAME; a title no longer counts, because titles
    carry the region's label), `county`, `wide_area` (same region, and the incident is a HUB or CORE
    site or has a child site down), or `region` (same region, a single site's outage). Only the first
    three link on their own; `person` is a staff link or a surge's confirmer. The strength is
    stored on the complaint (`support_complaints.link_strength`, schema v12). A region-only match is
    NOT linked: the tool returns it as `nearby_incident` (in the step detail), the reply adds "There
    is a known outage nearby (ticket X); your report is with our network team, and if it turns out to
    be the same outage we will link your complaint to it.", and the complaint counts towards surges.
    The flagship still holds: "hakuna network huku Kayole" links to the Embakasi East Aggregation
    HUB as `wide_area`. The eval is unchanged (below).
36. *Late linking (7.3)* runs on `POST /events`, `POST /events/batch` and the demo storm, after the
    ingest commits and only when THIS call created a new top-level incident (a merged duplicate or
    a cascade child adopts nobody). It takes the same strong matching (`tools.best_link`): a recent
    complaint is linked only when the new incident is now the best strong match for its place. A
    confirmed surge's new ticket late-links too. A failure is logged; the ingest's answer is
    unaffected.
37. *Raising again (7.1).* The close raises a held-back update automatically (a re-restore does
    not); `POST /incidents/{id}/customer-update` raises it on demand under the desk's write lock,
    with the reasons for a 409 in plain words. A number told about the incident (by the history) is
    never told again, and a number with an SMS behind a pending card is never put on a second card.
38. *Card evidence (7.2).* `restore_note` is the note the restorer wrote on that action (the restore
    note, the "mark restored" note, or the close's summary), else the latest restore note, else the
    resolution summary, cut to 200 characters; `restored_by` is the incident's `restored_by` (on a
    manual raise or a close with none, the person raising it). The surge card's texts carry the
    literal slot `{ticket}` until a ticket exists, with segments counted on a nine-character number.
    Staff-facing masks show four digits everywhere staff read them (the complaint's staff view, the
    trace from now on, cards, the incident panel, the outbox payload); the customer-facing replies
    and the public view keep the three-digit form the caller typed.
39. *Track walking (MAJOR 3, MINOR 8).* Failed attempts are budgeted per reference and per number
    (Decision 14); a matching pair and a malformed body never spend them. A budget that is spent
    refuses even the right pair until it rolls over (10 minutes per reference, a day per number):
    the alternative, answering a match while refusing a mismatch, would itself tell an attacker which
    guess was right. The address the limits key on is the direct peer unless it is listed in
    `SUPPORT_TRUSTED_PROXIES`, when the rightmost `X-Forwarded-For` entry is used; the run scripts and
    the Makefile start uvicorn with `--no-proxy-headers` (uvicorn's own middleware would otherwise
    rewrite the peer from the header before the app sees it).
40. *Echo-only messages (MAJOR 3) and honest closed tickets (MINOR 3).* On Track a staff member's
    reply is never shown verbatim ("A member of our team replied to your complaint. If you did not
    receive the reply, contact us with your reference."); every other message has each M-PESA code
    and each amount the caller did not type replaced by `[code]` / `[amount]`. "Typed by the caller"
    is the complaint on a self-service channel (web, app, sms, social) and their own Track notes; a
    call-centre complaint was typed by staff, so its codes and amounts are redacted too (bare 4-7 digit
    figures included, there). A complaint whose ticket closed or was cancelled without telling them
    shows "The outage ticket for {place} is closed" / "If service is still down for you, tell us
    below." and may report still down for 72 hours after the close; its work note says "after the
    ticket closed".
41. *The daily cap (MINOR 1, an accepted risk, bounded).* `customer_updates.max_sms_per_number_per_day`
    (4; the orchestrator raised it from 2) counts loop SMS to the number on their way or sent in the last 24 hours (by the HMAC in the
    payload). A number over it is left out of the notice (a `followup/sms_capped` step says why), its
    row is suppressed at approval, or it gets no message at a surge confirm (linked all the same, the
    step says why); the customer stays waiting, and a
    person can raise the update again once the day has rolled over. The default is 4, not 2, because the loop's own path for one
    customer on a bad day is four messages (restored, then the confirmed-outage SMS after a still-down
    report, then the follow-up ticket's restore, with one to spare); at 2 that customer would not hear
    the follow-up restore until the next day, which defeats the feature. Four still bounds what a
    stranger's typed number can be sent. Before a real SMS adapter is switched on,
    web-form numbers must be verified (OTP): startup logs a warning while `SMS_ENABLED=true` with a
    provider other than `mock` and no verification exists.
42. *Stale surges (MINOR 4).* An `open` surge whose card is no longer PENDING or CLAIMED with no
    decision applied (decided while the desk was off) is marked `stale` the next time a complaint
    about the place is observed: it lets go of the place, and its complaints count again towards
    the fresh card.
43. *Keyed hashes (MINOR 6).* Every number hash is HMAC-SHA256 keyed by `SUPPORT_HASH_KEY`, 24 hex
    characters; unset, a fixed development key is used and startup warns. Changing the key only
    changes new keys; the per-number cap counts by the current key.
44. *Retry (MINOR 2).* A retry needs `status="confirmed"`, no ticket and an `error`; the confirm
    claims the surge with a compare-and-set (`confirmed` → `ingesting`) before it ingests, so two
    retries, or a retry racing a confirm, run the ingest once (the loser gets 409). `mark_confirmed`
    confirms only an `open` surge.
45. *Still-down and surges (MINOR 5).* The surge step of a still-down report runs in a savepoint with
    the unique-key retry; a failure is logged, and the report, its message and its realtime events
    stand (the savepoint's rollback would otherwise drop the buffered events, so they are restored).
46. *The body cap (MINOR 7).* `Content-Length` over 4 KB is refused before reading, and the body is
    streamed with the cap, so an oversized body is never buffered. Both answer the same 404 as any
    malformed body (not 413), so size reveals nothing either.
47. *Operator scoping (MINOR 9).* Tested with the other operator's surges, outages, still-down and
    tell steps, update cards and complaints seeded beside ours, including a card of ours whose payload
    names their complaint.
48. *Tell history (MINOR 10).* Who was told about which incident comes from the
    `followup/told_restored` steps (each names its incident) united with the complaint's current
    `told_incident_id`; still-down reports from the `followup/still_down_reported` steps. Nothing
    rewrites a step, so a relink and a second tell never take an earlier tell away.
49. *A covering incident (MINOR 11).* Before a confirm ingests, the place is matched the
    `link_incident` way; a strong open incident covers it, the surge's complaints are linked to that
    incident (`outcome="linked_existing"`, strength `person`), told with its ticket number, and a
    note on it says no new ticket was opened. `from_customer_reports` and `spotted_by_customers`
    count only tickets a surge opened. The card shows `covering_incident_number` whenever one exists
    at raise or update time.

*Eval before and after the link-strength change* (`tests/eval/support_eval.py --compare`,
deterministic): identical on every metric and split -- dev 1.0 / 0.0 / 0.0 / 1.0, validation 1.0 /
0.0 / 0.0 / 1.0 (resolution / wrong escalation / safety missed / triage), holdout 0.75 / 0.25 /
0.0769 / 0.8901, regression gate PASSED; only the timings moved.

