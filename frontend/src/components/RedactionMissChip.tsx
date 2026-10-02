import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime, parseInstant } from "../lib/time";
import { useRealtimeState, useTickerEvents } from "../realtime/RealtimeContext";

/**
 * The §9.6 breach signal — a red chip on the Wallboard when a message that already left the
 * building is found to have carried an e-mail address or an MSISDN.
 *
 * WHERE IT COMES FROM. `services/housekeeping.post_send_redaction_scan` scans the payloads of
 * outbox rows SENT in the last 24 h. A hit writes `AuditRow(action="redaction.miss")` and
 * publishes `security.redaction_miss` after the commit, with this payload (`RedactionHit.
 * as_payload`): `{outbox_id, kind, incident_number, sent_at, email_matches, phone_matches,
 * paths, note}`.
 *
 * WHAT IT NEVER SHOWS, AND WHY THERE IS NO "DETAILS" BUTTON. The payload carries pattern
 * *counts* and JSON *paths*, and deliberately never the matched value: "a breach record that
 * quotes the breach is a second copy of it" (§9.5). This chip renders exactly those counts
 * and paths. It has no expand, no link to the outbox row's body and no affordance that would
 * need the value — a wallboard is the most-photographed screen in the building.
 *
 * DURABILITY. The WS frame is the immediate signal; the ticker keeps only 100 lines, so each
 * hit is copied into this component's own state the moment it arrives. On (re)load the chip
 * also backfills, best-effort, from `GET /api/v1/audit` rows with `action="redaction.miss"` —
 * that route returns the newest 100 audit rows and is gated to audit readers once auth is on,
 * so a failure there is swallowed and the WS path still works. Hits older than the scan's own
 * 24 h lookback drop off. There is no acknowledge route in the backend, so the chip cannot be
 * cleared from here — running the breach drill in docs/RUNBOOK.md is the response.
 *
 * 3 a.m. rules (§7.10): red **and** the words "REDACTION MISS"; an ✕ glyph beside them; no
 * animation (the `.chip.bad` pulse is deliberately not used); the time is absolute EAT.
 */

type Hit = {
  key: string;
  /** When the miss was *detected* (the frame's / audit row's time) — what the 24 h window runs on. */
  at: string | null;
  /** When the message itself left (WS payload only). */
  sentAt: string | null;
  kind: string | null;
  incident_number: string | null;
  email_matches: number | null;
  phone_matches: number | null;
  paths: string[];
  /** Audit-backfilled rows carry the server's rationale sentence (counts, kind — never the value). */
  rationale: string | null;
};

const LOOKBACK_MS = 24 * 3600 * 1000;
const AUDIT_POLL_MS = 60000;

function fresh(hit: Hit, now: number): boolean {
  const at = parseInstant(hit.at);
  return !at || now - at.getTime() <= LOOKBACK_MS;
}

export default function RedactionMissChip() {
  const rt = useRealtimeState();
  // The ticker lines live in the realtime feed store (security.redaction_miss is critical, so
  // quiet mode never holds one back); reading them here re-renders only this chip.
  const events = useTickerEvents();
  const auditRev = rt?.revisions?.audit ?? 0;
  const [hits, setHits] = useState<Record<string, Hit>>({});

  // WS: copy every security.redaction_miss frame into local state, keyed by outbox row, so
  // ticker eviction during a storm cannot make a breach disappear from the wall.
  useEffect(() => {
    if (!events || events.length === 0) return;
    const incoming: Record<string, Hit> = {};
    for (const ev of events) {
      if (ev.type !== "security.redaction_miss") continue;
      const p = ev.payload || {};
      const key = String(p.outbox_id || "ws-" + (ev.seq ?? "") + "-" + (ev.ts ?? ""));
      incoming[key] = {
        key,
        at: ev.ts,
        sentAt: typeof p.sent_at === "string" ? p.sent_at : null,
        kind: typeof p.kind === "string" ? p.kind : null,
        incident_number: typeof p.incident_number === "string" ? p.incident_number : null,
        email_matches: typeof p.email_matches === "number" ? p.email_matches : null,
        phone_matches: typeof p.phone_matches === "number" ? p.phone_matches : null,
        paths: Array.isArray(p.paths) ? p.paths.filter((x: unknown) => typeof x === "string") : [],
        rationale: null,
      };
    }
    if (Object.keys(incoming).length === 0) return;
    setHits((prev) => {
      const next = { ...prev };
      for (const [k, h] of Object.entries(incoming)) {
        // A WS hit carries more than an audit row; never downgrade it.
        if (!next[k] || next[k].rationale != null) next[k] = h;
      }
      return next;
    });
  }, [events]);

  // Audit backfill: best-effort, swallowed on any failure (403 for non-audit roles, etc.).
  useEffect(() => {
    let live = true;
    const load = () =>
      api
        .audit()
        .then((rows: any[]) => {
          if (!live || !Array.isArray(rows)) return;
          const found: Record<string, Hit> = {};
          for (const r of rows) {
            if (r?.action !== "redaction.miss") continue;
            const key = String(r.entity_id || r.id);
            found[key] = {
              key,
              at: r.ts ?? null,
              sentAt: null,
              kind: null,
              incident_number: null,
              email_matches: null,
              phone_matches: null,
              paths: [],
              rationale: typeof r.rationale === "string" ? r.rationale : null,
            };
          }
          if (Object.keys(found).length === 0) return;
          setHits((prev) => {
            const next = { ...prev };
            for (const [k, h] of Object.entries(found)) if (!next[k]) next[k] = h;
            return next;
          });
        })
        .catch(() => undefined);
    // A tick later, so a mount React undoes at once (its development double mount) asks nothing.
    const first = window.setTimeout(load, 0);
    const id = window.setInterval(load, AUDIT_POLL_MS);
    return () => {
      live = false;
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [auditRev]);

  const now = Date.now();
  const list = Object.values(hits)
    .filter((h) => fresh(h, now))
    .sort((a, b) => (parseInstant(b.at)?.getTime() ?? 0) - (parseInstant(a.at)?.getTime() ?? 0));

  if (list.length === 0) return null;

  const latest = list[0];
  return (
    <div className="wb-alarms">
      <div className="wb-breach" role="alert">
        <div className="wb-breach-word">
          <span aria-hidden="true">✕ </span>REDACTION MISS
          <span className="wb-breach-count">
            {" "}
            · {list.length} sent message{list.length === 1 ? "" : "s"} carried contact details
          </span>
        </div>
        <div className="wb-breach-detail">
          Latest detected {fmtDateTime(latest.at)} EAT. Pattern counts and JSON paths only — the matched values
          are never recorded or shown (§9.5). Run the breach drill in docs/RUNBOOK.md.
        </div>
        <ul className="wb-breach-list">
          {list.slice(0, 4).map((h) => (
            <li key={h.key}>
              {h.rationale != null ? (
                <>
                  detected {fmtDateTime(h.at)} EAT · {h.rationale}
                </>
              ) : (
                <>
                  detected {fmtDateTime(h.at)} EAT · {h.kind || "message"}
                  {h.sentAt ? " sent " + fmtDateTime(h.sentAt) + " EAT" : ""}
                  {h.incident_number ? " · " + h.incident_number : ""} · {h.email_matches ?? 0} e-mail /{" "}
                  {h.phone_matches ?? 0} MSISDN pattern(s)
                  {h.paths.length ? " at " + h.paths.slice(0, 3).join(", ") + (h.paths.length > 3 ? " …" : "") : ""}
                </>
              )}
            </li>
          ))}
          {list.length > 4 && <li>… and {list.length - 4} more in the audit log</li>}
        </ul>
      </div>
    </div>
  );
}
