"""The golden fixtures and the golden test must never drift apart.

``scripts/golden_diff.py`` diffs a fresh run against ``tests/fixtures/golden/*.json``, and
``tests/integration/test_golden_sequence.py`` pins the same run with Python literals. Two copies
of one truth drift unless something compares them, so this test does: every literal the golden
test pins -- the 26 run-scoped and 6 merge/cascade event tuples, envelope and payload key sets,
the ``email.sent`` payload and its position after ``agent.run.finished`` (spec 2.1 R4), run
rows, step rows, input summaries, tools, confidences, outputs, rationales and work-note order
(R3) -- is checked against the fixture JSON.

It runs no pipeline and opens no database: it reads two files. If it fails, one side was
edited without the other. Decide which one is right; never "fix" both to agree blindly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_golden_fixtures_hold_exactly_what_the_golden_test_pins():
    t = _load("_golden_sequence_literals", ROOT / "tests" / "integration" / "test_golden_sequence.py")
    fx = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "tests" / "fixtures" / "golden").glob("*.json")}
    assert set(fx) == {"full_lifecycle_hitl", "full_lifecycle_auto_broadcast", "merge_short_circuit", "cascade_child_short_circuit"}
    checks = 0

    def eq(a, b, what):
        nonlocal checks
        checks += 1
        assert a == b, f"{what}: fixture={a!r} test={b!r}"

    def tup(rows):
        return [tuple(r) for r in rows]

    H, A, M, C = fx["full_lifecycle_hitl"], fx["full_lifecycle_auto_broadcast"], fx["merge_short_circuit"], fx["cascade_child_short_circuit"]
    # inputs
    eq(H["input_events"], [t.HUB_EVENT], "hitl inputs")
    eq(A["input_events"], [t.BTS_EVENT], "auto inputs")
    eq(M["input_events"], [t.HUB_EVENT, t.HUB_EVENT], "merge inputs")
    eq(C["input_events"], [t.PARENT_HUB_EVENT, t.CHILD_EVENT], "cascade inputs")
    # event literals
    eq(tup(H["events"]), t.GOLDEN_FULL_HITL, "GOLDEN_FULL_HITL")
    eq(len(H["events"]), 26, "26 hitl")
    eq(tup(A["events"]), t.GOLDEN_FULL_AUTO, "GOLDEN_FULL_AUTO")
    eq(len(A["events"]), 26, "26 auto")
    eq(tup(M["events"]), t.GOLDEN_MERGE, "GOLDEN_MERGE")
    eq(len(M["events"]), 6, "6 merge")
    eq(tup(C["events"]), t.GOLDEN_CASCADE, "GOLDEN_CASCADE")
    eq(len(C["events"]), 6, "6 cascade")
    # envelope and payload key sets (_check_envelopes)
    for name, f in fx.items():
        eq(f["envelope_keys"], [sorted(t.ENVELOPE_KEYS)], f"{name} envelope keys")
        for etype, sets in f["payload_keys"].items():
            eq(sets, [sorted(t.PAYLOAD_KEYS[etype])], f"{name} payload keys {etype}")
        eq(f["runs_created"], 1, f"{name} _the_run exactly one new run")


    def payload(f, etype):
        return next(p["payload"] for p in f["event_payloads"] if p["type"] == etype)


    eq(payload(H, "incident.created"), {"incident_number": "INC000001", "priority": "P2", "site_id": "SFC-NBIE-HUB-EMB", "region_code": "NBI_E", "requires_hitl": True}, "hitl created payload")
    eq(payload(H, "agent.run.finished")["error"], None, "hitl run.finished error")
    eq(payload(A, "incident.created"), {"incident_number": "INC000001", "priority": "P4", "site_id": "SFC-MTK-BTS-MCH04", "region_code": "MTK", "requires_hitl": False}, "auto created payload")
    eq(payload(M, "incident.merged"), {"incident_number": "INC000001"}, "merge payload")
    eq(payload(C, "incident.cascade_child"), {"parent": "INC000001", "child_site": "SFC-NBIW-ENB-CBD07", "child_sites_down": 1}, "cascade payload")
    # email.sent (auto): exactly one, literal payload, incident id set, after agent.run.finished (R4)
    eq([g["type"] for g in A["global_events"]], ["email.sent"], "one email.sent")
    eq(A["global_events"][0]["payload"], {"incident_number": "INC000001", "mode": "mock", "to": [], "detail": "No DEMO_EMAIL_TO / GMAIL_ADDRESS \u2014 email body stored only (mock)", "status": "SENT"}, "email payload")
    eq(A["global_events"][0]["incident_id_set"], True, "email incident id")
    types = [ty for ty, _s in A["global_history"]]
    eq(types.index("email.sent") > types.index("agent.run.finished"), True, "R4 email after run.finished")
    for name in ("full_lifecycle_hitl", "merge_short_circuit", "cascade_child_short_circuit"):
        eq(fx[name]["global_events"], [], f"{name} no global events")
    # run rows
    eq((H["run"]["status"], H["run"]["current_node"], H["run"]["error_summary"]), ("WAITING_HITL", "MONITOR", None), "hitl run")
    eq((A["run"]["status"], A["run"]["current_node"], A["run"]["error_summary"]), ("SUCCEEDED", None, None), "auto run")
    for f in (M, C):
        eq((f["run"]["status"], f["run"]["current_node"]), ("SUCCEEDED", None), "short-circuit run")
    for f in fx.values():
        eq(f["run"]["bound_to_returned_incident"], True, "run.incident_id == returned incident")
    eq((M["incident"]["same_as_setup_incident"], M["incident"]["incidents_in_db"], M["incident"]["child_sites_down"]), (True, 1, 0), "merge incident")
    eq((C["incident"]["same_as_setup_incident"], C["incident"]["incidents_in_db"], C["incident"]["child_sites_down"]), (True, 1, 1), "cascade incident")


    def rows(f):
        return [(s["seq"], s["node"], s["agent"], s["status"]) for s in f["steps"]]


    eq(rows(H), [(seq, n, a, st) for (ty, seq, n, a, st, _x, _y) in t.GOLDEN_FULL_HITL if ty == "agent.step.completed"], "hitl step rows")
    eq(rows(A), [(seq, n, a, st) for (ty, seq, n, a, st, _x, _y) in t.GOLDEN_FULL_AUTO if ty == "agent.step.completed"], "auto step rows")
    eq([s["input_summary"] for s in H["steps"]], t.HITL_INPUT_SUMMARIES, "HITL_INPUT_SUMMARIES")
    eq([s["input_summary"] for s in A["steps"]], t.AUTO_INPUT_SUMMARIES, "AUTO_INPUT_SUMMARIES")
    for f, tools in ((H, t.TOOLS_HITL_RUN), (A, t.TOOLS_AUTO_RUN)):
        by = {s["node"]: s for s in f["steps"]}
        for node, tl in tools.items():
            eq(by[node]["tools_called"], tl, f"{f['scenario']} tools {node}")
            eq(by[node]["confidence"], t.CONFIDENCE.get(node, 0.9), f"{f['scenario']} confidence {node}")
    by = {s["node"]: s for s in H["steps"]}
    eq(by["INGEST"]["output_summary"], "normalized event fingerprint=SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER", "ingest out")
    eq(by["INGEST"]["rationale"], "Normalized alarm payload; ready for correlation window check", "ingest why")
    eq(by["CORRELATE"]["output_summary"], "no open duplicate \u2014 new incident candidate", "corr out")
    eq(by["CORRELATE"]["rationale"], "Fingerprint unique in correlation window; no parent HUB merge", "corr why")
    eq(by["ENRICH"]["output_summary"], "users_est=450000, region=NBI_E, hub=True, class=CRITICAL", "enrich out")
    eq(by["ENRICH"]["rationale"].startswith("CMDB/mock enrich: Nairobi East; FE on-call=FE-NBI-E-01; "), True, "enrich why prefix")
    eq(by["SEVERITY"]["output_summary"], "priority=P2 mpesa_risk=True", "sev out")
    eq(by["SEVERITY"]["rationale"].startswith("operator=safaricom; users=450000\u2192P2; site_type=HUB floor="), True, "sev why prefix")
    eq(by["TICKET"]["output_summary"], "created INC000001 category=POWER_GRID", "ticket out")
    eq(by["TICKET"]["rationale"], "Unique incident number; TT fields filled as NOC UI would: category=POWER_GRID, class=CRITICAL, tech=4G; HUB auto-ticket=yes", "ticket why")
    eq(by["ASSIGN"]["output_summary"], "MSP:EGYPRO", "assign out")
    eq(by["ASSIGN"]["rationale"].startswith("region=NBI_E ("), True, "assign why prefix")
    eq(by["HITL"]["output_summary"], "HITL task <uuid>", "hitl out (masked; test uses HITL_TASK_RE)")
    eq(bool(t.HITL_TASK_RE.match("HITL task 770628b8-65c1-42b1-ae5f-84cae4b3f808")), True, "regex sanity")
    eq(by["HITL"]["rationale"], "P2 under L2_GUARDED requires human approval before external blast", "hitl why")
    eq(by["BROADCAST"]["output_summary"], "broadcasts drafted; waiting HITL", "bc out")
    eq(by["BROADCAST"]["rationale"], "External wording held for supervisor/duty manager \u2014 approve HITL to send Gmail", "bc why")
    eq(by["EXEC_BRIEF"]["output_summary"], "exec brief published", "brief out")
    eq(by["EXEC_BRIEF"]["rationale"], "Proactive brief to reduce management call volume into NOC", "brief why")
    eq(by["LEDGER"]["output_summary"], "ledger_<date>_<shift>.xlsx", "ledger out (masked; test uses LEDGER_FILE_RE)")
    eq(bool(t.LEDGER_FILE_RE.match("ledger_2026-09-21_DAY.xlsx")), True, "ledger regex sanity")
    eq(by["LEDGER"]["rationale"], "Shift failure ledger row appended for supervisor scan", "ledger why")
    eq(by["RECURRENCE"]["output_summary"], "count=1 threshold=3", "rec out")
    eq(by["RECURRENCE"]["rationale"], "Below recurrence threshold \u2014 no problem record", "rec why")
    eq(by["MONITOR"]["output_summary"], "sla timers armed", "mon out")
    eq(by["MONITOR"]["rationale"], "Note-interval chase scheduled per priority/region multiplier", "mon why")
    ba = {s["node"]: s for s in A["steps"]}
    eq(ba["HITL"]["output_summary"], "auto-approved under autonomy policy", "auto hitl out")
    eq(ba["HITL"]["rationale"], "P4 allowed auto-broadcast at L2_GUARDED", "auto hitl why")
    eq(ba["BROADCAST"]["output_summary"], "queued 3 outbox rows for ['RNIO', 'FIELD_ENGINEER']; email=PENDING", "auto bc out")
    eq(ba["BROADCAST"]["rationale"], "P4 auto-send under L2_GUARDED (approved_by=policy:L2_GUARDED); dispatcher transmits after commit", "auto bc why")
    # merge / cascade steps
    eq(rows(M), [(1, "INGEST", "IngestCorrelationAgent", "SUCCEEDED"), (2, "CORRELATE", "IngestCorrelationAgent", "SUCCEEDED")], "merge rows")
    eq(rows(C), [(1, "INGEST", "IngestCorrelationAgent", "SUCCEEDED"), (2, "CORRELATE", "IngestCorrelationAgent", "SUCCEEDED")], "cascade rows")
    eq([s["input_summary"] for s in M["steps"]], t.HITL_INPUT_SUMMARIES[:2], "merge inputs")
    eq(M["steps"][1]["output_summary"], "merged into INC000001", "merge out")
    eq(M["steps"][1]["rationale"], "Duplicate within 15m window \u2014 idempotent merge", "merge why")
    eq(M["steps"][1]["tools_called"], [{"name": "find_open_by_fingerprint", "ok": True, "latency_ms": 2}], "merge tools")
    eq([s["input_summary"] for s in C["steps"]], ["site=SFC-NBIW-ENB-CBD07", "fp=SFC-NBIW-ENB-CBD07|SITE_DOWN|POWER"], "cascade inputs")
    eq(C["steps"][1]["output_summary"], "cascade child under INC000001", "cascade out")
    eq(C["steps"][1]["rationale"], "Site feeds parent HUB SFC-NBIW-HUB-WLG; linked as child note instead of new major (cascade control)", "cascade why")
    eq(C["steps"][1]["tools_called"], [{"name": "link_parent_hub", "ok": True, "latency_ms": 2}], "cascade tools")
    # work notes
    eq([(a, s) for a, _r, s, _b in H["work_notes"]], [("WorklogMonitorAgent", "agent")], "hitl notes")
    eq(H["work_notes"][0][3].startswith("Monitoring started. SLA ack due "), True, "monitor note prefix")
    eq([(a, s) for a, _r, s, _b in A["work_notes"]], [("WorklogMonitorAgent", "agent"), ("BroadcastCommsAgent", "email")], "R4 note order")
    eq([tuple(n) for n in M["work_notes"]], [("IngestCorrelationAgent", "AGENT", "agent", "Correlated duplicate alarm POWER_GRID_FAIL into open ticket.")], "merge note")
    eq([tuple(n) for n in C["work_notes"]], [("IngestCorrelationAgent", "AGENT", "cascade", "Cascade child alarm: SFC-NBIW-ENB-CBD07 (SITE_DOWN/POWER) linked under HUB major INC000001. Child count now 1.")], "cascade note")

    # A floor, so a refactor that silently skips a block cannot pass with fewer checks.
    assert checks >= 150, checks
