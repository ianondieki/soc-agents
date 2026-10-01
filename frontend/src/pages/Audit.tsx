import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmtDateTime, fmtTime } from "../lib/time";
import {
  actionLabel,
  actionTone,
  actorKind,
  actorLabel,
  entityLabel,
  groupConsecutive,
  type AuditEntry,
  type AuditGroup,
} from "../lib/audit";

/**
 * The regulator-facing record, newest first, read the way a person reads it: who, what,
 * why, grouped by the ticket or card the entries are about. The raw actor and action
 * strings stay on hover (title) so an auditor can quote them exactly.
 */
const FETCH = 500;
const PAGE = 8; // groups shown before "Show more"
type Kind = "all" | "agent" | "person";

type IncidentLite = { id: string; incident_number: string; site_id: string; priority: string };

export default function Audit({ tick }: { tick: number }) {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [incidents, setIncidents] = useState<Record<string, IncidentLite>>({});
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Kind>("all");
  const [shown, setShown] = useState(PAGE);

  useEffect(() => {
    api.audit(FETCH).then(setRows).catch(console.error);
    api
      .incidents()
      .then((list: IncidentLite[]) => {
        const byId: Record<string, IncidentLite> = {};
        for (const i of list || []) byId[i.id] = i;
        setIncidents(byId);
      })
      .catch(() => undefined);
  }, [tick]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((r) => {
      if (kind === "agent" && actorKind(r.actor) !== "agent") return false;
      if (kind === "person" && actorKind(r.actor) === "agent") return false;
      if (!needle) return true;
      const inc = incidents[r.entity_id];
      return [r.actor, actorLabel(r.actor), r.action, actionLabel(r.action), r.rationale, r.entity_type, r.entity_id, inc?.incident_number, inc?.site_id]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [rows, q, kind, incidents]);

  const groups = useMemo(() => groupConsecutive(filtered), [filtered]);
  const visible = groups.slice(0, shown);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1 className="page-title">Audit Explorer</h1>
          <p className="lead">
            Every agent step and human decision with its reason, newest first. This is the regulator-facing record.
          </p>
        </div>
        <div className="page-actions" role="group" aria-label="Filter the audit trail">
          <input
            type="search"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setShown(PAGE);
            }}
            placeholder="Search actor, action or reason…"
            aria-label="Search the audit trail"
            className="audit-search"
          />
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as Kind);
              setShown(PAGE);
            }}
            aria-label="Who acted"
          >
            <option value="all">Everyone</option>
            <option value="agent">Agents only</option>
            <option value="person">People and jobs</option>
          </select>
        </div>
      </div>

      <p className="muted audit-count">
        {filtered.length === rows.length
          ? `${rows.length} ${rows.length === 1 ? "entry" : "entries"}${rows.length >= FETCH ? ` (latest ${FETCH})` : ""}`
          : `${filtered.length} of ${rows.length} entries`}
      </p>

      {groups.length === 0 && (
        <div className="panel">
          <div className="empty">{rows.length === 0 ? "Nothing has happened yet. Launch the storm on Mission Control." : "No entries match."}</div>
        </div>
      )}

      {visible.map((g) => (
        <AuditBlock key={g.key} group={g} incident={incidents[g.entity_id]} />
      ))}

      {shown < groups.length && (
        <div className="audit-more">
          <button type="button" className="btn" onClick={() => setShown((s) => s + PAGE)}>
            Show more ({groups.length - shown} more {groups.length - shown === 1 ? "block" : "blocks"})
          </button>
        </div>
      )}
    </div>
  );
}

function AuditBlock({ group, incident }: { group: AuditGroup; incident?: IncidentLite }) {
  const newest = group.rows[0];
  // Steps before an INC number exists (intake, correlation, enrichment, severity) and the
  // alarms folded into an open ticket carry no entity id: they are the intake work.
  const intake = !group.entity_id;
  const what = intake ? "Intake" : entityLabel(group.entity_type);
  const ref = intake
    ? "before a ticket, or folded into one"
    : incident
      ? `${incident.incident_number} · ${incident.site_id}`
      : group.entity_id.slice(0, 8);
  return (
    <article className="panel audit-block">
      <div className="panel-head">
        <h2 className="panel-kicker">
          {what} <span className={"audit-ref" + (intake ? " muted" : "")}>{ref}</span>
        </h2>
        <span className="muted audit-when">
          {fmtDateTime(newest.ts)} · {group.rows.length} {group.rows.length === 1 ? "entry" : "entries"}
        </span>
      </div>
      <ol className="audit-list">
        {group.rows.map((r) => (
          <li key={r.id} className="audit-row">
            <time className="audit-time" dateTime={r.ts} title={fmtDateTime(r.ts)}>
              {fmtTime(r.ts)}
            </time>
            <div className="audit-body">
              <div className="audit-line">
                <strong className="audit-actor" title={r.actor}>
                  {actorLabel(r.actor)}
                </strong>
                {actorKind(r.actor) !== "agent" && <span className="audit-kind">{actorKind(r.actor) === "job" ? "job" : "person"}</span>}
                <span className={`chip ${actionTone(r.action)}`} title={r.action}>
                  {actionLabel(r.action)}
                </span>
              </div>
              <p className="audit-why">{r.rationale || "—"}</p>
            </div>
          </li>
        ))}
      </ol>
    </article>
  );
}
