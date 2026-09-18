# Runbook — install facts and machine gotchas

Facts established by running things on the real machine, not by reading documentation.
Every entry says **how it was checked** so you can re-check it when something changes.

Reference machine: Windows 10 Pro 19045, Python 3.13.5 at `C:\Python313`, checked **2026-09-16**.

> Use `C:\Python313\python.exe -m pytest -q` from the project root.
> A bare `python` on this machine resolves to an unrelated virtualenv that has no pytest.

---

## 1. MCP install facts (Phase 0 step (f))

**Verdict: GREEN — §7.1.5 decision tree outcome 1. Pin the `mcp` extra; Phase 7 runs the MCP client in-process. The sidecar fallback is NOT needed.**

The spec required this gate before committing the `mcp` extra, because `mcp>=2.2` forces
`pydantic>=2.12` and this suite had only ever run on pydantic 2.10.6.

### What was run

```powershell
C:\Python313\python.exe -m venv C:\Users\PC\.venvmcp
C:\Users\PC\.venvmcp\Scripts\python.exe -m pip install "anthropic[mcp]>=1.6,<2" "mcp>=2.2,<3" "pydantic>=2.12" "pywin32>=311"
# then the project's own runtime + dev dependencies, then:
cd C:\Users\PC\Desktop\second-brain\soc-agents
C:\Users\PC\.venvmcp\Scripts\python.exe -m pytest -q
```

### Resolved versions

| Package | Version |
|---|---|
| mcp | 2.2.0 |
| mcp-types | 2.2.0 |
| anthropic | 1.6.0 |
| pydantic | 2.13.5 |
| pydantic-core | 2.46.5 |
| pywin32 | 312 |

### Result

**341 passed, 0 failed** — byte-for-byte the same outcome as the main environment on
pydantic 2.10.6 (also 341 passed). No pydantic 2.12+ serialisation change affects this
codebase, and no golden literal moved.

| Environment | pydantic | mcp | Result |
|---|---|---|---|
| `C:\Python313` (main) | 2.10.6 | not installed | 341 passed |
| `C:\Users\PC\.venvmcp` (gate) | 2.13.5 | 2.2.0 + anthropic 1.6.0 | 341 passed |

The only diagnostic difference is a harmless
`StarletteDeprecationWarning` about `httpx` vs `httpx2` in the test client.

### Four things that will bite you

**(a) `mcp` 2.2 depends on `httpx2`, not `httpx`.** They are different distributions and
install side by side, so there is **no conflict** with the project's `httpx>=0.28`. The
dependency clash the spec was braced for does not exist. (Starlette does emit
`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead` —
harmless, and unrelated to mcp.)

**(b) `import mcp` works in a venv; it fails under `pip install --target`.** An earlier
attempt installed mcp with `--target` and got `ModuleNotFoundError: No module named 'pywintypes'`.
That is a pywin32 packaging artifact — `--target` skips pywin32's post-install step, which is
what puts `pywintypes` on the path. **It was never a real blocker.** Always use a venv.

**(c) The client API is not what an older memory expects.** In mcp 2.2 the streamable-HTTP
entry point is `mcp.client.streamable_http.streamable_http_client` (snake_case). The name
`streamablehttp_client` that older examples use **does not exist** and raises `ImportError`.
mcp 2.2 also ships a high-level `mcp.Client` class. Verify against the installed package
before writing Phase 7 code:

```powershell
C:\Users\PC\.venvmcp\Scripts\python.exe -c "import mcp.client.streamable_http as s; print([n for n in dir(s) if not n.startswith('_')])"
```

**(d) The Anthropic SDK cannot install under a deep path on this machine.** Windows long-path
support is **not** enabled here (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled`
is unset). The SDK ships

```
anthropic/types/beta/beta_managed_agents_self_hosted_resources_unsupported_deployment_paused_reason_error.py
```

which is 96 characters. Under a deep parent directory the total path exceeds `MAX_PATH` (260)
and pip fails **midway**, leaving a half-written package that then imports as
`ModuleNotFoundError: No module named 'anthropic.types.shared'` — an error that points at the
SDK rather than at the real cause, and the broken package lingers until removed.

*Symptom:* `ERROR: Could not install packages due to an OSError: [Errno 2] No such file or directory: '...beta_managed_agents_...py'`
*Fix:* create the venv at a short path (`C:\Users\PC\.venvmcp`), or enable long paths:
`New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force` (needs admin + reboot).

---

## 2. `tzdata` is a REQUIRED dependency (found by the step (f) clean-venv run)

**This was a latent deployment bug, not an MCP issue.**

`services/numbering.py`, `services/ledger.py` and `services/handover.py` all call
`ZoneInfo("Africa/Nairobi")`. Windows ships **no** system timezone database, and slim Linux
container images drop theirs. Python's `zoneinfo` then falls back to the `tzdata` PyPI package.

`tzdata` was installed globally on the development machine (2025.3) but was **never declared in
`pyproject.toml`**. That global install is the only reason the suite had ever passed.

In a genuinely clean venv, **9 tests fail** with
`zoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key Africa/Nairobi'`:

```
tests/integration/test_golden_sequence.py::test_golden_full_lifecycle_with_hitl
tests/integration/test_golden_sequence.py::test_golden_full_lifecycle_auto_broadcast
tests/integration/test_recurrence_handover.py::test_handover_lists_owners
tests/integration/test_runner_failures.py::test_fail_soft_ledger_error_keeps_the_run_going
tests/integration/test_runner_failures.py::test_ledger_agent_absorbs_locked_workbook
tests/system/test_api_system.py::test_health_and_six_regions
tests/system/test_api_system.py::test_system_ingest_inc9_msp_lifecycle
tests/system/test_contracts.py::test_workflow_node_status_vocabulary
tests/unit/test_fingerprint_shifts.py::test_day_shift_eat
```

The real-world consequence is worse than the test count suggests: **incident numbering is one of
the callers**, so a fresh install cannot allocate an incident number.

`tzdata` is now declared in `[project] dependencies`. **Do not remove it because it looks
unused** — nothing imports it by name; it is reached only through `ZoneInfo`.

---

## 3. TLS from Python fails on this machine — `NOC_USE_TRUSTSTORE=1` is mandatory

Run `C:\Python313\python.exe scripts/check_tls.py` on any new machine before starting a phase
that calls a live API (Phase 3 weather/KPLC, Phase 7 OpenWeather MCP).

**On this machine the bare check FAILS:**

```
CA source:  certifi bundle: C:\Users\PC\AppData\Roaming\Python\Python313\site-packages\certifi\cacert.pem
FAIL: certificate verification failed -- [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
      unable to get local issuer certificate (_ssl.c:1028)
```

**Cause: Avast antivirus, not a corporate proxy.** Avast's Web/Mail Shield terminates and
re-signs every HTTPS connection. The served certificate for `api.open-meteo.com` is:

```
Issuer:  CN=Avast Web/Mail Shield Root, O=Avast Web/Mail Shield
Subject: CN=open-meteo.com
```

That root is in the **Windows** certificate store but not in **certifi's** bundle.

**`curl` succeeding proves nothing** — curl uses Schannel (Windows store), Python uses certifi.
Verified: `curl.exe` returns HTTP 200 with real forecast data at the same moment Python fails.

**Fix, verified end to end:**

```powershell
C:\Python313\python.exe -m pip install truststore   # 0.10.4 installed 2026-09-16
$env:NOC_USE_TRUSTSTORE = "1"
C:\Python313\python.exe scripts\check_tls.py        # -> PASS after truststore injection (HTTP 200)
```

Adapters call `truststore.inject_into_ssl()` when `NOC_USE_TRUSTSTORE` is set, which makes Python
verify against the Windows store instead of certifi.

**Open decision for the owner:** `truststore` is currently an operator remediation, per spec
§7.0.11 — it is **not** declared in `pyproject.toml`. On this machine every outbound HTTPS call
depends on it. If Phase 3 should not trip over this on a fresh Windows checkout, it needs to
become a declared dependency (`truststore>=0.10; sys_platform == 'win32'`). Not done unilaterally
because the spec deliberately treats it as environmental.

`scripts/check_tls.py` exit codes: **0** pass · **1** real failure (certificate or HTTP error) ·
**2** no network at all (DNS/timeout) — so being offline can never be misread as a cert problem.

---

## 4. Restore the database (rollback procedure)

Migrations are **additive** — they only ever `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD
COLUMN`, never drop, rename or retype. Two consequences that shape the rollback:

- **Older code runs fine against a newer database.** Extra columns are simply ignored. So the
  fast rollback is **code only** — no database work at all.
- **You only need to restore the file if data was corrupted**, not merely because you want the
  previous release back.

`db/migrate.py` copies the database **before** it changes anything, using the sqlite3 backup API
(safe while WAL is active), to `data/backups/<db stem>.<from>-to-<to>.<timestamp>.db`.

### Code-only rollback (the usual case)

1. Stop the server.
2. Check out the previous phase's tag (`v2-phase-N`).
3. Start. The newer database still works.

### Full restore (only if data is damaged)

1. **Stop the server first.** Restoring underneath a running process will corrupt it.
2. List what you have: `ls data/backups/`
3. Restore:
   ```powershell
   C:\Python313\python.exe scripts\restore_db.py --from data\backups\noc_agents.1-to-2.<ts>.db
   ```
   Add `--to <path>` to target a database other than the app's default.
   The script prints the `schema_version` the restored file carries, refuses anything that is not
   a SQLite database, and keeps the file it replaced as `<name>.pre-restore.<ts>.db` — so a
   mistaken restore is itself reversible.
4. Check out the git tag matching that schema version, then start.

### What cannot happen

A half-applied migration. All DDL runs inside one transaction with an explicit `BEGIN IMMEDIATE`,
so if the process dies mid-migration nothing is committed, `schema_version` is unchanged, and the
next start simply retries. This was verified by injecting a crash before the version stamp: the
file came back byte-identical in shape and the retry succeeded.

**Verified on real data (2026-09-16).** The migration ran against the live `data/noc_agents.db`
(5 incidents, 20 work notes, 36 broadcasts, 4 HITL tasks, 20 agent runs). All row counts were
preserved, all 8 new incident columns were added, `schema_version` went to 2, WAL was enabled, and
the backup it wrote passes `PRAGMA integrity_check` as a valid v1 database.

---

## 5. Re-running the gate

Any time `mcp`, `anthropic` or `pydantic` bounds change:

```powershell
C:\Python313\python.exe -m venv C:\Users\PC\.venvmcp     # SHORT path — see 1(d)
C:\Users\PC\.venvmcp\Scripts\python.exe -m pip install -e ".[mcp,dev]"
cd C:\Users\PC\Desktop\second-brain\soc-agents
C:\Users\PC\.venvmcp\Scripts\python.exe -m pytest -q
```

Green → keep the extra pinned. Red with ≤ 5 trivially fixable failures → fix on a branch and
re-run; **any fix that changes a golden literal is forbidden** (guardrail G2). Red otherwise →
fall back to the sidecar design in spec §7.1.5, and record the decision here.

The throwaway venv is disposable: `Remove-Item -Recurse -Force C:\Users\PC\.venvmcp`.
