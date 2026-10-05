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
 * the backend has no dispute route (CONFORMANCE C-02). The control is shown once per line, in
 * the line's own panel (`LineDrawer`), switched off with `disputeReason` beside it, rather than
 * as 22 identical disabled buttons here; the reason is also printed above the table, because
 * the 3 a.m. rules forbid hiding state. A dispute's state gets a column once a line has one.
 *
 * A row, or the priority button that starts it, opens the `LineDrawer`: the formula, the
 * terms it cites, the excluded incidents, the stop-clock minutes and the dispute control.
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
  // A dispute's state gets a column only once some line has one (never before C-02 lands).
  const anyDispute = lines.some((l) => lineDisputeView(l));
  const COLS = anyDispute ? 8 : 7;

  if (lines.length === 0) {
    return <div className="empty">This card has no lines.</div>;
  }

  return (
    <>
      {!canDispute && (
        <p className="scl-note" title={disputeReason}>
          Disputes are not available yet. Each line's own panel shows the control, switched off, with the reason.
        </p>
      )}
      <div className="table-scroll scl-scroll">
        <table className="scl">
          <thead>
            <tr>
              <th>Priority</th>
              <th className="num">Raw</th>
              <th className="num" title={NORMALISED_EXPLAINER}>
                Normalised
              </th>
              <th>Band</th>
              <th className="num" title="Tickets counted, then tickets left out (each with its reason in the line's panel)">
                Counted / left out
              </th>
              <th className="num" title="Stop-clock minutes taken off">
                Stop-clock min
              </th>
              <th>Credit</th>
              {anyDispute && <th>Dispute</th>}
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => {
              const k = kpiView(g.kpi);
              return [
                <tr key={"kpi-" + g.kpi} className="scl-kpi">
                  <th colSpan={COLS} scope="colgroup">
                    <span className="scl-kpi-name">{k.label}</span>
                    {k.hint ? <span className="scl-kpi-hint">{k.hint}</span> : null}
                    <span className="scl-kpi-code">{k.code}</span>
                  </th>
                </tr>,
                ...g.lines.map((line) => {
                  const pair = valuePair(line);
                  const band = bandView(line.band);
                  const credit = creditView(line);
                  const mult = multiplierText(line.region_multiplier_applied);
                  const dispute = lineDisputeView(line);
                  const selected = selectedId === line.id;
                  const all = !line.priority || String(line.priority).toUpperCase() === "ALL";
                  return (
                    <tr
                      key={line.id}
                      onClick={() => onSelect(line)}
                      className={"scl-line" + (selected ? " selected" : "") + (all ? " all" : "")}
                      aria-selected={selected}
                    >
                      <td>
                        {/* The keyboard's way in; the row click stays for the mouse. */}
                        <button
                          type="button"
                          className="scl-open"
                          aria-pressed={selected}
                          aria-label={`${k.label}, ${priorityLabel(line.priority)}: formula and evidence`}
                          onClick={(e) => {
                            e.stopPropagation();
                            onSelect(line);
                          }}
                        >
                          {priorityLabel(line.priority)}
                        </button>
                      </td>
                      <td className="num scl-raw">{pair.raw}</td>
                      <td className="num scl-norm" title={pair.normalisedWhy || `${pair.normalisedLabel || normalisedLabel}. ${mult.title}`}>
                        {pair.normalisedWhy ? <span className="scl-dim">not normalised</span> : pair.normalised}
                        {mult.text && mult.text !== "—" ? <span className="scl-mult">region {mult.text}</span> : null}
                      </td>
                      <td>
                        {band.chip === "chip ok" ? (
                          <span className="scl-band ok" title={band.title}>
                            {sentence(humanEnum(band.label))}
                          </span>
                        ) : band.label === "No band" ? (
                          <span className="scl-dim" title={band.title}>
                            No band
                          </span>
                        ) : (
                          <span className={band.chip} title={band.title}>
                            {sentence(humanEnum(band.label))}
                          </span>
                        )}
                      </td>
                      <td className="num">
                        {line.eligible_incidents} / {line.excluded_incidents}
                      </td>
                      <td className="num">{line.scc_minutes_deducted || <span className="scl-dim">0</span>}</td>
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
                          <span className="scl-dim">None</span>
                        )}
                      </td>
                      {anyDispute && (
                        <td>
                          {dispute ? (
                            <span>
                              {humanEnum(dispute.label)}
                              {dispute.detail ? <span className="scl-dim"> {dispute.detail}</span> : null}
                            </span>
                          ) : (
                            <span className="scl-dim">—</span>
                          )}
                        </td>
                      )}
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

const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
