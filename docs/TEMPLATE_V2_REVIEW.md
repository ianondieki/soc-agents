# Message templates `@2` — for NOC manager review

**Status: DRAFT. Nobody has approved this. Nothing in it can be sent.**

---

> ## CORRECTION (2026-09-17) — read this before the rest of the page
>
> Part of what this page asks you to approve has already happened, by a different route.
>
> 1. **The SMS em-dash fix has landed on the live path, on the owner's explicit approval.** The
>    `—` in `Owner:… — ticket notes for updates` was replaced with `-` in the code that builds
>    the live message (`services/composition.py`, `services/render/sms.py`), and the incident
>    title built by `agents/ticket.py` was fixed the same way. Today's site-down SMS is
>    therefore already GSM-7: **1 part for a short incident, 2 parts for a long one** (the
>    golden HUB incident is 175 septets and still needs two). That was measured, and it is
>    pinned by `tests/unit/test_validators.py::test_the_live_sms_is_now_gsm7_and_fits_one_segment`
>    and `tests/integration/test_envelope_flag.py`, which now see the SMS pass §6.2 and be sent
>    with the flag on. **Approving the SMS `@2` no longer buys that fix.** §1's SMS row, §3's
>    "three parts become one", §4's "SMS today — 3 parts" and §5's caveat describe the
>    situation *before* that change; the measurements themselves are still correct as
>    measurements of the template rows, which have not changed.
> 2. **`@2`'s remaining value is the EMAIL body.** With the flag on, every email is still
>    refused `email_missing_incident_number`. The four body fields `@2` adds — the INC number,
>    the priority, the region label and the "Next update HH:MM EAT" line — are the only thing
>    still standing between the flag and a clean send. That is what your approval of the EMAIL
>    `@2` is for. (The SMS `@2` text is still correct and still worth approving — see point 4 —
>    but it is now catching the registry up, not fixing the message.)
> 3. **`@1` keeps its em dash.** It was not edited and must not be: it is the recorded history
>    of what actually went out, and a versioned registry answers a wording change with a new
>    version, never by rewriting an approved one. `tests/unit/test_template_v2.py::test_site_down_alert_v1_still_carries_the_em_dash_and_declares_ucs2`
>    fails if anyone tidies it.
> 4. **The live code is now AHEAD of the newest APPROVED template, and the registry must catch
>    up before it is ever wired to rendering.** This is safe today only because the table is an
>    approval *gate* (§7): the words still come from code. On the day the renderers read bodies
>    from the table, an APPROVED version carrying the new `Owner:… - ticket notes` wording must
>    already exist — otherwise every send either reverts to the em dash (if `@1` is served) or
>    is suppressed `no_template`. `tests/unit/test_template_v2.py::test_v1_has_been_superseded_by_the_live_code_and_the_registry_must_catch_up`
>    pins this order and fails loudly if the gap is closed without the version.
>
> Nothing below this box has been rewritten; read it with the four points above in mind.

---

An agent drafted these changes from measured failures. They are sitting next to the wording
they propose to replace, in a shape the sender deliberately cannot see. **Your name is not on
them and must not be added by anyone but you.** This page is here so you can read what would
actually reach an RNIO, a field engineer and an MSP, decide whether it is right, and approve it
yourself if it is.

Read time: about ten minutes. There are three questions at the end that an agent should not
answer for you.

---

## 1. Why anything is changing

There is a feature flag, `ALERT_ENVELOPE_V2`, that switches the NOC onto the new alert pipeline.
It is off today. **The moment it is switched on, every SMS and every email is refused** — not
delayed, refused, with a reason written on the incident. Two reasons, both in the template
wording:

| Channel | Refusal code | In plain words |
|---|---|---|
| SMS | `sms_not_gsm7` | The template contains one **em dash** (`—`), a character phones do not carry in the cheap alphabet. One of them re-encodes the *whole* message and cuts what fits in a message part from 160 characters to 70. |
| Email | `email_missing_incident_number` | The INC number, the priority and the region are in the **subject line only**. The new rules require them in the **body**, and require a "Next update HH:MM EAT" line, which today's email does not have at all. |

`@2` fixes exactly those two things and nothing else. No message is reworded, reordered or
redesigned. Every line the current wording writes is still there, in the same order.

One correction to the row above, because it matters: the em dash reaches the SMS from **two**
places, and the template is only one of them. See §5 — please read it before approving.

---

## 2. What changed, template by template

### `site_down_alert` — the first message out when a site drops

**SMS: one character.** `—` became `-`.

```
  @1   Owner:EGYPRO — ticket notes for updates
  @2   Owner:EGYPRO - ticket notes for updates
```

That is the entire change. Not a word, not a space, not the line order. It is verified
character by character by the test suite, so nobody has to take that on trust.

**Email: two lines added at the top, one added under Owner. Nothing removed, nothing reworded.**

```
  + Incident: INC000123 (P1)
  + Region: Nairobi East
  ...
  + Next update: 14:02 EAT
```

Why the body and not the subject: phone mail clients truncate subjects, several
ticket-to-email gateways strip them, and when an MSP forwards one of our alerts into their own
system the body is what survives. If an engineer can only see one part of the mail, the INC
number has to be in that part.

### `incident_update` and `incident_restored`

**SMS: no `@2` proposed, because none is needed.** Both were drafted with a plain hyphen and
both already carry the INC number and the priority. That is measured, not assumed — a test
re-measures their alphabet on every run, so if a smart quote is ever pasted into one of them
the build fails that day rather than at 02:00 some night.

**Email: the same three added lines as above.** `incident_update` already ended with a
"Next update" line, so it only needed the incident/priority/region line.

### What did **not** change, deliberately

- **`@1` is untouched.** It stays in the table exactly as it was sent, still marked APPROVED,
  em dash and all. That is how the NOC can answer "what exact words went out at 02:14 last
  Tuesday" a year from now. Approving `@2` adds a version; it does not edit or delete one.
- **No Kiswahili.** Owner decision D4: English only. The `translations_pending` notes in the
  template files are permanent records of that decision, not a to-do list, and they were left
  exactly as they are.
- **The email subject lines.** Unchanged.

---

## 3. The measurement: before and after

Measured with `services/gsm7.py`, the same arithmetic the phone network bills by — not by eye
and not by character count. Both incidents are rendered through the real template engine.

### `site_down_alert` SMS

| Incident | Version | Alphabet | Size | **Message parts** | Room left |
|---|---|---|---|---|---|
| P1 HUB, Westlands (`INC000123`) | `@1` | UCS-2 | 136 chars | **3** | 65 |
| P1 HUB, Westlands (`INC000123`) | `@2` | **GSM-7** | 139 units | **1** | 21 |
| P4 site, Naivasha (`INC000124`) | `@1` | UCS-2 | 145 chars | **3** | 56 |
| P4 site, Naivasha (`INC000124`) | `@2` | **GSM-7** | 148 units | **1** | 12 |

**Three parts become one.** Same message, same words, one character different.

*(Why 139 "units" for 136 characters: in the cheap alphabet `[`, `]` and `|` each cost two.
The template uses three of them, so the counter reads three higher than the character count.
This is the kind of off-by-one that makes counting by eye unsafe.)*

### What three parts instead of one costs

**Money.** Each part is billed separately. `@1` costs **3× what `@2` costs**, on every
site-down SMS, to every recipient, forever.

A worked example, with the inputs stated so you can substitute your own:

| | |
|---|---|
| Site-down alerts a month | 100 |
| SMS recipients each (RNIO + FE + MSP + duty) | 4 |
| Messages a month | 400 |
| Parts billed on `@1` | **1,200** |
| Parts billed on `@2` | **400** |
| Parts saved | **800/month** |
| At an assumed KES 0.80 per part | **~KES 640/month, ~KES 7,680/year** |

The per-part rate above is an assumption, not a quoted figure — **please substitute the real
Africa's Talking rate on our account.** The 3× multiplier is not an assumption; it is the
GSM 03.38 standard and it does not vary by supplier.

**Reliability, which matters more than the money.** A three-part SMS is three chances to lose a
part on a congested network, and the parts can arrive out of order. An alert that reaches a
field engineer reading `...est.users 45` because part two never landed is worse than no alert.
A one-part message cannot do that.

### Email

The check run here is the one the new pipeline runs. "Refused" means the mail is not sent.

| Incident | Template | `@1` | `@2` |
|---|---|---|---|
| P1 HUB | `site_down_alert` | **Refused** — no INC number, no priority, no next update in body | **Passes** |
| P1 HUB | `incident_update` | **Refused** — no INC number, no priority in body | **Passes** |
| P1 HUB | `incident_restored` | **Refused** — no INC number, no priority, no next update in body | **Passes** |
| P4 site | `site_down_alert` | **Refused** — the above **plus no region label** | **Passes** |
| P4 site | `incident_update` | **Refused** — INC number, priority, region label | **Passes** |
| P4 site | `incident_restored` | **Refused** — all four | **Passes** |

One detail worth your attention: on the P1 the region check *happens* to pass on `@1`, because
the words "Nairobi East" appear inside the incident title. On the P4 it fails, because "Rift
Valley" does not. That is luck, not design — the same template passing or failing depending on
how a site was named. `@2` puts the region on its own line, so it is the same every time.

---

## 4. What a person would actually receive

Exactly as rendered, not retyped.

### P1 — HUB power failure, Westlands, Nairobi East

**SMS today (`@1`) — 3 parts, UCS-2:**

```
[P1] INC000123 NBIE-HUB-01 NBI_E
POWER|est.users 620000
HUB POWER - Westlands Hub (Nairobi East)
Owner:EGYPRO — ticket notes for updates
```

**SMS proposed (`@2`) — 1 part, GSM-7:**

```
[P1] INC000123 NBIE-HUB-01 NBI_E
POWER|est.users 620000
HUB POWER - Westlands Hub (Nairobi East)
Owner:EGYPRO - ticket notes for updates
```

**Email proposed (`@2`)** — the three `+` lines are the only additions:

```
Subject: [P1] INC000123 | Westlands Hub (HUB) | Nairobi East | Safaricom PLC (demo profile)

+ Incident: INC000123 (P1)
+ Region: Nairobi East
  Service affecting: YES
  Est. users: 620000
  Services: VOICE, DATA, SMS, MPESA_CORRIDOR
  Failure domain: POWER
  M-PESA corridor risk: YES

  Summary: HUB POWER - Westlands Hub (Nairobi East)

  Narrative:
  Westlands Hub (NBIE-HUB-01) lost mains power at 13:41 EAT; the generator failed to start.
  About 620,000 subscribers affected; M-PESA corridor at risk. EGYPRO power desk dispatched.

  Owner: EGYPRO
  Hypothesis: Mains failure; genset starter battery flat. Awaiting MSP confirmation.
+ Next update: 14:02 EAT

  Do not call NOC for routine status — update ticket / wait for next brief.
```

### P4 — rack door alarm, Naivasha Kabati 2, Rift

**SMS proposed (`@2`) — 1 part, GSM-7:**

```
[P4] INC000124 RFT-CEL-0417 RFT
ENVIRONMENT|est.users 1200
ENV ACCESS - Naivasha Kabati 2 (Rift)
Owner:Rift FE on-call - ticket notes for updates
```

**Email proposed (`@2`):**

```
Subject: [P4] INC000124 | Naivasha Kabati 2 (BTS) | Rift Valley | Safaricom PLC (demo profile)

Incident: INC000124 (P4)
Region: Rift Valley
Service affecting: NO
Est. users: 1200
Services: DATA
Failure domain: ENVIRONMENT
M-PESA corridor risk: NO

Summary: ENV ACCESS - Naivasha Kabati 2 (Rift)

Narrative:
Rack door open alarm at Naivasha Kabati 2 (RFT-CEL-0417); site remains on air.

Owner: Rift FE on-call
Hypothesis: Door contact or physical access; site on air throughout.
Next update: 16:05 EAT

Do not call NOC for routine status — update ticket / wait for next brief.
```

### Update and restore emails (`@2`)

```
Subject: [P1] INC000123 UPDATE 3 | Westlands Hub (HUB) | Nairobi East

Incident: INC000123 (P1) UPDATE 3
Region: Nairobi East
Status: INVESTIGATING
...unchanged...
Next update: 14:02 EAT
```

```
Subject: [P1] INC000123 RESTORED | Westlands Hub (HUB) | Nairobi East

Incident: INC000123 (P1) RESTORED
Region: Nairobi East
Status: MONITORING
Service restored at: 15:30 EAT
Outage start: 13:41 EAT
...unchanged...
Next update: 15:45 EAT
```

---

## 5. What approving this does **not** fix

**Read this before you approve.** Approving `@2` on its own does **not** make today's SMS
messages one-part.

The alert's third line is the incident title, and the title is built elsewhere — in
`src/noc_agents/agents/ticket.py`, which puts an em dash of its own into every single title:

```
[POWER_GRID] HUB POWER — Embakasi East Aggregation HUB (Nairobi East)
                       ^ this one
```

So with `@2` approved and the title untouched, the SMS is still UCS-2 and still three parts.
`@2` removes the *template's* contribution to the problem, which is the part that lives in a
message template and needs your approval. The title is a code change in a different file, owned
by a different piece of work, and it needs to land before the flag is worth flipping.

**Both are needed. Neither is sufficient alone.** This is pinned by a test
(`test_a_dirty_title_still_defeats_v2`) so it cannot quietly stop being true.

---

## 6. Three things an agent should not decide for you

**1. "Next update" on a *restored* notice.** The new rules require a next-update time in every
email body, so `@2` puts one on the restore notice too. But a restore notice promising a further
update may be promising something the NOC does not intend to send. The honest alternatives are
things like `No further updates; PIR to follow` or `Closure note: HH:MM EAT`. The plainest form
is what is drafted. **This is the one line in the whole change worth arguing about.**

**2. The two-part update and restore SMS.** Neither needs a `@2` — they are already in the cheap
alphabet — but both spill into a second part on a long site name:

```
incident_update   @1 →  165 units → 2 parts
incident_restored @1 →  167 units → 2 parts
```

Shortening them is a wording decision (what to drop: the "- ticket notes for updates" tail? the
`est.users` figure?), not a defect fix, so nothing was changed. Say the word and it becomes a
separate `@2`.

**3. The tail itself.** `- ticket notes for updates` costs 26 characters of every single alert.
It reads like an instruction to a system, not to a person at 3am. It was left exactly as it is
because it is the operator's voice, not an agent's, and shortening it is your call.

---

## 7. What approving this unlocks — and what it does not

1. **The wording that clears the email blocker exists, approved, on the record.** The email half
   of the blocker is fully solved by `@2`'s text.
2. **The SMS half is solved as soon as the title fix lands too** (§5 above).
3. **A shadow shift becomes possible.** The spec (decision D3) requires one before the flag
   flips for real: run the new pipeline alongside the old for a shift, compare every rendered
   message, and only then switch.
4. **The SMS bill for site-down alerts drops to a third**, once everything below is in.

### Please do not expect approving this to change a single message yet

This is the most important caveat on the page, and it is easy to miss.

Today the template table is used as an **approval gate only**. It decides *whether* a channel
may send. The actual words that go out are still built in code — `services/composition.py` and
`services/render/*.py` — and the renderer accepts exactly one template, `site_down_alert@1`,
hard-coded. Anything else it is asked for comes back suppressed with `no_template`.

So:

- **Approving `@2` changes no rendered byte.** It records that the wording is approved and makes
  it available. A developer still has to wire the renderers to read bodies *from the table*
  instead of from code.
- **There is a trap in the middle.** If someone points the pipeline at version `2` before that
  wiring exists, every channel is suppressed `no_template` — worse than today, and for a
  confusing reason. The two changes have to land together.

That wiring is engineering work, not a wording decision, so it is not in front of you here. But
it means the order is: **you approve the words → a developer connects the table → the title fix
lands → a shadow shift → the flag flips.** Approving `@2` is the first of those five and it does
not commit you to the rest.

---

## 8. Approving it

### Where it stands right now

- `@2` exists only as a `version_2_draft` block inside `config/templates/*.yaml`. The seeder
  does not read that key, so **nothing from `@2` is in the database and nothing can send it.**
- Every draft says `status: DRAFT` and names **no** approver. There is no `approved_by`, and
  deliberately no `policy:` string standing in for a person either — that convention is only
  used to record "this was already the live wording before the registry existed", and reusing
  it on words nobody has read would manufacture an audit trail.

### Step 1 — promotion (a developer, in a reviewed pull request)

In each of the three template files, replace the channel block's `version:` / `encoding:` /
`params:` / `languages:` with the four keys from its `version_2_draft:` block, and delete the
`version_2_draft:` wrapper. It is a copy, key for key; that is why the draft is shaped like a
channel block. `@1` is **not** deleted from the database by this — the registry inserts the new
version and leaves the old row exactly as it is.

Then re-seed. `@2` arrives as **DRAFT**, still not sendable:

```powershell
C:\Python313\python.exe -c "from noc_agents.config import get_settings; from noc_agents.db.models import init_db, get_session; from noc_agents.services.templates import TemplateRegistry; s = get_settings(); init_db(s.database_url); ses = get_session(); reg = TemplateRegistry.for_config(ses, s.operator); print(reg.sync()); ses.commit()"
```

### Step 2 — the approval (you, by name)

**This is the one command.** Replace the email address with your own — it is recorded against
the template, with the timestamp, and it is what a regulator will be shown:

```powershell
C:\Python313\python.exe -c "from noc_agents.config import get_settings; from noc_agents.db.models import init_db, get_session; from noc_agents.services.templates import TemplateRegistry; s = get_settings(); init_db(s.database_url); ses = get_session(); reg = TemplateRegistry.for_config(ses, s.operator); [reg.set_status(r, 'APPROVED', actor='YOUR.NAME@safaricom.co.ke') if (r := reg.get(c, k, 'en', 2)) else print('no', c, k, '@2 yet - do step 1 first') for c, k in [('SMS','site_down_alert'),('EMAIL','site_down_alert'),('EMAIL','incident_update'),('EMAIL','incident_restored')]]; ses.commit(); [print(f'{x.channel:<6} {x.template_key:<19} @{x.version} {x.approval_status:<9} {x.approved_by}') for x in reg.all_rows()]"
```

Drop any `(channel, template)` pair from that list to approve only some of them — for example,
approve the SMS and hold the restore email back until question 1 in §6 is settled.

To check what a send would now use:

```powershell
C:\Python313\python.exe -c "from noc_agents.config import get_settings; from noc_agents.db.models import init_db, get_session; from noc_agents.services.templates import TemplateRegistry; s = get_settings(); init_db(s.database_url); reg = TemplateRegistry.for_config(get_session(), s.operator); [print(f'{r.channel:<6} {r.template_key:<19} @{r.version} {r.approval_status:<9} {r.approved_by}') for r in reg.all_rows()]"
```

Both commands were rehearsed end to end against a throwaway copy of the database before this
page was written. The registry refuses an approval that names nobody, so there is no way to
approve this anonymously by accident.

### Changing your mind

Approval is reversible: the same command with `'PAUSED'` instead of `'APPROVED'` stops `@2`
being used. Nothing is ever deleted, and the record of who approved what, when, is kept even
after a pause.

What a pause falls back to differs by template, and it is worth knowing before you need it:

- **`site_down_alert`** falls back to `@1`, which is APPROVED. Messages keep going out in
  today's wording — i.e. straight back to the em dash, so with the flag on they would be
  refused again. A pause here is safe but it is not "nothing happens".
- **`incident_update` and `incident_restored`** have **no** approved version to fall back to:
  their `@1` has always been a draft, because nothing in the system composes those messages
  today. Pausing their `@2` means those two messages stop entirely until you approve something.

---

## 9. The evidence behind this page

Every number and every rendering above is produced and re-checked by
`tests/unit/test_template_v2.py`:

```powershell
C:\Python313\python.exe -m pytest -q tests/unit/test_template_v2.py tests/unit/test_templates.py tests/integration/test_envelope_flag.py tests/unit/test_gsm7.py
```

It proves, on each run:

- every `@2` SMS body is in the cheap alphabet, measured by `services/gsm7.py`, and renders in
  one part where `@1` needs three;
- every `@2` email body carries the INC number, the priority, the region label and the
  next-update time, checked by the same validator the pipeline uses — and `@1` fails that check
  on the same data, so the fix is demonstrated rather than assumed;
- `@2` adds lines and removes none: every line `@1` wrote is still present, in order;
- `@1` is untouched — still the same words, still the em dash, still APPROVED — and seeding the
  drafts writes a new row rather than editing the old one;
- `@2` is **not** approved, names no approver, and a send that asks for it is refused;
- the §7 caveat still holds — the renderer accepts only `site_down_alert@1` from code, so on the
  day someone wires the table into the renderers that test fails and this page gets corrected
  rather than quietly misleading the next approver;
- the Kiswahili placeholders and the D4 headers survive untouched.

Each of those was confirmed to fail when the thing it checks is broken (the em dash was put
back, an approver was added, the INC number line was removed — each produced the expected
failure, and the file was restored byte-identical afterwards).
