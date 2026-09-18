# Gmail demo email setup

When configured, the NOC multi-agent system **really emails** on:

1. **Outage inject** with auto-broadcast (typically **P3/P4** at L2)  
2. **HITL Approve** for **P1/P2** (after supervisor approves)  
3. **Shift handover** generate  
4. **POST /api/v1/email/test** (one-off connectivity check)

## 1. Create a Gmail App Password

1. Use a Google account with **2-Step Verification** enabled.  
2. Open [Google Account → Security → App passwords](https://myaccount.google.com/apppasswords).  
3. Create an app password for **Mail**.  
4. Copy the **16-character** password (spaces optional).

> Normal Gmail login password will **not** work with SMTP.

## 2. Set environment variables (Windows PowerShell)

```powershell
cd C:\Users\PC\Desktop\second-brain\soc-agents

$env:GMAIL_ADDRESS = "your.email@gmail.com"
$env:GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"   # app password
$env:DEMO_EMAIL_TO = "your.email@gmail.com"       # where alerts go
$env:EMAIL_ENABLED = "true"                       # REQUIRED since Phase 0 — default is false

# Start API in THIS same terminal so it inherits env
python -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port 8000
```

Or create a local `.env` file (see `.env.example`) and load it before start:

```powershell
# Optional: pip install python-dotenv  (auto-loaded if present)
```

## 3. Verify

```powershell
# In another terminal with same env vars, or use browser/curl:
curl -X POST http://127.0.0.1:8000/api/v1/email/test
```

You should get JSON like `"mode": "smtp"` and a mail in your inbox.

Then:

1. Open Mission Control UI  
2. **Settings → Inject** a small outage (P4 BTS) for immediate mail, **or**  
3. Inject a HUB (P2) → **HITL Inbox → Approve** → mail sends  

## 4. Behaviour notes

| Situation | Email? |
|-----------|--------|
| Credentials missing **or `EMAIL_ENABLED` not `true`** | No real mail — mock only (safe for CI/tests). Since Phase 0 real sending is opt-in: credentials alone are not enough. |
| P4 / auto-broadcast | Sends immediately on inject |
| P1/P2 L2 | Sends only after **HITL approve** |
| SMS | Still mock (no Africa's Talking yet) |

## 5. Security

- Never commit `GMAIL_APP_PASSWORD` or `.env`  
- Prefer a spare Gmail for demos  
- You can revoke the app password anytime in Google Account settings  
