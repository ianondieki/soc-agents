import { useEffect, useRef } from "react";
import {
  bandView,
  columnWords,
  creditView,
  evidenceFacts,
  exclusionWords,
  kpiView,
  lineDisputeView,
  multiplierText,
  priorityLabel,
  sccBreakdown,
  tabulate,
  valuePair,
  watermarkImage,
  yamlPaths,
  type ExcludedIncident,
  type ScorecardLine,
} from "./scorecardModel";

/**
 * `LineDrawer{formula, yaml_path, excluded, sccMinutes}` (spec §7.10): everything needed to
 * check one line by hand.
 *
 * - **formula**: the server's human-readable formula, with this line's own operands, shown
 *   verbatim. It is the number's argument; paraphrasing it would weaken it.
 * - **yaml_path**: every term the line was judged against (`;`-separated server-side), one
 *   per row, so a reader can open `config/sla_terms.yaml` at each one.
 * - **excluded**: which incidents were left out and why, each reason in words beside its
 *   stable code. §7.6.2 requires inferred restores to be LISTED as excluded, not dropped silently.
 * - **sccMinutes**: the stop-clock minutes deducted, and which incidents or sites they came from.
 *
 * Raw and normalised are side by side here too. A SHADOW card's watermark follows the line into
 * the drawer, because the drawer is a separate layer and would otherwise lose it.
 *
 * Esc or the Close button dismisses it; focus returns to where it was.
 */
export default function LineDrawer({
  formula,
  yaml_path,
  excluded,
  sccMinutes,
  line,
  cardTitle,
  watermark,
  onClose,
}: {
  formula: string;
  yaml_path: string;
  excluded: ExcludedIncident[];
  sccMinutes: number;
  /** The rest of the row: values, band, credit, dispute columns and the evidence rows. */
  line: ScorecardLine;
  /** e.g. "EGYPRO · 2026-08". */
  cardTitle: string;
  /** The card's watermark text, or null. */
  watermark: string | null;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  // The parent passes a fresh closure each render. Reading it through a ref keeps the effect
  // below to mount/unmount, so focus is taken once and handed back once.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      try {
        before?.focus?.();
      } catch {
        /* the opener may have unmounted */
      }
    };
  }, []);

  const k = kpiView(line.kpi);
  const pair = valuePair(line);
  const band = bandView(line.band);
  const credit = creditView(line);
  const mult = multiplierText(line.region_multiplier_applied);
  const dispute = lineDisputeView(line);
  const paths = yamlPaths(yaml_path);
  const scc = sccBreakdown(line);
  const evidence = line.evidence || {};
  const measured = tabulate((evidence as Record<string, unknown>).measured);
  const sites = tabulate((evidence as Record<string, unknown>).sites);
  const facts = evidenceFacts(evidence);

  return (
    <>
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(3, 7, 18, 0.55)", zIndex: 40 }}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`${k.label} ${priorityLabel(line.priority)}: formula and evidence`}
        tabIndex={-1}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "min(640px, 100vw)",
          overflowY: "auto",
          zIndex: 41,
          background: "var(--panel-solid)",
          borderLeft: "1px solid var(--border-bright)",
          boxShadow: "-12px 0 40px rgba(0, 0, 0, 0.45)",
          padding: "1rem 1.1rem 2rem",
          outline: "none",
        }}
      >
        <div style={{ position: "relative" }}>
          {/* Inside the content wrapper, not the scroller, so it spans the full scrolled
              height. Above the text, faint, and never takes a click. */}
          {watermark && (
            <div
              aria-hidden="true"
              style={{
                position: "absolute",
                inset: 0,
                pointerEvents: "none",
                backgroundImage: watermarkImage(watermark),
                backgroundRepeat: "repeat",
                zIndex: 1,
              }}
            />
          )}
          <div className="panel-head" style={{ flexWrap: "wrap" }}>
            <div>
              <div className="muted">{cardTitle}</div>
              <h3 style={{ margin: "0.15rem 0 0" }}>
                {k.label} · {priorityLabel(line.priority)}
              </h3>
              <div className="muted" style={{ fontFamily: "var(--mono)" }}>
                {k.code}
              </div>
            </div>
            <div className="chips">
              {watermark && <span className="chip hitl">{watermark} · INTERNAL ONLY</span>}
              <span className={band.chip} title={band.title}>
                {band.label}
              </span>
              <button className="btn" onClick={onClose}>
                Close
              </button>
            </div>
          </div>

          {/* ---- raw beside normalised --------------------------------------- */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 0.7fr", gap: "0.6rem", margin: "0.3rem 0 0.9rem" }}>
            <Tile label="Raw" value={pair.raw} />
            <Tile label={"Normalised · " + pair.normalisedLabel} value={pair.normalised} note={pair.normalisedWhy} />
            <Tile label="Region multiplier" value={mult.text} note={mult.text === "—" ? mult.title : null} />
          </div>

          <h4 style={H4}>Formula</h4>
          <div className="pre" style={{ fontFamily: "var(--mono)", fontSize: "0.8rem" }}>
            {formula || "—"}
          </div>

          <h4 style={H4}>
            Terms cited <span className="muted">· yaml_path, resolved against the card's sla_terms version</span>
          </h4>
          {paths.length ? (
            <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
              {paths.map((p) => (
                <li key={p} style={{ fontFamily: "var(--mono)", fontSize: "0.82rem" }}>
                  {p}
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted">No term path recorded.</div>
          )}

          <h4 style={H4}>
            Stop-clock minutes deducted <span className="muted">· un-reversed SCC intervals inside the outage</span>
          </h4>
          <div>
            <strong style={{ fontFamily: "var(--mono)", fontSize: "1.1rem" }}>{sccMinutes} min</strong>
            {scc.length > 0 && (
              <span className="muted">
                {" "}
                from {scc.map((s) => `${s.who} (${s.minutes} min)`).join(", ")}
              </span>
            )}
            {sccMinutes === 0 && <span className="muted"> · none on this line</span>}
          </div>

          <h4 style={H4}>
            Excluded incidents <span className="muted">· {excluded.length} listed, {line.excluded_incidents} counted</span>
          </h4>
          {excluded.length ? (
            <table>
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Why excluded</th>
                  <th>Code</th>
                </tr>
              </thead>
              <tbody>
                {excluded.map((x, i) => (
                  <tr key={x.incident + "-" + i}>
                    <td style={{ fontFamily: "var(--mono)" }}>{x.incident}</td>
                    <td>{exclusionWords(x.reason)}</td>
                    <td className="muted" style={{ fontFamily: "var(--mono)", fontSize: "0.78rem" }}>
                      {x.reason}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="muted">No incident was excluded from this line.</div>
          )}

          <h4 style={H4}>
            Measured <span className="muted">· {line.eligible_incidents} eligible</span>
          </h4>
          <EvidenceTable table={measured} empty="No measured rows on this line." />
          {sites.rows.length > 0 && (
            <>
              <h4 style={H4}>Sites</h4>
              <EvidenceTable table={sites} empty="" />
            </>
          )}
          {facts.length > 0 && (
            <div style={{ marginTop: "0.6rem" }}>
              {facts.map(([key, value]) => (
                <div key={key} className="muted">
                  {columnWords(key)}: <span style={{ color: "var(--text)" }}>{value}</span>
                </div>
              ))}
            </div>
          )}

          <h4 style={H4}>Credit</h4>
          {credit ? (
            <span className={credit.chip} title={credit.title}>
              {credit.label}
            </span>
          ) : (
            <div className="muted">No credit proposed on this line.</div>
          )}
          {credit && <div className="muted" style={{ marginTop: "0.3rem" }}>{credit.title}</div>}

          {dispute && (
            <>
              <h4 style={H4}>Dispute</h4>
              <span className="chip accent">{dispute.label}</span>
              {dispute.detail ? <span className="muted"> {dispute.detail}</span> : null}
              {line.adjudicated_by ? <div className="muted">Adjudicated by {line.adjudicated_by}</div> : null}
              {line.adjudication_reason ? <div className="pre">{line.adjudication_reason}</div> : null}
            </>
          )}
        </div>
      </div>
    </>
  );
}

const H4 = { margin: "1.05rem 0 0.45rem", fontSize: "0.92rem", color: "var(--text-bright)" } as const;

function Tile({ label, value, note }: { label: string; value: string; note?: string | null }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "0.55rem 0.7rem", background: "rgba(8, 16, 30, 0.6)" }}>
      <div style={{ fontSize: "0.72rem", letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--muted)" }}>{label}</div>
      <div style={{ fontFamily: "var(--mono)", fontSize: "1.2rem", fontWeight: 700, color: "var(--text-bright)" }}>{value}</div>
      {note ? <div className="muted" style={{ fontSize: "0.76rem", marginTop: "0.2rem" }}>{note}</div> : null}
    </div>
  );
}

function EvidenceTable({ table, empty }: { table: { columns: string[]; rows: string[][] }; empty: string }) {
  if (!table.rows.length) return empty ? <div className="muted">{empty}</div> : null;
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            {table.columns.map((c) => (
              <th key={c}>{columnWords(c)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} style={{ fontFamily: "var(--mono)", fontSize: "0.8rem" }}>
                  {v}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
