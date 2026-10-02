import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import CardBoundary from "../components/CardBoundary";
import RiskStrip from "../components/RiskStrip";
import AgentsStatusTile from "../components/AgentsStatusTile";
import RedactionMissChip from "../components/RedactionMissChip";
import { humanEnum } from "../lib/agents";
import { labelFor } from "../lib/hitl";
import { fmtEAT, parseInstant } from "../lib/time";
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

/** One header figure: a number read from the back of the room, its label small beneath. */
function Stat({ label, value, tone }: { label: string; value: number | null | undefined; tone?: "p1" | "p2" | "hitl" }) {
  const n = typeof value === "number" ? value : null;
  return (
    <div className={"wb-stat" + (tone && n ? ` ${tone}` : "")}>
      <dt>{label}</dt>
      <dd>{n == null ? "—" : n}</dd>
    </div>
  );
}

export default function Wallboard({
  metrics,
  metricsStale = false,
  rev = 0,
  signalsRev = 0,
}: {
  metrics: any;
  /** The metrics call failed while the API still answers: the header counts are the last ones. */
  metricsStale?: boolean;
  rev?: number;
  /** Debounced `signals` slice revision — see realtime/renderers.ts. */
  signalsRev?: number;
}) {
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

  return (
    <div className="wallboard">
      <div className="wb-head">
        <h1>
          NOC WALLBOARD <span className="wb-head-sub">SAFARICOM DEMO</span>
        </h1>
        <div className="wb-head-right">
          <div className="chips">
            {/* Old tiles on the glass after a failed poll. Before the first answer the grid says so instead. */}
            {stale && lastOk && (
              <span className="chip" role="status">
                Not updating since {fmtEAT(lastOk)}
              </span>
            )}
            {metricsStale && (
              <span className="chip" role="status">
                Counts aren't updating
              </span>
            )}
            {red.length > 0 && <span className="chip bad">ESCALATED {red.length}</span>}
          </div>
          <dl className="wb-stats">
            <Stat label="Open" value={metrics?.open_total} />
            <Stat label="Decisions" value={metrics ? metrics.hitl_pending ?? 0 : null} tone="hitl" />
            <Stat label="P1" value={metrics ? metrics.by_priority?.P1 ?? 0 : null} tone="p1" />
            <Stat label="P2" value={metrics ? metrics.by_priority?.P2 ?? 0 : null} tone="p2" />
          </dl>
        </div>
      </div>
      {(dropMpesa || dropDecision) && list.length > 0 && (
        <div className="wb-flags wb-flags-head">
          {dropMpesa && (
            <span className="danger">
              M‑PESA AT RISK on {mpesaCount} of {list.length} tickets
            </span>
          )}
          {dropDecision && (
            <span className="hitl">
              DECISION WAITING on {decisionCount} of {list.length} tickets
            </span>
          )}
        </div>
      )}
      {/* Platform alarms (§4.6, §9.6, §10.4): "AGENTS OFFLINE" / circuit-open and the red
          redaction-miss chip. Above everything else on the glass; each boundary's fallback is
          null, so a broken alarm component can never blank the P1/P2 grid. */}
      <CardBoundary fallback={null}>
        <AgentsStatusTile />
      </CardBoundary>
      <CardBoundary fallback={null}>
        <RedactionMissChip />
      </CardBoundary>
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
            <span>DECISION WAITING</span>
            <span>UNCLAIMED PAST T+30</span>
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
        {rows !== null && rows.length === 0 && <div className="empty">No P1/P2 open — quiet glass.</div>}
        {shown.map((i) => {
          const esc = redByIncident.get(String(i.id));
          const flags = tileFlags(i);
          return (
            <div key={i.id} className={`wb-card ${i.priority}${esc ? " escalated" : ""}`}>
              <div className="big wb-line">
                <span>{i.priority}</span>
                <span>{i.incident_number}</span>
              </div>
              <div className="wb-site">{i.site_name}</div>
              <div className="wb-line wb-meta muted">
                <span className="mono">{i.region_code}</span>
                {/* The domain only when no category says it more exactly on the status line. */}
                {i.failure_domain && !i.tt_category && <span>{humanEnum(i.failure_domain)}</span>}
                <span>{i.users_affected?.toLocaleString()} subscribers</span>
              </div>
              <div className="wb-owner">Owner {nameOf(i.assignee_name)}</div>
              <div className="wb-line wb-meta muted">
                <span>{humanEnum(i.status)}</span>
                {i.tt_category && <span>{humanEnum(i.tt_category)}</span>}
              </div>
              {/* The flag line is held on every tile while any tile carries a flag, so the tiles
                  stay one height. */}
              {anyTileFlag && (
                <div className="wb-flags">
                  {flags.mpesa && <span className="danger">M‑PESA AT RISK</span>}
                  {flags.decision && <span className="hitl">DECISION WAITING</span>}
                </div>
              )}
              {esc && (
                <div className="wb-escalated-line wb-line">
                  <span>DECISION WAITING</span>
                  <span>
                    {labelFor(esc.task_type)} unclaimed
                    {esc.unclaimed_minutes != null ? ` ${esc.unclaimed_minutes} min` : ""}
                  </span>
                  {esc.since_eat && <span>red since {esc.since_eat}</span>}
                </div>
              )}
            </div>
          );
        })}
        {overflow && (
          <a className="wb-card wb-more" href="/incidents">
            <span className="wb-more-n">+{hidden.length} more</span>
            <span className="wb-more-what">
              {hiddenP1 > 0 ? `${hiddenP1} P1 and ${hidden.length - hiddenP1} P2` : "P2"} tickets, opened more recently
            </span>
            <span className="wb-more-where">On the Incident board</span>
          </a>
        )}
      </div>
      <p className="wb-foot muted" ref={footRef}>
        <a href="/">Back to Mission control</a>
      </p>
    </div>
  );
}
