# KMD CAP test fixtures — constructed, not captured

**None of these files was fetched from meteo.go.ke.** Each carries a `_provenance:
captured_live=false` comment saying so, and `tests/unit/test_kmd_cap.py` asserts that it does.

Why constructed: the suite is zero-network by rule (spec §7.3.7, §10), and this machine's Python
cannot complete TLS to external hosts without `truststore` (docs/RUNBOOK.md §3), so there was no
honest way to capture a live response while building the lane.

| File | Built from | What it exercises |
|---|---|---|
| `kmd_rss.xml` | RSS 2.0 spec, in the shape §7.3 records for `https://meteo.go.ke/api/cap/rss.xml` | two items; newest `pubDate` 2026-05-07, the date §5.3.13's acceptance criterion names ("stale on 2026-09-16") |
| `cap_wny_heavy_rain.xml` | OASIS CAP 1.2 | `Severe` → storm flag; four `<area>` blocks (Migori, Nyamira, Bungoma, Busia — the only detail taken from §7.3's record of a real KMD document); a Swahili second `<info>` that must not double the county list; `expires` present |
| `cap_nbi_strong_winds.xml` | OASIS CAP 1.2 | `Moderate` → no storm flag; one free-text `areaDesc` naming three counties; counties in several regions; a county (Turkana) in no Safaricom region; **no `expires`** |

Identifiers are deliberately synthetic (`fixture-...`) so neither alert can be mistaken for a
real KMD document. When a real capture becomes possible (a machine with `truststore`, or a
supervised one-off fetch), add it **beside** these with its own provenance rather than replacing
them — these pin the edge cases on purpose, and a real feed on any given day will not contain
all of them.
