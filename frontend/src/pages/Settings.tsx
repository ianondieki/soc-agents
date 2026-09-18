import { useEffect, useState } from "react";
import { api, runLiveRainStorm } from "../api";

const PRESETS = [
  {
    label: "Nairobi East HUB power → EGYPRO",
    body: {
      site_id: "SFC-NBIE-HUB-EMB",
      site_name: "Embakasi East Aggregation HUB",
      site_type: "HUB",
      region_code: "NBI_E",
      county: "Nairobi",
      alarm_code: "POWER_GRID_FAIL",
      failure_domain: "POWER",
      users_affected: 450000,
      access_notes: "Genset not auto-started",
    },
  },
  {
    label: "Nairobi West radio → HUAWEI_RADIO",
    body: {
      site_id: "SFC-NBIW-ENB-CBD07",
      site_name: "Upper Hill eNodeB 07",
      site_type: "ENODEB",
      region_code: "NBI_W",
      county: "Nairobi",
      alarm_code: "RADIO_CELL_DOWN",
      failure_domain: "RADIO",
      users_affected: 22000,
    },
  },
  {
    label: "Mt Kenya TX fibre → SOLITON",
    body: {
      site_id: "SFC-MTK-HUB-THK",
      site_name: "Thika Mt Kenya HUB",
      site_type: "HUB",
      region_code: "MTK",
      county: "Kiambu",
      alarm_code: "TX_FIBRE_CUT",
      failure_domain: "TRANSMISSION",
      users_affected: 160000,
    },
  },
  {
    label: "Coast power (Huawei radio region)",
    body: {
      site_id: "SFC-CST-HUB-MSA",
      site_name: "Mombasa Island HUB",
      site_type: "HUB",
      region_code: "CST",
      county: "Mombasa",
      alarm_code: "POWER_GRID_FAIL",
      failure_domain: "POWER",
      users_affected: 220000,
    },
  },
  {
    label: "Rift HUB power → TETRANET",
    body: {
      site_id: "SFC-RFT-HUB-NKR",
      site_name: "Nakuru Rift HUB",
      site_type: "HUB",
      region_code: "RFT",
      county: "Nakuru",
      alarm_code: "GENSET_FAIL",
      failure_domain: "POWER",
      users_affected: 180000,
    },
  },
  {
    label: "Western-Nyanza HUB power → TETRANET",
    body: {
      site_id: "SFC-WNY-HUB-KSM",
      site_name: "Kisumu Western-Nyanza HUB",
      site_type: "HUB",
      region_code: "WNY",
      county: "Kisumu",
      alarm_code: "POWER_GRID_FAIL",
      failure_domain: "POWER",
      users_affected: 190000,
    },
  },
  {
    label: "Cascade child under Westlands HUB",
    body: {
      site_id: "SFC-NBIW-ENB-CBD07",
      site_name: "Upper Hill eNodeB 07",
      site_type: "ENODEB",
      region_code: "NBI_W",
      alarm_code: "SITE_DOWN",
      failure_domain: "POWER",
      users_affected: 22000,
      parent_hub_id: "SFC-NBIW-HUB-WLD",
    },
  },
  {
    label: "P1 national CORE (>500k)",
    body: {
      site_id: "SFC-NBI-CORE-PS01",
      site_name: "Nairobi PS-Core Node (demo)",
      site_type: "CORE",
      region_code: "NBI_W",
      alarm_code: "CORE_DEG",
      failure_domain: "CORE",
      users_affected: 2500000,
    },
  },
];

export default function Settings({
  session,
  onSession,
  profile,
  onInjected,
}: {
  session: any;
  onSession: (s: any) => void;
  profile: any;
  onInjected: () => void;
}) {
  const [name, setName] = useState(session?.display_name || "NOC Analyst");
  const [role, setRole] = useState(session?.role || "noc_analyst");
  const [sites, setSites] = useState<any[]>([]);
  const [last, setLast] = useState("");
  const [emailSt, setEmailSt] = useState<any>(null);
  const [emailMsg, setEmailMsg] = useState("");

  useEffect(() => {
    api.sites().then(setSites).catch(() => undefined);
    fetch("/api/v1/email/status")
      .then((r) => r.json())
      .then(setEmailSt)
      .catch(() => undefined);
  }, []);

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Settings & Demo Inject</h2>
      <div className="panel">
        <h3>Session role (team demo)</h3>
        <div className="form-row">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Display name" />
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="noc_analyst">noc_analyst</option>
            <option value="shift_supervisor">shift_supervisor</option>
            <option value="duty_manager">duty_manager</option>
            <option value="rnio">rnio</option>
            <option value="msp_viewer">msp_viewer</option>
            <option value="automation_admin">automation_admin</option>
          </select>
          <button
            className="btn primary"
            onClick={async () => {
              const s = await api.setSession({ display_name: name, role });
              onSession(s);
            }}
          >
            Save session
          </button>
        </div>
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Gmail demo email</h3>
        {emailSt ? (
          <div className="muted">
            <div>
              Status:{" "}
              <strong style={{ color: emailSt.configured ? "var(--ok)" : "var(--warn)" }}>
                {emailSt.configured ? "SMTP READY (Gmail)" : "MOCK ONLY"}
              </strong>
            </div>
            <div>From: {emailSt.from || "—"}</div>
            <div>To: {(emailSt.recipients || []).join(", ") || "—"}</div>
            <div>{emailSt.hint}</div>
            <p className="muted">
              Set <code>GMAIL_ADDRESS</code> + <code>GMAIL_APP_PASSWORD</code> (+ optional{" "}
              <code>DEMO_EMAIL_TO</code>) then restart API. See docs/GMAIL_SETUP.md
            </p>
            <button
              className="btn primary"
              onClick={async () => {
                const r = await fetch("/api/v1/email/test", { method: "POST" });
                const j = await r.json();
                setEmailMsg(`${j.mode}: ${j.detail}`);
              }}
            >
              Send test email now
            </button>
            {emailMsg && <p className="chip ok">{emailMsg}</p>}
          </div>
        ) : (
          <p className="muted">Loading email status…</p>
        )}
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Six Safaricom geographical regions + MSP map</h3>
        <p className="muted">
          ~7000 sites / 50M+ subscribers. Power: NBI_E+MTK→Egypro; RFT+WNY→Tetranet. Radio: NBI_W+CST→Huawei.
          Fibre: Egypro Fibre, Soliton (Mt Kenya), Camusat, Ecta, Adrian, Alan Dick.
        </p>
        <div className="list">
          {profile?.regions &&
            Object.entries(profile.regions).map(([code, r]: any) => (
              <div key={code} className="row" style={{ cursor: "default", gridTemplateColumns: "90px 1fr" }}>
                <strong>{code}</strong>
                <div>
                  <div>
                    {r.label} · RNIO {r.rnio} · FE {r.fe_oncall}
                  </div>
                  <div className="muted">{r.description}</div>
                </div>
              </div>
            ))}
        </div>
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Heavy-rain MW storm (LIVE)</h3>
        <p className="muted">
          Best launched from <strong>Mission Control</strong> so agent steps stream live. Bulk button also
          available:
        </p>
        <button
          className="btn storm"
          onClick={async () => {
            setLast("Storm running — watch Mission Control ticker…");
            const res = await runLiveRainStorm();
            setLast(`Storm done: ${res.count} events`);
            onInjected();
          }}
        >
          Run rain / MW cascade LIVE
        </button>
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Single-event inject (creates INC###### tickets)</h3>
        <div className="list">
          {PRESETS.map((p) => (
            <div key={p.label} className="row" style={{ gridTemplateColumns: "1fr auto" }}>
              <div>{p.label}</div>
              <button
                className="btn primary"
                onClick={async () => {
                  const res = await api.inject(p.body);
                  const i = res.incident;
                  setLast(
                    `${i.incident_number} · ${i.priority} · MSP ${i.responsible_msp || i.msp_name} · FE ${i.fe_name}`
                  );
                  onInjected();
                }}
              >
                Inject
              </button>
            </div>
          ))}
        </div>
        {last && <p className="chip ok">Last: {last}</p>}
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Seed sites ({sites.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Site ID</th>
              <th>Name</th>
              <th>Type</th>
              <th>Region</th>
            </tr>
          </thead>
          <tbody>
            {sites.map((s) => (
              <tr key={s.site_id}>
                <td>{s.site_id}</td>
                <td>{s.site_name}</td>
                <td>{s.site_type}</td>
                <td>{s.region_code}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
