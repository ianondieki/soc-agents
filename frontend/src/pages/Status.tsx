import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, AlertTriangle, CheckCircle2, CircleX, MessageSquare, Phone, Smartphone, Wifi, Wrench, type LucideIcon } from "lucide-react";
import { api } from "../api";
import PublicFrame from "../components/support/PublicFrame";
import { useCalm } from "../lib/motion";
import { fmtHM, parseInstant } from "../lib/time";
import "./Status.css";

/**
 * The customer's service status page at /status, in the public frame beside /complain and /track.
 * It answers "is it my phone or the network?" from `GET /api/v1/public/status`, which carries only
 * region states, services, counties and approved planned works (services/public_status.py): no
 * site, ticket or person ever reaches this page. The headline says the worst state in words; each
 * region says its own, with the services affected and since when; planned work follows. It checks
 * again every minute while the tab is visible, and a failed check keeps the last answer on screen
 * with the time it was true. The one motion is the live dot's slow breath; calm stops it.
 */

type State = "MAJOR" | "PARTIAL" | "MINOR" | "MAINTENANCE" | "NORMAL";
type Service = "VOICE" | "DATA" | "SMS" | "MPESA";
interface Region {
  region_code: string;
  label: string;
  state: State;
  services: Service[];
  counties: string[];
  since: string | null;
  restored_at: string | null;
}
interface Planned {
  starts_at: string;
  ends_at: string;
  scope: "REGION" | "LOCAL" | "NETWORK";
  region_code: string | null;
  label: string | null;
  county: string | null;
}
interface Payload {
  generated_at: string;
  operator_display_name: string;
  state: State;
  regions: Region[];
  planned: Planned[];
}

const REFRESH_MS = 60_000;

const STATE: Record<State, { word: string; icon: LucideIcon }> = {
  MAJOR: { word: "Major outage", icon: CircleX },
  PARTIAL: { word: "Partial outage", icon: AlertTriangle },
  MINOR: { word: "Minor issues", icon: AlertCircle },
  MAINTENANCE: { word: "Planned work", icon: Wrench },
  NORMAL: { word: "Normal", icon: CheckCircle2 },
};

const SERVICE: Record<Service, { word: string; icon: LucideIcon }> = {
  VOICE: { word: "Calls", icon: Phone },
  DATA: { word: "Mobile data", icon: Wifi },
  SMS: { word: "SMS", icon: MessageSquare },
  MPESA: { word: "M-PESA", icon: Smartphone },
};

/** The headline for the worst state: the regions in that state first, then the rest affected. */
function headline(state: State, affected: Region[], total: number): { title: string; lead: string } {
  const worst = listWords(affected.filter((r) => r.state === state).map((r) => r.label));
  const others = affected.filter((r) => r.state !== state && r.state !== "MAINTENANCE").map((r) => r.label);
  const also = others.length ? ` Service is also disrupted in ${listWords(others)}.` : "";
  switch (state) {
    case "MAJOR":
      return { title: "There is a major outage", lead: `Customers in ${worst} may have no service.${also} Our engineers are on it.` };
    case "PARTIAL":
      return { title: "Some customers have a disrupted service", lead: `In ${worst}.${also} Our engineers are working on it.` };
    case "MINOR":
      return { title: "A few customers may notice problems", lead: `In ${worst}. Most services are working; we are fixing the rest.` };
    case "MAINTENANCE":
      return { title: "Planned work is under way", lead: `In ${worst}. Service comes back when the work ends.` };
    default:
      return {
        title: "All services are running normally",
        lead: `Across all ${total} regions. If something is still wrong for you, tell us and we will look into it.`,
      };
  }
}

/** "Coast", "Coast and Mt Kenya", "Coast, Mt Kenya and Rift Valley". */
function listWords(items: string[]): string {
  if (items.length <= 1) return items[0] || "your area";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Thu 15 Oct" for an instant, in EAT (UTC+3 all year). */
function eatDay(value: string): string {
  const d = parseInstant(value);
  if (!d) return "";
  const t = new Date(d.getTime() + 3 * 3_600_000);
  return `${WEEKDAYS[t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
}

/** "Thu 15 Oct, 00:00 to 04:00", or across midnight "Thu 15 Oct, 22:00 to Fri 16 Oct, 02:00". */
function windowWords(p: Planned): string {
  const startDay = eatDay(p.starts_at);
  const endDay = eatDay(p.ends_at);
  const end = endDay === startDay ? fmtHM(p.ends_at) : `${endDay}, ${fmtHM(p.ends_at)}`;
  return `${startDay}, ${fmtHM(p.starts_at)} to ${end}`;
}

function whereWords(p: Planned): string {
  if (p.scope === "NETWORK") return "Across the whole network";
  if (p.scope === "LOCAL") return p.county ? `Part of ${p.county} county, ${p.label}` : `One area in ${p.label}`;
  return `${p.label}, the whole region`;
}

/** "since 14:05" today, "since Thu 15 Oct, 14:05" on an earlier day (EAT). */
function sinceWords(value: string | null, nowMs: number): string {
  if (!value) return "";
  const d = parseInstant(value);
  if (!d) return "";
  const today = eatDay(new Date(nowMs).toISOString());
  const day = eatDay(value);
  return day === today ? `since ${fmtHM(value)}` : `since ${day}, ${fmtHM(value)}`;
}

/** Where and since when, in one sentence: "Around Kilifi and Mombasa, since 14:05." */
function regionNote(r: Region, nowMs: number): string {
  if (r.state === "MAINTENANCE") return "Planned work is under way here.";
  const parts = [r.counties.length ? `Around ${listWords(r.counties)}` : "", sinceWords(r.since, nowMs)].filter(Boolean);
  if (!parts.length) return "Our engineers are working on it.";
  const sentence = parts.join(", ");
  return sentence[0].toUpperCase() + sentence.slice(1) + ".";
}

function StateChip({ state }: { state: State }) {
  const { word, icon: Icon } = STATE[state];
  return (
    <span className={`st-chip ${state.toLowerCase()}`}>
      <Icon size={15} strokeWidth={2.2} aria-hidden />
      {word}
    </span>
  );
}

export default function Status() {
  const calm = useCalm();
  const [data, setData] = useState<Payload | null>(null);
  const [failed, setFailed] = useState(false);
  const [checking, setChecking] = useState(false);
  // Only the newest request may write: a slow answer must never replace a newer one.
  const seq = useRef(0);

  const load = useCallback(() => {
    const mine = ++seq.current;
    setChecking(true);
    api
      .publicStatus()
      .then((body: Payload) => {
        if (mine !== seq.current) return;
        if (body && Array.isArray(body.regions)) {
          setData(body);
          setFailed(false);
        } else {
          setFailed(true);
        }
      })
      .catch(() => {
        if (mine === seq.current) setFailed(true);
      })
      .finally(() => {
        if (mine === seq.current) setChecking(false);
      });
  }, []);

  useEffect(() => {
    load();
    // Every minute while someone can see the page; a hidden tab checks again when it comes back.
    const tick = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, REFRESH_MS);
    const onShow = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      window.clearInterval(tick);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [load]);

  useEffect(() => {
    document.title = "Service status, Kenya NOC Support";
    return () => {
      document.title = "Kenya NOC Mission Control";
    };
  }, []);

  const regions = data?.regions ?? [];
  const affected = regions.filter((r) => r.state !== "NORMAL");
  const head = data ? headline(data.state, affected, regions.length) : null;
  const nowMs = data ? parseInstant(data.generated_at)?.getTime() ?? Date.now() : Date.now();
  // Problems first, worst first; the regions where all is well follow, in their own order.
  const order: State[] = ["MAJOR", "PARTIAL", "MINOR", "MAINTENANCE", "NORMAL"];
  const sorted = [...regions].sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state));

  return (
    <PublicFrame wide>
      <div className={"st" + (calm ? " calm" : "")}>
        {!data && !failed && (
          <div className="st-loading" role="status">
            <h1>Service status</h1>
            <p className="cp-lead">Checking the network…</p>
          </div>
        )}
        {!data && failed && (
          <div role="alert">
            <h1>Service status</h1>
            <p className="cp-lead">We could not check the network just now. Try again in a moment.</p>
            <button type="button" className="cp-btn secondary st-retry" onClick={load} disabled={checking}>
              {checking ? "Checking…" : "Try again"}
            </button>
          </div>
        )}

        {data && head && (
          <>
            <section className={`st-hero ${data.state.toLowerCase()}`} aria-labelledby="st-title">
              <p className="st-live">
                <span className="st-dot" aria-hidden="true" />
                <span>
                  {failed ? (
                    <>Could not refresh. This is the status at {fmtHM(data.generated_at)} EAT.</>
                  ) : (
                    <>
                      Updated at {fmtHM(data.generated_at)} EAT. The page checks again every minute.
                    </>
                  )}
                </span>
              </p>
              <h1 id="st-title">{head.title}</h1>
              <p className="cp-lead">{head.lead}</p>
              <div className="st-actions">
                <Link className="cp-btn primary" to="/complain">
                  Report a problem
                </Link>
                <Link className="cp-btn secondary" to="/track">
                  Track a complaint
                </Link>
              </div>
            </section>

            <section className="st-section" aria-labelledby="st-regions">
              <h2 id="st-regions">By region</h2>
              <ul className="st-regions">
                {sorted.map((r) => (
                  <li key={r.region_code} className={`st-region ${r.state.toLowerCase()}`}>
                    <div className="st-region-head">
                      <h3>{r.label}</h3>
                      <StateChip state={r.state} />
                    </div>
                    {r.state === "NORMAL" ? (
                      <p className="st-region-note">
                        {r.restored_at ? `Every service is working. Service was restored at ${fmtHM(r.restored_at)} EAT.` : "Every service is working."}
                      </p>
                    ) : (
                      <>
                        {r.services.length > 0 && (
                          <ul className="st-services" aria-label={`Affected in ${r.label}`}>
                            {r.services.map((s) => {
                              const { word, icon: Icon } = SERVICE[s];
                              return (
                                <li key={s}>
                                  <Icon size={14} strokeWidth={2} aria-hidden />
                                  {word}
                                </li>
                              );
                            })}
                          </ul>
                        )}
                        <p className="st-region-note">{regionNote(r, nowMs)}</p>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            </section>

            <section className="st-section" aria-labelledby="st-planned">
              <h2 id="st-planned">Planned work</h2>
              {data.planned.length === 0 ? (
                <p className="st-empty">No planned work in the next seven days.</p>
              ) : (
                <ul className="st-planned">
                  {data.planned.map((p, i) => (
                    <li key={`${p.starts_at}-${p.region_code ?? "all"}-${i}`}>
                      <Wrench size={16} strokeWidth={2} aria-hidden />
                      <div>
                        <p className="st-planned-when">{windowWords(p)} EAT</p>
                        <p className="st-planned-where">{whereWords(p)}. Some services may be off while the work runs.</p>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </div>
    </PublicFrame>
  );
}
