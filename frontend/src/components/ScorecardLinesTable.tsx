import { humanEnum } from "../lib/agents";
import {
  bandView,
  creditView,
  groupByKpi,
  kpiView,
  lineDisputeView,
  multiplierText,
  NORMALISED_EXPLAINER,
  NORMALISED_LABEL_DEFAULT,
  priorityLabel,
  valuePair,
  type ScorecardLine,
} from "./scorecardModel";

/**
 * `ScorecardLinesTable{lines, canDispute}` (spec §7.10): the 22 lines of one card, in the
 * server's order (`seq`), grouped by KPI.
 *
 * **Raw and normalised sit in adjacent columns** (§7.6.2: "shown beside raw, never instead of
 * it"). The normalised column carries the server's own label ("contract-agreed regional
 * allowance"). Its header tooltip is `NORMALISED_EXPLAINER`: the figure is the line's own formula
 * recomputed per incident, not raw divided by a multiplier, so it can sit above raw. Where a
 * line has no normalised figure, the cell says so and the reason is in its tooltip and in the
 * drawer.
 *
 * **Dispute.** `canDispute` comes from `scorecardModel.disputeAffordance()`. It is false while
 * the backend has no dispute route (CONFORMANCE C-02), so every line's Dispute button is
 * disabled and `disputeReason` says why. The reason is also printed above the table, because
 * a tooltip on a disabled control is easy to miss and the 3 a.m. rules forbid hiding state.
 *
 * A row, or its "Formula" button, opens the `LineDrawer`: the formula, the terms it cites,
 * the excluded incidents and the stop-clock minutes.
 *
 * At most two chips on a row: a band that is not green, and a credit that is still proposed.
 * A green band, a settled credit and a dispute's state are words.
 */
export default function ScorecardLinesTable({
  lines,
  canDispute,
  disputeReason,
  selectedId,
  onSelect,
  onDispute,
}: {
  lines: ScorecardLine[];
  canDispute: boolean;
  /** Shown as the tooltip on a disabled Dispute button, and above the table. */
  disputeReason: string;
  selectedId: string | null;
  onSelect: (line: ScorecardLine) => void;
  /** Called only when `canDispute` is true. Nothing passes it today: there is no route. */
  onDispute?: (line: ScorecardLine) => void;
}) {
  const normalisedLabel = lines.find((l) => l.normalised_label)?.normalised_label || NORMALISED_LABEL_DEFAULT;
  const groups = groupByKpi(lines);
  const COLS = 10;
  const MONO = { fontFamily: "var(--mono)", fontSize: "var(--fs-sm)" } as const;

  if (lines.length === 0) {
    return <div className="empty">This card has no lines.</div>;
  }

  return (
    <>
      {!canDispute && (
        <div className="muted" style={{ margin: "0 0 0.5rem" }}>
          Disputes are not available yet. {disputeReason}
        </div>
      )}
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Priority</th>
              <th>Raw</th>
              <th title={NORMALISED_EXPLAINER}>Normalised ({normalisedLabel})</th>
              <th>Region ×</th>
              <th>Band</th>
              <th>Eligible / excluded</th>
              <th>SCC min deducted</th>
              <th>Credit</th>
              <th>Evidence</th>
              <th>Dispute</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => {
              const k = kpiView(g.kpi);
              return [
                <tr key={"kpi-" + g.kpi}>
                  <td colSpan={COLS} style={{ background: "rgba(62, 203, 255, 0.05)" }}>
                    <div className="head-row">
                      <strong>{k.label}</strong>
                      {k.hint ? <span className="muted">{k.hint}</span> : null}
                      <span className="muted" style={MONO}>
                        {k.code}
                      </span>
                    </div>
                  </td>
                </tr>,
                ...g.lines.map((line) => {
                  const pair = valuePair(line);
                  const band = bandView(line.band);
                  const credit = creditView(line);
                  const mult = multiplierText(line.region_multiplier_applied);
                  const dispute = lineDisputeView(line);
                  const selected = selectedId === line.id;
                  return (
                    <tr
                      key={line.id}
                      onClick={() => onSelect(line)}
                      style={{ cursor: "pointer", background: selected ? "rgba(62, 203, 255, 0.07)" : undefined }}
                    >
                      <td>{priorityLabel(line.priority)}</td>
                      <td style={{ ...MONO, color: "var(--text-bright)" }}>{pair.raw}</td>
                      <td style={MONO} title={pair.normalisedWhy || pair.normalisedLabel}>
                        {pair.normalised}
                        {pair.normalisedWhy ? <div className="muted">not normalised</div> : null}
                      </td>
                      <td className="muted" style={MONO} title={mult.title}>
                        {mult.text}
                      </td>
                      <td>
                        {band.chip === "chip ok" ? (
                          <span title={band.title}>{humanEnum(band.label)}</span>
                        ) : (
                          <span className={band.chip} title={band.title}>
                            {humanEnum(band.label)}
                          </span>
                        )}
                      </td>
                      <td style={MONO}>
                        {line.eligible_incidents} / {line.excluded_incidents}
                      </td>
                      <td style={MONO}>{line.scc_minutes_deducted}</td>
                      <td>
                        {credit ? (
                          credit.chip === "chip warn" ? (
                            <span className={credit.chip} title={credit.title}>
                              {humanEnum(credit.label)}
                            </span>
                          ) : (
                            <span title={credit.title}>{humanEnum(credit.label)}</span>
                          )
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>
                        <button
                          className="btn sm"
                          aria-pressed={selected}
                          onClick={(e) => {
                            e.stopPropagation();
                            onSelect(line);
                          }}
                        >
                          Formula
                        </button>
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {dispute ? (
                          <div>
                            <span>{humanEnum(dispute.label)}</span>
                            {dispute.detail ? <div className="muted">{dispute.detail}</div> : null}
                          </div>
                        ) : null}
                        {/* The wrapper carries the tooltip. A disabled button gets no hover in some
                            browsers, so its pointer events pass through to the wrapper. */}
                        <span title={canDispute ? "Dispute this line" : disputeReason} style={{ display: "inline-block" }}>
                          <button
                            className="btn sm"
                            style={{
                              pointerEvents: canDispute && onDispute ? undefined : "none",
                            }}
                            disabled={!canDispute || !onDispute}
                            aria-label={canDispute ? "Dispute this line" : "Dispute: " + disputeReason}
                            onClick={() => onDispute?.(line)}
                          >
                            Dispute
                          </button>
                        </span>
                      </td>
                    </tr>
                  );
                }),
              ];
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
