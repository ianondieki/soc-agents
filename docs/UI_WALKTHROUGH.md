# 5-minute team demo — Safaricom NOC Mission Control

## Setup

1. Start API: `python -m uvicorn noc_agents.main:app --app-dir src --port 8000`
2. Start UI: `cd frontend && npm run dev` → http://127.0.0.1:5173
3. Open **two browsers** (or one normal + one private): Analyst + Supervisor

## Script

| Step | Actor | Action | What to show |
|------|--------|--------|----------------|
| 1 | Both | Open Mission Control | Empty or prior state; LIVE WS chip green |
| 2 | Analyst | Settings → Inject **Westlands HUB power** | Ticker fires; KPI open/P2/P1 bump |
| 3 | Both | Open the new `SFC-INC-…` | Workflow nodes green; click Severity → rationale + M-PESA |
| 4 | Supervisor | HITL Inbox → Claim → Approve | Broadcasts released; HITL count drops |
| 5 | Analyst | Inject Mombasa fibre + Kitui BTS | Regional diversity NBI/CST/EST |
| 6 | Supervisor | Shift Desk → Generate handover | Owners + priorities for night shift |
| 7 | Both | Wallboard | Large P1/P2 cards for room TV |
| 8 | Analyst | Inject same Westlands event again | Idempotent merge — no duplicate major |

## Trust questions the UI must answer

1. Why not P4? → Severity step rationale (HUB floor / users)  
2. Why ATC? → Assign step matrix rationale  
3. Waiting on human? → HITL purple nodes + inbox  
4. Who owns it for night shift? → Handover watchlist  
