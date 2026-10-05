import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { MapPin, Timer, UsersRound, Wrench } from "lucide-react";
import { api } from "../api";
import CardBoundary from "../components/CardBoundary";
import RiskStrip from "../components/RiskStrip";
import AgentsStatusTile from "../components/AgentsStatusTile";
import RedactionMissChip from "../components/RedactionMissChip";
import { BrandMark } from "../components/shell/BrandMark";
import { humanEnum, regionName } from "../lib/agents";
import { labelFor } from "../lib/hitl";
import { fmtEAT, fmtHM, parseInstant } from "../lib/time";
import { useMinute } from "../lib/useMinute";
import { useRealtimeState } from "../realtime/RealtimeContext";
import "./Wallboard.escalation.css";

/**
 * §6.5 escalation ladder, T+30 rung: the ladder (services/hitl_escalation.py) marks a P1/P2
 * approval card that has sat PENDING and unclaimed past the Wallboard rung by writing
 * `proposed_payload.escalation.wallboard_red = true` on the task, which the existing
 * `/api/v1/hitl/pending` route already returns — so the glass needs no new route.
 *
 * Red is "still waiting": the card must still be PENDING and unclaimed here too. A claim
 * means someone who can decide it is looking, which is what the ladder exists to bring
 * about, so the red clears the moment a name goes on the card.
 */
type RedCard = {
  id: string;
  incident_id: string | null;
  incident_number: string | null;
  priority: string | null;
  task_type: string;
  unclaimed_minutes: number | null;
  since_eat: string | null;
};

function redCards(tasks: any[]): RedCard[] {
  const out: RedCard[] = [];
  for (const t of tasks) {
    const esc = t?.proposed_payload?.escalation;
    if (!esc || esc.wallboard_red !== true) continue;
    if (t.status !== "PENDING" || t.claimed_by) continue;
    const rung = esc.rungs?.[String(esc.level_minutes ?? "")] ?? null;
    out.push({
      id: String(t.id),
      incident_id: t.incident_id ?? null,
      incident_number: t.incident_number ?? null,
      priority: t.priority ?? t?.proposed_payload?.priority ?? null,
      task_type: String(t.task_type ?? "task"),
      unclaimed_minutes: typeof rung?.unclaimed_minutes === "number" ? rung.unclaimed_minutes : null,
      since_eat: typeof esc.red_since_eat === "string" ? esc.red_since_eat : null,
    });
  }
  return out;
}

/** A vendor or owner enum as a name on the glass: "EGYPRO_FIBRE" → "Egypro Fibre", "FIELD_ENGINEER"
 *  → "Field Engineer"; acronyms humanEnum keeps (MSP, RNIO) stay as they are. */
const nameOf = (s: unknown) => {
  const raw = String(s ?? "").trim();
  if (!/^[A-Z]+(_[A-Z]+)+$/.test(raw)) return raw; // an id or a name as written: NOC-QUEUE, ATC, FE-RFT-01
  return humanEnum(raw)
    .split(" ")
    .map((w) => (/^[a-z]/.test(w) ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
};

/** Skeleton bar widths for the cards shown before the first answer. */
const SKELETON_WIDTHS = ["46%", "78%", "62%", "54%"];

/** The glass's order: P1 first, then P2; within a priority the ticket open longest first. */
const RANK: Record<string, number> = { P1: 0, P2: 1 };
function wallOrder(a: any, b: any): number {
  const r = (RANK[a?.priority] ?? 9) - (RANK[b?.priority] ?? 9);
  if (r) return r;
  const ta = parseInstant(a?.created_at)?.getTime() ?? Infinity;
  const tb = parseInstant(b?.created_at)?.getTime() ?? Infinity;
  if (ta !== tb) return ta < tb ? -1 : 1;
  return String(a?.incident_number ?? "").localeCompare(String(b?.incident_number ?? ""));
}

/** Below this width the Wallboard is a phone looking at the glass: it scrolls like any page. */
const FIT_MIN_WIDTH = 701;

/** One figure in the strip under the head: a number read from the back of the room, its label
 *  above it. Its colour only when it is not zero. */
function Stat({ label, value, tone }: { label: string; value: number | null | undefined; tone?: "p1" | "p2" | "hitl" }) {
  const n = typeof value === "number" ? value : null;
  return (
    <div className={"wb-stat" + (tone && n ? ` ${tone}` : "")}>
      <dt>{label}</dt>
      <dd>{n == null ? "—" : n.toLocaleString()}</dd>
    </div>
  );
}

const DAY_FMT = (() => {
  try {
    return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "Africa/Nairobi" });
  } catch {
    return null;
  }
})();

/** The wall clock: the time in Nairobi, big, with the day under it. */
function WallClock() {
  const now = useMinute();
  return (
    <div className="wb-clock" title="Time in Nairobi (EAT)">
      <time className="wb-clock-time" dateTime={now.toISOString()}>
        {fmtHM(now)}
      </time>
      <span className="wb-clock-day">{DAY_FMT ? `${DAY_FMT.format(now)}, EAT` : "EAT"}</span>
    </div>
  );
}

/** How long a ticket has been open: "8 min", "2 h 5 min", "3 d 4 h". */
function openFor(createdAt: unknown, now: Date): string | null {
  const t = parseInstant(createdAt);
  if (!t) return null;
  const m = Math.max(0, Math.floor((now.getTime() - t.getTime()) / 60000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

const CARD_ICON = { size: "0.9em", strokeWidth: 2, "aria-hidden": true } as const;

/** A subscriber count read from across the room: 900k, 1.2M (the exact figure is in its title). */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1).replace(/\.0$/, "")}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString();
}

export default function Wallboard({
  metrics,
  profile = null,
  metricsStale = false,
  rev = 0,
  signalsRev = 0,
}: {
  metrics: any;
  /** The operator profile: its name in the head and its region names on the tiles. */
  profile?: any;
  /** The metrics call failed while the API still answers: the header counts are the last ones. */
  metricsStale?: boolean;
  rev?: number;
  /** Debounced `signals` slice revision — see realtime/renderers.ts. */
  signalsRev?: number;
}) {
  const now = useMinute();
  const rt = useRealtimeState();
  const link = rt?.link ?? "connecting";
  const operator = profile?.display_name ? String(profile.display_name).replace(" (demo profile)", "") : null;
  // null until the first answer: the glass never says "quiet" before it has asked.
  const [rows, setRows] = useState<any[] | null>(null);
  const [red, setRed] = useState<RedCard[]>([]);
  // When the incident poll last answered, and whether the latest one failed: a wall that has
  // stopped updating must say so instead of showing old tiles as if they were live.
  const [lastOk, setLastOk] = useState<Date | null>(null);
  const [stale, setStale] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const load = () =>
      api
        .incidents()
        .then((all) => {
          setRows(
            all.filter((i) => ["P1", "P2"].includes(i.priority) && !["CLOSED", "CANCELLED"].includes(i.status)).sort(wallOrder)
          );
          setLastOk(new Date());
          setStale(false);
        })
        // A failed poll leaves the last good rows on the glass rather than blanking the
        // wall, and raises the "Not updating" chip.
        .catch(() => setStale(true));
    // The ladder's red state rides on the pending inbox. A role the inbox refuses (403), or
    // the ladder being off (no card ever carries the mark), both leave the wall exactly as it
    // was: the last good list stays, and an empty list shows nothing extra.
    const loadRed = () =>
      api
        .hitl()
        .then((tasks) => setRed(redCards(Array.isArray(tasks) ? tasks : [])))
        .catch(() => undefined);
    // A tick later, so a mount React undoes at once (its development double mount) asks nothing.
    const first = window.setTimeout(() => {
      load();
      loadRed();
    }, 0);
    // `rev` is the debounced `incidents` slice revision, so a storm refreshes
    // the wall once per burst instead of once per frame. The 5 s poll stays as
    // the fallback for a dropped WS.
    const id = window.setInterval(() => {
      load();
      loadRed();
    }, 5000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [rev, retry]);

  const list = rows || [];
  const redByIncident = new Map<string, RedCard>();
  for (const c of red) if (c.incident_id && !redByIncident.has(c.incident_id)) redByIncident.set(c.incident_id, c);
  const onGrid = new Set(list.map((i) => String(i.id)));
  const offGrid = red.filter((c) => !c.incident_id || !onGrid.has(c.incident_id));

  // A flag most tiles carry is noise on each tile: said once in the header instead.
  const decisionOf = (i: any) => Boolean(i.requires_hitl) && !redByIncident.has(String(i.id));
  const mpesaCount = list.filter((i) => i.mpesa_risk).length;
  const decisionCount = list.filter(decisionOf).length;
  const dropMpesa = mpesaCount * 2 > list.length;
  const dropDecision = decisionCount * 2 > list.length;
  const tileFlags = (i: any) => ({ mpesa: !dropMpesa && !!i.mpesa_risk, decision: !dropDecision && decisionOf(i) });
  const anyTileFlag = list.some((i) => {
    const f = tileFlags(i);
    return f.mpesa || f.decision;
  });

  // ---- fit the glass: never a scrollbar on the wall ------------------------------------------
  // As many whole rows of tiles as fit under the header; when the tickets need more, the last
  // tile says how many more there are. Tiles are one height (the grid's rows are 1fr), so one
  // tile measures them all.
  const gridRef = useRef<HTMLDivElement>(null);
  const footRef = useRef<HTMLParagraphElement>(null);
  const [cap, setCap] = useState<number | null>(null);
  const [viewport, setViewport] = useState(0);
  useEffect(() => {
    const on = () => setViewport((v) => v + 1);
    window.addEventListener("resize", on);
    // The fonts arriving can change a tile's height.
    document.fonts?.ready.then(on).catch(() => undefined);
    // Projector mode changes the type size: measure again when <html data-display> changes.
    const mo = typeof MutationObserver !== "undefined" ? new MutationObserver(on) : null;
    mo?.observe(document.documentElement, { attributes: true, attributeFilter: ["data-display"] });
    return () => {
      window.removeEventListener("resize", on);
      mo?.disconnect();
    };
  }, []);
  useLayoutEffect(() => {
    const g = gridRef.current;
    if (!g) return;
    if (window.innerWidth < FIT_MIN_WIDTH) {
      setCap(null);
      return;
    }
    const tile = g.querySelector<HTMLElement>(".wb-card");
    if (!tile) return;
    const cs = window.getComputedStyle(g);
    const cols = Math.max(1, cs.gridTemplateColumns.split(" ").filter(Boolean).length);
    const gap = parseFloat(cs.rowGap) || 0;
    const tileH = tile.getBoundingClientRect().height;
    const top = g.getBoundingClientRect().top + window.scrollY;
    const foot = footRef.current;
    const footH = foot ? foot.getBoundingClientRect().height + (parseFloat(window.getComputedStyle(foot).marginTop) || 0) : 0;
    const wall = g.parentElement;
    const padB = wall ? parseFloat(window.getComputedStyle(wall).paddingBottom) || 0 : 0;
    const avail = window.innerHeight - top - footH - padB;
    const fitRows = Math.max(1, Math.floor((avail + gap) / (tileH + gap)));
    const next = cols * fitRows;
    setCap((c) => (c === next ? c : next));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, red.length, offGrid.length, viewport, dropMpesa, dropDecision, anyTileFlag, stale, metricsStale]);

  const overflow = cap != null && list.length > cap;
  const shown = overflow ? list.slice(0, Math.max(1, cap - 1)) : list;
  const hidden = list.slice(shown.length);
  const hiddenP1 = hidden.filter((i) => i.priority === "P1").length;
  const hiddenP2 = hidden.length - hiddenP1;
  // "3 P1 and 24 P2 tickets", with a part left out when it is zero ("24 P2 tickets", never "and 0 P2").
  const hiddenWhat = [hiddenP1 > 0 ? `${hiddenP1} P1` : "", hiddenP2 > 0 ? `${hiddenP2} P2` : ""]
    .filter(Boolean)
    .join(" and ");

  // The link to the agents: green only while live and the polls answer; red when the wall has
  // stopped updating.
  const liveTone = stale || link === "down" ? "bad" : link === "live" ? "ok" : "wait";
  const liveWord = stale && lastOk ? `Not updating since ${fmtEAT(lastOk)}` : link === "live" ? "Live" : link === "down" ? "Reconnecting" : "Connecting";

  return (
    <div className="wallboard">
      <header className="wb-head">
        <div className="wb-title">
          <BrandMark size={48} />
          <div className="wb-title-text">
            <h1>NOC wallboard</h1>
            <p className="wb-head-sub">{operator ? `${operator}, P1 and P2 tickets` : "P1 and P2 tickets"}</p>
          </div>
        </div>
        <div className="wb-head-right">
          <div className="wb-state">
            {/* Old tiles on the glass after a failed poll say so here, in red. Before the first
                answer the grid says so instead. */}
            <span className={`wb-live ${liveTone}`} role="status">
              <span className="wb-live-dot" aria-hidden="true" />
              {liveWord}
            </span>
            {metricsStale && (
              <span className="wb-live wait" role="status">
                Counts aren't updating
              </span>
            )}
            {red.length > 0 && <span className="chip bad">Escalated {red.length}</span>}
          </div>
          <WallClock />
        </div>
      </header>
      <dl className="wb-stats">
        <Stat label="Open tickets" value={metrics?.open_total} />
        <Stat label="P1 critical" value={metrics ? metrics.by_priority?.P1 ?? 0 : null} tone="p1" />
        <Stat label="P2 major" value={metrics ? metrics.by_priority?.P2 ?? 0 : null} tone="p2" />
        <Stat label="Approvals waiting" value={metrics ? metrics.hitl_pending ?? 0 : null} tone="hitl" />
        <Stat label="M‑PESA at risk" value={rows === null ? null : mpesaCount} tone="p1" />
      </dl>
      {/* Notes under the figures, on one row: a flag most tiles carry (said once here instead of
          on every tile; M-PESA has its own figure above), then the platform alarms (§4.6, §9.6,
          §10.4): "AGENTS OFFLINE" / circuit-open and the red redaction-miss chip, which take the
          whole row when they fire. Each boundary's fallback is null, so a broken alarm component
          can never blank the P1/P2 grid; with nothing to say the row is empty and takes no room. */}
      <div className="wb-notices">
        {dropDecision && list.length > 0 && (
          <p className="wb-note hitl">
            Decision waiting on {decisionCount} of {list.length} tickets
          </p>
        )}
        <CardBoundary fallback={null}>
          <AgentsStatusTile />
        </CardBoundary>
        <CardBoundary fallback={null}>
          <RedactionMissChip />
        </CardBoundary>
      </div>
      {/*
        Weather context sits above the incident grid so it stays on screen during
        a storm — the one time it is worth anything — but it is deliberately the
        quietest band on the wall (see components/RiskStrip.tsx). It renders
        nothing at all when WEATHER_ENABLED is off or the read endpoint is
        missing, so the wallboard is unchanged in the default configuration.

        The boundary's fallback is `null`: if a future payload shape somehow
        breaks the strip's render, the strip vanishes and the P1/P2 grid below it
        keeps working. Weather advisory may never be able to blank the wallboard.
      */}
      <CardBoundary fallback={null}>
        <RiskStrip rev={signalsRev} />
      </CardBoundary>
      {/* §6.5 T+30: red cards with no tile on the grid below. Rendered only when there are
          any, so the default glass is byte-for-byte what it was. */}
      {offGrid.length > 0 && (
        <div className="wb-escalation-strip" role="alert">
          <div className="wb-line">
            <span>Decision waiting</span>
            <span>Unclaimed past T+30</span>
          </div>
          {offGrid.map((c) => (
            <div key={c.id} className="wb-line muted">
              <span>{c.priority ?? "P?"}</span>
              <span>{labelFor(c.task_type)}</span>
              <span className="mono">{c.incident_number ?? "no ticket"}</span>
              {c.unclaimed_minutes != null && <span>unclaimed {c.unclaimed_minutes} min</span>}
              {c.since_eat && <span>red since {c.since_eat}</span>}
            </div>
          ))}
        </div>
      )}
      {rows === null && !stale && (
        <span className="sr-only" role="status">
          Loading the P1 and P2 tickets
        </span>
      )}
      {rows === null && stale && (
        <div className="empty" role="alert">
          Couldn't reach the ticket list; the wall retries every 5 seconds.{" "}
          <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
            Retry
          </button>
        </div>
      )}
      <div className="wb-grid" ref={gridRef} aria-busy={rows === null || undefined}>
        {rows === null &&
          !stale &&
          Array.from({ length: 4 }, (_, k) => (
            <div key={"sk-" + k} className="wb-card wb-card-skeleton" aria-hidden="true">
              {SKELETON_WIDTHS.map((w, j) => (
                <span key={j} className="skeleton" style={{ width: w }} />
              ))}
            </div>
          ))}
        {rows !== null && rows.length === 0 && <div className="empty wb-quiet">No P1 or P2 ticket is open. A quiet glass.</div>}
        {shown.map((i) => {
          const esc = redByIncident.get(String(i.id));
          const flags = tileFlags(i);
          const age = openFor(i.created_at, now);
          const subs = typeof i.users_affected === "number" ? i.users_affected : Number(i.users_affected);
          const status = [humanEnum(i.status), i.tt_category ? humanEnum(i.tt_category) : i.failure_domain ? humanEnum(i.failure_domain) : ""]
            .filter(Boolean)
            .join(", ");
          return (
            <article key={i.id} className={`wb-card ${i.priority}${esc ? " escalated" : ""}`} aria-label={`${i.priority} ${i.site_name || i.incident_number}`}>
              <div className="wb-card-top">
                <span className={`wb-prio ${i.priority}`}>{i.priority}</span>
                {/* A long number is cut at its start, so its end (the part that differs) stays. */}
                <span className="wb-ticket" title={String(i.incident_number ?? "")}>
                  <bdi>{i.incident_number}</bdi>
                </span>
                {age && (
                  <span className="wb-age" title="Open for">
                    <Timer {...CARD_ICON} />
                    {age}
                  </span>
                )}
              </div>
              <div className="wb-site">{i.site_name || i.site_id || "Unknown site"}</div>
              <div className="wb-meta">
                <span>
                  <MapPin {...CARD_ICON} />
                  <span className="wb-tx">{regionName(i.region_code, profile)}</span>
                </span>
                {Number.isFinite(subs) && subs > 0 && (
                  <span title={`${subs.toLocaleString()} subscribers affected`}>
                    <UsersRound {...CARD_ICON} />
                    <span className="wb-tx">{compact(subs)} subscribers</span>
                  </span>
                )}
              </div>
              <div className="wb-owner-line">
                <span className="wb-owner">
                  <Wrench {...CARD_ICON} />
                  <span className="wb-tx">{nameOf(i.assignee_name) || "Unassigned"}</span>
                </span>
                {status && <span className="wb-status">{status}</span>}
              </div>
              {/* The flag line is held on every tile while any tile carries a flag, so the tiles
                  stay one height. */}
              {anyTileFlag && (
                <div className="wb-flags">
                  {flags.mpesa && <span className="wb-flag danger">M‑PESA at risk</span>}
                  {flags.decision && <span className="wb-flag hitl">Decision waiting</span>}
                </div>
              )}
              {esc && (
                <div className="wb-escalated-line wb-line">
                  <span>Decision waiting</span>
                  <span>
                    {labelFor(esc.task_type)} unclaimed
                    {esc.unclaimed_minutes != null ? ` ${esc.unclaimed_minutes} min` : ""}
                  </span>
                  {esc.since_eat && <span>red since {esc.since_eat}</span>}
                </div>
              )}
            </article>
          );
        })}
        {overflow && (
          <a className="wb-card wb-more" href="/incidents">
            <span className="wb-more-n">+{hidden.length} more</span>
            <span className="wb-more-what">
              {hiddenWhat} {hidden.length === 1 ? "ticket" : "tickets"}, opened more recently
            </span>
            <span className="wb-more-where">On the Incident board</span>
          </a>
        )}
      </div>
      <p className="wb-foot" ref={footRef}>
        <a href="/mission">Back to Mission control</a>
      </p>
    </div>
  );
}
