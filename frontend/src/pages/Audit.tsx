import { Fragment, useEffect, useId, useMemo, useRef, useState } from "react";
import { Bot, Clock, Search, User } from "lucide-react";
import { priorityTitle } from "../lib/agents";
import { Link } from "react-router-dom";
import { api } from "../api";
import { statusOf } from "../lib/apiError";
import { usePrinting } from "../lib/display";
import { IconAlert, IconCheck, IconDot, IconPause } from "../lib/icons";
import { fmtDate, fmtDateTime, fmtTime, parseInstant } from "../lib/time";
import {
  actorKind,
  actorLabel,
  auditUrl,
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
  searchTerms,
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
 *
 * SEARCH IS HONEST ABOUT ITS REACH. Typing filters the newest 500 entries in the browser, at once.
 * When that finds nothing it says so ("No match in the newest 500 entries") and offers "Search
 * all entries", which asks the API (`q`, up to 1000 matches) with what was typed plus the id of
 * each ticket the text names (a step row stores the ticket's id, not its INC number). The same
 * offer sits in the count line whenever a search runs over a capped page. Changing the search
 * goes back to the newest 500.
 */
const FETCH = 500;
/** "Search all entries": the API's own bound on `limit`. */
const SEARCH_ALL = 1000;
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

/** The audit rows for `terms` (each a `q`, matched by the API over every entry), newest first. */
async function searchAudit(terms: string[]): Promise<AuditEntry[]> {
  const r = await fetch(auditUrl(SEARCH_ALL, terms), { headers: { "Content-Type": "application/json" } });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  const list = await r.json();
  return Array.isArray(list) ? (list as AuditEntry[]) : [];
}

type SearchAll = { needle: string; terms: string[] };

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
  // Site names, for a record block (a maintenance task, a window) whose rows name a site code.
  const [siteNames, setSiteNames] = useState<Record<string, string>>({});
  const [load, setLoad] = useState<Load>("loading");
  const [stale, setStale] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Kind>("all");
  const [view, setView] = useState<View>(readView);
  const [shown, setShown] = useState(PAGE);
  const [forbidden, setForbidden] = useState(false);
  const loaded = useRef(false);
  // "Search all entries": the request (what was typed when it was pressed), its answer, and its state.
  const [searchReq, setSearchReq] = useState<SearchAll | null>(null);
  const [found, setFound] = useState<{ needle: string; rows: AuditEntry[] } | null>(null);
  const [searchState, setSearchState] = useState<"idle" | "loading" | "error">("idle");
  const [said, setSaid] = useState("");
  const focusResults = useRef(false);
  const panelRef = useRef<HTMLDivElement>(null);

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
      .catch((e: unknown) => {
        if (!live) return;
        setForbidden(statusOf(e) === 403);
        if (loaded.current) setStale(true);
        else setLoad("error");
      });
    api
      .incidents()
      .then((list: IncidentLite[]) => {
        if (!live) return;
        const byId: Record<string, IncidentLite> = {};
        const names: Record<string, string> = {};
        for (const i of list || []) {
          byId[i.id] = i;
          if (i.site_id && i.site_name) names[i.site_id] = i.site_name;
        }
        setIncidents(byId);
        setSiteNames((prev) => ({ ...names, ...prev }));
      })
      .catch(() => undefined);
    api
      .sites()
      .then((list: any[]) => {
        if (!live) return;
        const names: Record<string, string> = {};
        for (const x of Array.isArray(list) ? list : []) if (x?.site_id && x?.site_name) names[x.site_id] = x.site_name;
        setSiteNames((prev) => ({ ...prev, ...names }));
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

  // The search over every entry, asked again when the trail moves (a new row may match).
  useEffect(() => {
    if (!searchReq) return;
    let live = true;
    searchAudit(searchReq.terms)
      .then((list) => {
        if (!live) return;
        setFound({ needle: searchReq.needle, rows: list });
        setSearchState("idle");
      })
      .catch(() => {
        if (live) setSearchState("error");
      });
    return () => {
      live = false;
    };
  }, [searchReq, tick]);

  // Tickets per site, for a folded run whose rationale names its parent HUB.
  const bySite = useMemo(() => {
    const out: Record<string, IncidentLite[]> = {};
    for (const i of Object.values(incidents)) (out[i.site_id] ||= []).push(i);
    return out;
  }, [incidents]);

  const needle = q.trim().toLowerCase();
  // The rows on screen: every entry's matches once "Search all entries" has answered for what is
  // typed now; otherwise the newest 500.
  const searchedAll = found !== null && found.needle === needle && needle !== "";
  const source = searchedAll ? found.rows : rows;
  const cap = searchedAll ? SEARCH_ALL : FETCH;
  // At the cap the oldest block is probably cut short (its earlier rows are past the cap), so
  // it is left out rather than shown as if it were the whole ticket.
  const capped = source.length >= cap;
  const blocks = useMemo(() => {
    const all = groupBlocks(source);
    return capped && all.length > 1 ? all.slice(0, -1) : all;
  }, [source, capped]);
  const baseCapped = rows.length >= FETCH;
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

  // Paper has no "Show N older tickets" button: while printing, every ticket is on the page.
  const printing = usePrinting();
  const { visible, olderTickets } = useMemo(() => {
    const limit = printing ? Infinity : shown;
    const vis: typeof filtered = [];
    let tickets = 0;
    let i = 0;
    for (; i < filtered.length; i++) {
      if (filtered[i].block.kind === "ticket") {
        if (tickets >= limit) break;
        tickets += 1;
      }
      vis.push(filtered[i]);
    }
    return { visible: vis, olderTickets: filtered.slice(i).filter((f) => f.block.kind === "ticket").length };
  }, [filtered, shown, printing]);

  const ticketCount = useMemo(
    () => new Set(filtered.filter((f) => f.block.kind === "ticket").map((f) => f.block.ticketId)).size,
    [filtered]
  );
  const entryCount = useMemo(() => filtered.reduce((n, f) => n + f.rows.length, 0), [filtered]);

  /** A new search (or none) goes back to the newest 500 and forgets the last search-all. */
  const forgetSearchAll = () => {
    setSearchReq(null);
    setFound(null);
    setSearchState("idle");
  };
  const clearFilters = () => {
    setQ("");
    setKind("all");
    setShown(PAGE);
    forgetSearchAll();
  };
  const searchAll = () => {
    if (searchState === "loading" || !needle) return;
    focusResults.current = true;
    setSearchState("loading");
    setShown(PAGE);
    setSearchReq({ needle, terms: searchTerms(q, Object.values(incidents)) });
  };
  // Pressing "Search all entries" removes the button it was pressed on: the answer is spoken, and
  // focus goes to the first block found (the panel when nothing was).
  useEffect(() => {
    if (!searchedAll || !focusResults.current) return;
    focusResults.current = false;
    const tickets = new Set(filtered.filter((f) => f.block.kind === "ticket").map((f) => f.block.ticketId)).size;
    const entries = filtered.reduce((n, f) => n + f.rows.length, 0);
    setSaid(
      entries === 0
        ? "Searched every entry: nothing matches."
        : `Searched every entry: ${tickets} ${tickets === 1 ? "ticket" : "tickets"}, ${entries} ${entries === 1 ? "entry" : "entries"}.`
    );
    window.requestAnimationFrame(() => {
      const first = panelRef.current?.querySelector<HTMLElement>(".audit-head-title");
      (first || panelRef.current)?.focus({ preventScroll: false });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchedAll, found]);
  const chooseView = (v: View) => {
    setView(v);
    saveView(v);
  };
  const more = Math.min(PAGE, olderTickets);

  // The figures describe what this page has read (the newest 500, or a search over every entry).
  const figures = useMemo(() => {
    let people = 0;
    let exceptions = 0;
    let waiting = 0;
    for (const r of source) {
      if (actorKind(r.actor) === "person") people += 1;
      const o = outcomeOf(r.action);
      if (o && (o.tone === "danger" || o.tone === "warn")) exceptions += 1;
      if (o && o.tone === "hitl") waiting += 1;
    }
    const tickets = new Set(blocks.filter((b) => b.kind === "ticket").map((b) => b.ticketId)).size;
    const newest = source.length ? source[0].ts : null;
    const oldest = source.length ? source[source.length - 1].ts : null;
    return { people, exceptions, waiting, tickets, newest, oldest };
  }, [source, blocks]);

  return (
    <div className="content-narrow audit-page">
      <div className="page-head">
        <div>
          <h1>Audit trail</h1>
          <p className="lead">Every agent step and every person's decision, with its reason, as it was recorded. Times in EAT.</p>
        </div>
      </div>

      {load === "ready" && rows.length > 0 && (
        <dl className="audit-figures">
          <div>
            <dt>{searchedAll ? "Entries found" : capped ? `Newest ${cap} entries` : "Entries"}</dt>
            <dd>{source.length}</dd>
          </div>
          <div>
            <dt>Tickets</dt>
            <dd>{figures.tickets}</dd>
          </div>
          <div>
            <dt>Decisions by people</dt>
            <dd>{figures.people}</dd>
          </div>
          <div className={figures.exceptions ? "warn" : undefined}>
            <dt>Exceptions</dt>
            <dd>{figures.exceptions}</dd>
          </div>
          <div>
            <dt>Covers</dt>
            <dd className="audit-figure-word" title={`${fmtDateTime(figures.oldest)} to ${fmtDateTime(figures.newest)} EAT`}>
              {spanWords(figures.oldest, figures.newest)}
            </dd>
          </div>
        </dl>
      )}

      <div className="panel audit-toolbar" role="group" aria-label="Filter the audit trail">
        <span className="audit-search-field">
          <Search size={16} strokeWidth={1.75} aria-hidden="true" />
          <input
            type="search"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setShown(PAGE);
              if (searchReq || found) forgetSearchAll();
            }}
            placeholder="Search a ticket, site, person or reason"
            aria-label="Search ticket, site, actor or reason"
            className="audit-search"
          />
        </span>
        <div className="seg" role="group" aria-label="Who acted">
          {(
            [
              ["all", "Everyone"],
              ["automated", "Agents and jobs"],
              ["people", "People"],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              aria-pressed={kind === k}
              onClick={() => {
                setKind(k);
                setShown(PAGE);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Rows to show">
          <button type="button" aria-pressed={view === "decisions"} onClick={() => chooseView("decisions")}>
            Decisions and exceptions
          </button>
          <button type="button" aria-pressed={view === "all"} onClick={() => chooseView("all")}>
            Every step
          </button>
        </div>
      </div>

      <div className="audit-bar">
        <p className="audit-count">
          {/* With nothing matching, the panel says so (and how far it looked); no "0 tickets" here. */}
          {load === "ready" && rows.length > 0 && entryCount > 0 && (
            <span>
              Showing {ticketCount} {ticketCount === 1 ? "ticket" : "tickets"}, {entryCount} {entryCount === 1 ? "entry" : "entries"}
            </span>
          )}
          {load === "ready" && searchedAll && (
            <span
              title={
                capped
                  ? `More than ${SEARCH_ALL} entries match; these are the newest ${SEARCH_ALL}, less the oldest ticket, which they only cover in part.`
                  : "Every entry the trail keeps was searched."
              }
            >
              {capped ? `Newest ${SEARCH_ALL} matches` : "Searched every entry"}
            </span>
          )}
          {load === "ready" && !searchedAll && capped && entryCount > 0 && (
            <span title={`The trail keeps more; this page reads the newest ${FETCH} and leaves out the oldest ticket, which those entries only cover in part.`}>
              from the newest {FETCH} entries
            </span>
          )}
          {/* A search over a capped page may miss older matches: the way to reach them, in reach. */}
          {load === "ready" && !searchedAll && baseCapped && needle && filtered.length > 0 && (
            <button type="button" className="audit-link" onClick={searchAll} aria-disabled={searchState === "loading" || undefined}>
              {searchState === "loading" ? "Searching all entries…" : "Search all entries"}
            </button>
          )}
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
      </div>

      <span className="sr-only" role="status">
        {said}
      </span>

      <div className="panel audit-panel" ref={panelRef} tabIndex={-1} aria-label="Audit entries">
        {load === "loading" ? (
          <SkeletonRows />
        ) : load === "error" ? (
          <div className="empty" role="alert">
            {forbidden ? "Your role cannot read the audit trail." : "Couldn't load the audit trail."}
            {!forbidden && (
              <button type="button" className="btn sm" onClick={retry}>
                Retry
              </button>
            )}
          </div>
        ) : rows.length === 0 ? (
          <div className="empty">
            Nothing has happened yet. Run an alarm from <Link to="/mission">Mission control</Link>; every agent step lands
            here.
          </div>
        ) : filtered.length === 0 ? (
          needle && baseCapped && !searchedAll ? (
            // The newest 500 are not the whole trail: say which part was searched, and offer the rest.
            <div className="empty audit-nomatch" role={searchState === "error" ? "alert" : undefined}>
              {searchState === "error" ? "Couldn't search all entries." : `No match in the newest ${FETCH} entries.`}
              <span className="audit-nomatch-actions">
                <button
                  type="button"
                  className="btn sm"
                  onClick={searchAll}
                  aria-disabled={searchState === "loading" || undefined}
                >
                  {searchState === "loading" ? "Searching…" : searchState === "error" ? "Retry" : "Search all entries"}
                </button>
                <button type="button" className="btn sm" onClick={clearFilters}>
                  Clear filters
                </button>
              </span>
            </div>
          ) : (
            <div className="empty audit-nomatch">
              {searchedAll ? "No entry in the whole trail matches." : "No entries match."}
              <button type="button" className="btn sm" onClick={clearFilters}>
                Clear filters
              </button>
            </div>
          )
        ) : (
          visible.map((f, i) => {
            // A day line above the first block of each day (by the block's newest row).
            const day = dayWords(f.rows[0]?.ts);
            const prevDay = i > 0 ? dayWords(visible[i - 1].rows[0]?.ts) : "";
            return (
              <Fragment key={f.block.key}>
                {day !== prevDay && <h2 className="audit-day">{day}</h2>}
                <Block
                  block={f.block}
                  rows={f.rows}
                  incident={incidentOf(f.block, incidents)}
                  parent={parentOf(f.block, bySite)}
                  view={view}
                  siteNames={siteNames}
                />
              </Fragment>
            );
          })
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
    case "alarm":
      return "Alarm run, no ticket";
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
  siteNames,
}: {
  block: AuditBlock;
  rows: AuditEntry[];
  incident?: IncidentLite;
  parent?: IncidentLite;
  view: View;
  siteNames: Record<string, string>;
}) {
  const headId = useId();
  const [showRoutine, setShowRoutine] = useState(false);
  const display = useMemo(() => mergeIdentical(rows), [rows]);
  const routine = useMemo(() => display.filter((d) => isRoutine(d.entry)), [display]);
  const folding = view === "decisions" && routine.length > 0;
  const printing = usePrinting(); // a printed trail shows every step; nothing stays folded on paper
  const listed = folding && !showRoutine && !printing ? display.filter((d) => !isRoutine(d.entry)) : display;

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
        <h3 id={headId} className="audit-head-title" tabIndex={-1}>
          <BlockTitle block={block} incident={incident} parent={parent} site={block.kind === "record" || block.kind === "job" ? siteOf(rows) : null} siteNames={siteNames} />
        </h3>
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

function BlockTitle({
  block,
  incident,
  parent,
  site,
  siteNames,
}: {
  block: AuditBlock;
  incident?: IncidentLite;
  parent?: IncidentLite;
  site?: string | null;
  siteNames?: Record<string, string>;
}) {
  // A record's rows that name one site: the site's name and code beside the record's kind.
  const where = site ? (
    <>
      {siteNames?.[site] && <span className="audit-site audit-site-name">{siteNames[site]}</span>}
      <span className="audit-site audit-code">{site}</span>
    </>
  ) : null;
  const ticket = (inc: IncidentLite, label?: string) => (
    <>
      {inc.priority && <span className={`pill ${inc.priority}`} title={priorityTitle(inc.priority)}>{inc.priority}</span>}
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
          <span className="audit-site">not in the ticket list</span>
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
    case "alarm":
      return <span className="audit-inc">Alarm run, no ticket</span>;
    case "approval":
      return incident ? ticket(incident, "Approval") : <span className="audit-inc">Approval, not linked to a ticket</span>;
    case "job":
      return (
        <>
          <span className="audit-inc">{block.jobName || "Scheduled job"}</span>
          {block.jobName && <span className="audit-site">Scheduled job</span>}
          {where}
        </>
      );
    default:
      return (
        <>
          <span className="audit-inc">{entityLabel(block.entityType)}</span>
          {where}
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
          {who === "agent" ? (
            <Bot size={14} strokeWidth={1.75} aria-hidden="true" />
          ) : who === "job" ? (
            <Clock size={14} strokeWidth={1.75} aria-hidden="true" />
          ) : (
            <User size={14} strokeWidth={1.75} aria-hidden="true" />
          )}
          <span className="audit-actor-name">{actorLabel(r.actor)}</span>
          <span className="sr-only">{who === "agent" ? " (agent)" : who === "job" ? " (job)" : " (person)"}</span>
        </span>
        {/* Expanded, the row keeps only the step name: the sentence and the facts are printed
            once, in full, in the detail below. */}
        <span className="audit-summary">
          <span className="audit-step">{rowTitle(r)}</span>
          {d.count > 1 && <span className="audit-times">×{d.count}</span>}
          {!open && parsed.sentence && <span className="audit-sentence">{parsed.sentence}</span>}
          {!open &&
            parsed.facts.slice(0, 3).map((f, i) => (
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
  // When the stored reason IS the sentence printed above (no facts were split out), printing it
  // again here would say it twice; Copy still carries it, exactly as stored.
  const rawWhy = r.rationale && r.rationale.trim() !== sentence.trim() ? r.rationale : "";

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
          {rawWhy ? <span className="audit-record-why">{rawWhy}</span> : r.rationale ? <span className="sr-only">{r.rationale}</span> : null}
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

/** The one site a record's rows name ("AC_SERVICE at SFC-CST-HUB-VOI due …"), or null. */
const SITE_CODE = /\b([A-Z]{2,4}-[A-Z0-9]+(?:-[A-Z0-9]+){1,4})\b/g;
function siteOf(rows: AuditEntry[]): string | null {
  const seen = new Set<string>();
  for (const r of rows) for (const m of String(r.rationale || "").matchAll(SITE_CODE)) seen.add(m[1]);
  const sites = [...seen].filter((x) => !/^(RNIO|FE|INC|PRB|CA)-/.test(x));
  return sites.length === 1 ? sites[0] : null;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** An instant's EAT calendar day as a number of days since the epoch (Kenya is UTC+3, no DST). */
function eatDay(ms: number): number {
  return Math.floor((ms + 3 * 3_600_000) / 86_400_000);
}
/** "Today", "Yesterday" or "Sat 4 Oct": the day line above a day's first block. */
function dayWords(ts: unknown): string {
  const d = parseInstant(ts);
  if (!d) return "";
  const day = eatDay(d.getTime());
  const today = eatDay(Date.now());
  if (day === today) return "Today";
  if (day === today - 1) return "Yesterday";
  const t = new Date(d.getTime() + 3 * 3_600_000);
  return `${WEEKDAYS[t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
}
/** "5 Oct, 12:02 to 6 Oct, 00:08", or one day's "12:02 to 18:40 today". */
function spanWords(oldest: unknown, newest: unknown): string {
  const a = parseInstant(oldest);
  const b = parseInstant(newest);
  if (!a || !b) return "—";
  const hm = (d: Date) => {
    const t = new Date(d.getTime() + 3 * 3_600_000);
    return `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}`;
  };
  const dm = (d: Date) => {
    const t = new Date(d.getTime() + 3 * 3_600_000);
    return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
  };
  if (eatDay(a.getTime()) === eatDay(b.getTime())) return `${dm(a)}, ${hm(a)} to ${hm(b)}`;
  return `${dm(a)} to ${dm(b)}`;
}
