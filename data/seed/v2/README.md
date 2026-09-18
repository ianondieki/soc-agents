# `data/seed/v2/` — the demo seed set (spec §7.0.7)

Loaded by `noc-seed-v2` (`src/noc_agents/scripts/seed_v2.py`). It exists to close
the "nothing to see" gap: §8 makes every phase exit require the feature to be
visible in the UI *with this seed*, so an empty screen is a failed exit.

```
C:\Python313\python.exe -m noc_agents.scripts.seed_v2      # or: noc-seed-v2
```

Running it twice changes nothing. Running it today loads nothing either — and
says so, in full sentences, for the reason in the next section.

## Nothing here has a table yet

The seed files land in Phase 1. Their tables do not:

| file | target table | arrives in |
|---|---|---|
| `vendors.yaml` | `vendors` | Phase 4 (§7.6.1) |
| `sla_terms.yaml` | *(none — §7.6.1 keeps these in YAML)* | — |
| `maintenance_plans.yaml` | `maintenance_plans` | Phase 4 (§7.5.1) |
| `capacity_sample.csv` | `capacity_observations` | Phase 4 (§7.5.1) |
| `contracts/*_sample.md` | `contracts` | Phase 5 (§7.8.1) |
| `contracts/faq.yaml` | `contract_faq` | Phase 5 (§7.8.1) |
| `contracts/golden.yaml` | *(none — retrieval eval set)* | — |

So the loader asks the live database what exists, loads what it can, names what
it cannot and why, and never raises. When Phase 4 lands `vendors`, the same
command starts filling it with no change to this folder. It writes only the
columns the real table turns out to have, so a table that spells a column
differently loads its intersection rather than failing.

## Where the data came from

**`vendors.yaml` is derived, not invented.** Every row's `code` and `contacts`
come straight out of `cfg.msp_contacts` — 11 codes from
`config/operators/safaricom.yaml`, 3 from `config/operators/airtel.yaml` — and
`tests/unit/test_seed_v2.py::test_vendors_are_derived_from_msp_contacts` fails if
the two ever drift apart. Display names use the spelling the same profile
already uses in prose.

Three things in that file are **not** derived and are flagged on every row:
`type` (a first-pass reading of the four-value §7.6.1 enum, with `type_basis`
recording what each guess rests on), `contract_ref` (null everywhere except the
two synthetic samples) and `active_from` (a placeholder so the UNIQUE constraint
has a value — not a commencement date). These want a human before anyone quotes
them.

`sla_terms.yaml`'s `default` block is a copy of `cfg.sla_minutes`; a test pins
that too. The credit shapes are the illustrative ones named in the spec text,
not commercial terms. `maintenance_plans.yaml` uses the §7.5.1 intervals, each
carrying the spec's own warning that the standards behind them are paywalled and
**secondary-sourced** — nobody here has read NFPA 110, IEEE 1187/1188 or TIA-222.

`capacity_sample.csv` is generated, not sampled: 72 rows over three cells and
eight days at three busy hours each, hand-set so the §7.5.3 trigger (≥ 70 % PRB
on ≥ 3 busy hours/day for ≥ 7 days) fires on exactly one cell, misses on one
that crosses the threshold on only four days, and stays quiet on a third.
`busy_hour_at` is naive UTC; 15:00/16:00/17:00 UTC is 18:00/19:00/20:00 EAT.

## The two contracts are fiction

`contracts/egypro_msa_sample.md` and `contracts/tetranet_sla_sample.md` are
**made up**. They are not copies, extracts, paraphrases or summaries of any real
agreement, and they do not describe the terms agreed with any real company.
Nobody signed them and no lawyer saw them. They exist so Phase 5 retrieval has
documents with a realistic *shape* — numbered clauses, response tables, an
escalation ladder, stop-clock conditions, a penalty structure — to index.

Each is marked in six independent places, so no reader can land on one without
seeing it: the filename, YAML front matter (`synthetic: true`,
`is_real_contract: false`, `document_class: FICTIONAL_TRAINING_SAMPLE`), the
stored title, a banner in the first screenful, a banner at the end, and the word
`SAMPLE — FICTIONAL` in **every section heading**, so that a single clause
extracted on its own still announces itself. Real company names appear only in
metadata; the clause text says "the Operator" and "the Service Provider". The
fictional service levels were deliberately chosen to differ from
`cfg.sla_minutes` so they cannot be read as the operator's real targets.
`tests/unit/test_seed_v2.py` asserts all of this and fails if any marking is
removed.

## Deliberately absent

§7.0.7 also lists a KPLC golden PDF, one KMD CAP XML and one Open-Meteo JSON
fixture. They are **not** here, and should not be added by hand. Those are Phase
3 *recorded* fixtures: their value is that they are real captured responses with
the quirks of the real feeds, and a plausible-looking file written from
imagination would be worse than no file, because every parser built against it
would encode a fiction. Capture them from the live sources when Phase 3 starts.

Clause chunking, the FTS5 index and `contract_clauses` rows are likewise absent:
§7.8.3 gives those to `services/contracts.py` in Phase 5, which owns the clause
regex and the `context_header` wording.
