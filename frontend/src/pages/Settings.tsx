import { memo, useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { humanEnum } from "../lib/agents";
import { detailOf, statusOf } from "../lib/apiError";
import { IconAlert, IconDot } from "../lib/icons";
import "./Settings.css";

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

/**
 * A failed action as one short clause a person can act on; the server's own words stay in the
 * element's `title`. Every action on this page reports failure next to its button.
 */
function failText(e: unknown): string {
  const code = statusOf(e);
  const raw = e instanceof Error ? e.message : String(e ?? "");
  if (code === 401) return "your session has ended; sign in again";
  if (code === 403) return "your role cannot do this";
  if (code != null && code >= 500) return "the API failed; nothing changed";
  if (/failed to fetch|networkerror|load failed/i.test(raw)) return "the API is unreachable";
  const d = detailOf(e);
  return d.length > 120 ? `${d.slice(0, 120)}…` : d;
}

/** A failure beside the control that caused it. */
function Failed({ text, detail }: { text: string; detail?: string }) {
  return (
    <span className="state danger" role="alert" title={detail || text}>
      <IconAlert />
      {text}
    </span>
  );
}

type Fail = { text: string; detail: string } | null;
const toFail = (e: unknown): Fail => ({ text: failText(e), detail: e instanceof Error ? e.message : String(e) });

/**
 * A site type as a person reads it. `humanEnum` keeps the acronyms it knows (HUB, CORE, TX,
 * eNodeB); a short all-capitals type it does not know (BTS) is an acronym too, not a word.
 */
function siteType(v: unknown): string {
  const raw = typeof v === "string" ? v.trim() : "";
  if (/^[A-Z]{2,4}$/.test(raw) && humanEnum(raw) === raw.toLowerCase()) return raw;
  return humanEnum(raw);
}

/** The test email's outcome, without the env-var names the server puts there. A mock adapter
 *  kept the message and delivered nothing, so its wording never says "sent". */
function testMailWords(r: any): { ok: boolean; text: string } {
  const mode = typeof r?.mode === "string" ? r.mode : "";
  const to: string[] = Array.isArray(r?.to) ? r.to.filter((x: unknown) => typeof x === "string") : [];
  if (mode === "smtp" && r?.ok !== false) return { ok: true, text: to.length ? `Sent to ${to.join(", ")}` : "Sent" };
  if (mode === "mock") return { ok: true, text: "Kept in the demo outbox by the mock adapter; not delivered" };
  return { ok: false, text: "Couldn't send the test email" };
}

function Settings({
  session,
  onSession,
  profile,
  onInjected,
  storming,
  stormProg,
  stormErr,
  onLaunchStorm,
  onResumeStorm,
}: {
  session: any;
  onSession: (s: any) => void;
  profile: any;
  onInjected: () => void;
  /** The storm is App's (one storm, whichever page started it): its state and its two actions. */
  storming: boolean;
  /** The storm's progress or completion line ("" while idle or stopped). */
  stormProg: string;
  /** Where and why it stopped ("" unless it did). */
  stormErr: string;
  onLaunchStorm: () => void;
  onResumeStorm: () => void;
}) {
  const sessionId = useId();
  const emailId = useId();
  const regionsId = useId();
  const stormId = useId();
  const injectId = useId();
  const sitesId = useId();
  const [name, setName] = useState(session?.display_name || "NOC Analyst");
  const [role, setRole] = useState(session?.role || DEFAULT_ROLE);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveFail, setSaveFail] = useState<Fail>(null);

  // `undefined` until the first answer: loading shows skeleton rows, never an empty table.
  const [sites, setSites] = useState<any[] | undefined>(undefined);
  const [sitesFail, setSitesFail] = useState<Fail>(null);
  const [emailSt, setEmailSt] = useState<any>(undefined);
  const [emailFail, setEmailFail] = useState<Fail>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string; detail: string } | null>(null);

  const [injecting, setInjecting] = useState<string | null>(null);
  const [injectFail, setInjectFail] = useState<{ label: string; fail: Fail } | null>(null);
  const [last, setLast] = useState<any>(null);

  const loadSites = useCallback(() => {
    setSitesFail(null);
    api
      .sites()
      .then((rows) => setSites(Array.isArray(rows) ? rows.filter((r) => r && typeof r === "object") : []))
      .catch((e) => setSitesFail(toFail(e)));
  }, []);

  const loadEmail = useCallback(() => {
    setEmailFail(null);
    api
      .emailStatus()
      .then((st) => setEmailSt(st && typeof st === "object" ? st : {}))
      .catch((e) => setEmailFail(toFail(e)));
  }, []);

  useEffect(() => {
    loadSites();
    loadEmail();
  }, [loadSites, loadEmail]);

  const saveSession = async () => {
    setSaving(true);
    setSaved(false);
    setSaveFail(null);
    try {
      const s = await api.setSession({ display_name: name, role });
      onSession(s);
      setSaved(true);
    } catch (e) {
      setSaveFail(toFail(e));
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r: any = await api.emailTest();
      const words = testMailWords(r);
      setTestResult({ ...words, detail: typeof r?.detail === "string" ? r.detail : "" });
    } catch (e) {
      const f = toFail(e);
      setTestResult({ ok: false, text: `Couldn't send: ${f?.text}`, detail: f?.detail || "" });
    } finally {
      setTesting(false);
    }
  };

  const inject = async (label: string, body: Record<string, unknown>) => {
    setInjecting(label);
    setInjectFail(null);
    try {
      const res: any = await api.inject(body);
      setLast(res?.incident && typeof res.incident === "object" ? res.incident : null);
      onInjected();
    } catch (e) {
      setInjectFail({ label, fail: toFail(e) });
    } finally {
      setInjecting(null);
    }
  };

  const mailReady = emailSt?.configured === true;
  const recipients: string[] = Array.isArray(emailSt?.recipients) ? emailSt.recipients : [];

  return (
    <div className="content-narrow">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="lead">Who the demo records as the actor, the email channel, and alarms to inject.</p>
        </div>
      </div>

      <div className="stack">
        <section className="panel" aria-labelledby={sessionId}>
          <h2 id={sessionId} className="panel-title">
            Session role (team demo)
          </h2>
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
            {/* The one primary action on the page. */}
            <button className="btn primary" onClick={saveSession} disabled={saving}>
              {saving ? "Saving…" : "Save session"}
            </button>
            <span role="status" className="muted">
              {saved && !saving ? "Saved" : ""}
            </span>
            {saveFail && <Failed text={`Couldn't save: ${saveFail.text}`} detail={saveFail.detail} />}
          </div>
          <p
            className="muted settings-note"
            title="With AUTH_DISABLED=true the switcher grants nothing. With sign-in on, the signed-in user decides instead."
          >
            The nine roles the API defines. A demo switch, not a sign-in: it sets the name recorded as the actor and
            what the role-aware screens say.
          </p>
        </section>

        <section className="panel" aria-labelledby={emailId}>
          <h2 id={emailId} className="panel-title">
            Gmail demo email
          </h2>
          {emailFail ? (
            <div className="empty" role="alert" title={emailFail.detail}>
              Couldn't load the email status: {emailFail.text}.
              <button className="btn sm" onClick={loadEmail}>
                Retry
              </button>
            </div>
          ) : emailSt === undefined ? (
            <div className="skeleton-rows" aria-busy="true" aria-label="Loading the email status">
              <span className="skeleton" style={{ width: "30%" }} />
              <span className="skeleton" style={{ width: "45%" }} />
              <span className="skeleton" style={{ width: "40%" }} />
            </div>
          ) : (
            <div className="stack tight">
              <dl className="rail-dl">
                <dt>Status</dt>
                <dd>
                  {mailReady ? (
                    "SMTP ready (Gmail)"
                  ) : (
                    <span className="attn warn">
                      <IconDot /> Mock only: mail is kept in the outbox, not delivered
                    </span>
                  )}
                </dd>
                <dt>From</dt>
                <dd>{emailSt.from || "—"}</dd>
                <dt>To</dt>
                <dd>{recipients.join(", ") || "—"}</dd>
              </dl>
              {/* The setup sentence, once; the variable names are in its title. */}
              {!mailReady && (
                <p
                  className="muted settings-note"
                  title="Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Google app password), optionally DEMO_EMAIL_TO, then restart the API. See docs/GMAIL_SETUP.md."
                >
                  To send real mail, give the API a Gmail address and app password, then restart it.
                </p>
              )}
              <div className="settings-actions">
                <button className="btn" onClick={sendTest} disabled={testing}>
                  {testing ? "Sending…" : "Send test email now"}
                </button>
                <span role="status">
                  {testResult && testResult.ok && (
                    <span className="muted" title={testResult.detail}>
                      {testResult.text}
                    </span>
                  )}
                </span>
                {testResult && !testResult.ok && <Failed text={testResult.text} detail={testResult.detail} />}
              </div>
            </div>
          )}
        </section>

        <section className="panel" aria-labelledby={regionsId}>
          <h2 id={regionsId} className="panel-title">
            Regions and MSP map
          </h2>
          <p className="muted settings-note">
            About 7,000 sites and 50M+ subscribers in six Safaricom regions. Power: NBI_E and MTK to Egypro, RFT and
            WNY to Tetranet. Radio: NBI_W and CST to Huawei. Fibre: Egypro Fibre, Soliton (Mt Kenya), Camusat, Ecta,
            Adrian, Alan Dick.
          </p>
          {profile?.regions && typeof profile.regions === "object" ? (
            <ul className="settings-regions">
              {Object.entries(profile.regions).map(([code, r]: [string, any]) => (
                <li key={code} className="row static">
                  <span className="mono">{code}</span>
                  <div className="row-main">
                    <div className="head-row">
                      <span className="settings-region-name">{r?.label}</span>
                      {/* The RNIO and FE codes name themselves ("RNIO-NBI-E"); no label before them. */}
                      {r?.rnio && <span className="mono">{r.rnio}</span>}
                      {r?.fe_oncall && <span className="mono">{r.fe_oncall}</span>}
                    </div>
                    {r?.description && <div className="muted">{r.description}</div>}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="skeleton-rows" aria-busy="true" aria-label="Loading the regions">
              <span className="skeleton" />
              <span className="skeleton" style={{ width: "70%" }} />
            </div>
          )}
        </section>

        <section className="panel" aria-labelledby={stormId}>
          <h2 id={stormId} className="panel-title">
            Heavy-rain microwave storm
          </h2>
          <p className="muted settings-note">
            Best launched from <Link to="/">Mission control</Link>, so the agent steps stream live. It also runs from
            here.
          </p>
          <div className="settings-actions">
            <button className="btn" onClick={onLaunchStorm} disabled={storming}>
              {storming ? "Storm running…" : "Launch the storm"}
            </button>
            {stormErr && !storming && (
              <button className="btn sm" onClick={onResumeStorm}>
                Resume storm
              </button>
            )}
            <span role="status" className="muted">
              {stormProg}
            </span>
            {stormErr && !storming && <Failed text={`The storm stopped. ${stormErr}`} detail={stormErr} />}
          </div>
        </section>

        <section className="panel" aria-labelledby={injectId}>
          <div className="panel-head">
            <h2 id={injectId} className="panel-title">
              Single-event inject
            </h2>
            <span className="muted">Each one opens an INC ticket</span>
          </div>
          <ul className="settings-inject">
            {PRESETS.map((p) => (
              <li key={p.label} className="row static">
                <div className="head-row">
                  <span className="settings-inject-label">{p.label}</span>
                  {p.route && <span>routes to {p.route}</span>}
                </div>
                <div className="settings-inject-act">
                  {injectFail?.label === p.label && injectFail.fail && (
                    <Failed text={`Couldn't inject: ${injectFail.fail.text}`} detail={injectFail.fail.detail} />
                  )}
                  <button
                    className="btn sm"
                    onClick={() => inject(p.label, p.body)}
                    disabled={injecting !== null}
                    aria-label={`Inject: ${p.label}`}
                  >
                    {injecting === p.label ? "Injecting…" : "Inject"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
          <p className="facts settings-last" role="status">
            {last && (
              <>
                <span>Last inject</span>
                {typeof last.id === "string" && last.incident_number ? (
                  <Link className="mono" to={`/incidents/${last.id}`}>
                    {last.incident_number}
                  </Link>
                ) : (
                  last.incident_number && <span className="mono">{last.incident_number}</span>
                )}
                {last.priority && <span className={`pill ${last.priority}`}>{last.priority}</span>}
                {(last.responsible_msp || last.msp_name) && <span>MSP {last.responsible_msp || last.msp_name}</span>}
                {last.fe_name && (
                  <span>
                    FE <span className="mono">{last.fe_name}</span>
                  </span>
                )}
              </>
            )}
          </p>
        </section>

        <section className="panel" aria-labelledby={sitesId}>
          <div className="panel-head">
            <h2 id={sitesId} className="panel-title">
              Seed sites
            </h2>
            {sites && <span className="muted">{sites.length} sites</span>}
          </div>
          {sitesFail ? (
            <div className="empty" role="alert" title={sitesFail.detail}>
              Couldn't load the sites: {sitesFail.text}.
              <button className="btn sm" onClick={loadSites}>
                Retry
              </button>
            </div>
          ) : sites === undefined ? (
            <div className="skeleton-rows" aria-busy="true" aria-label="Loading the sites">
              {Array.from({ length: 6 }, (_, i) => (
                <span key={i} className="skeleton" style={{ width: `${60 + ((i * 7) % 30)}%` }} />
              ))}
            </div>
          ) : sites.length === 0 ? (
            <div className="empty">No sites are seeded on this operator.</div>
          ) : (
            <div className="table-scroll">
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
                  {sites.map((s, i) => (
                    <tr key={s.site_id || i}>
                      <td className="mono">{s.site_id}</td>
                      <td>{s.site_name}</td>
                      <td>{siteType(s.site_type)}</td>
                      <td className="mono">{s.region_code}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

/** Memoised: App's flushes and metrics answers do not re-render a page whose props held still. */
export default memo(Settings);
