import { memo, useCallback, useEffect, useId, useMemo, useState } from "react";
import { CloudLightning, Search, Server, Wifi, Zap, RadioTower, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { autonomyMeaning, humanEnum, priorityTitle, regionName } from "../lib/agents";
import { PROJECTOR_MEANING, QUIET_MEANING } from "../lib/display";
import { detailOf, statusOf } from "../lib/apiError";
import { IconAlert } from "../lib/icons";
import { MOCK_EMAIL_LINE } from "../realtime/renderers";
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
    label: "Child site under Westlands HUB (folds in)",
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
  { value: "noc_analyst", label: "the floor: tickets, notes, timeline" },
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

/** The test email's outcome, without the env-var names the server puts there. With sending off,
 *  the message was kept and nothing was delivered, so its wording never says "sent". */
function testMailWords(r: any): { ok: boolean; text: string } {
  const mode = typeof r?.mode === "string" ? r.mode : "";
  const to: string[] = Array.isArray(r?.to) ? r.to.filter((x: unknown) => typeof x === "string") : [];
  if (mode === "smtp" && r?.ok !== false) return { ok: true, text: to.length ? `Sent to ${to.join(", ")}` : "Sent" };
  if (mode === "mock") return { ok: true, text: MOCK_EMAIL_LINE };
  return { ok: false, text: "Couldn't send the test email" };
}

/** A preset's failure domain: its icon and the floor's word. */
const DOMAIN_ICON: Record<string, LucideIcon> = { POWER: Zap, RADIO: Wifi, TRANSMISSION: RadioTower, CORE: Server };
const DOMAIN_WORD: Record<string, string> = { POWER: "Power", RADIO: "Radio", TRANSMISSION: "Transmission", CORE: "Core" };

/** 450000 -> "450k", 2500000 -> "2.5M": subscribers at a glance. */
function compact(n: unknown): string {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(v % 1_000_000 ? 1 : 0)}M`;
  if (v >= 1_000) return `${Math.round(v / 1_000)}k`;
  return String(v);
}

/** The page's sections, for the "On this page" list. */
const SECTIONS = [
  { id: "settings-you", label: "You in this demo" },
  { id: "settings-display", label: "Autonomy and display" },
  { id: "settings-email", label: "Email" },
  { id: "settings-alarms", label: "Demo alarms" },
  { id: "settings-regions", label: "Regions and vendors" },
  { id: "settings-sites", label: "Sites" },
];

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
  const stormId = useId();
  const [siteQ, setSiteQ] = useState("");
  const [name, setName] = useState(session?.display_name || "NOC Analyst");
  const [role, setRole] = useState(session?.role || DEFAULT_ROLE);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveFail, setSaveFail] = useState<Fail>(null);
  // The session often answers after this page has mounted: the fields follow it when it does.
  useEffect(() => {
    if (session?.display_name) setName(session.display_name);
    if (session?.role) setRole(session.role);
  }, [session?.display_name, session?.role]);

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

  const roleNow = session?.role || DEFAULT_ROLE;
  const roleMeaning = ROLES.find((r) => r.value === role)?.label;
  const shownSites = useMemo(() => {
    const n = siteQ.trim().toLowerCase();
    if (!sites) return [];
    if (!n) return sites;
    return sites.filter((x) =>
      [x.site_id, x.site_name, x.site_type, x.region_code, regionName(x.region_code, profile)]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(n),
    );
  }, [sites, siteQ, profile]);

  return (
    <div className="settings">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="lead">Who the demo records as you, how the floor is set up, and the alarms you can send through the agents.</p>
        </div>
      </div>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="On this page">
          <p className="settings-nav-title">On this page</p>
          <ol>
            {SECTIONS.map((x) => (
              <li key={x.id}>
                <a href={`#${x.id}`}>{x.label}</a>
              </li>
            ))}
          </ol>
        </nav>

        <div className="settings-main">
          <section className="panel settings-section" id="settings-you" aria-labelledby="settings-you-title">
            <div className="settings-section-head">
              <h2 id="settings-you-title" className="panel-title">
                You in this demo
              </h2>
              <span className="settings-chip">Viewing as {lcFirst(roleWords(roleNow))}</span>
            </div>
            <p
              className="settings-note"
              title="With AUTH_DISABLED=true the switcher grants nothing. With sign-in on, the signed-in user decides instead."
            >
              A demo switch, not a sign-in. It sets the name recorded as the person who acted, and what the role-aware
              screens show you.
            </p>
            <div className="settings-you">
              <label className="settings-field">
                <span>Your name</span>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Display name" aria-label="Display name" />
              </label>
              <label className="settings-field">
                <span>Role</span>
                <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Session role">
                  {ROLES.map((r) => (
                    <option key={r.value} value={r.value}>
                      {roleWords(r.value)}
                    </option>
                  ))}
                  {/* A role stored before this list was corrected would otherwise leave the select
                      blank, with no way to see what it is set to. */}
                  {role && !ROLES.some((r) => r.value === role) && (
                    <option value={role}>{role}: not a role this backend knows</option>
                  )}
                </select>
                <small>{roleMeaning ? sentence(roleMeaning) + "." : "Not one of the nine roles the API defines."}</small>
              </label>
              {/* The one primary action on the page. */}
              <div className="settings-you-act">
                <button className="btn primary" onClick={saveSession} disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </button>
                <span role="status" className="settings-saved">
                  {saved && !saving ? "Saved" : ""}
                </span>
              </div>
            </div>
            {saveFail && <Failed text={`Couldn't save: ${saveFail.text}`} detail={saveFail.detail} />}
          </section>

          {/* What the top bar's tooltips say, in words a tablet can read (a finger never sees a title). */}
          <section className="panel settings-section" id="settings-display" aria-labelledby="settings-display-title">
            <h2 id="settings-display-title" className="panel-title">
              Autonomy and display
            </h2>
            <dl className="settings-cards">
              <div>
                <dt>Autonomy</dt>
                <dd>{autonomyMeaning(profile?.autonomy_level).replace(/^Autonomy\s+/, "")}</dd>
              </div>
              <div>
                <dt>Quiet mode</dt>
                <dd>{QUIET_MEANING}</dd>
              </div>
              <div>
                <dt>Projector</dt>
                <dd>{PROJECTOR_MEANING}</dd>
              </div>
            </dl>
            <p className="settings-note">Quiet mode and the projector are switched from Display in the top bar.</p>
          </section>

          <section className="panel settings-section" id="settings-email" aria-labelledby="settings-email-title">
            <h2 id="settings-email-title" className="panel-title">
              Email
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
              </div>
            ) : (
              <>
                <p className={"settings-status" + (mailReady ? " ok" : " warn")}>
                  <span className="settings-status-dot" aria-hidden="true" />
                  {mailReady ? "Sending through Gmail" : "Sending is off in this demo: messages are kept, not delivered"}
                </p>
                <dl className="settings-pairs">
                  <div>
                    <dt>From</dt>
                    <dd>{emailSt.from || "Not set"}</dd>
                  </div>
                  <div>
                    <dt>To</dt>
                    <dd>{recipients.join(", ") || "Not set"}</dd>
                  </div>
                </dl>
                {/* The setup sentence, once; the variable names are in its title. */}
                {!mailReady && (
                  <p
                    className="settings-note"
                    title="Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Google app password), optionally DEMO_EMAIL_TO, then restart the API. See docs/GMAIL_SETUP.md."
                  >
                    To send real mail, give the API a Gmail address and an app password, then restart it.
                  </p>
                )}
                <div className="settings-actions">
                  <button className="btn" onClick={sendTest} disabled={testing}>
                    {testing ? "Sending…" : "Send a test email"}
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
              </>
            )}
          </section>

          <section className="panel settings-section" id="settings-alarms" aria-labelledby="settings-alarms-title">
            <div className="settings-section-head">
              <h2 id="settings-alarms-title" className="panel-title">
                Demo alarms
              </h2>
              <span className="muted">Each one goes through all twelve agent steps</span>
            </div>

            <div className="settings-storm" aria-labelledby={stormId}>
              <span className="settings-storm-icon" aria-hidden="true">
                <CloudLightning size={22} strokeWidth={1.75} />
              </span>
              <div className="settings-storm-text">
                <h3 id={stormId}>Heavy-rain microwave storm</h3>
                <p>
                  A burst of linked alarms across the microwave ring. Best launched from{" "}
                  <Link to="/mission">Mission control</Link>, where the agent steps stream live.
                </p>
                <p role="status" className="settings-storm-prog">
                  {stormProg}
                </p>
                {stormErr && !storming && <Failed text={stormErr} detail={stormErr} />}
              </div>
              <div className="settings-storm-act">
                <button className="btn" onClick={onLaunchStorm} disabled={storming}>
                  {storming ? "Storm running…" : "Launch the storm"}
                </button>
                {stormErr && !storming && (
                  <button className="btn sm" onClick={onResumeStorm}>
                    Resume storm
                  </button>
                )}
              </div>
            </div>

            <h3 className="settings-sub">One alarm at a time</h3>
            <ul className="settings-presets">
              {PRESETS.map((p) => {
                const b = p.body as Record<string, any>;
                const domain = String(b.failure_domain || "").toUpperCase();
                const Icon = DOMAIN_ICON[domain] || Zap;
                return (
                  <li key={p.label} className="settings-preset">
                    <div className="settings-preset-head">
                      <span className="settings-preset-icon" aria-hidden="true">
                        <Icon size={16} strokeWidth={1.75} />
                      </span>
                      <span className="settings-preset-label">{p.label}</span>
                    </div>
                    <p className="settings-preset-site">{b.site_name}</p>
                    <p className="settings-preset-facts">
                      <span>{DOMAIN_WORD[domain] || humanEnum(domain)}</span>
                      <span>{compact(b.users_affected)} subscribers</span>
                      <span>{regionName(b.region_code, profile)}</span>
                    </p>
                    <div className="settings-preset-foot">
                      <span className="settings-preset-route">
                        {p.route ? `Routes to ${p.route}` : b.parent_hub_id ? "Folds into its HUB's ticket" : "Routed by the matrix"}
                      </span>
                      <button
                        className="btn sm"
                        onClick={() => inject(p.label, p.body)}
                        disabled={injecting !== null}
                        aria-label={`Inject: ${p.label}`}
                      >
                        {injecting === p.label ? "Injecting…" : "Inject"}
                      </button>
                    </div>
                    {injectFail?.label === p.label && injectFail.fail && (
                      <Failed text={`Couldn't inject: ${injectFail.fail.text}`} detail={injectFail.fail.detail} />
                    )}
                  </li>
                );
              })}
            </ul>
            <p className="settings-last" role="status">
              {last && (
                <>
                  <span className="settings-last-label">Last alarm opened</span>
                  {typeof last.id === "string" && last.incident_number ? (
                    <Link className="mono" to={`/incidents/${last.id}`}>
                      {last.incident_number}
                    </Link>
                  ) : (
                    last.incident_number && <span className="mono">{last.incident_number}</span>
                  )}
                  {last.priority && (
                    <span className={`pill ${last.priority}`} title={priorityTitle(last.priority)}>
                      {last.priority}
                    </span>
                  )}
                  {(last.responsible_msp || last.msp_name) && <span>Vendor {last.responsible_msp || last.msp_name}</span>}
                  {last.fe_name && (
                    <span>
                      Field engineer <span className="mono">{last.fe_name}</span>
                    </span>
                  )}
                </>
              )}
            </p>
          </section>

          <section className="panel settings-section" id="settings-regions" aria-labelledby="settings-regions-title">
            <h2 id="settings-regions-title" className="panel-title">
              Regions and vendors
            </h2>
            <p className="settings-note">
              About 7,000 sites and 50M+ subscribers in six regions. Power: Nairobi East and Mt Kenya go to Egypro, Rift
              Valley and Western-Nyanza to Tetranet. Radio: Nairobi West and Coast to Huawei. Fibre: Egypro Fibre, Soliton
              (Mt Kenya), Camusat, Ecta, Adrian and Alan Dick.
            </p>
            {profile?.regions && typeof profile.regions === "object" ? (
              <ul className="settings-regions">
                {Object.entries(profile.regions).map(([code, r]: [string, any]) => (
                  <li key={code} className="settings-region">
                    <div className="settings-region-head">
                      <span className="settings-region-name">{r?.label || code}</span>
                      <span className="settings-code">{code}</span>
                    </div>
                    {r?.description && <p className="settings-region-desc">{r.description}</p>}
                    {/* The RNIO and FE codes name themselves ("RNIO-NBI-E"); no label before them. */}
                    <p className="settings-region-codes">
                      {r?.rnio && <span className="mono" title="Regional network operations office">{r.rnio}</span>}
                      {r?.fe_oncall && <span className="mono" title="Field engineer on call">{r.fe_oncall}</span>}
                    </p>
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

          <section className="panel settings-section" id="settings-sites" aria-labelledby="settings-sites-title">
            <div className="settings-section-head">
              <h2 id="settings-sites-title" className="panel-title">
                Sites
              </h2>
              {sites && <span className="muted">{siteQ.trim() ? `${shownSites.length} of ${sites.length}` : `${sites.length} seeded`}</span>}
            </div>
            {sites && sites.length > 0 && (
              <span className="settings-search">
                <Search size={16} strokeWidth={1.75} aria-hidden="true" />
                <input
                  type="search"
                  value={siteQ}
                  onChange={(e) => setSiteQ(e.target.value)}
                  placeholder="Find a site by name, code, type or region"
                  aria-label="Find a site"
                />
              </span>
            )}
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
            ) : shownSites.length === 0 ? (
              <div className="empty">No site matches “{siteQ.trim()}”.</div>
            ) : (
              <div className="table-scroll settings-sites">
                <table>
                  <thead>
                    <tr>
                      <th>Site</th>
                      <th>Type</th>
                      <th>Region</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shownSites.map((x, i) => (
                      <tr key={x.site_id || i}>
                        <td>
                          <span className="settings-site-name">{x.site_name}</span>
                          <span className="settings-site-code">{x.site_id}</span>
                        </td>
                        <td>
                          <span className="settings-type">{siteType(x.site_type)}</span>
                        </td>
                        <td>{regionName(x.region_code, profile)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

const sentence = (t: string) => (t ? t[0].toUpperCase() + t.slice(1) : t);
/** "Duty manager" -> "duty manager", but "MSP coordinator" stays as written. */
const lcFirst = (t: string) => (t && /^[A-Z][a-z]/.test(t) ? t[0].toLowerCase() + t.slice(1) : t);

/** Memoised: App's flushes and metrics answers do not re-render a page whose props held still. */
export default memo(Settings);
