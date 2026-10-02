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

---

## 6. Turning a feature lane on (Phases 3–5)

Every lane added after the Phase 3 stop line ships **off**. With its flag unset the routes 404,
its scheduled jobs report `enabled: false`, and nothing about the incident pipeline changes.
That is deliberate: it means a lane can be merged, reviewed and shipped long before anyone has
done the paperwork it needs.

Turning one on is three steps, in this order. Do not skip step 1.

### Step 1 — check what the lane still needs from a person

`docs/PHASE4.md` and `docs/PHASE5.md` each have a "before you switch anything on" section. Those
are not a wish-list; they are the things that make the lane *wrong* rather than merely absent.
The two that bite hardest:

* **Recipients must resolve.** A lane that emails anyone (regulatory notices, maintenance
  invites, complaint reminders) resolves a `recipients_ref` against the operator profile. If the
  key is missing the dispatch is **refused**, on purpose — the alternative is that a notice to
  the Communications Authority, or a reminder that a confidential complaint exists, lands in
  whatever `DEMO_EMAIL_TO` points at. A refusal is visible; a misdirected disclosure is not.
* **Paperwork gates are real gates.** Anything that sends clause text or complaint text to a
  hosted model needs a reg 41(2) transfer record first. The code fails closed to a deterministic
  path without one, which looks like "the AI answer is missing" rather than an error.

### Step 2 — turn the flag on in ONE place and watch it

Set the flag in `.env` (see `.env.example` for the list and what each one covers), restart, and
check `GET /api/v1/scheduler/status`. A lane's job should move from `enabled: false` to `true`;
if it does not, the flag name is wrong — the status surface reads the same env var the job does,
which is exactly why it is there.

### Step 3 — the ones that need more than a flag

* **`HOUSEKEEPING_ENABLED` does not delete anything on its own.** Deleting needs
  `HOUSEKEEPING_APPLY=true` **and** `posture.dry_run: false` in `config/retention.yaml`. Two
  keys, because the YAML is the document Legal signs off and the env flag is the operator's
  decision, and neither party should be able to start removing operational records alone. With
  the flag on and apply off you get a report of what *would* go. Read that report first.
* **`SCORECARDS_ENABLED` must not publish before a shadow period.** §7.6 requires one shadow
  shift covering the first scorecard period before any scorecard is PUBLISHED. These numbers
  decide vendor money.
* **`MAINTENANCE_ENABLED` needs `maintenance.recipients.FE_ONCALL`** in the operator profile, and
  Legal's confirmation of CA licence Condition 9.1 — the code enforces a `ca_approval_ref` for
  REGION and NETWORK windows and labels it UNVERIFIED until someone checks the licence class.
* **`ICS_UID_DOMAIN` is set once and never changed.** It is the right-hand side of every calendar
  invite's UID. Changing it after invites have gone out gives every attendee a **duplicate**
  event rather than an update.
* **`UPLOAD_DIR` must never point inside `frontend/dist`.** Every accepted file would become a
  URL. The code refuses and warns, but do not rely on that.

### Turning a lane back off

Unset the flag and restart. No data is deleted and no schema changes — the tables stay, the rows
stay, the routes go back to 404. A lane that has been on and is then turned off leaves its audit
rows behind on purpose: "this was enabled between these dates" is itself a thing a regulator may
ask about.

---

# Part 2 — operating the system (sections 7–21)

Sections 1–6 are install facts. The sections below are for the person on shift when something
is wrong. Each one says what you will see, what to type, what "worked" looks like, and what to
do if it did not.

Several pages say **"not built yet"**. That is deliberate. Where the build spec describes
something the code does not do, the page says so and tells you what to do by hand instead.
Anything marked **as of 2026-09-21** depends on work that was in progress that day. Check it
again before you rely on it.

---

## 7. How to run the commands on these pages

Open PowerShell **in the project folder** (`C:\Users\PC\Desktop\second-brain\soc-agents`).
Every command below assumes that folder.

**The API** answers at `http://127.0.0.1:8000`.

* To read something, use `curl.exe` — with the `.exe`. In PowerShell a bare `curl` is a
  different program with different flags.
* To send something with a JSON body, use `Invoke-RestMethod`. Quoting JSON through `curl.exe`
  behaves differently in Windows PowerShell 5.1 and PowerShell 7; `Invoke-RestMethod` does not.

**The database** is `data\noc_agents.db`. There is **no `sqlite3` command on this machine**.
Paste this once per PowerShell window. It opens the file **read-only**, so it cannot damage it:

```powershell
function noc-sql([string]$q) { C:\Python313\python.exe -c "import sqlite3,sys; db=sqlite3.connect('file:data/noc_agents.db?mode=ro',uri=True); [print(r) for r in db.execute(sys.argv[1])]" $q }
```

Then, for example:

```powershell
noc-sql "SELECT status, COUNT(*) FROM outbox GROUP BY status"
```

No output means the query matched nothing. It does not mean it failed — a failure prints a
Python error. Times in the database are **UTC**. Nairobi is UTC+3.

**Flags** live in `.env` in the project folder. `.env` is read **once, at start-up**. Changing
it does nothing until you restart. If `NOC_SKIP_DOTENV=1` is set in the window that starts
the server, `.env` is ignored completely.

**Restart** means: go to the window running the server, press `Ctrl+C`, wait for the prompt,
then start it again:

```powershell
C:\Python313\python.exe -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port 8000
```

**Roles.** This build runs with `AUTH_DISABLED=true` by default. With that setting every role
check on every route is **switched off**, and anyone who can reach the port can call anything,
including the routes marked "admin" below. The Settings page role switcher only labels who you
are; it does not grant or refuse anything.

Each page names the roles a route asks for, so you know what applies once auth is on. Three
groups come up again and again:

| Group | Roles |
|---|---|
| **operations** | `noc_analyst`, `shift_supervisor`, `duty_manager`, `admin` |
| **supervisors** | `shift_supervisor`, `duty_manager`, `admin` |
| **platform readers** | `noc_analyst`, `shift_supervisor`, `duty_manager`, `management`, `admin` |

The full table is `tests/system/test_auth.py`. As of 2026-09-21 that table, and the role checks
on many read routes, are new and not yet committed.

**Do not set `AUTH_DISABLED=false` in this build.** There is no login route yet — the signed
session cookie the role checks read is issued by "a future login route" (`api/auth.py`). With
auth on today, nobody can sign in through the app, and every gated route — the Approvals
included — answers `401`. Turning auth on needs engineering first.

**The database queries need no role.** They need a PowerShell window on the server. That is
the fallback when the screens are unavailable to you.

### The UI says API unreachable

The backend is not answering: start it from the project folder with `C:\Python313\python.exe -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port 8000` (for the UI dev server, also `cd frontend; npm run dev` and open `http://127.0.0.1:5173`); the page retries every 8 seconds and recovers on its own.

---

## 8. Rotate a key or secret

There is no secret store and no rotation screen. A secret is a line in `.env`. Rotating it is
editing that line and restarting.

**Secrets the code reads today:**

| Secret | `.env` line | What breaks if it is wrong |
|---|---|---|
| Gmail app password | `GMAIL_APP_PASSWORD` (or `SMTP_PASSWORD`) | outbound email fails; rows go `FAILED` or `DEAD` |
| Anthropic Console key | `ANTHROPIC_API_KEY` | AI drafting stops; templates carry on (section 15) |
| Session-cookie signing secret | `NOC_SESSION_SECRET` | with auth on, nobody can log in (503) |
| OpenAI-compatible endpoint token | `OPENAI_COMPAT_API_KEY` | only with `LLM_PROVIDER=openai_compat` on a hosted endpoint |
| Weather provider key | `WEATHER_API_KEY` | only for a provider that needs one; Open-Meteo does not |

`AT_API_KEY`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `OPT_IN_SALT`, `SOCIAL_HASH_SALT`,
`X_BEARER_TOKEN` and `OPENWEATHER_AGENT_KEY` are listed in `.env.example` and are **read by no
code in this build**. Rotating them changes nothing here. If one leaked, rotate it at the
provider anyway.

### Steps

1. **Get the new value first.** Never revoke the old secret before you hold the new one.
   Gmail: Google Account → Security → 2-Step Verification → App passwords → create one for
   Mail. A normal Gmail password does not work.
2. Stop the server (`Ctrl+C`).
3. Edit `.env`. Replace the value on the existing line. **Delete** the old value — do not leave
   it behind as a comment.
4. Start the server (section 7).
5. Check it (below).
6. Only now revoke the old secret at the provider.

Rotating `NOC_SESSION_SECRET` invalidates every signed cookie. With auth on, everyone is logged
out at once. Warn the floor first.

### What you should see

The two status routes ask for a **platform reader**; the test send asks for `admin`.

Email:

```powershell
curl.exe http://127.0.0.1:8000/api/v1/email/status
```

`"configured": true`. Then send one test message to the demo inbox:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/email/test
```

`"ok": true` and `"mode": "smtp"`.

Anthropic:

```powershell
curl.exe http://127.0.0.1:8000/api/v1/llm/status
```

`"credential_present": true` (and `"enabled": true` if `LLM_ENABLED=true`).

### If it did not work

* `"mode": "mock"` — nothing was sent. Either `EMAIL_ENABLED` is not `true`, or the address or
  password line is empty. Mock mode opens no connection at all.
* `"configured": false` after you set the password — check `EMAIL_ENABLED=true` is set too.
  Real sending is opt-in even when credentials are present.
* `"credential_present": false` with a key set — a bare `ANTHROPIC_AUTH_TOKEN` is refused on
  purpose unless `LLM_ALLOW_AUTH_TOKEN=true`. The app needs a Console `ANTHROPIC_API_KEY`.
* Still wrong after all that: you edited `.env` but did not restart, or the server was started
  with `NOC_SKIP_DOTENV=1`.

If you are rotating the secret **because it leaked**, you are also in section 16.

---

## 9. Read a FAILED run

Every piece of agent work is a "run": the 12-step incident pipeline, and every scheduled job.

### Where it shows

* **Agent observatory** page (`/agents`), panel "Live / recent runs". It lists the 15 newest.
  Needs a **platform reader** role.
* The API. This is the only place that shows **why** a run failed. `GET /api/v1/runs` is open
  to operations, `management`, `msp_coordinator`, `field_engineer` and `planning`:

```powershell
curl.exe "http://127.0.0.1:8000/api/v1/runs"
curl.exe "http://127.0.0.1:8000/api/v1/runs?graph_name=incident_lifecycle"
```

Up to 50 runs, newest first. The fields that matter: `status`, `graph_name`, `trigger`,
`current_node`, `error_summary`, `incident_id`, `steps`.

Or straight from the database, with no 50-row limit:

```powershell
noc-sql "SELECT started_at, graph_name, trigger, current_node, error_summary FROM agent_runs WHERE status='FAILED' ORDER BY started_at DESC LIMIT 20"
```

**Two traps on the Agent observatory page:**

1. **A FAILED run is drawn green.** Only `RUNNING` and `WAITING_HITL` get a different chip
   colour. Every other status, FAILED included, gets the green "ok" chip. Read the word, not the
   colour.
2. **It does not show `error_summary`.** It shows the status, the graph, the trigger and the
   node. For the reason, use the API or the query above.

### What a FAILED incident run means

`graph_name: "incident_lifecycle"` and `status: "FAILED"`:

* **`incident_id` is `null`. There is no ticket.** The whole run was rolled back — the incident,
  its number, its notes, its audit rows. Only the failure record was kept.
* `current_node` is the step it died on.
* `error_summary` is `"<ErrorType>: <message>"`, cut at 2,000 characters.
* The run keeps one step at most: the one that failed. It has none if the failure came while
  saving, after the last step.

The alarm is still in the upstream alarm system. Once the cause is fixed, **inject it again**.
Nobody else will.

### Fail-closed and fail-soft

The 12 steps are split in two. Full table: `docs/ORCHESTRATOR.md` §4.

| Steps | Mode | A failure here means |
|---|---|---|
| 1–5: `INGEST`, `CORRELATE`, `ENRICH`, `SEVERITY`, `TICKET` | **fail-closed** | no ticket exists |
| 6–8: `ASSIGN`, `HITL`, `BROADCAST` | **fail-closed** | the ticket is discarded |
| 9–12: `EXEC_BRIEF`, `LEDGER`, `RECURRENCE`, `MONITOR` | fail-soft | that one output is missing; the run carries on |

The line sits after `BROADCAST` on purpose. Up to there, a half-made ticket is worse than none.
After it, you lose a report, not an outage.

So:

* **`FAILED` and `incident_id: null`** → no ticket. Act now: re-inject the alarm, or raise the
  ticket by hand.
* **A run that finished, with one FAILED step after `BROADCAST`** → the ticket and the broadcast
  are fine. The exec brief, the ledger line, the problem record or the SLA note is missing. Fix
  it when the shift allows.

### Statuses that are not failures

* `WAITING_HITL` — a card is waiting in the Approvals. See sections 13 and 14.
* `CANCELLED` with `error_summary: "HITL rejected: <reason>"` — a supervisor rejected the
  broadcast. The drafts were cancelled and nothing was sent. That is the gate working.

### Scheduled jobs

They appear in the same list with `trigger: "SCHEDULE"` and a `graph_name` such as `outbox`,
`monitor`, `weather`, `pir`, `regulatory`, `complaints`, `maintenance`, `capacity` or
`housekeeping`. They never have an incident. Three failures in a row open that job's circuit —
see section 11.

---

## 10. The outbox: what is stuck, and what to do about it

Nothing leaves the system directly. Every email, SMS, calendar invite and ledger line is first
written as a row in the `outbox` table, in the same transaction as the incident. A separate
step — the drain — then sends it.

**There is no outbox screen and no outbox route.** The spec's dead-letter view
(`GET /api/v1/outbox?status=`) and retry (`POST /api/v1/outbox/{id}/retry`) are **not built**
(`docs/CONFORMANCE.md` C-10). The router file for them is an empty placeholder as of
2026-09-21. Everything on this page is a database query.

### What each status means

| Status | Meaning | Your move |
|---|---|---|
| `PENDING` | Queued for the next drain. Also where a retry waits: after a transient failure the row goes back to `PENDING` with `next_attempt_at` set, a few seconds to a minute ahead. As of 2026-09-21 an email daily cap (`EMAIL_DAILY_CAP`) is being added: an email over the cap will also wait here until the window frees a slot, P1 mail excepted. | None, unless it sits for minutes. Then the drain is not running (section 12). |
| `HELD` | A draft whose HITL card is still open. The row is not stuck; the card is. | Approve or reject the card (section 13). |
| `CLAIMED` | A drain has picked it up, with a 120-second lease. | None. A row `CLAIMED` for more than 120 s belongs to a drain that crashed; the next drain takes it back by itself. |
| `SENT` | Handed to the provider — **unless the `provider` column says `mock`**, which means nothing left the machine (see below). The system records nothing after `SENT`; delivery receipts are not written yet. | Check `provider`. |
| `FAILED` | A transient error (network, SMTP 4xx, HTTP 429/5xx) on every allowed attempt (3 by default). | Read `last_error`, fix the cause, then see "Resending". |
| `DEAD` | Refused permanently. The drain will never try it again. | Read `last_error` (below). |
| `SUPPRESSED` | Deliberately not sent: a newer approved wording replaced it, the card was rejected, or a validator refused the rendering. | None. Correct behaviour. |
| `REJECTED_UNAPPROVED` | An email, SMS, WhatsApp or calendar row that needed a human approval and had none. The dispatcher refused it. | None. The approval gate working. Nothing left. |

### `SENT` does not always mean sent

* **Every SMS row in this build is a mock.** There is no SMS adapter. SMS rows are marked `SENT`
  with `provider = 'mock'` and the text is stored only. **No engineer received an SMS from this
  system.** If an SMS mattered, somebody had to send it by hand.
* **An email row with `provider = 'mock'`** was not sent either: `EMAIL_ENABLED` was off, or no
  recipient resolved. Nothing will ever re-send it.

### See what is there

```powershell
noc-sql "SELECT status, kind, provider, COUNT(*) FROM outbox GROUP BY status, kind, provider"
```

The rows that need a person:

```powershell
noc-sql "SELECT id, kind, status, attempts, updated_at, last_error FROM outbox WHERE status IN ('FAILED','DEAD','REJECTED_UNAPPROVED') ORDER BY updated_at DESC LIMIT 20"
```

Rows sitting in `PENDING`:

```powershell
noc-sql "SELECT id, kind, created_at, attempts, next_attempt_at, last_error FROM outbox WHERE status='PENDING' ORDER BY created_at LIMIT 20"
```

### What to do about a `DEAD` row

Read `last_error`. It tells you which case you are in.

* **`refused: requires HITL approval and approved_at is NULL`** — nobody approved it. Nothing was
  sent. Find the card in the Approvals.
* **`no transmitter for outbox kind 'WHATSAPP'`** — this build has no WhatsApp adapter. That
  message is not coming. If it mattered, send it by hand.
* **`refused: payload is not redacted (...)`** — an AI-drafting row still carried a phone number
  or email address. It was stopped before sending. Treat it as section 16 until you know how it
  got there.
* **An SMTP 5xx, or `refused: <reason>`** — the provider or a validator rejected the content or
  the recipient. Resending the same row fails the same way.
* **A recipients error** — a lane tried to send to a recipient list that is not configured in
  the operator profile. That refusal is deliberate: the alternative is a regulator notice landing
  in the demo inbox. Fix the profile (section 6), not the row.

### Resending

**There is no retry.** No route, no button, no script.

Do **not** set a row's `status` back to `PENDING` by hand. Nothing records who did it or why,
the payload is the one that was already refused, and for an email it writes a second entry in
the cross-border transfer register.

If a `FAILED` or `DEAD` message genuinely has to reach someone, **send it by hand** from the NOC
mailbox. Then add a work note to the incident: what you sent, to whom, and why. That is the
procedure until C-10 is built.

### What you should see

After the cause is fixed, new rows of that kind go `PENDING` → `SENT` within one drain. The
`GROUP BY` query shows no new `FAILED` or `DEAD` rows. `GET /api/v1/metrics/summary` does **not**
count outbox rows yet (the spec's `outbox{…}` key is not built); the query is the source of
truth.

---

## 11. Re-run a poller or scheduled job by hand

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/scheduler/run/outbox_dispatch"
```

Put the job you want in place of `outbox_dispatch`. Names are exact. A wrong one returns
`404 {"detail": "job not found"}`.

| Job | Runs every | Its own flag | Obeys its flag when run by hand? | What it does |
|---|---|---|---|---|
| `outbox_dispatch` | 5 s | `OUTBOX_DISPATCH_ENABLED` (on unless set false) | yes | drains the outbox |
| `monitor_tick` | 60 s | `SCHEDULER_MONITOR_ENABLED` (on unless set false) | yes | SLA chases |
| `weather_regions` | 15 min | `WEATHER_ENABLED` | yes | weather poller |
| `pir_autoopen` | 5 min | `PIR_ENABLED` | yes | opens draft post-incident reviews |
| `regulatory_sweep` | 5 min | `REGULATORY_ENABLED` | yes | regulatory countdowns; never sends |
| `complaints_followup` | 1 h | `COMPLAINTS_ENABLED` | yes | complaint reminders |
| `maintenance_plan_due` | 1 h | `MAINTENANCE_ENABLED` | yes | proposes maintenance tasks |
| `maintenance_window_sweep` | 5 min | `MAINTENANCE_ENABLED` | yes | closes finished windows |
| `capacity_scan` | 1 h | `CAPACITY_ENABLED` | yes | capacity advisories |
| `housekeeping` | daily | `HOUSEKEEPING_ENABLED` | yes | backup, redaction scan, retention |

The "obeys" column is pinned by `tests/unit/test_manual_job_flags.py`. It runs every job by hand
with every flag set `false`, against a database with work waiting for each job, and checks that
no job writes anything.

As of 2026-09-21 new pollers and memory jobs are being added. `GET /api/v1/scheduler/status`
always lists the current set.

### What it does

1. **Resets that job's circuit breaker**: `consecutive_failures` to 0, `circuit_open` to false.
   This is how you reopen a job that failed three times in a row.
2. Runs the job once, now, as a normal run you can find in section 9.
3. Works **whether or not the scheduler is enabled**. It is the manual override.

It does **not** override the job's own flag. All ten jobs check their flag themselves. When the
flag is off, the job runs, does nothing, and says so. Its summary names the flag, for example
`outbox_dispatch skipped: OUTBOX_DISPATCH_ENABLED is off` or
`PIR_ENABLED=false — no reviews opened`. Apart from the run record itself (section 9), nothing
is claimed, sent, written or deleted.

* `outbox_dispatch` and `monitor_tick` are **on unless set false**. With the flag unset they run
  as normal, and only an explicit `false` makes them skip. So with the kill sequence of
  section 16 in force, pressing `outbox_dispatch` drains nothing.
* The eight lane jobs are **off unless set true**. Unset or `false`, they skip.

The route asks for the `admin` role. With `AUTH_DISABLED=true` (this build's default) nobody is
checked — so anyone who can reach the port can press any of these.

**`POST /api/v1/monitor/tick` is a different route, and `SCHEDULER_MONITOR_ENABLED` does not
gate it.** It is the chase button for the analyst on shift (**operations** role). It chases
silent incidents, writing work notes and escalation cards, whatever that flag says. This is
deliberate: the flag stops the *scheduled* chase, not a person who presses chase.

### What you should see

```json
{"job": "weather_regions", "run_id": "…", "status": "SUCCEEDED",
 "summary": "weather_regions skipped: WEATHER_ENABLED is not true",
 "error": null, "duration_ms": 79, "consecutive_failures": 0, "circuit_open": false}
```

`"status": "SUCCEEDED"` with a real summary means it ran. A "skipped" summary means the lane's
flag is off. That is not a failure.

### If it did not work

* `"status": "FAILED"` with `"error"` set — the job raised. `error` is the reason, and the run is
  in section 9's list. `consecutive_failures` is now 1. Fix the cause before pressing it again.
* `404` — the job name is wrong. Copy it from `GET /api/v1/scheduler/status`.
* It keeps failing — stop pressing it. Repeated failures mean something outside the job is wrong:
  the network (`scripts\check_tls.py`, section 3), the provider, or the configuration.

---

## 12. "AGENTS OFFLINE": is the scheduler alive?

**In the committed build the Wallboard does not show "AGENTS OFFLINE".** No screen draws it
(`docs/CONFORMANCE.md` C-19). A Wallboard tile that does is being built as of 2026-09-21. Until
it is committed, the only place the answer exists is this route (**platform reader** role):

```powershell
curl.exe http://127.0.0.1:8000/api/v1/scheduler/status
```

```json
{
  "enabled": true,
  "lease_owner": "NOC-PC:14872:3fa9c1",
  "lease_expires_at": "2026-09-21T07:14:05Z",
  "seconds_since_tick": 3.2,
  "jobs": [{"name": "outbox_dispatch", "interval_s": 5, "enabled": true,
            "last_started_at": "…", "last_status": "SUCCEEDED",
            "consecutive_failures": 0, "circuit_open": false}]
}
```

It reads the database, so any running copy of the server answers for whichever one is ticking.

### How to read it

| You see | It means |
|---|---|
| `"enabled": false` | **The scheduler was never started.** `SCHEDULER_ENABLED` is not `true` in `.env`. Nothing is broken — but **no scheduled job runs at all**, whatever each job line says. |
| `"enabled": true`, `seconds_since_tick` under about 10 | **Alive.** It ticks every 5 seconds. |
| `"enabled": true`, `seconds_since_tick` over 15 and climbing | **This is AGENTS OFFLINE.** The process holding the lease has stopped renewing it. |
| `lease_owner: null` and `seconds_since_tick: null` | No process has ever ticked against this database. |

**"Not enabled" and "dead" are different things.** `enabled: false` is a setting.
`enabled: true` with a stale tick is a fault.

**When the Wallboard tile lands,** it follows the same rule. Red **AGENTS OFFLINE** only from a
fresh reading with a stale tick; red **CIRCUIT OPEN** per failed job; grey **SCHEDULER OFF** for
`enabled: false`. Grey **SIGN IN**, **NOT AVAILABLE TO YOUR ROLE** or **STATUS UNKNOWN** means the
Wallboard cannot read the status. That is "we cannot see", not "the agents are down". Check the
route from a machine that can.

A job line saying `"enabled": true` only means that job's own flag is on. If the top-level
`enabled` is false, the job still does not run.

### The lease

One row in the database: `scheduler_lease`, name `main`. It is valid for 30 seconds and renewed
on every tick. Only the holder runs jobs, so two copies of the server never both drain the
outbox.

If the holder dies, its lease runs out within 30 seconds and the next copy to tick takes over.
A clean shutdown hands the lease back at once. **You never need to clear a lease by hand.**
Nothing waits on a dead owner.

### Circuit open

A job line with `"circuit_open": true` has failed three times in a row. The loop now skips it
until someone runs it by hand (section 11), which resets it.

### What to do

1. `enabled: false` and you expected it on → set `SCHEDULER_ENABLED=true` in `.env`, restart.
2. Tick is stale → look at the server window for a traceback. Restart the server. Check the route
   again.
3. Stale again after a restart → the tick itself is failing. Look in section 9 for FAILED runs
   with `trigger: "SCHEDULE"`.

While the scheduler is down, incidents are still created and broadcasts still go out. The
in-request drain (`OUTBOX_SYNC_DRAIN`, on by default) does not depend on the scheduler. What
stops is SLA chasing, the pollers, and every lane's periodic job.

### What you should see

`"enabled": true`, a `lease_owner`, `seconds_since_tick` under 10, and no job with
`circuit_open: true`.

---

## 13. Approve a broadcast — and why you cannot approve your own

P1 and P2 broadcasts wait for a supervisor (with the default `AUTONOMY_LEVEL=L2_GUARDED`). The
drafts sit `HELD` in the outbox until you decide. Nothing external leaves before that.

### Steps

1. Open **Approvals** (`/hitl`). Oldest first. The header counts how many are unclaimed.
2. **Claim** the card. It is a shared queue; claiming tells everyone else you have it.
3. Read the card — and **read the incident too** (see "Re-render on approve" below).
4. **Approve** or **Reject**. A reject always needs a reason. An approve needs one only when
   `HITL_APPROVE_REASON_REQUIRED=true` (default false; production should set it true).

From PowerShell, if the page is unavailable:

```powershell
curl.exe http://127.0.0.1:8000/api/v1/hitl/pending
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/hitl/<task_id>/claim" -ContentType application/json -Body '{"resolved_by":"Your Name"}'
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/hitl/<task_id>/approve" -ContentType application/json -Body '{"resolved_by":"Your Name","reason":"wording and facts checked"}'
```

Listing and claiming ask for an **operations** role. Approving and rejecting ask for a
**supervisor**: `shift_supervisor`, `duty_manager` or `admin`. A `noc_analyst` may claim a card
but not decide it.

### Why you cannot approve your own

The rule is **raiser ≠ approver**. If the name deciding a card is the name that raised it,
approve and reject both return:

```
403  the person who raised a task may not approve or reject it
```

A second pair of eyes is the whole point of the gate. One person raising and approving their own
release is one person, twice.

**Where the software actually enforces it today — read this before you rely on it:**

* **Maintenance cards** (`APPROVE_SCHEDULE`, `APPROVE_MAINTENANCE_WINDOW`) and **regulatory
  notices** (`APPROVE_REGULATORY_NOTICE`) are raised under the name of the person who asked. The
  403 bites.
* **Broadcast cards raised by the pipeline** are stamped `agent:SupervisorAgent`. Handover cards
  are stamped `agent:ShiftHandoverAgent`. No person's name can equal those, so **the 403 never
  fires on a routine broadcast.** That is deliberate — an agent has no conflict of interest —
  but it means that on broadcasts "not your own" is **floor discipline, not a software control**.
* The SLA-monitor escalation cards carry no raiser at all, so the rule cannot fire on them.

With `AUTH_DISABLED=true` the name recorded is whatever the Settings page (or `resolved_by`)
says. The rule compares names, so it is only as strong as the names are honest. With auth on,
the name comes from the login and cannot be typed.

### Re-render on approve — what happens to the draft you read

**The wording on the card is not what leaves.** When you approve, the system rebuilds the
message from the incident **as it stands at that second**, renders every channel again, and
queues that. The draft you read stays on the card for the audit trail. What actually left is
recorded beside it.

What that means for you:

* Anything changed on the incident between the card being raised and your click — a new
  assignee, a corrected site, a changed priority, a work note — **goes out in the message**.
  If a colleague has been working the incident while you read the card, re-read the incident.
* When the released wording differs from the draft, the card is marked `edited = 1`. That is the
  audit record that the text changed after it was proposed.
* Any older held draft for the same incident is marked `SUPPRESSED`. Only one wording ever
  reaches the wire.
* **You cannot type your own wording on the card.** The API accepts one override (`priority`);
  the Approvals does not send it. To change what goes out, change the incident, then approve.

### What you should see

The card leaves the inbox. The incident gets a work note: "HITL approved broadcast/assignment."
The outbox rows for that incident go `HELD` → `PENDING` → `SENT`.

### If it did not work

* `409 task already APPROVED` (or `REJECTED`) — someone decided it first. Refresh.
* `403` naming your role — you are not a supervisor role (auth on only).
* `403 the person who raised a task…` — see above. Ask another supervisor.
* Rows stay `PENDING` — nothing is draining. `OUTBOX_SYNC_DRAIN` is off, or the kill sequence of
  section 16 is in force.
* Rows go `SENT` but nobody got the email — look at `provider` (section 10). `mock` means
  `EMAIL_ENABLED` is off (section 8).
* The card will not draw — the Inbox shows the raw payload and a **Reject unread** button.
  Decide from the raw payload or open the incident. Never approve what you could not read.

---

## 14. A P1 is sitting unapproved and nobody has claimed it

**Nothing will warn you. The escalation ladder is not built.**

The spec's ladder — a nudge at 5 minutes unclaimed, the duty manager plus a `hitl.escalated`
alert at 15, red on the Wallboard at 30 — does not exist. There is no configuration for it, no
nudge message, no event, no test (`docs/CONFORMANCE.md` B-01). As of 2026-09-21 an unclaimed
P1 card sits there silently for as long as nobody looks.

Until the ladder is built, **the ladder is you.** Keep this timer by hand.

### Why it matters

While the card is open, nothing about that P1 has gone to the field, the MSP or management. The
drafts are `HELD`. The dispatcher would refuse them if anything tried (`REJECTED_UNAPPROVED`).
Silence on the card is silence to everyone who needs to act on the outage.

### Every shift

1. Keep the **Approvals** open. It sorts oldest first and counts unclaimed cards.
2. Check the queue with ages:

```powershell
curl.exe http://127.0.0.1:8000/api/v1/hitl/pending
```

Each card has `priority`, `incident_number`, `created_at` (UTC) and `claimed_by`. `null` in
`claimed_by` means nobody has it. That route asks for an **operations** role. The open count is
also `hitl_pending` on `GET /api/v1/metrics/summary`, which is open to everyone — put it on a
screen the whole floor can see.

### The manual ladder

| Age of an unclaimed P1 card | Do this |
|---|---|
| 5 minutes | Claim it yourself, or phone the supervisor on duty and stay on until they claim it. |
| 15 minutes | Phone the duty manager. Say the incident number and how long the card has waited. |
| 30 minutes | The duty manager decides it, or rejects it and sends the message by hand. Record why in the incident's work notes. |

The phone numbers are on the floor's own contact sheet. They are **not** in this system: the
operator profile holds role labels such as `RNIO-NBI-E`, not numbers.

### Record it

Until the ladder exists, the only evidence that a card waited is its `created_at` and its
decision time. Put every P1 that waited more than 5 minutes on the shift handover, with the
reason.

### What you should see

No P1 card older than 5 minutes without a `claimed_by`.

---

## 15. Turn `LLM_ENABLED` off in an emergency

Use this when the model is misbehaving, when a provider has an incident, when the spend is
running away, or when the Data Commissioner suspends a transfer (DPA s.49). It is one line and
a restart.

### Steps

1. Stop the server.
2. In `.env`, set:

```
LLM_ENABLED=false
```

3. Start the server.
4. Check (**platform reader** role):

```powershell
curl.exe http://127.0.0.1:8000/api/v1/llm/status
```

### What you should see

`"enabled": false`.

### What keeps working: everything

The model only ever drafted **on top of** the deterministic templates. Every broadcast, SMS,
email, exec brief and ledger line has a template path, and that path is the one that runs now.

This is proved, not assumed. `tests/system/test_degraded_mode.py` runs the whole 12-step
pipeline with `LLM_ENABLED=false`, the `anthropic` and `mcp` packages blocked from importing, no
email credentials, and **every outbound network connection blocked** — for an auto-sent P4 and
for a gated P2 including its approval and close. It checks that:

* every step succeeds;
* the SMS, email, exec brief and ledger text is real template text quoting the incident number,
  priority, site and region — not an empty placeholder;
* every outbox row reaches `SENT` and the ledger workbook is on disk with the incident in it;
* the on-demand AI routes (`POST /api/v1/incidents/{id}/analysis` and `…/brief/draft`) answer
  with `"source": "template"` and real content rather than an error;
* no outbound connection was attempted anywhere.

What you lose: AI-polished wording and root-cause suggestions. Nothing operational.

### If it did not work

* Still `"enabled": true` — you did not restart, or `NOC_SKIP_DOTENV=1` is set, or
  `LLM_ENABLED=true` is set in the environment of the window that starts the server (that beats
  `.env`). Close that window and start from a fresh one.
* An AI route returns an error or an empty answer instead of template text — that is a bug. The
  pipeline itself does not depend on those routes. Note the incident number and report it.

### Not the switch you want?

* To stop **email** leaving: `EMAIL_ENABLED=false`. Know the cost: queued email is then marked
  `SENT` as a mock and is **never** sent later.
* To stop **everything** leaving and keep it queued for later: section 16's kill sequence.
  `LLM_ENABLED=false` alone stops only the model.

---

## 16. Redaction miss / data left the building

A phone number, an email address or a person's details went out in a message, or to the AI
provider, when it should not have. This page is the breach drill in spec §9.6: **detect, kill,
assess, notify, learn**. Read the whole page once before you need it.

### 1. Detect

**What exists:**

* **The daily redaction scan.** It reads every outbox message `SENT` in the last 24 hours (the
  default in `config/retention.yaml`) and
  looks for email addresses and Kenyan phone numbers, using the same patterns the scrubber uses.
  A hit writes an audit row with action `redaction.miss`. The audit row never quotes the leaked
  detail — a breach record that repeats the breach is a second copy of it.
* **One per-message check, for AI-drafting rows only.** An AI-drafting outbox row that still
  carries a phone number or email address is refused before sending and goes `DEAD` with
  `refused: payload is not redacted (...)`.

**What does not exist — as of 2026-09-21:**

* **No pre-send contact check on email or SMS.** The spec names one (`validate_no_contacts`,
  per row). It is not in the code. For email, the daily scan is the whole of detection, and it
  is after the fact.
* **No red chip on the Wallboard in the committed build.** The `security.redaction_miss` alert
  has no screen that draws it (`docs/CONFORMANCE.md` C-19). **Nobody is told.** You find a miss
  by looking. A red **REDACTION MISS** chip is being built as of 2026-09-21. When it lands it
  shows counts and field paths, never the leaked value, and it cannot be cleared from the
  screen — working this page is the response to it.
* **The scan only runs if housekeeping is on.** It is part of the `housekeeping` job, which
  ships **off**. With `HOUSEKEEPING_ENABLED` unset, the scan never runs — not even when you
  press the button.

**To run the scan now:**

1. In `.env`, set `HOUSEKEEPING_ENABLED=true`. **Leave `HOUSEKEEPING_APPLY` unset.** With it
   unset, housekeeping deletes nothing: it takes a backup, runs the scan, and reports what it
   *would* remove.
2. Restart.
3. Run it (`admin` role):

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/scheduler/run/housekeeping"
```

4. Look for misses:

```powershell
noc-sql "SELECT ts, entity_type, entity_id, rationale FROM audit_events WHERE action='redaction.miss' ORDER BY ts DESC"
```

The Audit page (`/audit`) also lists them, among the 100 newest audit rows. It asks for
`duty_manager`, `management`, `legal` or `admin` — **a shift supervisor cannot open it** once auth
is on. The query above works for anyone at the server.

A miss on an **SMS** row, or on any row whose `provider` is `mock`, did not leave the building:
nothing was transmitted (section 10). Record it, fix the leak, but it is not a transfer.

If you already know something leaked — someone forwarded you the message — do not wait for the
scan. Go to step 2.

### 2. Kill — any supervisor, no deploy

The spec's one-click freeze, `POST /api/v1/admin/freeze`, is **not built**. Its router file is
an empty placeholder as of 2026-09-21 (`docs/CONFORMANCE.md` C-11). Do not go looking for it.

The kill sequence today is four lines in `.env` and a restart:

```
EMAIL_ENABLED=false
OUTBOX_DISPATCH_ENABLED=false
OUTBOX_SYNC_DRAIN=false
LLM_ENABLED=false
```

Then stop the server and start it again (section 7).

**Why these four, and not the spec's three.** The spec lists `LLM_ENABLED`,
`OUTBOX_DISPATCH_ENABLED` and `MCP_RUNTIME_ENABLED`. Checked against the code:

* **`EMAIL_ENABLED=false` is the line that actually stops mail.** It puts the email adapter in
  mock mode: no connection is opened, on any path. It is not in the spec's list. It must be in
  yours.
* **`OUTBOX_DISPATCH_ENABLED=false`** stops only the *scheduled* drain. It is **on unless you set
  it false**, so leaving it out leaves it on.
* **`OUTBOX_SYNC_DRAIN=false`** stops the *in-request* drain — the one that sends during a storm,
  straight after each incident is saved. It is **on by default**. The spec's three lines leave it
  running. Without this line, the kill sequence stops nothing on the main incident path.
* **`LLM_ENABLED=false`** stops the hosted-model transfer.
* **`MCP_RUNTIME_ENABLED`** is read by no code in this build. There is no MCP runtime to stop
  (`docs/CONFORMANCE.md` C-27). Setting it is harmless. Relying on it is wrong.
* **SMS and WhatsApp need no line.** Neither has an adapter in this build: SMS rows are stored as
  mock sends, WhatsApp rows go `DEAD`.

**Why all four, together.** `EMAIL_ENABLED=false` on its own does not freeze anything.
Any drain that still runs turns each queued email into `SENT` with provider `mock` — consumed,
never delivered, even after the freeze lifts. The two drain lines keep the messages `PENDING`, so
they can be reviewed and released later. `EMAIL_ENABLED=false` is the backstop in case a drain
runs anyway.

**One thing bypasses the flags. Do not use it while frozen:** **the demo script** (`noc-demo`,
`make demo`, or `python -m noc_agents.scripts.demo_safaricom`). It drains the outbox directly
after every event, whatever `OUTBOX_SYNC_DRAIN` says.

With `EMAIL_ENABLED=false` in place it would not reach the wire — but it would consume every
queued email as a mock. Nothing you froze would ever be sent.

`POST /api/v1/scheduler/run/outbox_dispatch` is **not** a bypass. Run by hand, the job checks
`OUTBOX_DISPATCH_ENABLED` itself. With it `false`, the job claims and sends nothing, and its
summary reads `outbox_dispatch skipped: OUTBOX_DISPATCH_ENABLED is off` (section 11).

**Check that nothing can leave** (status routes: **platform reader** role):

```powershell
curl.exe http://127.0.0.1:8000/api/v1/email/status
curl.exe http://127.0.0.1:8000/api/v1/llm/status
curl.exe http://127.0.0.1:8000/api/v1/scheduler/status
```

You should see `"configured": false` and `"provider": "mock"`; `"enabled": false`; and the
`outbox_dispatch` job with `"enabled": false`.

Queued messages stay `PENDING` or `HELD`. Nothing is lost. Nothing moves. Incidents are still
created, numbered and assigned, and HITL cards still appear, so the floor keeps working. Their
messages simply queue behind the freeze. Send anything urgent by hand, from the NOC mailbox or
phone, after checking it carries no personal data.

### 3. Assess — DPO and duty manager, same day

Which messages, to whom, which fields. Start here:

```powershell
noc-sql "SELECT a.ts, a.entity_type, a.entity_id, o.kind, o.sent_at, o.incident_id FROM audit_events a LEFT JOIN outbox o ON o.id = a.entity_id WHERE a.action='redaction.miss' ORDER BY a.ts DESC"
noc-sql "SELECT ts, actor, action, entity_type, entity_id, payload_json FROM audit_events WHERE action IN ('external.call','llm.call') ORDER BY ts DESC LIMIT 50"
```

The `external.call` and `llm.call` rows are the transfer register: recipient, recipient country,
and a description of the data — never the data itself.

**If the recipient was Anthropic, record the retention exposure honestly:**

* prompts and responses are **not retained by default**;
* a Covered Model (Fable 5 / 5.1) **requires 30-day retention** — this system's reasoning model
  is `claude-fable-5-1`;
* content flagged by Anthropic's trust-and-safety systems may be retained **up to two years**.
  Ask: was any of it flagged?
* **Zero data retention is not in force.** `LLM_ZDR_CONFIRMED` is false and no confirmation is
  on file (`docs/COMPLIANCE.md`). Assume standard retention.

**A breach on the provider's side** reaches the operator on the processor's 48-hour leg (DPA
s.43). By the time you hear of it, part of the 72 hours may already be gone.

### 4. Notify

**Who to call, in this order.** The names and numbers are on the floor's contact sheet, not in
this system.

1. The **duty manager** — now.
2. The **DPO** — the 72-hour clock is theirs.
3. **Legal** — the notification to the Data Commissioner, and whether the recipient's contract
   carries the 48-hour processor clause.
4. The owner of any **credential** involved — then rotate it (section 8).

**The clock — DPA 2019 s.43.** Where personal data has been accessed or acquired by an
unauthorised person and there is a real risk of harm, the controller notifies the Data
Commissioner without delay and **within 72 hours of becoming aware**. A notification sent after
72 hours must give the reasons for the delay. A **processor** must tell the controller within
**48 hours** — that matters whenever a vendor (Anthropic, Google, Meta, Africa's Talking)
processes on the operator's behalf.

**Put the 72 hours on the screen.** `ODPC_BREACH_72H` is a real notification kind with a 72-hour
deadline. The route answers `503` unless `REGULATORY_ENABLED=true`; set it and restart (it sends
nothing — the regulatory lane only drafts). It asks for a **supervisor**. Then:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/incidents/<incident_id>/regulatory" -ContentType application/json -Body '{"kind":"ODPC_BREACH_72H","requested_by":"Your Name"}'
```

Know what that clock counts from. **It starts at the incident's `failure_time`**, not at the
moment you became aware. For a leak in an outage message, the outage began before the leak was
found, so the deadline on screen is **earlier** than the legal one. That is the safe direction,
but tell the DPO which time the clock is using. If the incident has no failure time at all the
route refuses with `422` rather than invent one.

This system has no incident type for its own failures. If there is no incident to hang the
clock on, **do not raise a fake alarm to get one.** Track the 72 hours on paper from the moment
the operator became aware, and tell the DPO the in-system clock is not in use.

The system **drafts** the notice. It never sends one. The DPO and Legal write, approve and send
the notification.

### 5. Learn

* Open a post-incident review marked `MANUAL` on the incident (**operations** role;
  `PIR_ENABLED=true` needed, otherwise the route is a `404`):

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/incidents/<incident_id>/pir"
```

* The rule that missed gets a test. Engineering's job; raise it with them.

### Undo the kill sequence

Only when the DPO agrees. **First read what is queued.** Every `PENDING` row goes out the moment
the drain resumes, including anything queued behind the leak. `HELD` rows go out when their card
is approved.

```powershell
noc-sql "SELECT id, kind, incident_id, created_at FROM outbox WHERE status IN ('PENDING','HELD') ORDER BY created_at"
```

There is no route to cancel a queued row. If one of them must not go, stop and ask engineering
before you lift the freeze. Then remove the four lines (or set them back to what they were),
restart, and check the three routes again.

### Rehearsal

Once a quarter, with the DPO, signed in `docs/SIGNOFF.md`. It has never been done. The form is
there and empty.

---

## 17. Roll back a phase

The spec gives three steps, in this order: **tag, flag, restore.** Most rollbacks end at step 2.
Step 3 is almost never needed.

### Step 1 — Tag: find where you are, and where you would go back to

```powershell
git tag
git log --oneline -5
```

The phase tags that exist are `v2-phase-0`, `v2-phase-3-stopline`, `v2-phase-4`, `v2-phase-5`
and `v2-phase-5-complete`. **`v2-phase-1`, `-2` and `-3` do not exist and cannot:** version
control began at the Phase 3 stop line, so no commit matches those boundaries
(`docs/CONFORMANCE.md` §E). Do not look for them.

| Tag | Database schema version it expects |
|---|---|
| `v2-phase-0`, `v2-phase-3-stopline` | 4 |
| `v2-phase-4` | 5 |
| `v2-phase-5` | 6 |
| `v2-phase-5-complete` | 7 |

Decide which tag you would return to. **Do not check it out yet** — try step 2 first.

**As of 2026-09-21 the live database is at version 8, and no tag — not even the latest commit —
carries version-8 code.** The version-8 code was running uncommitted. So today *every* code
rollback is a rollback past version 8. Read "The one exception" below before you check anything
out.

### Step 2 — Flag first; the tag only if there is no flag

Every lane added in Phases 3–5 ships **off**, behind its own flag. If what broke belongs to one
of them, you do not need different code at all:

1. Unset that lane's flag in `.env` (section 6 lists them).
2. Restart.
3. `curl.exe http://127.0.0.1:8000/api/v1/scheduler/status` — the lane's job shows
   `"enabled": false`. Its routes answer `404` or `503` again.

Nothing is deleted and the schema does not change. **Try this first. It is usually the whole
rollback.**

If the fault is in code that has no flag — the incident pipeline itself — check out the tag:

1. Stop the server.
2. Run `git status`. **If it lists any changed or untracked files, stop and call engineering.**
   Checking out over uncommitted work either refuses or carries that work along, and on a machine
   where anyone is developing it destroys theirs.
3. `git checkout v2-phase-5-complete` (the tag you chose). You are now on a "detached HEAD". That
   is expected for a rollback.
4. If the frontend changed between the two versions: `cd frontend; npm run build; cd ..`
5. Start the server.

**Older code runs on the newer database.** Migrations only add tables and columns; old code
ignores what it does not know. You do not touch the database for a code rollback — with the one
exception below.

### Step 3 — `scripts\restore_db.py`: only if the DATA is damaged

**Do you need this at all? Usually not.** Restoring replaces the whole database file with the
copy taken before the last migration. **Every incident, note, approval and audit row written
since then is gone.** Use it only when the data itself is wrong — corrupted rows, a bad bulk
write — never just because you want the previous release back.

The procedure is section 4 of this runbook. In short: stop the server, list `data\backups\`,
restore, start.

### The one exception: schema version 8 (`hitl_tasks`)

Everything above assumes migrations only add. Section 4 says the same ("never drop, rename or
retype") — that was true up to version 7. **Schema version 8 is the one that does not.** It
rebuilt the `hitl_tasks` table so a HITL card no longer needs an incident (maintenance windows,
scorecard disputes) and carries its own operator. The live database went to version 8 on
2026-09-21; the backup taken just before is `data\backups\noc_agents.7-to-8.20260921T050736Z.db`.
The details are in the module docstring of `src\noc_agents\db\migrate.py` ("THE ONE EXCEPTION"),
worked out against the version-7 code:

* **An older release still starts** and still reads and decides every incident card.
* **It still writes incident cards — without an owner.** Nobody notices while the old code runs.
* **Rolling forward again hides those cards.** Version 8 shows an ownerless card to nobody, and
  the migration will not repair them, because the file is already at version 8. **Before you
  start the newer code again**, with the server stopped, run this once:

```powershell
C:\Python313\python.exe -c "import sqlite3; db=sqlite3.connect('data/noc_agents.db'); n=db.execute('UPDATE hitl_tasks SET operator_id=(SELECT operator_id FROM incidents WHERE incidents.id=hitl_tasks.incident_id) WHERE operator_id IS NULL AND incident_id IS NOT NULL').rowcount; db.commit(); print('cards given back their owner:', n)"
```

  It is safe to run twice. Cards with no incident stay as they are.
* **Cards with no incident are invisible to an older release.** A maintenance-window card cannot
  be seen or approved, so the window it gates stays `PROPOSED`. Nothing leaks — but **nobody can
  approve a planned outage while you are rolled back past version 8.** Tell planning before you
  do it.
* **Restoring the file is not affected.** A pre-version-8 backup holds the old table as it was.

### What you should see after each step

| After | You should see |
|---|---|
| Step 2, flag | The lane's job `"enabled": false` in `/scheduler/status`; its routes 404/503; the incident pipeline unchanged. |
| Step 2, tag | The server starts. The log may say the database is newer than the code and it is "carrying on". That is expected. |
| Step 3 | `restore_db.py` prints the schema version it restored; the replaced file is kept as `<name>.pre-restore.<timestamp>.db`. |

### If it did not work

* The server will not start after a checkout — read the error. An `ImportError` or
  `ModuleNotFoundError` means the two versions need different packages: run
  `C:\Python313\python.exe -m pip install -e .` and start again. Anything else: go back to the
  newer tag and call engineering.
* Cards vanished after rolling **forward** — you skipped the `UPDATE` above. Stop, run it, start.
* The restore was a mistake — restore the `.pre-restore.` file the same way.

---

## 18. Export a QBR pack or a vendor pack

**Not built.** The quarterly-review workbook and the per-vendor pack (both `.xlsx`) do not exist.
Neither do scorecard disputes or the vendor notice draft (`docs/CONFORMANCE.md` C-02). They wait
on the scorecard lane, which is being built as of 2026-09-21, and on the HITL rebuild. The
scorecards router says so in its own header.

What **does** exist, and can go into a pack you assemble by hand:

| What | Route | Notes |
|---|---|---|
| **Evidence pack for one incident** | `GET /api/v1/incidents/{incident_id}/evidence-pack` | Works with the regulatory lane **off**, on purpose. Asking again returns the same id and the same sha256 — that stable hash is what makes it evidence. `?refresh=true` rebuilds from the current rows and writes a new pack only if the content changed; an old pack is never rewritten. Asks for a supervisor, duty manager, management, legal or admin role. |
| **Shift ledger workbook** | `GET /api/v1/shifts/ledger/{shift_id}.xlsx` | Built in memory from the database and streamed; no file on disk. Asks for `shift_supervisor`, `duty_manager`, `management` or `admin`. |
| **Complaint statistics** | `GET /api/v1/complaints/stats` | Needs `COMPLAINTS_ENABLED=true` and a complaints role. |
| **Scorecards** | `GET /api/v1/scorecards?vendor=&period=&status=` and `GET /api/v1/scorecards/{id}` | **Being built as of 2026-09-21 — not in the committed code yet.** When it lands: JSON with each line's raw and normalised value, excluded minutes, formula and the config path it came from — the substance of a QBR, unpackaged. Every route answers `404` while `SCORECARDS_ENABLED` is off. |

```powershell
curl.exe -o INC000123-evidence.json "http://127.0.0.1:8000/api/v1/incidents/<incident_id>/evidence-pack"
```

### Rules if you build one by hand this quarter

* **Only `PUBLISHED` or `FINAL` scorecards go to a vendor.** `DRAFT`, `SHADOW` and `WITHHELD` are
  the operator's working papers (spec §7.6.2). The lane being built hides them from anyone below
  duty manager; do not undo that by pasting them into a spreadsheet.
* **A vendor sees only its own rows.** Check every sheet before it leaves.
* **The contract numbers are placeholders.** `config/sla_terms.yaml` is marked
  `contract_is_synthetic: true` until Supply Chain fills it in. Say "defaults, not contract" on
  the cover page, and treat every credit as a proposal.

### What you should see

The evidence pack returns JSON with a `sha256`. Asking again returns the same hash.

---

## 19. The shadow-shift checklist

**The shadow flags do not work.** `BROADCAST_SHADOW`, `SMS_SHADOW` and `SCORECARDS_SHADOW` are in
`.env.example` and are **read by no code** (`docs/CONFORMANCE.md` B-10). Setting them does
nothing. So a shadow shift as the spec defines it — the feature writing its rows and rendering
its drafts while sending nothing — **cannot be run today.** Four sign-offs are waiting on it.
As of 2026-09-21.

### What a shadow shift is

Spec §8.9 rule 2. Any phase that changes the words engineers receive (Phase 2), what is scored
(Phase 4), or what leaves the building (Phase 3 SMS, Phase 6) runs **one full NOC shift** with
the feature in shadow. A **named floor lead** then signs `docs/SIGNOFF.md` — date, phase, what
was inspected, issues found — **before** the flag goes live.

It is how the floor, not the developer, decides the wording is right.

### The checklist, once the flags work

Before the shift:

- [ ] Engineering confirms in writing that the shadow flag for this phase is wired (B-10 closed).
- [ ] The shadow flag is set in `.env`; the live flag is **not**. Restart.
- [ ] `curl.exe http://127.0.0.1:8000/api/v1/email/status` — note what it says. A shadow shift
      must not depend on email being mocked to stay safe.
- [ ] The floor lead is named, and is not the developer who built the feature.
- [ ] The `docs/SIGNOFF.md` block for this phase is open and the "under which flags" line is
      filled in.

During the shift:

- [ ] Every shadow rendering is read against what the floor actually sent for the same incident.
- [ ] Every wording or number the floor would not have sent is written down with its incident
      number.
- [ ] Nothing reached anyone outside the NOC. Check with the outbox query in section 10: no `SENT`
      row produced by the shadow feature.

After the shift:

- [ ] Issues listed in `docs/SIGNOFF.md`, each with who owns the fix.
- [ ] The floor lead signs **only** if the pass criteria in that block are met.
- [ ] The live flag is switched on only after the signature — and only by the product owner's
      decision where one is needed (Phase 2 needs decision D3).

### What you can do today instead — and what it does not prove

* **Wording (Phase 2).** Read the proposed SMS and email on HITL cards during a normal shift and
  write down every objection. Worth doing. **Not** a shadow shift: with `ALERT_ENVELOPE_V2` off
  you are reading the old renderer, not the one the sign-off is about.
* **SMS (Phase 3).** There is no SMS adapter. Nothing to shadow.
* **Scorecards (Phase 4).** The lane being built on 2026-09-21 computes a first-period card as
  `SHADOW`, and `POST /api/v1/scorecards/{id}/shadow-review` records that a named person
  inspected it. When it lands, that is a real control — but it is per card, not a shift, and it
  does not replace the signature.

**Do not sign `docs/SIGNOFF.md` for a shift the flags could not gate.** A signed record of a
shadow shift that did not happen is worse than an empty form.

---

## 20. What "STALE" and "LOW CONFIDENCE" mean

### STALE

On the **Regions** page (`/regions`) and the risk strip on the **Wallboard**, **STALE means "we
cannot see this region"**. It does not mean calm. It is grey on purpose: grey reads as absence.
The Regions data asks for any **reader** role: operations, `management`, `msp_coordinator`,
`field_engineer` or `planning`.

Each region gets one status, checked worst first:

| Status | When |
|---|---|
| **ALERT** | an open P1, or a live storm or flood flag |
| **WATCH** | an open P2, or any open incident past its restore SLA |
| **STALE** | none of the above, **and no outside signal for this region is fresh** |
| **CALM** | none of the above, and at least one fresh signal says so |

A storm or flood flag counts only while it is fresh. An old flag cannot make a region ALERT.

STALE outranks CALM even when P3 or P4 incidents are open. Those are already shown in the counts.
STALE says only that nobody can vouch for the region.

**Why a region with no data is never green.** A green tile is a claim: "we looked, and it is
fine". With no fresh signal, nobody looked. A dead poller, a mis-mapped region or a silent alarm
feed would all look like reassurance. The page refuses to make that claim. Every configured
region is always shown, even with zero incidents — a region with nothing on it is the one to ask
about.

**On day one most regions read STALE.** Weather is off by default (`WEATHER_ENABLED=false`), so no
forecast has ever been stored. That is the honest answer, not a fault. (Flood and KMD warning
pollers are being added as of 2026-09-21; when they land, their readings count as signals too.)
Each region carries its weather reason, in words:

| Reason shown | What it means | Do |
|---|---|---|
| `WEATHER_ENABLED is false — the weather poller has never run` | the lane is off | nothing, unless you want weather |
| `WEATHER_ENABLED is false — this reading is frozen, not live` | old data from when it was on | nothing; do not trust it |
| `no forecast stored for this region yet` | on, but nothing fetched for this region | check the job (below) |
| `last fetch failed: <error>` | on, and the provider call failed | check the job; check TLS (section 3) |
| `last reading is past its validity window` | on, but the newest reading has expired | check the job |

**How old is too old.** The weather poller runs every 15 minutes and each reading is valid for one
hour. On the Wallboard's risk strip, a reading older than 20 minutes shows **AGEING** — a poll was
missed. After 60 minutes, or once the reading's own validity time has passed, it shows **STALE**.
A reading with no readable time is treated as STALE: an unknown age is not a fresh one.

**Check the poller** (**platform reader** role):

```powershell
curl.exe http://127.0.0.1:8000/api/v1/scheduler/status
```

Find `weather_regions`: `enabled`, `last_status`, `consecutive_failures`, `circuit_open`. Then
section 11 or 12.

### LOW CONFIDENCE

**Not in the committed build.** The spec wants a region whose storm warnings have proved wrong —
precision below 20 % for 30 days — labelled "LOW CONFIDENCE" in grey, not hidden. That needs a
backtest that scores past warnings against what followed (`docs/CONFORMANCE.md` C-14). In the
committed code the dashboard sends `precision_30d: null` for every region and the words "LOW
CONFIDENCE" appear nowhere on screen.

The backtest is being built as of 2026-09-21 (`scripts\backtest_signals.py`). Even when it lands,
it refuses to print a precision until there are **90 days** of stored history and at least 10
finished warnings — below that it says `INSUFFICIENT_DATA`. With weather off by default, that is
at least three months after the poller is switched on.

Until then, what you see beside a weather flag is **"precision unmeasured"** (or "precision: not
yet measured" once the backtest lands). That is the truth:
no storm warning in this system has been checked against what actually happened. Treat every
weather flag as unproven advice.

When LOW CONFIDENCE does appear, it means: this region's warnings have been wrong more than four
times in five for a month. The flag is still shown. Do not act on it alone.

### What you should see

With weather off: every region without a P1, P2 or SLA breach reads STALE, with the
"WEATHER_ENABLED is false" reason. With weather on and healthy: regions read CALM or worse, and
the `weather_regions` job shows `"last_status": "SUCCEEDED"`.

---

## 21. Confirm a KPLC planned-power notice

**Not built.** The KPLC planned-power lane does not exist (`docs/CONFORMANCE.md` C-16). There is no
KPLC poller (`src\noc_agents\pollers\` holds only the weather one), no notice parser, no table of
planned interruptions, no `/power-notices` route, and nothing that raises a confirmation card. The
card type `CONFIRM_POWER_NOTICE` was declared on 2026-09-21 and nothing uses it yet. `KPLC_ENABLED`
and `KPLC_NOTICES_URL` are in `.env.example` and read by nothing. Part of the lane is also waiting
on the floor: pinning the parser needs a real KPLC interruption-notice PDF.

Until it lands, handle a KPLC notice the way the floor did before this system existed. Read the
notice. Work out which sites it covers — the site catalogue carries `kplc_region` and
`kplc_area_hints` for each site (`curl.exe http://127.0.0.1:8000/api/v1/sites`, any **reader**
role; the catalogue is still the demo one until the floor supplies real site data). Put it on the shift handover, and add a work note
to any open incident it explains. There is nothing in the system to confirm, and nothing will ask
you to.
