# Recorded weather fixtures (Phase 3, spec §7.3.7)

Both files were **constructed from the providers' documented response schemas, not captured
live**. Two reasons, both on the record:

1. This machine's Python cannot complete TLS to either API without `truststore`
   (docs/RUNBOOK.md §3: Avast re-signs HTTPS and certifi lacks its root).
2. The suite is zero-network by rule; a fixture captured today would still be replayed
   offline, so what matters is that the *shape* is right, and the shapes come from
   https://open-meteo.com/en/docs and
   https://api.met.no/weatherapi/locationforecast/2.0/documentation.

Each file carries a `_provenance` object saying exactly that; the parsers ignore it.
The numbers are typed to exercise the storm thresholds (20 mm / 6 h, 60 km/h gusts,
1500 J/kg CAPE, WMO 95/96/99, MET `*thunder*` symbols) and are **not real forecasts**.

| file | provider | site (data/seed/safaricom_sites.json) | storm window |
|---|---|---|---|
| open_meteo_westlands_nbi_w.json | Open-Meteo `/v1/forecast` | SFC-NBIW-HUB-WLD, Westlands, −1.27 / 36.81 | 2026-09-17 13:00–20:00 EAT |
| met_norway_kisumu_wny.json | MET Norway `locationforecast/2.0/complete` | SFC-WNY-HUB-KSM, Kisumu, −0.09 / 34.77 | 2026-09-17 15:00–19:00 UTC |

To replace either with a real capture: run the request in `_provenance.request` with
`NOC_USE_TRUSTSTORE=1` (and, for MET, a real `MET_NO_USER_AGENT`), save the body verbatim,
and update `_provenance.captured_live` and the storm window the tests pin.
