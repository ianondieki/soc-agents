# GloFAS (Open-Meteo Flood API) test fixtures — constructed, not captured

**Nothing here was fetched from flood-api.open-meteo.com.** Each JSON file carries a
`_provenance` block with `"captured_live": false` and the words "NOT a live capture", exactly as
`tests/fixtures/weather/*.json` do, and `tests/unit/test_flood.py` asserts it.

Why constructed: the suite is zero-network by rule (spec §7.3.7), and this machine's Python cannot
complete TLS to external hosts without `truststore` (docs/RUNBOOK.md §3).

| File | Built from | What it exercises |
|---|---|---|
| `glofas_kisumu_wny.json` | the documented `/v1/flood` daily response (`daily.time`, `daily.river_discharge`, `daily.river_discharge_mean`, m³/s) | Kisumu (`SFC-WNY-HUB-KSM`), the catalogue's only riverine site; a flood *pulse* whose peak/mean ratio (~2.63) crosses the §7.3.1 threshold of 2.0; grid-snapped `latitude`/`longitude` that differ from the request |

The values are typed, not modelled. The calm, null, misaligned and zero-mean variants the tests
need are derived from this file inside the tests, so the one fixture on disk stays the one
documented shape.
