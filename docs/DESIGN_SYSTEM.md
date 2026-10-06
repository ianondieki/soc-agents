# Design system: "Shift light"

The console for a Kenyan NOC floor. This file is the single source for the look; `frontend/src/styles.css`
implements it. Where a page stylesheet disagrees with this file, this file wins.

## The idea

**The console follows the shift.** Day shift works in a lit office: crisp white sheets on a cool mineral
desk, ink-black text. Night shift turns the lights down: graphite (not navy), the same signals glowing a
step brighter. `Auto` (the default) picks Day from 06:00 to 18:59 EAT and Night otherwise; a person can pin
Day or Night from the Display menu. The Wallboard is always Night (it hangs on a TV across the room).

**Colour is a signal, never decoration.** P1 red, P2 orange, P3 yellow, P4 slate. Violet means "a person
decides". Green done, amber watch, red broken. One action colour, Fibre-1 blue, for the primary action,
the current selection and focus. Everything else is ink on paper (Day) or ink on graphite (Night).

**The memorable thing is the fibre ribbon.** A 12-fibre tube is colour-coded (TIA-598): blue, orange,
green, brown, slate, white, red, black, yellow, violet, rose, aqua. Every field engineer on the floor
knows the sequence by heart. The twelve agents wear those twelve colours, in pipeline order, wherever an
agent's identity is drawn (the landing hero, the Workflow map, the Agent observatory). Fibre colours are
identity marks only: never a status, never a fill behind text. White and black fibres get a 1px outline
in the opposite ink so they read on both themes.

| # | Agent | Fibre | Day | Night |
|---|---|---|---|---|
| 1 | Ingest | Blue | `#1F5FD6` | `#5E8BFF` |
| 2 | Correlate | Orange | `#E06A00` | `#FF9433` |
| 3 | Enrich | Green | `#178A4C` | `#3CCB7F` |
| 4 | Severity | Brown | `#8A5A34` | `#B98458` |
| 5 | Ticket | Slate | `#6B7787` | `#94A0B2` |
| 6 | Assign | White | `#FFFFFF` + outline | `#F4F6FA` |
| 7 | Approval | Red | `#D3263F` | `#FF5A70` |
| 8 | Broadcast | Black | `#11151B` | `#0A0C10` + outline |
| 9 | Exec brief | Yellow | `#E2B800` | `#F2D03A` |
| 10 | Shift ledger | Violet | `#7A3FD1` | `#B48CFF` |
| 11 | Recurrence | Rose | `#E0608E` | `#FF8DB5` |
| 12 | Monitor | Aqua | `#0E9AA7` | `#3FD3DE` |

Exported from `frontend/src/lib/fibre.ts` as `FIBRES` (node key to name and CSS variable) so no page
hard-codes them; the CSS variables are `--fibre-1` .. `--fibre-12`.

## Type

One family, two widths, chosen for the job: **Atkinson Hyperlegible Next** (variable, 200 to 800) for all
text, and **Atkinson Hyperlegible Mono** for ticket numbers, site ids, times and measurements. Atkinson was
drawn by the Braille Institute so that confusable glyphs stay distinct: `INC000001`, `SFC-RFT-HUB-NKR`,
`0O`, `1lI` read correctly from a wallboard at four metres and on a tired night shift. Self-hosted from
`frontend/public/fonts` (latin subset, `font-display: swap` with metric-adjusted fallbacks), OFL licences
alongside.

Root size 16px. Product scale (fixed rem, ratio about 1.2):

| Token | Size | Use |
|---|---|---|
| `--fs-xs` | 12px | meta, table captions |
| `--fs-sm` | 13px | dense UI, chips, sidebar items |
| `--fs-md` | 14px | body in panels, table cells |
| `--fs-base` | 16px | reading text, form fields |
| `--fs-lg` | 18px | panel titles |
| `--fs-xl` | 24px | page titles (weight 700, tracking -0.015em) |
| `--fs-2xl` | 32px | big figures |
| `--fs-display` | clamp(2.75rem, 5.6vw, 5.25rem) | landing hero only (weight 800, tracking -0.035em) |

Weights: 400 body, 500 UI labels, 600 emphasis and panel titles, 700 page titles, 800 landing display.
On Night, body text gets +0.005em tracking. Numerals in tables and figures are `tabular-nums`. Sentence
case everywhere. No all-caps labels, no eyebrow kickers above headings, no middle-dot meta strings
(use commas or separate elements), no arrows appended to button text.

## Colour tokens

Semantic names only; a page never uses a raw hex. Both themes define every token.

| Token | Day | Night | Role |
|---|---|---|---|
| `--bg` | `#EEF1F4` | `#101318` | page canvas |
| `--surface` | `#FFFFFF` | `#171B22` | panels, cards, tables |
| `--surface-nav` | `#F7F8FA` | `#13161C` | sidebar, top bar |
| `--surface-raised` | `#F2F4F7` | `#1F242D` | hover rows, inputs, segmented controls |
| `--surface-sunk` | `#E6EAEF` | `#0C0F13` | wells, code, skeletons |
| `--line` | `#DDE2E8` | `#272D37` | hairlines |
| `--line-strong` | `#C5CCD6` | `#363E4B` | control borders |
| `--text` | `#161B22` | `#E8ECF1` | body |
| `--text-strong` | `#0A0D12` | `#FFFFFF` | titles, figures |
| `--muted` | `#4F5968` | `#A0A9B8` | secondary text |
| `--muted-dim` | `#5F6977` | `#8A93A3` | tertiary text (still AA) |
| `--accent` | `#2D52D6` | `#7C9CFF` | primary action, selection, focus |
| `--accent-ink` | `#FFFFFF` | `#0A1440` | text on an accent fill |
| `--hitl` | `#6E35C4` | `#C9A2FF` | a person decides |
| `--ok` | `#0B7444` | `#45D49A` | done |
| `--warn` | `#8A5600` | `#F2BE55` | watch |
| `--danger` | `#C2223B` | `#FF6B7D` | broken |
| `--p1` / `--p1-ink` | `#D91E3E` / `#FFFFFF` | `#FF4D6A` / `#1A0005` | priority pills |
| `--p2` / `--p2-ink` | `#E07000` / `#1A0F00` | `#FF9F1A` / `#1A0F00` | |
| `--p3` / `--p3-ink` | `#E8C200` / `#1A1500` | `#F0D000` / `#1A1500` | |
| `--p4` / `--p4-ink` | `#7A889C` / `#0A1220` | `#8FA0B8` / `#0A1220` | |

Each state colour also has `-soft` (tinted fill, about 10 to 14 percent), `-line` (tinted hairline) and
`-text` (text on its soft fill) variants, derived per theme with `color-mix(in oklab, ...)`. Every pair
above was checked at AA (4.5:1 for text) on `--bg`, `--surface`, `--surface-nav` and `--surface-raised`.
Older token names in the stylesheets (`--text-bright`, `--surface-2`, `--surface-3`, `--panel`, ...)
stay as aliases of the new ones so nothing breaks; new code uses the new names.

The theme is `data-theme="day" | "night"` on `<html>`, set before first paint by an inline script in
`index.html` (reads `localStorage["noc.theme"]`: `auto` | `day` | `night`; auto resolves by EAT hour), and
re-evaluated every minute while on auto. `color-scheme` follows the theme so native controls and
scrollbars match.

## Shape, depth, motion

- Radii by hierarchy, not one radius everywhere: 14px page-level panels, 10px inner blocks and inputs,
  8px buttons, 999px chips and pills only.
- Panels are a hairline step above the canvas: no shadow at rest. Shadows only on things that float
  (menus, flyouts, the phone menu, toasts): `0 12px 32px -12px` with a soft blur, tinted from the theme.
- Motion answers a person: 160 to 220ms, `cubic-bezier(0.2, 0.8, 0.2, 1)`. Disclosure opens animate
  `grid-template-rows: 0fr to 1fr`. The landing hero has the one authored moment (the pulse along the
  fibres). `prefers-reduced-motion` and Quiet mode stop all of it.
- Browser surfaces are themed: `::selection`, caret, scrollbars, focus ring (2px accent, 2px offset),
  link underline offset.

## The shell

```
Desktop >= 1100px                         Tablet 761-1099px          Phone <= 760px
+----------+---------------------------+  +--+--------------------+  +------------------------+
| K  Kenya | top bar (56px)            |  |K |top bar             |  | K Kenya NOC   [Menu v] |
| NOC      +---------------------------+  +--+--------------------+  +------------------------+
| v Operate|                           |  |O*|                    |  | (menu drops down over  |
|   Mission|  page                     |  |S |  page              |  |  the page: the same    |
|   ...    |                           |  |A |                    |  |  accordion groups)     |
| > Support|                           |  |Q |                    |  |                        |
| > Agents |                           |  |V |                    |  | page                   |
| > Quality|                           |  |P |                    |  |                        |
| > Vendors|                           |  |  |                    |  |                        |
| > Platfrm|                           |  |  |                    |  |                        |
| [<< Collapse]                        |  |>>|                    |  |                        |
+----------+---------------------------+  +--+--------------------+  +------------------------+
```

- **Sidebar groups are dropdowns.** Each group header is a `<button aria-expanded>` with the group's icon
  (accent when it holds the current page), its name in 14px semibold, a count while it is closed and a
  chevron. Its pages hang from a 1px guide line under the icon, their text lined up with the group's name,
  in muted ink; the current page is filled in the accent tint and lights its stretch of the guide line.
  Once a group is open its count moves onto the page it belongs to (Approvals, Support desk). A hairline
  separates the two daily desks (Operate, Support) from the rest. The group holding the current page opens
  itself; the rest remember their state (`localStorage["noc.nav.groups"]`). Several may be open at once.
- **Go to a page.** A quiet search box under the brand (the first icon on the rail) and Ctrl+K / Cmd+K
  anywhere in the console open one dialog with a combobox over every page: it matches the label, the group
  and a few keywords per page (`nav.ts`), the arrow keys move, Enter opens, Escape or the scrim closes and
  focus goes back where it was. Never over the front page, the public support pages or the Wallboard.
- **Collapse to a rail.** The foot of the sidebar has "Collapse sidebar". Collapsed (72px), each group is an
  icon button (the current one tinted, with a 3px accent tab on the screen edge); pressing it (or hovering for 150ms with a fine pointer) opens a flyout menu beside the rail
  listing that group's pages. The flyout is `position: fixed`, closes on Escape, outside click or
  navigation, and returns focus to its icon. Remembered in `localStorage["noc.nav.rail"]`.
- **Phone.** The sidebar is replaced by a "Menu" dropdown in the top bar that opens a sheet over the page
  with the same accordion groups; it closes on navigation and Escape and traps nothing else.
- **Top bar (56px).** Left: the operator ("Safaricom PLC") and one quiet pill track for the state of the
  floor: the live link (a green dot while the stream is up; red words only when it is broken), the autonomy
  level (its meaning on hover), the shift, and the time in Nairobi in Mono ("14:05 EAT", hidden below
  1280px; the autonomy level goes below 1100px). Right: the decisions button ("5 waiting for a decision",
  the count in a violet bubble; the one coloured control, quiet at zero), Guided demo and the **Display**
  menu (Theme: Auto, Day, Night; Quiet mode; Projector) as quiet buttons with no box until hovered, a
  hairline, and the user's initials. Once the page scrolls under it, the bar lifts with a soft shadow. On
  a phone: the mark, the live dot, the decisions count and Menu; the operator and the track open at the
  top of the sheet.
- **The mark** (`components/shell/BrandMark.tsx`, and `public/favicon.svg` with fixed colours): the agent
  dial. A ring, half of it lit in the accent blue (the work the agents do on their own) ending in the violet
  dot where a person decides, with three ascending ink bars for the network inside. The dot is cut out of
  the ring with the colour behind it (`--mark-cut`, set by pages whose header sits on `--bg`).
- Icons: `lucide-react`, 1.75 stroke, 18px in the sidebar, 16px inline. No emoji or unicode glyphs as icons.

## Page anatomy

Every console page: a page head (title, one-line lead, actions on the right), then content. Panels hold
one job each and are never nested inside other panels. KPI strips are one row of figures separated by
hairlines, colour only when a figure means something. Tables: 44px rows, sticky header, tabular numerals,
hover row in `--surface-raised`. Empty states say what will fill the space and offer the action that does.
Skeletons, not spinners, while loading.

**Mission control.** The page head, the seven-figure strip, then the latest alarm's twelve agent steps as
one even row across the panel's width (six a row on a narrow panel, four on a phone: a container query),
with a line under it that says what selecting a step does. Then the two lists a shift acts on, side by
side at two thirds and one third: Live tickets, each row led by its site with the ticket number small
beside it, and Approvals. Then one row of three panels of one height: the agent activity, the recent
runs and the open tickets by region.

**The Incident board.** A toolbar where every filter says how many tickets it holds: a switch for
Open (the default), Restored, Closed and All; the four priorities as buttons in their own colours,
one chosen at a time; the search (ticket number, site code or site name) and the region. The view
lives in the address (`/incidents?state=closed&p=P1&region=NBI_E`), and one line under the toolbar
says what is shown, with "Clear filters". The table is led by the site: its name strong, the ticket
number, site code and domain under it; then region, owner, the status as a pill with a dot (amber
with a vendor, blue with the NOC, green restored, grey closed), how long it has been open (red, with
"past restore SLA", once the restore deadline has gone; "took 3 h" once done) and M-PESA. P1 rows
carry a red edge. On a narrower panel the region and then the owner fold into the site's second line
(a container query), so the table never scrolls sideways; a phone gets one card per ticket.

**Approvals.** Five figures over the page: waiting, P1, P2, nobody has claimed (violet) and the
longest wait (in the watch colour when most cards are past the escalation ladder). From 1360px the
open card takes the left and the queue the right ("Waiting", every card one two-line row, the open
one marked in the accent), kept in view while the page scrolls and scrolling on its own when long;
narrower, the open card sits in the queue between its rows. On the card, what Approve causes is a
soft violet panel over the facts; the drafts and the sticky decision bar are unchanged.

**The Shift desk.** This shift at a glance: its name with a sun or moon, its hours from the operator
config (`shift_hours` on the profile), a bar of how much has gone with the time left and the next
shift, and four figures (logged this shift, open now, P1 and P2 open, approvals waiting). Then the
ledger as a timeline, one group per shift and date: the time, a dot in the priority's colour on one
line, the site (a link to the ticket on the Incident board) with its status, and the number, code,
region, owner and M-PESA flag under it. Beside it (above it below 1100px) the handover to the next
shift: three numbered steps and when it is due, "Prepare the handover"; once prepared, where it is
(waiting in Approvals, with a link to that card), the watchlist and the email text. "Download the
ledger" gives this shift's workbook.

**The Agent observatory.** Five figures (agents, steps run, steps failed, waiting for a person, alarm to
ticket median), then the twelve agents as cards in pipeline order (two columns on a wide screen) beside
the live runs, which stay in view as the page scrolls. A card: the agent's fibre marks (one per step it
runs, identity only), its name and steps, its mission, four small figures (steps, average, failed, last
step), what an error does (stops the run, or fails only its step), its connections (read only, or how
many can write with approval) and how many are hosted abroad. "Tools and data" opens its own tools, each
declared connection (access, maturity, where it runs, why) and what it may and may never see; an open
card takes the whole row.

**The Workflow map.** Four figures (steps per alarm, minutes by hand per alarm, the agents' median alarm
to ticket, alarms through so far), then the twelve steps on one line in six phases (take the alarm in,
work out what it is, open and assign the ticket, a person decides, tell people and keep the record,
follow up). Step numbers are neutral rings: nothing is lit, because a lit step would show work that
never ran. Each step names its agent behind its fibre colour, says what it does, and gives the minutes
by hand, the runs and the average. The approval step is the one tinted violet, with what the autonomy
level lets through on its own.

**Problems.** Four figures (open problems, sites, faults behind them, the commonest cause), then one
card per problem: its number and state (open amber, monitoring blue), the site by name with its code
and region, how often it failed (a big number and a dot per fault, up to eight), the cause said as a
sentence behind its icon ("Power failed 3 times here in 30 days.") and "See its tickets", the Incident
board filtered to that site. With none, the rule as a picture (three dots, an arrow, "problem") and how
to make one in the demo.

**Post-incident reviews.** Four figures (reviews, waiting for a reviewer, published, time to restore
median), the status switch with a count on each, then the list: the site leads, the ticket number and
priority under it, the status as a pill (draft amber, in review violet, published green), subscribers,
time to restore (and the adjusted figure when time was stopped) and who signed, a review nobody signed
saying "Not signed yet" in amber. On a phone each review is a small card: the site and status, then
three figures. A lane that is switched off (reviews, maintenance, scorecards, contracts) shows one
panel: a power-off icon, "Switched off", the lane's name and the flag that turns it on.

**Regions.** Five figures (on alert, on watch, open tickets, past restore SLA, open problems), then one
card per region in the profile's order. A card on alert carries a red rail down its left edge and one on
watch an amber rail; calm and stale cards none. What is open is two figures (open, past SLA, amber only
when not zero) and one bar split by priority, with a legend naming only the priorities that have tickets;
nothing open is one phrase. Then repeat faults, open problems (a link to Problems) and each problem on a
line: its number, the site, how many times and when last.

**Vendor scorecards.** Five figures (cards, released to vendors, waiting for a person, withheld by the
gate, the latest month), a filter bar (vendor, month, status, Refresh) and, for a shift supervisor and
above, "Compute an ended month". Then the cards as rows: the vendor by name with its code and month, the
status as a pill with its words (draft grey, shadow violet, withheld red, published and final green), the
data-quality gate, the terms ("Default terms, no contract" in amber) and the dispute window. A row opens
the card under the list: its state and summary, the terms notice, four facts (gate, shadow review,
published or disputes close, computed), "What a person does next" (each act a labelled field above its
button) and the 22 lines by KPI in seven columns, the "All priorities" line in bold. The dispute control
is in each line's own panel, switched off with its reason, not a disabled button on every row.

**Contracts.** Four figures (contracts this role may see, clauses indexed, how answers come, whether
clause search is ready) and one plain sentence on how an answer is made. Then the two ways in, ask a
question and search the clauses (the best eight first), beside the contracts as cards: the title with a
"Sample" tag when it is one, the counterparty by name, in force from, version, clauses, size, who may read
it and whether the hosted model may.

**Maintenance.** Five figures (windows ahead, waiting for sign-off, scheduled, tasks due in 30 days or
missed, the next window). Each window is a row: a calendar leaf (weekday, day, month), the site or region
by name, when and for how long it takes customers off air, its sign-off as four steps (proposed, sign-off
asked, signed off, scheduled; done ones ticked green, the next ringed violet), the rain guard's own
verdict, the Authority approval and the customer notice, and its actions. The tasks follow, soonest first:
the work behind its icon, the site by name, the due date with how far off, the status and the proposed
crew; the first ten until asked.

**Audit trail.** Five figures (entries read, tickets, decisions by people, exceptions, the days they
cover), one filter bar (a search, who acted: everyone, agents and jobs, people; which rows: decisions
and exceptions, or every step), then the trail by day ("Today", "Yesterday", "Sat 4 Oct"), one block per
ticket or record. A record whose rows name one site carries the site's name and code. Each actor has a
small icon (an agent, a job, a person) and a person's name is violet. Stored text is never rewritten.

**Settings.** An "On this page" list beside six sections: you in this demo (name and role, with what
the role is for, and Save), autonomy and display as three cards, email with its state as a dot, the demo
alarms (the storm, then one card per preset: the site, the domain, subscribers, region, where it routes,
Inject), the regions as cards and the sites with a finder.

**A ticket.** A way back to the Incident board, then the head: the priority and the site by name, with
the ticket number, the status as a pill, the region and county, the site code and the regional office
under it; M-PESA at risk and a waiting decision on the right. Five figures (open for or took, the restore
deadline with the time left or how late, subscribers, the owner with the field engineer, the decision).
Then two columns: on the left what happened (the title, the likely cause, the impact in words, the
agents' narrative folded, why this priority and why this owner), the agents' run, the customers, the
ticket fields as label over value, and the timeline on a line; on the right the work note (the vendor
update folded), reassign and close, the brief, the stop clock, earlier faults at this site and the
contracts. In one column the work note comes straight after what happened.

**The public forms** (/complain, /track). Beside the form on a wide screen, under it on a phone, one
side note in a customer's words: what happens after you send (we read it at once; an answer, or a person
by a stated time; follow it with your reference; never share your M-PESA PIN), or where a complaint can
be (received, with a person, part of a known outage, answered or fixed). The page widens only while the
note is there, header and footer with it.

**Support desk.** The queue's rows each lead with how the desk handled the case (the resolver, the
action agent, or a person in violet), then the reference and age, the customer's words on two lines, and
a status pill (violet while a person has it) with the category and, for a person, when the reply is due.
The open case has a rail and a tint in the list. The case: its reference and status pill, how and when it
came in, then the customer's words as a quote card, the verdict, the agents' steps, the linked ticket and
the replies as messages (the agents' and a person's each behind its own mark). Before the first eval run
the Evals tab shows the three gates the desk is scored against.

**The Wallboard** (always Night, made for a TV across the room). The head: the mark, "NOC wallboard"
and the operator; on the right the live state as a pill (green dot while live, red words when the wall
has stopped updating, the escalation count beside it at the same size) and the time in Nairobi, big, in
Mono, with the day under it. Under it one slab of five figures (open, P1, P2, approvals waiting, M-PESA at
risk), then one quiet row of notes (a flag most tiles carry, the platform alarms, which take the row when
they fire). The tiles: a priority stripe down the left edge (P1 also tinted), never a loud border all
round; the priority, the ticket number (a long one cut at its start, so the end that differs stays) and
how long it has been open; the site, big; the region, the subscribers (900k) and the owner with the state,
each behind a small icon; the flags as tinted chips. As many whole rows as fit the glass, the last tile
saying how many more and where. Tickets still down come before restored ones.

## Writing

Plain verbs, sentence case, the floor's own words. A button says what happens ("Approve and send",
"Open the ticket"). Errors say what failed and what to do next. Ordinary product copy is fine to write;
no claims about Safaricom or Airtel policy. The product is a demo: "Not an official Safaricom or Airtel
system" stays in the landing footer.
