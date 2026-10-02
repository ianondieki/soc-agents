import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { IconAlert, IconCheck, IconDot, IconPause } from "../lib/icons";
import { fmtDate, fmtDateTime, fmtTime, parseInstant } from "../lib/time";
import {
  actorKind,
  actorLabel,
  entityLabel,
  groupBlocks,
  isOpaqueId,
  isRoutine,
  mergeIdentical,
  outcomeOf,
  parseRationale,
  routineNames,
  rowHaystack,
  rowTitle,
  type AuditBlock,
  type AuditEntry,
  type DisplayRow,
  type Outcome,
} from "../lib/audit";
import "./Audit.css";

/**
 * The regulator-facing record as a timeline an auditor scans in seconds: one ticket per block
 * (a run's intake steps sit with the ticket they opened), routine steps quiet, exceptions in
 * their state colour. Stored text is never rewritten: a row's raw actor, action, time and
 * rationale are one click away under "Recorded as", with Copy.
 */
const FETCH = 500;
const PAGE = 8; // tickets shown before "Show N older tickets"
const VIEW_KEY = "noc_audit_view_v1";

type Kind = "all" | "automated" | "people";
type View = "all" | "decisions";
type Load = "loading" | "ready" | "error";
type IncidentLite = {
  id: string;
  incident_number: string;
  site_id: string;
  site_name?: string | null;
  priority?: string | null;
  created_at?: string | null;
};

function readView(): View {
  try {
    return sessionStorage.getItem(VIEW_KEY) === "all" ? "all" : "decisions";
  } catch {
    return "decisions";
  }
}

function saveView(view: View): void {
  try {
    sessionStorage.setItem(VIEW_KEY, view);
  } catch {
    /* private window or blocked storage: the toggle still works for this visit */
  }
}

export default function Audit({ tick }: { tick: number }) {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [incidents, setIncidents] = useState<Record<string, IncidentLite>>({});
  const [load, setLoad] = useState<Load>("loading");
  const [stale, setStale] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Kind>("all");
  const [view, setView] = useState<View>(readView);
  const [shown, setShown] = useState(PAGE);
  const loaded = useRef(false);

  useEffect(() => {
    let live = true;
    api
      .audit(FETCH)
      .then((list) => {
        if (!live) return;
        setRows(Array.isArray(list) ? (list as AuditEntry[]) : []);
        setLoad("ready");
        setStale(false);
        loaded.current = true;
      })
      .catch(() => {
        if (!live) return;
        if (loaded.current) setStale(true);
        else setLoad("error");
      });
    api
      .incidents()
      .then((list: IncidentLite[]) => {
        if (!live) return;
        const byId: Record<string, IncidentLite> = {};
        for (const i of list || []) byId[i.id] = i;
        setIncidents(byId);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [tick, attempt]);

  const retry = () => {
    if (!loaded.current) setLoad("loading");
    setAttempt((n) => n + 1);
  };

  // Tickets per site, for a folded run whose rationale names its parent HUB.
  const bySite = useMemo(() => {
    const out: Record<string, IncidentLite[]> = {};
    for (const i of Object.values(incidents)) (out[i.site_id] ||= []).push(i);
    return out;
  }, [incidents]);

  const blocks = useMemo(() => groupBlocks(rows), [rows]);
  const needle = q.trim().toLowerCase();
  const filterActive = needle !== "" || kind !== "all";

  // Blocks are formed from every loaded row first, so a filter never orphans a run's intake
  // steps from their ticket; the filter then keeps the rows (or the whole block) that match.
  const filtered = useMemo(() => {
    const out: { block: AuditBlock; rows: AuditEntry[] }[] = [];
    for (const block of blocks) {
      let kept = block.rows;
      if (kind !== "all") kept = kept.filter((r) => (actorKind(r.actor) === "person") === (kind === "people"));
      if (needle && kept.length) {
        const head = blockHaystack(block, incidentOf(block, incidents), parentOf(block, bySite));
        if (!head.includes(needle)) kept = kept.filter((r) => rowHaystack(r).includes(needle));
      }
      if (kept.length) out.push({ block, rows: kept });
    }
    return out;
  }, [blocks, incidents, bySite, kind, needle]);

  const { visible, olderTickets } = useMemo(() => {
    const vis: typeof filtered = [];
    let tickets = 0;
    let i = 0;
    for (; i < filtered.length; i++) {
      if (filtered[i].block.kind === "ticket") {
        if (tickets === shown) break;
        tickets += 1;
      }
      vis.push(filtered[i]);
    }
    return { visible: vis, olderTickets: filtered.slice(i).filter((f) => f.block.kind === "ticket").length };
  }, [filtered, shown]);

  const ticketCount = useMemo(
    () => new Set(filtered.filter((f) => f.block.kind === "ticket").map((f) => f.block.ticketId)).size,
    [filtered]
  );
  const entryCount = useMemo(() => filtered.reduce((n, f) => n + f.rows.length, 0), [filtered]);

  const clearFilters = () => {
    setQ("");
    setKind("all");
    setShown(PAGE);
  };
  const chooseView = (v: View) => {
    setView(v);
    saveView(v);
  };
  const more = Math.min(PAGE, olderTickets);

  return (
    <div className="content-narrow audit-page">
      <div className="page-head">
        <div>
          <h1>Audit trail</h1>
          <p className="lead">Every agent step and human decision, with its reason. Times in EAT.</p>
        </div>
        <div className="page-actions" role="group" aria-label="Filter the audit trail">
          <input
            type="search"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setShown(PAGE);
            }}
            placeholder="Search ticket, site, actor or reason"
            aria-label="Search ticket, site, actor or reason"
            className="audit-search"
          />
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as Kind);
              setShown(PAGE);
            }}
            aria-label="Who acted"
            className="audit-kind"
          >
            <option value="all">All</option>
            <option value="automated">Automated</option>
            <option value="people">People</option>
          </select>
        </div>
      </div>

      <div className="audit-bar">
        <p className="audit-count">
          {load === "ready" && rows.length > 0 && (
            <span>
              {ticketCount} {ticketCount === 1 ? "ticket" : "tickets"}, {entryCount} {entryCount === 1 ? "entry" : "entries"}
            </span>
          )}
          {load === "ready" && rows.length >= FETCH && <span>Latest {FETCH} loaded</span>}
          {filterActive && (
            <button type="button" className="audit-link" onClick={clearFilters}>
              Clear filters
            </button>
          )}
          {stale && (
            <span className="audit-stale">
              Couldn't refresh; showing the last load.{" "}
              <button type="button" className="audit-link" onClick={retry}>
                Retry
              </button>
            </span>
          )}
        </p>
        <div className="audit-seg" role="group" aria-label="Rows to show">
          <button type="button" aria-pressed={view === "all"} onClick={() => chooseView("all")}>
            Every step
          </button>
          <button type="button" aria-pressed={view === "decisions"} onClick={() => chooseView("decisions")}>
            Decisions and exceptions
          </button>
        </div>
      </div>

      <div className="panel audit-panel">
        {load === "loading" ? (
          <SkeletonRows />
        ) : load === "error" ? (
          <div className="audit-state" role="alert">
            <p>Couldn't load the audit trail.</p>
            <button type="button" className="btn sm" onClick={retry}>
              Retry
            </button>
          </div>
        ) : rows.length === 0 ? (
          <div className="audit-state">
            <p>Nothing has happened yet.</p>
            <p className="audit-state-hint">
              Run an alarm from <Link to="/">Mission control</Link>; every agent step lands here.
            </p>
          </div>
        ) : filtered.length === 0 ? (
          <div className="audit-state">
            <p>No entries match.</p>
            <button type="button" className="btn sm" onClick={clearFilters}>
              Clear filters
            </button>
          </div>
        ) : (
          visible.map((f) => (
            <Block
              key={f.block.key}
              block={f.block}
              rows={f.rows}
              incident={incidentOf(f.block, incidents)}
              parent={parentOf(f.block, bySite)}
              view={view}
            />
          ))
        )}
      </div>

      {load === "ready" && more > 0 && (
        <div className="audit-more">
          <button type="button" className="btn" onClick={() => setShown((s) => s + PAGE)}>
            Show {more} older {more === 1 ? "ticket" : "tickets"}
          </button>
        </div>
      )}
    </div>
  );
}

function incidentOf(block: AuditBlock, incidents: Record<string, IncidentLite>): IncidentLite | undefined {
  return block.ticketId ? incidents[block.ticketId] : undefined;
}

/** The parent a folded run joined: the ticket at the named HUB that was opened most recently
 *  before the run, so an older fold never points at a newer ticket at the same site. */
function parentOf(block: AuditBlock, bySite: Record<string, IncidentLite[]>): IncidentLite | undefined {
  if (block.kind !== "folded" || !block.parentSite) return undefined;
  const at = parseInstant(block.rows[block.rows.length - 1].ts)?.getTime() ?? Infinity;
  let best: IncidentLite | undefined;
  let bestT = -Infinity;
  for (const i of bySite[block.parentSite] || []) {
    const t = parseInstant(i.created_at)?.getTime() ?? -Infinity;
    if (t <= at && t >= bestT) {
      best = i;
      bestT = t;
    }
  }
  return best;
}

function blockHaystack(block: AuditBlock, incident?: IncidentLite, parent?: IncidentLite): string {
  return [
    incident?.incident_number,
    incident?.site_id,
    incident?.site_name,
    parent?.incident_number,
    block.parentSite,
    block.jobName,
    blockTitleText(block, incident),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function blockTitleText(block: AuditBlock, incident?: IncidentLite): string {
  switch (block.kind) {
    case "ticket":
      return incident ? incident.incident_number : "Ticket";
    case "folded":
      return "Folded into an open ticket";
    case "approval":
      return incident ? `Approval ${incident.incident_number}` : "Approval, not linked to a ticket";
    case "job":
      return block.jobName || "Scheduled job";
    default:
      return entityLabel(block.entityType);
  }
}

/** "01 Oct, 15:13:01–15:13:02" for the block's visible rows, oldest to newest. */
function rangeText(rows: AuditEntry[]): string {
  const newest = rows[0].ts;
  const oldest = rows[rows.length - 1].ts;
  const from = fmtDateTime(oldest);
  if (fmtTime(oldest) === fmtTime(newest) && fmtDate(oldest) === fmtDate(newest)) return from;
  return fmtDate(oldest) === fmtDate(newest) ? `${from}–${fmtTime(newest)}` : `${from} – ${fmtDateTime(newest)}`;
}

function countWord(rows: AuditEntry[]): string {
  const n = rows.length;
  if (rows.every((r) => r.action.startsWith("step."))) return `${n} ${n === 1 ? "step" : "steps"}`;
  return `${n} ${n === 1 ? "entry" : "entries"}`;
}

function Block({
  block,
  rows,
  incident,
  parent,
  view,
}: {
  block: AuditBlock;
  rows: AuditEntry[];
  incident?: IncidentLite;
  parent?: IncidentLite;
  view: View;
}) {
  const headId = useId();
  const [showRoutine, setShowRoutine] = useState(false);
  const display = useMemo(() => mergeIdentical(rows), [rows]);
  const routine = useMemo(() => display.filter((d) => isRoutine(d.entry)), [display]);
  const folding = view === "decisions" && routine.length > 0;
  const listed = folding && !showRoutine ? display.filter((d) => !isRoutine(d.entry)) : display;

  let routineLabel = "";
  if (folding) {
    const n = routine.reduce((sum, d) => sum + d.count, 0);
    const steps = routine.every((d) => d.entry.action.startsWith("step."));
    routineLabel = `${n} routine ${steps ? (n === 1 ? "step" : "steps") : n === 1 ? "entry" : "entries"}`;
  }

  let prevTime = "";
  return (
    <section className="audit-block" aria-labelledby={headId}>
      <header className="audit-head">
        <h2 id={headId} className="audit-head-title">
          <BlockTitle block={block} incident={incident} parent={parent} />
        </h2>
        <p className="audit-head-meta">
          <span>{rangeText(rows)}</span>
          {block.kind === "folded" && block.runs > 1 && <span>{block.runs} alarms</span>}
          <span>{countWord(rows)}</span>
        </p>
      </header>
      <ol className="audit-rows">
        {listed.map((d) => {
          const time = fmtTime(d.entry.ts);
          const showTime = time !== prevTime;
          prevTime = time;
          return <Row key={d.id} d={d} showTime={showTime} />;
        })}
        {folding && (
          <li>
            <button
              type="button"
              className="audit-routine"
              aria-expanded={showRoutine}
              onClick={() => setShowRoutine((s) => !s)}
            >
              {showRoutine
                ? `Hide ${routineLabel}`
                : `Show ${routineLabel} (${routineNames(routine.map((d) => d.entry))})`}
            </button>
          </li>
        )}
      </ol>
    </section>
  );
}

function BlockTitle({ block, incident, parent }: { block: AuditBlock; incident?: IncidentLite; parent?: IncidentLite }) {
  const ticket = (inc: IncidentLite, label?: string) => (
    <>
      {inc.priority && <span className={`pill ${inc.priority}`}>{inc.priority}</span>}
      <span className="audit-inc">{inc.incident_number}</span>
      {label && <span className="audit-site">{label}</span>}
      <span className="audit-site">{inc.site_id}</span>
      {inc.site_name && <span className="audit-site audit-site-name">{inc.site_name}</span>}
    </>
  );
  switch (block.kind) {
    case "ticket":
      return incident ? (
        ticket(incident)
      ) : (
        <>
          <span className="audit-inc">Ticket</span>
          <span className="audit-site">not in the incident list</span>
        </>
      );
    case "folded":
      return (
        <>
          <span className="audit-inc">Folded into an open ticket</span>
          {parent && <span className="audit-site audit-parent">{parent.incident_number}</span>}
          {block.parentSite && <span className="audit-site">under HUB {block.parentSite}</span>}
        </>
      );
    case "approval":
      return incident ? ticket(incident, "Approval") : <span className="audit-inc">Approval, not linked to a ticket</span>;
    case "job":
      return (
        <>
          <span className="audit-inc">{block.jobName || "Scheduled job"}</span>
          {block.jobName && <span className="audit-site">Scheduled job</span>}
        </>
      );
    default:
      return (
        <>
          <span className="audit-inc">{entityLabel(block.entityType)}</span>
          {block.entityId && !isOpaqueId(block.entityId) && <span className="audit-site audit-code">{block.entityId}</span>}
          {incident && <span className="audit-site">{incident.incident_number}</span>}
        </>
      );
  }
}

function OutcomeMark({ outcome }: { outcome: Outcome }) {
  const Icon = outcome.icon === "pause" ? IconPause : outcome.icon === "check" ? IconCheck : outcome.icon === "dot" ? IconDot : IconAlert;
  return (
    <span className={`audit-out ${outcome.tone}`}>
      <Icon />
      {outcome.word}
    </span>
  );
}

function Row({ d, showTime }: { d: DisplayRow; showTime: boolean }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const r = d.entry;
  const parsed = useMemo(() => parseRationale(r.rationale), [r.rationale]);
  const outcome = outcomeOf(r.action);
  const who = actorKind(r.actor);
  const time = fmtTime(r.ts);
  const iso = parseInstant(r.ts)?.toISOString();
  return (
    <li className={"audit-entry" + (open ? " open" : "")}>
      <button
        type="button"
        className="audit-row"
        aria-expanded={open}
        aria-controls={open ? detailId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        <time className={"audit-time" + (showTime ? "" : " same")} dateTime={iso}>
          {showTime ? time : <span className="sr-only">{time}</span>}
        </time>
        <span className={`audit-actor ${who}`}>
          {actorLabel(r.actor)}
          {who === "job" && <span className="audit-job"> (job)</span>}
        </span>
        <span className="audit-summary">
          <span className="audit-step">{rowTitle(r)}</span>
          {d.count > 1 && <span className="audit-times">×{d.count}</span>}
          {parsed.sentence && <span className="audit-sentence">{parsed.sentence}</span>}
          {parsed.facts.slice(0, 3).map((f, i) => (
            <span key={`${f.key}-${i}`} className="audit-fact">
              <span className="audit-fact-k">{f.label}</span> {f.value}
            </span>
          ))}
        </span>
        <span className="audit-outcome">{outcome && <OutcomeMark outcome={outcome} />}</span>
      </button>
      {open && <RowDetail id={detailId} d={d} sentence={parsed.sentence} facts={parsed.facts} />}
    </li>
  );
}

function RowDetail({
  id,
  d,
  sentence,
  facts,
}: {
  id: string;
  d: DisplayRow;
  sentence: string;
  facts: ReturnType<typeof parseRationale>["facts"];
}) {
  const r = d.entry;
  const rawRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState<"" | "done" | "select">("");
  const iso = parseInstant(r.ts)?.toISOString() ?? String(r.ts);
  const record = [iso, r.actor, r.action, r.entity_type + (r.entity_id ? `:${r.entity_id}` : "")].join("  ") + (r.rationale ? `\n${r.rationale}` : "");

  useEffect(() => {
    if (copied !== "done") return;
    const t = window.setTimeout(() => setCopied(""), 2000);
    return () => window.clearTimeout(t);
  }, [copied]);

  const selectRaw = (): boolean => {
    const el = rawRef.current;
    const sel = window.getSelection();
    if (!el || !sel) return false;
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    }
  };

  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(record);
      setCopied("done");
    } catch {
      setCopied(selectRaw() ? "done" : "select");
    }
  };

  return (
    <div id={id} className="audit-detail">
      {sentence && <p className="audit-detail-sentence">{sentence}</p>}
      {facts.length > 0 && (
        <dl className="audit-facts">
          {facts.map((f, i) => (
            <div key={`${f.key}-${i}`}>
              <dt>{f.label}</dt>
              <dd>{f.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {d.count > 1 && (
        <p className="audit-detail-note">
          Recorded {d.count} times, {fmtTime(d.oldestTs)} to {fmtTime(r.ts)}.
        </p>
      )}
      <div className="audit-record">
        <span className="audit-record-label">Recorded as</span>
        <div className="audit-record-raw" ref={rawRef}>
          <span>{r.actor}</span>
          <span>{r.action}</span>
          <span>{iso}</span>
          {r.rationale && <span className="audit-record-why">{r.rationale}</span>}
        </div>
        <div className="audit-record-copy">
          <button type="button" className="btn sm" onClick={copy}>
            {copied === "done" ? "Copied" : "Copy"}
          </button>
          {copied === "select" && <span className="audit-record-hint">Selected. Press Ctrl+C to copy.</span>}
        </div>
      </div>
    </div>
  );
}

function SkeletonRows() {
  const widths = ["62%", "48%", "70%", "55%", "40%", "66%", "52%", "58%", "45%", "64%"];
  return (
    <div className="audit-skeleton" aria-busy="true" aria-label="Loading the audit trail">
      <div className="audit-head" aria-hidden="true">
        <span className="skeleton" style={{ width: "14rem" }} />
      </div>
      {widths.map((w, i) => (
        <div key={i} className="audit-skel-row" aria-hidden="true">
          <span className="skeleton" style={{ width: "3.6rem" }} />
          <span className="skeleton" style={{ width: "7rem" }} />
          <span className="skeleton" style={{ width: w }} />
          <span />
        </div>
      ))}
    </div>
  );
}
