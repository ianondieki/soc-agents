import { useEffect, useState } from "react";
import { api } from "../api";
import { humanEnum } from "../lib/agents";
import { fmtDateTime } from "../lib/time";

/**
 * "Earlier at this site" — the incident workspace panel for agent memory M0 (spec §7.11).
 *
 * Nothing in this system reads across incidents: the last outage at this mast and what
 * actually fixed it are in the database and have never been shown next to the ticket
 * somebody is working. This panel is that, and nothing more — it reads
 * `GET /api/v1/memory/sites/{site_id}` and renders what comes back.
 *
 * Three rules it is built around:
 *
 * 1. **It can never take the workspace down.** Its own `try`/`catch` swallows every failure
 *    into a muted line; the page it sits on is the tool people use during an outage, and a
 *    blank panel is a disappointment where a thrown render is an outage of the tool.
 * 2. **Empty is explained, never implied.** `MEMORY_ENABLED` defaults to false (§7.11.3); with
 *    the feature off the panel is not drawn at all (an empty panel would read as "this site has
 *    a clean record", and the flag name is not manager copy). With it on, an empty panel says
 *    how far back it looked.
 * 3. **Advisory, and labelled as such.** Memory changes no priority, no assignment and no SLA
 *    (MEM1/G15). The footer says so, because a number rendered beside a live ticket at 03:00
 *    will otherwise be read as an instruction.
 *
 * 3 a.m. rules (§7.10): no status is carried by colour alone — every state is a word;
 * nothing blinks; every timestamp is absolute EAT via `fmtDateTime`.
 *
 * Self-contained on purpose: it owns its fetch and its state, so wiring it into the workspace
 * is one import and one element, and removing it is deleting the same two lines.
 */

type Episode = {
  incident_id: string;
  incident_number: string;
  site_id: string;
  fault_class: string;
  closed_at: string | null;
  restore_minutes: number | null;
  resolution_code: string;
  resolution_summary: string;
  match_reason: string;
  score: number;
};

type SiteMemory = {
  site_id: string;
  enabled: boolean;
  lookback_days: number;
  episodes: Episode[];
  facts: unknown[];
  degraded: boolean;
};

/** "3 h 34 min" reads faster at 03:00 than "214 min" once it is past an hour. */
function fmtDuration(minutes: number | null): string {
  if (minutes == null || minutes < 0) return "duration not recorded";
  if (minutes < 60) return `restored in ${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `restored in ${h} h${m ? ` ${m} min` : ""}`;
}

export default function EarlierAtThisSite({ siteId }: { siteId?: string | null }) {
  const [data, setData] = useState<SiteMemory | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    if (!siteId) {
      setData(null);
      return;
    }
    setFailed(false);
    api
      .siteMemory(siteId)
      .then((d: SiteMemory) => {
        if (live) setData(d);
      })
      .catch(() => {
        // Console only. A failed advisory read must not raise a toast, retry in a loop, or
        // touch any other state on the page.
        if (live) {
          setData(null);
          setFailed(true);
        }
      });
    return () => {
      live = false;
    };
  }, [siteId]);

  if (!siteId) return null;
  // Recall switched off is a deployment setting, not news for the person working the ticket:
  // the panel is simply absent, like the other lanes that are off in this demo.
  if (data && !data.enabled) return null;

  const episodes = data?.episodes ?? [];
  // Why the panel is empty, in words. "Off" and "clean record" must never look the same.
  const emptyReason = failed
    ? "History could not be loaded. The rest of this page is unaffected."
    : data == null
      ? "Loading…"
      : `No earlier resolved incidents recorded at this site in the last ${data.lookback_days} days.`;

  return (
    <div className="panel" style={{ marginTop: "1rem" }}>
      <div className="panel-head">
        <h2 className="panel-title">Earlier at this site</h2>
        <div className="facts">
          <span className="mono">{siteId}</span>
        </div>
      </div>

      {episodes.length === 0 ? (
        <p className="muted">{emptyReason}</p>
      ) : (
        <div className="list">
          {episodes.map((e) => (
            <div key={e.incident_id} className="row" style={{ cursor: "default" }}>
              <span className="mono">{e.incident_number}</span>
              <div>
                <div className="head-row">
                  <strong>{humanEnum(e.fault_class)}</strong>
                  <span className="muted">{fmtDuration(e.restore_minutes)}</span>
                  {e.resolution_code ? <span className="muted">{humanEnum(e.resolution_code)}</span> : null}
                </div>
                <div className="muted">{e.resolution_summary || "No resolution note was recorded."}</div>
              </div>
              {/* Absolute EAT, never "3 days ago": the reader is about to compare it with a
                  timestamp on another screen. */}
              <span className="muted">{fmtDateTime(e.closed_at)}</span>
            </div>
          ))}
        </div>
      )}

      <p className="muted" style={{ marginTop: "0.6rem" }}>
        Advisory only — prior incidents at this site. Nothing here sets the priority, the
        assignment or the SLA on this ticket.
      </p>
    </div>
  );
}
