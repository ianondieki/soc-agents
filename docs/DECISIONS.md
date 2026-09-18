# Product decisions on record

Decisions the owner has made that the build spec left open, or that later work must not
quietly reverse. The spec's own decision register uses `D<n>` ids; those are reused here.

---

## D4 — Message language: **English only.** Kiswahili is out of scope.

**Decided by the owner, 2026-09-17: "I don't need Kiswahili here."**

### What this changes

- **No `sw` content is written, reviewed or shipped.** The `translations_pending` blocks in
  `config/templates/*.yaml` stay as they are — honest placeholders recording *why* nothing was
  translated — but they are now permanent, not a to-do.
- **The "Kiswahili template reviewer named" procurement thread is CLOSED.** Spec §8.1 listed it as
  a days-lead-time item owned by the product owner and a gate on any `sw` template reaching
  `APPROVED`. It no longer gates anything.
- **`ALERT_ENVELOPE_V2` no longer waits on a translator.** The only remaining blockers to that
  flag are the `@2` template fixes (GSM-7 SMS body, INC number in the email body) and the shadow
  shift.

### What deliberately does NOT change

The `Language = Literal["en", "sw"]` type in `domain/alerts.py`, the `language` field on
`AudienceSpec`, and the renderer's `sw → en` fallback all stay. Three reasons:

1. **English is already mandatory** (`NocAlert` validates that `content["en"]` exists), so the
   optional second language costs nothing at runtime and changes no rendered byte.
2. Removing the type would touch `domain/alerts.py`, `services/render/`, `services/templates.py`,
   `db/models.py` and five test modules across a suite of 900+ tests — real regression risk, in
   exchange for deleting a `Literal` member.
3. If a Kiswahili requirement ever returns (a CA consumer-protection obligation is the plausible
   route), the seam is there and the decision is reversible by writing content, not by re-plumbing.

**So: the capability remains, the content does not.** Anyone reading `Literal["en", "sw"]` and
assuming Kiswahili is supported should read this entry first.

### Why the placeholders were right in the first place

The templates were drafted with an explicit instruction not to invent Kiswahili, and the drafting
agent declined to, recording its reasoning in each file. Its argument stands on its own merits and
is worth preserving: the internal NOC SMS is half register-specific jargon — *failure domain*,
*est. users*, *ticket notes* — with no settled Kiswahili equivalent in Kenyan telecom practice, and
a wrong word reaches an engineer standing at a site at 02:00. **Absent beats invented.** Had it
guessed, this decision would now be a cleanup job instead of a one-line scope reduction.
