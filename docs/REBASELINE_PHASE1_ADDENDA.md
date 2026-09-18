# Phase 1 re-baseline addenda — changes to existing tests NOT in the §2.1 register

**Read this before accepting Phase 1.** The build spec keeps an enumerated register of the only
permitted changes to baseline assertions (§2.1, rows R1–R8). Guardrail G1 says any change to a
pre-existing test must cite the register entry it implements.

Four changes to existing tests in Phase 1 **cannot cite one**, because the register does not cover
them. None of them weakens a test. Each is written up here so it can be reviewed on its merits
rather than discovered later in a diff.

**This is itself a finding: the register is incomplete.** §7.0.9 mandates the G13 subscription
guard, and §7.0.2/§5.3.9 mandate moving the Excel append out of the LEDGER node — but §2.1 lists
no consequence for the tests that pin the old behaviour. The spec contradicts itself here. It is
not a licence to edit tests freely: these four are enumerated, justified and bounded, exactly as
R1–R8 are.

---

## A1 — `tests/unit/test_llm_client.py::test_enabled_with_key_and_sdk_builds_client_with_timeout_and_retries`

**Mandated by:** §7.0.9, the G13 subscription guard.

| | |
|---|---|
| **Old** | Sets `ANTHROPIC_AUTH_TOKEN`; asserts a client is built; asserts `last_kwargs == {"timeout": 20.0, "max_retries": 1}`; asserts the key is **absent** from the kwargs, with the comment *"the SDK reads the env itself"* |
| **New** | Sets `ANTHROPIC_API_KEY`; asserts `last_kwargs == {"api_key": KEY, "timeout": 20.0, "max_retries": 1}`; asserts the key is absent from `llm_status()` |

**Why both halves of the old assertion are now deliberately wrong:**

1. *A bare auth token no longer builds a client.* A Claude subscription token licenses a human
   using Claude Code to build this repository. It does **not** license this application to call
   the API at runtime — that needs a Console API key
   (https://code.claude.com/docs/en/legal-and-compliance). Before Phase 1,
   `credential_present()` accepted `ANTHROPIC_AUTH_TOKEN` unconditionally and
   `LLM_ALLOW_AUTH_TOKEN` was referenced **nowhere in `src/`**, so the documented guard did not
   exist. The old test pinned that hole open.
2. *The key is now passed explicitly.* Letting the SDK read the environment is precisely what G13
   forbids, because the SDK can then fall back to an OAuth profile on disk. Passing `api_key=`
   explicitly is the mechanism of the guard, so it must appear in the kwargs.

**Nothing was weakened:** the timeout/retry defaults this test exists to pin are unchanged, and the
"secret must not leak" intent is preserved — retargeted from the kwargs (where it now legitimately
belongs) to `llm_status()`, which is served unauthenticated and is where a leak would actually
matter.

**Coverage added, not removed** — two new tests pin the guard in both directions:
`test_a_bare_subscription_auth_token_is_refused` and
`test_auth_token_is_honoured_only_behind_the_explicit_flag`.

## A2 — `tests/unit/test_llm_client.py::test_client_is_memoised_until_the_settings_change`

**Mandated by:** §7.0.9 (same guard).

| | |
|---|---|
| **Old** | `assert first.kwargs == {"timeout": 9.0, "max_retries": 2}` |
| **New** | `assert first.kwargs == {"api_key": KEY, "timeout": 9.0, "max_retries": 2}` |

One key added. The memoisation behaviour this test exists to prove — same client returned until a
setting changes, cache never overriding the switches — is untouched.

## A3 — `tests/integration/test_runner_failures.py::test_fail_soft_ledger_error_keeps_the_run_going`

**Mandated by:** §7.0.2 / §5.3.9, moving the xlsx append into the outbox.

| | |
|---|---|
| **Old** | `monkeypatch.setattr("noc_agents.agents.ledger.write_excel_row", boom)` |
| **New** | `monkeypatch.setattr("noc_agents.agents.ledger.ledger_row_cells", boom)` |

**This is a patch-target fix, not a re-baseline.** Every assertion in the test is byte-identical.
The test proves the runner absorbs a raising fail-soft node; it did so by making the node raise,
and the function it patched to achieve that moved out of the node. It now patches the pure render
the node actually calls. Same intent, same assertions, same guarantee.

## A4 — `tests/integration/test_runner_failures.py::test_ledger_agent_absorbs_locked_workbook`

**Mandated by:** §7.0.2 / §5.3.9. **Replaced** by
`test_ledger_node_does_no_file_io_so_a_locked_workbook_cannot_reach_the_run`.

The old test asserted the LEDGER **agent** absorbed a `PermissionError` from the workbook: step
SUCCEEDED, tool `ok=False`, DB row still written. That scenario can no longer occur — the node
performs no file I/O at all, so a workbook open in Excel is structurally incapable of reaching the
run.

**The behaviour is strictly better than what the old test protected.** Previously the error was
absorbed and the ledger line was **lost forever**. The dispatcher now retries it.

Coverage was moved, not dropped, and it is stronger in three places:

- `tests/unit/test_outbox.py::test_locked_workbook_is_the_dispatchers_problem_not_the_runs` blocks
  the workbook, shows the run untouched, then unblocks it and shows the row **actually appended on
  the next drain** — the guarantee the old test could not make.
- The replacement in `test_runner_failures.py` asserts the structural invariant that makes this
  true: the node opens **no `.xlsx` at all** during a full run. If anyone reintroduces file I/O
  into the LEDGER node, it fails loudly.
- The DB ledger row assertion (`ShiftLedgerRow == 1`) is retained in both.

---

## What did NOT change

For the avoidance of doubt, and verified by diff against the pre-Phase-1 snapshot:

- `tests/system/test_contracts.py` — **untouched**, byte-identical.
- The 26 run-scoped WS event literals (`GOLDEN_FULL_HITL` / `GOLDEN_FULL_AUTO`), their types, order
  and per-run `seq` — **untouched**.
- The 12 nodes and 11 edges — **untouched**.
- The `email.sent` payload literal, including the mock detail string — **untouched**.
- Everything in `tests/integration/test_golden_sequence.py` other than the R3/R4 literals recorded
  in `docs/REBASELINE_R3_R4.md` and the R5 spy in `docs/REBASELINE_R5.md`.
