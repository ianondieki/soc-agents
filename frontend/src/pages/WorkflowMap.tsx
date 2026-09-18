const NODES = [
  "INGEST",
  "CORRELATE",
  "ENRICH",
  "SEVERITY",
  "TICKET",
  "ASSIGN",
  "HITL",
  "BROADCAST",
  "EXEC_BRIEF",
  "LEDGER",
  "RECURRENCE",
  "MONITOR",
];

export default function WorkflowMap({ profile }: { profile: any }) {
  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Global Workflow Map</h2>
      <p className="muted">
        Training view of the multi-agent graph. Autonomy: {profile?.autonomy_level}. HITL gate holds P1/P2 external
        broadcasts under L2.
      </p>
      <div className="panel">
        <div className="workflow">
          {NODES.map((n, i) => (
            <div key={n} style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
              <div className={`node ${n === "HITL" ? "waiting_hitl" : "succeeded"}`}>{n}</div>
              {i < NODES.length - 1 && <span className="arrow">→</span>}
            </div>
          ))}
        </div>
        <ul className="muted" style={{ marginTop: "1rem", lineHeight: 1.6 }}>
          <li>
            <strong>INGEST/CORRELATE</strong> — collapse alarm floods; Safaricom HUB cascade awareness
          </li>
          <li>
            <strong>SEVERITY</strong> — P4 under 50k users; HUB floor P2; CORE/NBI M-PESA tag
          </li>
          <li>
            <strong>ASSIGN</strong> — power→ATC/Camusat, fibre→Egypro, radio→regional FE
          </li>
          <li>
            <strong>HITL</strong> — whole team inbox with claim
          </li>
          <li>
            <strong>LEDGER/HANDOVER</strong> — EAT shift continuity
          </li>
        </ul>
      </div>
    </div>
  );
}
