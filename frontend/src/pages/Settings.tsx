import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, runLiveRainStorm } from "../api";
import { humanEnum } from "../lib/agents";
import { IconDot } from "../lib/icons";

/** `route` names the MSP the assignment matrix should pick, shown beside the label. */
const PRESETS: { label: string; route?: string; body: Record<string, unknown> }[] = [
  {
    label: "Nairobi East HUB power",
    route: "Egypro",
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
    label: "Nairobi West radio",
    route: "Huawei radio",
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
    label: "Mt Kenya TX fibre",
    route: "Soliton",
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
    label: "Rift HUB power",
    route: "Tetranet",
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
    label: "Western-Nyanza HUB power",
    route: "Tetranet",
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

/**
 * The nine roles `api/auth.py` defines (`Role`, and `ROLES` derived from it). The switcher
 * offered three names that do not exist there -- `rnio`, `msp_viewer`, `automation_admin` -- and
 * omitted `management`, `msp_coordinator` and `admin`, so the demo could not reach the roles the
 * §9.3 matrix gates on: nothing could be viewed as management, and no unreleased scorecard could
 * be read at all (CONFORMANCE C-32). The backend does not validate what this switcher stores, so
 * an unknown name is accepted and then matches no allow-list.
 *
 * The labels say what the role is for on this demo, not what it is allowed: with
 * `AUTH_DISABLED=true` (the demo default) `require_role` is inert and the switcher grants
 * nothing. It decides which name is RECORDED as the actor, and what the role-aware pages say.
 */
const ROLES: { value: string; label: string }[] = [
  { value: "noc_analyst", label: "the floor: incidents, notes, timeline" },
  { value: "shift_supervisor", label: "closes and reverses stop clocks, computes scorecards" },
  { value: "duty_manager", label: "approves, publishes and finalises" },
  { value: "management", label: "reads everything, including unreleased scorecards" },
  { value: "msp_coordinator", label: "vendor side: its own vendor's released cards" },
  { value: "field_engineer", label: "field notes from site" },
  { value: "planning", label: "maintenance windows and capacity" },
  { value: "legal", label: "contracts, audit, released scorecards" },
  { value: "admin", label: "everything, including platform actions" },
];

/** `api/auth.DEFAULT_ROLE`: what a client that never touched this switcher already is. */
const DEFAULT_ROLE = "noc_analyst";

/** "msp_coordinator" → "MSP coordinator": the role id as a person reads it (the value sent stays the id). */
function roleWords(value: string): string {
  const h = humanEnum(value.toUpperCase());
  return h ? h[0].toUpperCase() + h.slice(1) : value;
}

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
  const [role, setRole] = useState(session?.role || DEFAULT_ROLE);
  const [sites, setSites] = useState<any[]>([]);
  // The last inject or storm result, as separate facts (rendered as spans, never joined with dots).
  const [last, setLast] = useState<string[]>([]);
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
    <div className="content-narrow">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="lead">Who the demo records as the actor, the email channel, and alarms to inject.</p>
        </div>
      </div>
      <div className="panel">
        <h3>Session role (team demo)</h3>
        <div className="form-row">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Display name" aria-label="Display name" />
          <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Session role">
            {ROLES.map((r) => (
              <option key={r.value} value={r.value}>
                {roleWords(r.value)} — {r.label}
              </option>
            ))}
            {/* A role stored before this list was corrected would otherwise leave the select
                blank, with no way to see what it is set to. */}
            {role && !ROLES.some((r) => r.value === role) && (
              <option value={role}>{role} — not a role this backend knows</option>
            )}
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
        <div className="muted">
          These are the nine roles the API defines. The switcher is a demo affordance, not a sign-in: with{" "}
          <code>AUTH_DISABLED=true</code> it grants nothing, and it decides which name is recorded as the actor and what
          the role-aware screens say. With sign-in on, the signed-in user decides instead.
        </div>
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Gmail demo email</h3>
        {emailSt ? (
          <div className="muted">
            <div>
              Status:{" "}
              {emailSt.configured ? (
                <span>SMTP ready (Gmail)</span>
              ) : (
                <span className="attn warn">
                  <IconDot /> Mock only
                </span>
              )}
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
            {emailMsg && (
              <p className="muted" role="status">
                {emailMsg}
              </p>
            )}
          </div>
        ) : (
          <p className="muted">Loading email status…</p>
        )}
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Regions and MSP map</h3>
        <p className="muted">
          About 7,000 sites and 50M+ subscribers in six Safaricom regions. Power: NBI_E and MTK to Egypro, RFT and WNY
          to Tetranet. Radio: NBI_W and CST to Huawei. Fibre: Egypro Fibre, Soliton (Mt Kenya), Camusat, Ecta, Adrian,
          Alan Dick.
        </p>
        <div className="list">
          {profile?.regions &&
            Object.entries(profile.regions).map(([code, r]: any) => (
              <div key={code} className="row" style={{ cursor: "default", gridTemplateColumns: "90px 1fr" }}>
                <strong>{code}</strong>
                <div>
                  <div className="head-row">
                    <div>{r.label}</div>
                    <span>RNIO {r.rnio}</span>
                    <span>FE {r.fe_oncall}</span>
                  </div>
                  <div className="muted">{r.description}</div>
                </div>
              </div>
            ))}
        </div>
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Heavy-rain microwave storm</h3>
        <p className="muted">
          Best launched from <Link to="/">Mission control</Link>, so the agent steps stream live. It also runs from
          here:
        </p>
        <button
          className="btn storm"
          onClick={async () => {
            setLast(["Storm running; watch the Mission control ticker…"]);
            const res = await runLiveRainStorm();
            setLast([`Storm done: ${res.count} events`]);
            onInjected();
          }}
        >
          Run the rain and MW cascade live
        </button>
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <div className="panel-head">
          <h3>Single-event inject</h3>
          <span className="muted">Each one opens an INC ticket</span>
        </div>
        <div className="list">
          {PRESETS.map((p) => (
            <div key={p.label} className="row" style={{ gridTemplateColumns: "1fr auto" }}>
              <div className="head-row">
                <div>{p.label}</div>
                {p.route && <span>routes to {p.route}</span>}
              </div>
              <button
                className="btn primary"
                onClick={async () => {
                  const res = await api.inject(p.body);
                  const i = res.incident;
                  setLast([
                    i.incident_number,
                    i.priority,
                    `MSP ${i.responsible_msp || i.msp_name}`,
                    `FE ${i.fe_name}`,
                  ]);
                  onInjected();
                }}
              >
                Inject
              </button>
            </div>
          ))}
        </div>
        {last.length > 0 && (
          <p className="facts" role="status">
            <span>Last:</span>
            {last.map((part, n) => (
              <span key={n}>{part}</span>
            ))}
          </p>
        )}
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <div className="panel-head">
          <h3>Seed sites</h3>
          <span className="muted">{sites.length} sites</span>
        </div>
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
