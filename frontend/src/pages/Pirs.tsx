import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import LaneOff from "../components/LaneOff";
import PirEditor, { PIR_REASON_WORDS, pirStatus } from "../components/PirEditor";
import { humanEnum, humanStatus } from "../lib/agents";
import { detailOf, isLaneOff } from "../lib/apiError";
import { IconDot } from "../lib/icons";
import { fmtDateTime } from "../lib/time";
import "./Pirs.css";

/**
 * Post-incident reviews (spec §7.7, §7.10) — the screen for a lane that was complete,
 * tested and invisible: draft → review → publish, with the action items that come out of it.
 *
 * Three things this page is built to show rather than hide.
 *
 * 1. **The flag is off by default and the API 404s the whole lane.** That is not an error to
 *    shout about — the page says the lane is off and how to turn it on, exactly the way
 *    `pages/Maintenance.tsx` does for its own flag. "Off" and "no reviews" must never look
 *    the same (§7.10, the 3 a.m. rules).
 * 2. **A review is worth nothing until somebody signs it.** The list leads with the status
 *    and the reviewer, and the awaiting-review count is in the list heading, because the failure
 *    mode of this lane is not a bad review, it is a DRAFT nobody ever returns to.
 * 3. **Blameless is a rule you learn before you break it**, so `BlamelessHint` lives in the
 *    editor above the two validated fields rather than appearing as a 422 toast.
 *
 * WHAT THE BACKEND DOES NOT OFFER: the `/pir` routes carry `incident_id` and never
 * `incident_number`, so a UUID is all the API gives a reader. The page joins against
 * `GET /api/v1/incidents` (which returns closed incidents too) to put `INC-…`, the site and
 * the priority on each row. The join is best-effort: if that call fails the list still
 * renders, with the id.
 */

type PirRow = {
  id: string;
  incident_id: string;
  status: string;
  opened_reason: string;
  summary: string | null;
  impact: { users_affected?: number };
  mtta_minutes: number | null;
  mttr_minutes: number | null;
  adjusted_mttr_minutes: number | null;
  ai_assisted: number;
  reviewer: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
};

type IncidentLite = {
  id: string;
  incident_number: string;
  site_id: string;
  site_name?: string;
  priority: string;
  status: string;
};

const STATUSES = ["ALL", "DRAFT", "IN_REVIEW", "PUBLISHED", "NOT_REQUIRED"];

/** The filter's button words; the value sent stays the status. */
function statusWord(s: string): string {
  if (s === "ALL") return "All";
  const h = humanEnum(s);
  return h ? h[0].toUpperCase() + h.slice(1) : s;
}

export default function Pirs({ tick }: { tick: number }) {
  const [rows, setRows] = useState<PirRow[]>([]);
  const [incidents, setIncidents] = useState<IncidentLite[]>([]);
  const [status, setStatus] = useState("ALL");
  const [awaiting, setAwaiting] = useState<number | null>(null);
  const [off, setOff] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [openFor, setOpenFor] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    api
      .pirs(status === "ALL" ? "" : status)
      .then((list: PirRow[]) => {
        setRows(list);
        setOff(false);
        setLoaded(true);
        setFailed(false);
      })
      .catch((e) => {
        // 404 is the whole lane being off, which is the shipped default — not a failure.
        if (isLaneOff(e)) setOff(true);
        else {
          setNote(detailOf(e, "The review list could not be loaded."));
          setFailed(true);
        }
      });
    api
      .pirAwaitingReview()
      .then((r: { awaiting_review: number }) => setAwaiting(r?.awaiting_review ?? null))
      .catch(() => setAwaiting(null));
  }, [status]);

  useEffect(() => {
    load();
  }, [load, tick]);

  useEffect(() => {
    // Best-effort join for incident_number / site / priority. A failure costs the labels
    // only; the list below renders either way.
    api
      .incidents()
      .then((all: IncidentLite[]) => setIncidents(all))
      .catch(() => setIncidents([]));
  }, [tick]);

  const byId = useMemo(() => {
    const map: Record<string, IncidentLite> = {};
    for (const i of incidents) map[i.id] = i;
    return map;
  }, [incidents]);

  const label = (incidentId: string) => byId[incidentId]?.incident_number || incidentId;

  /** Resolved incidents with no review yet — the candidates for a manual open (§7.7.2). */
  const openable = useMemo(() => {
    const reviewed = new Set(rows.map((r) => r.incident_id));
    return incidents
      .filter((i) => ["RESTORED", "CLOSED", "CANCELLED"].includes(i.status) && !reviewed.has(i.id))
      .slice(0, 100);
  }, [incidents, rows]);

  const openManual = async () => {
    if (!openFor) return;
    setBusy(true);
    setNote(null);
    try {
      const res = await api.openPirForIncident(openFor);
      setNote(
        res?.created
          ? "Review opened, " + humanStatus(res.status) + "."
          : "That ticket already had a review; it is selected below."
      );
      setSelected(res.id);
      setOpenFor("");
      load();
    } catch (e) {
      setNote(detailOf(e, "The review could not be opened."));
    } finally {
      setBusy(false);
    }
  };

  const head = (
    <div className="page-head">
      <div>
        <h1>Post-incident reviews</h1>
        <p
          className="lead"
          title="Root causes and contributing factors are validated against the people on the ticket, and an action item is owned by a role token. A postmortem nobody signed is a document nobody owns."
        >
          Blameless by rule: a named person signs each review. Times in EAT.
        </p>
      </div>
      {!off && (
        <div className="page-actions">
          <select
            aria-label="Open a review by hand for a resolved ticket"
            value={openFor}
            disabled={busy}
            onChange={(e) => setOpenFor(e.target.value)}
          >
            <option value="">Open a review by hand…</option>
            {openable.map((i) => (
              <option key={i.id} value={i.id}>
                {i.incident_number}, {i.site_id}, {i.priority}, {humanStatus(i.status)}
              </option>
            ))}
          </select>
          <button className="btn" disabled={busy || !openFor} onClick={openManual}>
            Open review
          </button>
        </div>
      )}
    </div>
  );

  if (off) {
    return (
      <div className="content-narrow">
        {head}
        <LaneOff title="Post-incident reviews are off in this demo" flag="PIR_ENABLED">
          open a blameless review after every P1 and P2 and track its action items
        </LaneOff>
      </div>
    );
  }

  return (
    <div className="content-narrow">
      {head}

      {note && (
        <div className="panel" style={{ marginBottom: "0.75rem" }} role="status">
          <div className="muted">{note}</div>
        </div>
      )}

      <div className="form-row" role="group" aria-label="Filter reviews by status">
        {STATUSES.map((s) => (
          <button
            key={s}
            className={"btn sm" + (status === s ? " primary" : "")}
            aria-pressed={status === s}
            onClick={() => setStatus(s)}
          >
            {statusWord(s)}
          </button>
        ))}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Reviews</h2>
          <div className="facts">
            <span>{rows.length} shown</span>
            {awaiting != null &&
              (awaiting > 0 ? (
                <span className="attn warn">
                  <IconDot /> {awaiting} awaiting review
                </span>
              ) : (
                <span>none awaiting review</span>
              ))}
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Status</th>
              <th>Why it opened</th>
              <th>Subscribers</th>
              <th>MTTR / adjusted</th>
              <th>Reviewer</th>
              <th>Updated (EAT)</th>
            </tr>
          </thead>
          <tbody aria-busy={!loaded || undefined}>
            {!loaded &&
              !failed &&
              Array.from({ length: 5 }, (_, i) => (
                <tr key={"sk-" + i}>
                  <td colSpan={7}>
                    <div className="skeleton" />
                  </td>
                </tr>
              ))}
            {rows.map((r) => (
              <tr
                key={r.id}
                onClick={() => setSelected(r.id)}
                className={"pir-row" + (selected === r.id ? " pir-row-selected" : "")}
              >
                <td>
                  {/* The keyboard's way in; the row click stays for the mouse. */}
                  <button
                    type="button"
                    className="pir-open"
                    aria-expanded={selected === r.id}
                    aria-controls={selected === r.id ? "pir-editor" : undefined}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelected(r.id);
                    }}
                  >
                    {label(r.incident_id)}
                  </button>
                  <div className="muted">
                    {byId[r.incident_id]?.site_name || byId[r.incident_id]?.site_id || "—"}
                  </div>
                </td>
                <td>
                  {(() => {
                    const st = pirStatus(r.status);
                    return st.chip ? <span className={st.chip}>{st.word}</span> : st.word;
                  })()}
                  {r.ai_assisted ? <div className="muted">AI-assisted</div> : null}
                </td>
                <td className="muted">{PIR_REASON_WORDS[r.opened_reason] || humanEnum(r.opened_reason)}</td>
                <td>{(r.impact?.users_affected ?? 0).toLocaleString()}</td>
                <td className="muted">
                  {r.mttr_minutes != null ? Math.round(r.mttr_minutes) + " min" : "—"}
                  {" / "}
                  {r.adjusted_mttr_minutes != null ? Math.round(r.adjusted_mttr_minutes) + " min" : "—"}
                </td>
                <td className="muted">{r.reviewer || "unsigned"}</td>
                <td className="muted">{fmtDateTime(r.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loaded && failed && (
          <div className="empty" role="alert">
            Couldn't load the reviews.{" "}
            <button className="btn sm" onClick={load}>
              Retry
            </button>
          </div>
        )}
        {loaded && rows.length === 0 && status !== "ALL" && (
          <div className="empty">
            No reviews with this status.{" "}
            <button className="btn sm" onClick={() => setStatus("ALL")}>
              Show all
            </button>
          </div>
        )}
        {loaded && rows.length === 0 && status === "ALL" && (
          <div className="empty">
            No reviews yet. One opens within 5 minutes of a qualifying ticket being restored or closed; open any
            other by hand above.
          </div>
        )}
      </div>

      {selected && (
        <div id="pir-editor" className="pir-editor-slot">
          <PirEditor
            key={selected}
            pirId={selected}
            incidentLabel={label(rows.find((r) => r.id === selected)?.incident_id || "")}
            onChanged={load}
          />
        </div>
      )}
    </div>
  );
}
