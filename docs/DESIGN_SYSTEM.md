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

- **Sidebar groups are dropdowns.** Each group header is a `<button aria-expanded>` with the group's icon,
  its name, a count when it has one (Approvals waiting, Support cases with a person) and a chevron. The
  group holding the current page opens itself; the rest remember their state (`localStorage
  ["noc.nav.groups"]`). Several may be open at once.
- **Collapse to a rail.** The foot of the sidebar has "Collapse sidebar". Collapsed (72px), each group is an
  icon button; pressing it (or hovering for 150ms with a fine pointer) opens a flyout menu beside the rail
  listing that group's pages. The flyout is `position: fixed`, closes on Escape, outside click or
  navigation, and returns focus to its icon. Remembered in `localStorage["noc.nav.rail"]`.
- **Phone.** The sidebar is replaced by a "Menu" dropdown in the top bar that opens a sheet over the page
  with the same accordion groups; it closes on navigation and Escape and traps nothing else.
- **Top bar (56px).** Left: the operator ("Safaricom PLC"), its autonomy and shift as plain text, and the
  live state (a muted "Live"; red only when broken). Right: the decisions button ("4 waiting for a
  decision", violet when non-zero), Guided demo, a **Display** menu (Theme: Auto, Day, Night; Quiet mode;
  Projector) and the user's initials. Three buttons and a menu, never seven chips.
- Icons: `lucide-react`, 1.75 stroke, 18px in the sidebar, 16px inline. No emoji or unicode glyphs as icons.

## Page anatomy

Every console page: a page head (title, one-line lead, actions on the right), then content. Panels hold
one job each and are never nested inside other panels. KPI strips are one row of figures separated by
hairlines, colour only when a figure means something. Tables: 44px rows, sticky header, tabular numerals,
hover row in `--surface-raised`. Empty states say what will fill the space and offer the action that does.
Skeletons, not spinners, while loading.

## Writing

Plain verbs, sentence case, the floor's own words. A button says what happens ("Approve and send",
"Open the ticket"). Errors say what failed and what to do next. Ordinary product copy is fine to write;
no claims about Safaricom or Airtel policy. The product is a demo: "Not an official Safaricom or Airtel
system" stays in the landing footer.
