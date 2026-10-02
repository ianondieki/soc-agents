import { useState } from "react";

/**
 * One-series horizontal bars with a table view.
 *
 * A single measure (minutes, steps) across a dozen categories is a magnitude comparison, so
 * every bar is the same hue and the length does the talking; the category name sits beside
 * the bar and the value at its end in text, never only as length. "Show as table" gives the
 * same rows as text for a screen reader, a printout or anyone who distrusts a bar.
 */
export interface BarRow {
  key: string;
  label: string;
  value: number;
  /** Secondary text after the value, e.g. "× 11 steps". */
  note?: string;
  /** Tooltip for the hover layer. */
  title?: string;
}

export default function ToilBars({
  rows,
  unit,
  caption,
  maxValue,
  format = (v) => String(Math.round(v * 10) / 10),
}: {
  rows: BarRow[];
  unit: string;
  caption: string;
  maxValue?: number;
  format?: (v: number) => string;
}) {
  const [table, setTable] = useState(false);
  const max = Math.max(1, maxValue ?? Math.max(...rows.map((r) => r.value), 0));
  return (
    <figure className="toil">
      <figcaption className="toil-cap">
        <span>{caption}</span>
        <button type="button" className="btn sm toil-toggle" onClick={() => setTable((t) => !t)} aria-pressed={table}>
          {table ? "Show as bars" : "Show as table"}
        </button>
      </figcaption>
      {table ? (
        <table className="toil-table">
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col" style={{ textAlign: "right" }}>
                {unit ? unit[0].toUpperCase() + unit.slice(1) : unit}
              </th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td>{r.label}</td>
                <td style={{ textAlign: "right", fontFamily: "var(--mono)" }}>{format(r.value)}</td>
                <td className="muted">{r.note || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <ol className="toil-bars" aria-label={caption}>
          {rows.map((r) => (
            <li key={r.key} className="toil-row" title={r.title || `${r.label}: ${format(r.value)} ${unit}`}>
              <span className="toil-label">{r.label}</span>
              <span className="toil-track" aria-hidden="true">
                <span className="toil-fill" style={{ width: `${Math.max(r.value > 0 ? 1.5 : 0, (r.value / max) * 100)}%` }} />
              </span>
              <span className="toil-value">
                {format(r.value)}
                {r.note ? <span className="muted dim"> {r.note}</span> : null}
              </span>
            </li>
          ))}
        </ol>
      )}
    </figure>
  );
}
