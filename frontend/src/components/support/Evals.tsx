import { useMemo } from "react";
import { IconAlert, IconCheck } from "../../lib/icons";
import {
  CATEGORY_WORD,
  FAILURE_KIND_WORD,
  GATE_WORD,
  METRIC_DEFS,
  confusionLabel,
  fmtMs,
  gateFor,
  keyLabel,
  pct,
  valueText,
  type EvalFailure,
  type EvalGate,
  type EvalMetrics,
  type EvalReport,
} from "../../lib/support";
import { fmtDateTime } from "../../lib/time";

/** Splits in the order they were written, how the page names them, and what each one is. */
const SPLIT_ORDER = ["dev", "validation", "test", "holdout"];
const SPLIT_WORD: Record<string, { short: string; long: string; what: string }> = {
  dev: { short: "Dev", long: "dev split", what: "is what the rules were tuned on" },
  validation: { short: "Validation", long: "validation split", what: "was written blind, then seen" },
  test: { short: "Test", long: "held-out test split", what: "was written blind and frozen" },
  holdout: { short: "Holdout", long: "blind holdout", what: "was written blind by another author and is never tuned against" },
};

/**
 * The eval suite: the desk scored on a labelled set against fixed gates. Two headline figures
 * (resolution rate, wrong-escalation rate) each drawn against its gate, the two other gates, the
 * supporting metrics each with its one-sentence meaning, the route confusion matrix, the
 * per-category table, the failures grouped by case, and the dataset facts. Every split the report
 * carries (dev, validation, holdout) sits side by side, each named for what it is. A gate with no
 * evidence in the split (value null) shows a dash and the report's note, never a figure.
 */

export interface EvalsProps {
  report: EvalReport | null;
  state: "loading" | "ok" | "missing" | "error";
  error: string | null;
  running: boolean;
  onRun: () => void;
  onRetry: () => void;
  runError: string | null;
}

const SUPPORTING: (keyof EvalMetrics)[] = ["containment_rate", "routing_accuracy", "grounded_answer_rate", "tool_accuracy", "escalation_reason_accuracy", "missed_escalation_rate", "p50_ms"];

function metricText(key: keyof EvalMetrics, v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return METRIC_DEFS[key].ms ? fmtMs(v) : pct(v, v > 0 && v < 0.01 ? 1 : 0);
}

function gateSentence(g: EvalGate): string {
  const t = g.op === "==" && g.threshold === 0 ? "zero" : pct(g.threshold);
  return `${g.passed ? "Passes" : "Fails"} the gate of ${GATE_WORD[g.op]} ${t}`;
}

function Gate({ g, big }: { g: EvalGate; big: boolean }) {
  const def = METRIC_DEFS[g.metric as keyof EvalMetrics];
  const label = def?.label ?? keyLabel(g.metric);
  const hasValue = typeof g.value === "number" && Number.isFinite(g.value);
  const value = hasValue ? pct(g.value, (g.value as number) > 0 && (g.value as number) < 0.01 ? 1 : 0) : "—";
  const bar = hasValue && g.op !== "==";
  return (
    <div className={"sd-gate" + (big ? " big" : "") + (hasValue && !g.passed ? " fail" : "") + (hasValue ? "" : " none")}>
      <div className="sd-gate-label">{label}</div>
      <div className="sd-gate-value mono">{value}</div>
      {bar && (
        <div className="sd-gatebar" aria-hidden="true">
          <span className="sd-gatebar-fill" style={{ width: `${Math.max(0, Math.min(100, (g.value as number) * 100))}%` }} />
          <span className="sd-gatebar-tick" style={{ left: `${Math.max(0, Math.min(100, g.threshold * 100))}%` }} />
        </div>
      )}
      {hasValue ? (
        <div className={"sd-gate-word state" + (g.passed ? " ok" : " danger")}>
          {g.passed ? <IconCheck /> : <IconAlert />}
          {gateSentence(g)}
        </div>
      ) : (
        <div className="sd-gate-word sd-gate-note">{g.note || "No case in this split to measure it on."}</div>
      )}
      {def && <p className="sd-gate-means">{def.means}</p>}
    </div>
  );
}

function Matrix({ labels, matrix }: { labels: string[]; matrix: number[][] }) {
  const max = Math.max(1, ...matrix.flat());
  return (
    <table className="sd-matrix">
      <caption className="sr-only">Route confusion: rows are the expected route, columns the route the desk chose.</caption>
      <thead>
        <tr>
          <th scope="col" className="sd-matrix-corner">
            <span className="sd-matrix-exp">Expected</span>
          </th>
          <th scope="colgroup" colSpan={labels.length} className="sd-matrix-act">
            Desk chose
          </th>
        </tr>
        <tr>
          <th scope="col" aria-hidden="true" />
          {labels.map((l) => (
            <th key={l} scope="col">
              {confusionLabel(l)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {matrix.map((row, i) => (
          <tr key={labels[i] ?? i}>
            <th scope="row">{confusionLabel(labels[i] ?? String(i))}</th>
            {row.map((n, j) => {
              const diag = i === j;
              const k = n / max;
              const cls = "sd-cell" + (n === 0 ? " zero" : diag ? " diag" : " off");
              return (
                <td key={j} className={cls} style={{ ["--k" as string]: k }} title={`${n} ${n === 1 ? "case" : "cases"} expected ${confusionLabel(labels[i])}, chose ${confusionLabel(labels[j])}`}>
                  {n}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function renderVal(k: string, v: unknown): string {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.length ? v.map((x) => valueText(k, x)).join(", ") : "none";
  if (typeof v === "object") return JSON.stringify(v);
  if (k === "escalation_reason") return valueText(k, v);
  return valueText(k, v);
}

/** One failed case: every way it failed, with expected against actual for each. */
function FailedCase({ caseId, items }: { caseId: string; items: EvalFailure[] }) {
  const kinds = items.map((f) => FAILURE_KIND_WORD[f.kind] ?? f.kind);
  const text = items[0]?.text ?? "";
  return (
    <details className="sd-fail">
      <summary>
        <span className="mono">{caseId}</span>
        <span className="sd-fail-kinds">{kinds.join(", ")}</span>
        <span className="sd-fail-text">{text}</span>
      </summary>
      <div className="sd-fail-body">
        <p className="sd-fail-full">{text}</p>
        {items.map((f, i) => {
          const keys = Array.from(new Set([...Object.keys(f.expected ?? {}), ...Object.keys(f.actual ?? {})]));
          return (
            <div key={`${f.kind}-${i}`} className="sd-fail-one">
              <h4 className="sd-fail-kind">{FAILURE_KIND_WORD[f.kind] ?? f.kind}</h4>
              <div className="table-scroll">
                <table className="sd-fail-table">
                  <thead>
                    <tr>
                      <th scope="col">Field</th>
                      <th scope="col">Expected</th>
                      <th scope="col">Actual</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keys.map((k) => {
                      const e = f.expected?.[k];
                      const a = f.actual?.[k];
                      const differs = JSON.stringify(e) !== JSON.stringify(a);
                      return (
                        <tr key={k} className={differs ? "differs" : undefined}>
                          <th scope="row">{keyLabel(k)}</th>
                          <td>{renderVal(k, e)}</td>
                          <td>{renderVal(k, a)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>
    </details>
  );
}

/** The three gates, said before the first run (the report carries the live thresholds after it). */
const GATE_PREVIEW = [
  {
    name: "Resolution rate",
    need: "At least 80%",
    def: "Of the complaints the desk should answer itself, the share it got right: the right route, and the right article or tool.",
  },
  {
    name: "Wrong escalations",
    need: "At most 10%",
    def: "Of the complaints sent to a person, the share that did not need one.",
  },
  {
    name: "Missed safety escalations",
    need: "None",
    def: "Fraud, SIM swap, legal, regulator and threat cases kept by the agents instead of handed to a person.",
  },
];

export default function Evals({ report, state, error, running, onRun, onRetry, runError }: EvalsProps) {
  // Failures grouped by case: one case can fail in more than one way.
  const byCase = useMemo(() => {
    const m = new Map<string, EvalFailure[]>();
    for (const f of report?.failures ?? []) m.set(f.case_id, [...(m.get(f.case_id) ?? []), f]);
    return Array.from(m.entries());
  }, [report]);

  const runButton = (
    <button type="button" className="btn primary" onClick={() => !running && onRun()} aria-disabled={running ? true : undefined} aria-busy={running || undefined}>
      {running ? "Running the evals…" : "Run the evals"}
    </button>
  );

  if (state === "loading") {
    return (
      <div className="panel" aria-busy="true">
        <span className="sr-only" role="status">
          Loading the latest eval report.
        </span>
        <div className="skeleton-rows" aria-hidden="true">
          {["30%", "72%", "58%", "66%", "40%"].map((w, i) => (
            <span key={i} className="skeleton" style={{ width: w }} />
          ))}
        </div>
      </div>
    );
  }
  if (state === "error") {
    return (
      <div className="panel">
        <div className="empty" role="alert">
          Couldn't load the eval report. {error}
          <button type="button" className="btn sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      </div>
    );
  }
  if (state === "missing" || !report) {
    return (
      <section className="panel sd-evals-empty" aria-labelledby="sd-evals-empty-title">
        <h2 id="sd-evals-empty-title" className="panel-title">
          No eval run yet
        </h2>
        <p className="sd-evals-empty-lead">
          Running the suite sends the golden set of complaints, in English, Kiswahili and Sheng, through the desk in memory and scores it against
          three gates. It takes about a second, and nothing reaches a customer.
        </p>
        <ul className="sd-gates-preview">
          {GATE_PREVIEW.map((g) => (
            <li key={g.name}>
              <span className="sd-gate-name">{g.name}</span>
              <span className="sd-gate-need">{g.need}</span>
              <span className="sd-gate-def">{g.def}</span>
            </li>
          ))}
        </ul>
        {runError && (
          <p className="sd-error" role="alert">
            {runError}
          </p>
        )}
        <p className="sd-evals-empty-act">{runButton}</p>
      </section>
    );
  }

  const r = report;
  const headline = ["resolution_rate", "wrong_escalation_rate"].map((k) => gateFor(r, k)).filter((g): g is EvalGate => !!g);
  const others = r.gates.filter((g) => !headline.includes(g));
  const failed = r.gates.filter((g) => !g.passed).length;
  // The splits in the order they were written, each named for what it is.
  const splitKeys = r.by_split ? SPLIT_ORDER.filter((k) => r.by_split?.[k]) : [];
  const splits = splitKeys.length >= 2 ? r.by_split! : null;
  const splitNote = splitKeys.map((k, i) => `${i === 0 ? "" : ""}${SPLIT_WORD[k]?.short ?? k} ${SPLIT_WORD[k]?.what ?? "is a split of the set"}`).join("; ");
  const splitWord = r.dataset.split === "all" ? "whole set" : SPLIT_WORD[r.dataset.split]?.long ?? `${r.dataset.split} split`;
  const excluded = r.dataset.excluded ? `, ${r.dataset.excluded} contested left out` : "";
  const failureCount = r.failures.length;
  const caseCount = byCase.length;

  return (
    <div className="sd-evals">
      <div className="sd-ev-head">
        <div className="sd-ev-verdict">
          <span className={"state " + (r.passed ? "ok" : "danger")}>
            {r.passed ? <IconCheck /> : <IconAlert />}
            {r.passed ? `All ${r.gates.length} gates pass` : `${failed} of ${r.gates.length} gates ${failed === 1 ? "fails" : "fail"}`}
          </span>
          <span className="facts">
            <span title={`${r.dataset.name}, version ${r.dataset.version}`}>
              {r.dataset.size} cases, {splitWord}
              {excluded}
              {splits ? " (scored above)" : ""}
            </span>
            <span>{r.mode === "llm" ? "with the LLM tie-break" : "deterministic"}</span>
            <span>
              ran <span className="mono">{fmtDateTime(r.ran_at)}</span> EAT
            </span>
            <details className="sd-ev-dataset">
              <summary>Dataset</summary>
              <span>
                {r.dataset.name}, version <span className="mono">{r.dataset.version}</span>
              </span>
            </details>
          </span>
        </div>
        <div className="sd-ev-run">
          {runError && (
            <span className="sd-error" role="alert">
              {runError}
            </span>
          )}
          {runButton}
        </div>
      </div>

      <section className="panel" aria-labelledby="sd-ev-gates">
        <h2 id="sd-ev-gates" className="panel-title">
          The gates
        </h2>
        <div className="sd-gates">
          {headline.map((g) => (
            <Gate key={g.metric} g={g} big />
          ))}
          {others.map((g) => (
            <Gate key={g.metric} g={g} big={false} />
          ))}
        </div>
      </section>

      <section className="panel" aria-labelledby="sd-ev-sup">
        <h2 id="sd-ev-sup" className="panel-title">
          Supporting metrics
        </h2>
        <div className="sd-metrics">
          {SUPPORTING.map((k) => (
            <div key={k} className="sd-metric">
              <div className="sd-metric-label">{METRIC_DEFS[k].label}</div>
              <div className="sd-metric-value mono">{metricText(k, r.metrics[k])}</div>
              <p className="sd-metric-means">{METRIC_DEFS[k].means}</p>
            </div>
          ))}
        </div>
      </section>

      <div className="sd-ev-grid">
        <section className="panel" aria-labelledby="sd-ev-conf">
          <h2 id="sd-ev-conf" className="panel-title">
            Where cases went
          </h2>
          <p className="sd-ev-note">Rows are the route the gold set expects; columns the route the desk chose. The diagonal is correct.</p>
          <Matrix labels={r.confusion.labels} matrix={r.confusion.matrix} />
        </section>

        <section className="panel" aria-labelledby="sd-ev-cat">
          <h2 id="sd-ev-cat" className="panel-title">
            By category
          </h2>
          <div className="table-scroll">
            <table className="sd-cat">
              <thead>
                <tr>
                  <th scope="col">Category</th>
                  <th scope="col" className="num">
                    Cases
                  </th>
                  <th scope="col" className="num">
                    Resolution rate
                  </th>
                  <th scope="col" className="num">
                    Wrong escalations
                  </th>
                  <th scope="col" className="num">
                    Triage accuracy
                  </th>
                </tr>
              </thead>
              <tbody>
                {r.by_category.map((row) => (
                  <tr key={row.category}>
                    <th scope="row">{(CATEGORY_WORD as Record<string, string>)[row.category] ?? row.category}</th>
                    <td className="num">{row.n}</td>
                    <td className="num" title={row.resolution_rate == null ? "No resolvable case to measure" : undefined}>
                      {row.resolution_rate == null ? "—" : pct(row.resolution_rate)}
                    </td>
                    <td className="num" title={row.wrong_escalation_rate == null ? "No escalation to measure" : undefined}>
                      {row.wrong_escalation_rate == null ? "—" : pct(row.wrong_escalation_rate)}
                    </td>
                    <td className="num" title={row.triage_accuracy == null ? "No case to measure" : undefined}>
                      {row.triage_accuracy == null ? "—" : pct(row.triage_accuracy)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="sd-ev-note">A dash means the set has no case of that kind to measure.</p>
        </section>
      </div>

      {splits && (
        <section className="panel" aria-labelledby="sd-ev-split">
          <h2 id="sd-ev-split" className="panel-title">
            By split
          </h2>
          <p className="sd-ev-note">{splitNote}.</p>
          <div className="table-scroll sd-splits">
            <table>
              <thead>
                <tr>
                  <th scope="col">Metric</th>
                  {splitKeys.map((k) => (
                    <th key={k} scope="col" className="num">
                      {SPLIT_WORD[k]?.short ?? k}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(Object.keys(METRIC_DEFS) as (keyof EvalMetrics)[]).map((k) => (
                  <tr key={k}>
                    <th scope="row">{METRIC_DEFS[k].label}</th>
                    {splitKeys.map((sk) => (
                      <td key={sk} className="num">
                        {metricText(k, splits[sk][k])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="panel" aria-labelledby="sd-ev-fail">
        <div className="head-row">
          <h2 id="sd-ev-fail" className="panel-title">
            Failures
          </h2>
          <span>
            {failureCount === 0
              ? "none"
              : `${caseCount} ${caseCount === 1 ? "case" : "cases"} of ${r.dataset.size}, ${failureCount} ${failureCount === 1 ? "failure" : "failures"}`}
          </span>
        </div>
        {failureCount === 0 ? (
          <p className="sd-ev-note">Every case ended where the gold set says, with the right article or tool. Edit a rule in triage or the knowledge base and run again to see one appear here.</p>
        ) : (
          <>
            <p className="sd-ev-note">One case can fail in more than one way; each is listed once, with every way it failed.</p>
            <div className="sd-fails">
              {byCase.map(([caseId, items]) => (
                <FailedCase key={caseId} caseId={caseId} items={items} />
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
