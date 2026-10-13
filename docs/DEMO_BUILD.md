# The public demo build

A copy of the console that runs in any browser with **no server**: every API answer is replayed
from a recording of the real backend. It is what the shareable demo link serves.

## What it is

- `scripts/demo/record.py` runs this repository's FastAPI app in-process on a throwaway SQLite
  file and drives it through its own HTTP routes, the way the console does. Nothing is mocked.
  It records, in order:
  - **history**: a believable recent past under a frozen clock (earlier outages at other sites,
    decided, restored and closed; a recurring Kisumu power fault that opens a problem record;
    contracts, maintenance plans and windows, last month's scorecard, a post-incident review,
    support cases and an eval run);
  - **before the storm**: every console read;
  - **the storm**: the heavy-rain scenario one alarm at a time, with the realtime frames each
    alarm published and every read after it;
  - **after the storm**: every read, including each ticket, review, scorecard, window and case;
  - **presses**: approve, reject or claim each card; a note, a restore, a close or a reassign on
    each open ticket; the shift handover and its card; the monitor check; eight kinds of customer
    complaint with their tracking. Each press is made on its own copy of the database and stored
    as a patch, so the demo can apply presses in any order. What the visitor types (their name, a
    note, a reason, a vendor) replaces a placeholder in the patch.
- `frontend/src/demo/` is the browser side. `install.ts` answers every `/api/…` request and the
  `/ws/ops` stream from the recording; `server.ts` keeps where the demo is (before the storm,
  which alarm, after it) and which presses were made; `patch.ts` reads the patches (the same
  algorithm as the recorder's, checked to rebuild every recorded answer exactly); `clock.ts`
  moves every recorded time to now, so a ticket the storm opened reads "opened a minute ago"
  whenever the link is opened.
- `vite build --mode demo` (`npm run build:demo`) builds the console with hash routes and
  relative asset paths into `frontend/dist-demo`, and adds the line at the top of every page
  that says this is a demo, with **Start over**. A normal build never includes `src/demo`.

The operator profile carries a real company's name, so the public recording names a fictional
operator instead ("Savanna Mobile"; `DEMO_OPERATOR_NAME` changes it, and an empty value keeps the
profile's own name for a private demo).

## Build it

```bash
bash scripts/demo/build.sh          # PYTHON=/path/to/python if the project's venv is elsewhere
(cd frontend/dist-demo && python -m http.server 8090)   # then open http://127.0.0.1:8090/
```

The build writes `frontend/dist-demo/page.html` as well: the same entry page without its
`<html>`, `<head>` and `<body>` wrapper, for a host that adds its own (a claude.ai artifact does).
Any static host works for the folder as it is: it runs from any path.

## What the demo does not do

- Changes the recording has no press for (editing a review, computing a scorecard, a maintenance
  action, a stop clock, a contract question) answer with a sentence saying the demo does not save
  them, in the place each page shows a server error.
- The storm runs once per visit; **Start over** brings back the quiet network.
- A complaint is answered as the closest of the eight recorded ones (a network fault in the Rift,
  Nairobi East or Mt Kenya areas, M-PESA, data, airtime, SIM, or a network fault elsewhere), with
  the visitor's own words and place in it.
- Downloads (the ledger workbook, the board's CSV) are hidden: the demo's host blocks them.
