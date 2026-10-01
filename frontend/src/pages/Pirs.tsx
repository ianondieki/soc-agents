import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import PirEditor from "../components/PirEditor";
import { detailOf, isLaneOff } from "../lib/apiError";
import { fmtDateTime } from "../lib/time";

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
 *    and the reviewer, and the awaiting-review count is in the header, because the failure
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

/** §7.7.3 trigger matrix, spelled out — "P1_P2" on its own tells a new analyst nothing. */
const REASON_WORDS: Record<string, string> = {
  P1_P2: "P1/P2 incident",
  HUB_CORE: "HUB or CORE site",
  SLA_BREACH: "SLA breached",
  PROBLEM_LINKED: "linked to a problem record",
  RUN_FAILED: "an agent run failed",
  MANUAL: "opened by hand",
};

function statusChip(status: string): string {
  if (status === "PUBLISHED") return "chip ok";
  if (status === "IN_REVIEW") return "chip accent";
  return "chip";
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

  const load = useCallback(() => {
    api
      .pirs(status === "ALL" ? "" : status)
      .then((list: PirRow[]) => {
        setRows(list);
        setOff(false);
      })
      .catch((e) => {
        // 404 is the whole lane being off, which is the shipped default — not a failure.
        if (isLaneOff(e)) setOff(true);
        else setNote(detailOf(e, "The review list could not be loaded."));
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
          ? "Review opened (" + res.status + ")."
          : "That incident already had a review; it is selected below."
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

  if (off) {
    return (
      <div>
        <h1 className="page-title">Post-incident reviews</h1>
        <div className="panel">
          <div className="empty">
            Post-incident reviews are not enabled on this deployment. Set{" "}
            <code>PIR_ENABLED=true</code> in <code>.env</code> and restart the API. With the flag
            off the whole lane is invisible by design — no review is opened, and the{" "}
            <code>pir_autoopen</code> job is a no-op.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", gap: "0.6rem", alignItems: "baseline", flexWrap: "wrap" }}>
        <h1 className="page-title">Post-incident reviews</h1>
        {awaiting != null && (
          <span className={awaiting > 0 ? "chip warn" : "chip ok"}>
            {awaiting} AWAITING REVIEW
          </span>
        )}
      </div>
      <p className="muted">
        Blameless by rule, not by convention: root causes and contributing factors are validated
        against the people on the incident, and an action item is owned by a role token. A named
        human publishes — a postmortem nobody signed is a document nobody owns. Times are EAT.
      </p>

      {note && (
        <div className="panel" style={{ marginBottom: "0.75rem" }}>
          <div className="muted">{note}</div>
        </div>
      )}

      <div className="panel" style={{ marginBottom: "1rem" }}>
        <div className="form-row" style={{ marginBottom: 0 }}>
          {STATUSES.map((s) => (
            <button
              key={s}
              className={"btn" + (status === s ? " primary" : "")}
              onClick={() => setStatus(s)}
            >
              {s}
            </button>
          ))}
          <span style={{ flex: 1 }} />
          <label className="muted">
            Open a review by hand{" "}
            <select value={openFor} disabled={busy} onChange={(e) => setOpenFor(e.target.value)}>
              <option value="">select a resolved incident…</option>
              {openable.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.incident_number} · {i.site_id} · {i.priority} · {i.status}
                </option>
              ))}
            </select>
          </label>
          <button className="btn" disabled={busy || !openFor} onClick={openManual}>
            Open review
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h3>Reviews</h3>
          <span className="chip">{rows.length}</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Incident</th>
              <th>Status</th>
              <th>Why it opened</th>
              <th>Users</th>
              <th>MTTR / adjusted</th>
              <th>Reviewer</th>
              <th>Updated (EAT)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.id}
                onClick={() => setSelected(r.id)}
                style={{ cursor: "pointer" }}
                className={selected === r.id ? "pir-row-selected" : undefined}
              >
                <td>
                  <strong>{label(r.incident_id)}</strong>
                  <div className="muted">
                    {byId[r.incident_id]?.site_name || byId[r.incident_id]?.site_id || "—"}
                  </div>
                </td>
                <td>
                  <span className={statusChip(r.status)}>{r.status}</span>
                  {r.ai_assisted ? (
                    <div>
                      <span className="chip accent">AI-ASSISTED</span>
                    </div>
                  ) : null}
                </td>
                <td className="muted">{REASON_WORDS[r.opened_reason] || r.opened_reason}</td>
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
        {rows.length === 0 && (
          <div className="empty">
            No reviews with this status. The <code>pir_autoopen</code> job opens one every 5
            minutes for incidents that became RESTORED or CLOSED and match the trigger matrix;
            anything else is opened by hand above.
          </div>
        )}
      </div>

      {selected && (
        <div style={{ marginTop: "1rem" }}>
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
