import { Fragment, useMemo } from "react";
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
  type FailureKind,
} from "../../lib/support";
import { fmtDateTime } from "../../lib/time";

/**
 * The eval suite: the desk scored on a labelled set against fixed gates. Two headline figures
 * (resolution rate, wrong-escalation rate) each drawn against its gate, the two other gates, the
 * supporting metrics each with its one-sentence meaning, the route confusion matrix, the
 * per-category table, the failures grouped by kind, and the dataset facts. Dev and test splits
 * sit side by side when the report carries them.
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
const FAILURE_ORDER: FailureKind[] = ["missed_escalation", "wrong_escalation", "wrong_route", "wrong_category", "wrong_article", "wrong_tool", "unresolved"];

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
  const value = pct(g.value, g.value > 0 && g.value < 0.01 ? 1 : 0);
  const bar = g.op !== "==";
  return (
    <div className={"sd-gate" + (big ? " big" : "") + (g.passed ? "" : " fail")}>
      <div className="sd-gate-label">{label}</div>
      <div className="sd-gate-value mono">{value}</div>
      {bar && (
        <div className="sd-gatebar" aria-hidden="true">
          <span className="sd-gatebar-fill" style={{ width: `${Math.max(0, Math.min(100, g.value * 100))}%` }} />
          <span className="sd-gatebar-tick" style={{ left: `${Math.max(0, Math.min(100, g.threshold * 100))}%` }} />
        </div>
      )}
      <div className={"sd-gate-word state" + (g.passed ? " ok" : " danger")}>
        {g.passed ? <IconCheck /> : <IconAlert />}
        {gateSentence(g)}
      </div>
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

function Failure({ f }: { f: EvalFailure }) {
  const keys = Array.from(new Set([...Object.keys(f.expected ?? {}), ...Object.keys(f.actual ?? {})]));
  return (
    <details className="sd-fail">
      <summary>
        <span className="mono">{f.case_id}</span>
        <span className="sd-fail-text">{f.text}</span>
      </summary>
      <div className="sd-fail-body">
        <p className="sd-fail-full">{f.text}</p>
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
    </details>
  );
}

function renderVal(k: string, v: unknown): string {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.length ? v.map((x) => valueText(k, x)).join(", ") : "none";
  if (typeof v === "object") return JSON.stringify(v);
  return valueText(k, v);
}

export default function Evals({ report, state, error, running, onRun, onRetry, runError }: EvalsProps) {
  const groups = useMemo(() => {
    const m = new Map<FailureKind, EvalFailure[]>();
    for (const f of report?.failures ?? []) m.set(f.kind, [...(m.get(f.kind) ?? []), f]);
    return FAILURE_ORDER.filter((k) => m.has(k)).map((k) => ({ kind: k, items: m.get(k)! }));
  }, [report]);

  const runButton = (
    <button type="button" className="btn primary" onClick={onRun} aria-disabled={running ? true : undefined} aria-busy={running || undefined}>
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
      <div className="panel">
        <div className="empty">
          No eval run yet. Running the suite sends the golden set of complaints, in English, Kiswahili and Sheng, through the desk in memory and scores it
          against the gates. It takes about a second.
          {runError && (
            <p className="sd-error" role="alert">
              {runError}
            </p>
          )}
          <p>{runButton}</p>
        </div>
      </div>
    );
  }

  const r = report;
  const headline = ["resolution_rate", "wrong_escalation_rate"].map((k) => gateFor(r, k)).filter((g): g is EvalGate => !!g);
  const others = r.gates.filter((g) => !headline.includes(g));
  const failed = r.gates.filter((g) => !g.passed).length;
  const splits = r.by_split && r.by_split.dev && r.by_split.test ? r.by_split : null;
  const splitWord = r.dataset.split === "all" ? "whole set" : `${r.dataset.split} split`;

  return (
    <div className="sd-evals">
      <div className="sd-ev-head">
        <div className="sd-ev-verdict">
          <span className={"state " + (r.passed ? "ok" : "danger")}>
            {r.passed ? <IconCheck /> : <IconAlert />}
            {r.passed ? `All ${r.gates.length} gates pass` : `${failed} of ${r.gates.length} gates ${failed === 1 ? "fails" : "fail"}`}
          </span>
          <span className="facts">
            <span>
              {r.dataset.name} <span className="mono">{r.dataset.version}</span>
            </span>
            <span>
              {r.dataset.size} cases, {splitWord}
              {splits ? " (test scored above)" : ""}
            </span>
            <span>{r.mode === "llm" ? "with the LLM tie-break" : "deterministic"}</span>
            <span>
              ran <span className="mono">{fmtDateTime(r.ran_at)}</span> EAT
            </span>
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
          {splits && (
            <>
              <h3 className="sd-ev-sub">Dev and test splits</h3>
              <div className="table-scroll sd-splits">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Metric</th>
                      <th scope="col" className="num">
                        Dev
                      </th>
                      <th scope="col" className="num">
                        Test
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(Object.keys(METRIC_DEFS) as (keyof EvalMetrics)[]).map((k) => (
                      <tr key={k}>
                        <th scope="row">{METRIC_DEFS[k].label}</th>
                        <td className="num">{metricText(k, splits.dev[k])}</td>
                        <td className="num">{metricText(k, splits.test[k])}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
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

      <section className="panel" aria-labelledby="sd-ev-fail">
        <div className="head-row">
          <h2 id="sd-ev-fail" className="panel-title">
            Failures
          </h2>
          <span>{r.failures.length === 0 ? "none" : `${r.failures.length} of ${r.dataset.size} cases`}</span>
        </div>
        {r.failures.length === 0 ? (
          <p className="sd-ev-note">Every case ended where the gold set says, with the right article or tool. Edit a rule in triage or the knowledge base and run again to see one appear here.</p>
        ) : (
          groups.map((g) => (
            <Fragment key={g.kind}>
              <h3 className="sd-ev-sub">
                {FAILURE_KIND_WORD[g.kind]} <span className="muted">({g.items.length})</span>
              </h3>
              <div className="sd-fails">
                {g.items.map((f) => (
                  <Failure key={f.case_id} f={f} />
                ))}
              </div>
            </Fragment>
          ))
        )}
      </section>
    </div>
  );
}
