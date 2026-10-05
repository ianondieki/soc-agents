import { Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  CATEGORY_WORD,
  MONO_KEYS,
  fmtMs,
  humanWords,
  keyLabel,
  reasonWord,
  softWords,
  stepSentence,
  toolPhrase,
  toolWord,
  valueText,
  type CaseDetail,
  type Step,
  type ToolStatus,
} from "../../lib/support";
import { AgentDot, ToolStatusWord, agentWord } from "./marks";

/**
 * The agent trace as a short story: one line per step (the agent, then one plain sentence built
 * from the word maps), each line a disclosure that opens on its structured detail. Only the step
 * the verdict turns on starts open: the escalation, the held call, the article the resolver
 * answered from, the tool that fixed it. Facts the header and the verdict already show (channel,
 * language, number, category, urgency, sentiment) are not repeated, a tool result that merely
 * echoes its arguments is dropped, and the raw JSON sits behind one disclosure at the foot.
 */

/** Triage sends a case to a person below this confidence (config/support/policy.yaml). */
const LOW_CONFIDENCE = 0.55;

const isPlain = (v: unknown): v is Record<string, unknown> => v != null && typeof v === "object" && !Array.isArray(v);
const isScalar = (v: unknown): boolean => v == null || ["string", "number", "boolean"].includes(typeof v);

/** A value cell: scalars as words (numbers, codes and money in the mono), lists joined, anything deeper behind a disclosure. */
function Value({ k, v }: { k: string; v: unknown }) {
  if (Array.isArray(v)) {
    if (v.length === 0) return <>none</>;
    if (v.every(isScalar)) return <>{v.map((x) => valueText(k, x)).join(", ")}</>;
    return <Nested label={`${v.length} ${v.length === 1 ? "item" : "items"}`} value={v} />;
  }
  if (isPlain(v)) {
    const keys = Object.keys(v);
    if (keys.length === 0) return <>none</>;
    if (keys.length <= 4 && keys.every((x) => isScalar(v[x]))) {
      return <>{keys.map((x) => `${keyLabel(x).toLowerCase()} ${valueText(x, v[x])}`).join(", ")}</>;
    }
    return <Nested label={`${keys.length} fields`} value={v} />;
  }
  const text = valueText(k, v);
  const mono = typeof v === "number" || MONO_KEYS.has(k) || /^[A-Z0-9-]{6,}$/.test(text) || /^KES /.test(text);
  return mono ? <span className="mono wrap">{text}</span> : <>{text}</>;
}

function Nested({ label, value }: { label: string; value: unknown }) {
  return (
    <details className="sd-nested">
      <summary>{label}</summary>
      <pre className="pre sd-pre">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

/** Label and value pairs, the shared `.rail-dl` primitive. `order` puts named keys first; nulls are left out. */
export function KvList({ obj, order = [], skip = [], twoUp = true }: { obj: Record<string, unknown>; order?: string[]; skip?: string[]; twoUp?: boolean }) {
  const skipSet = new Set(skip);
  const has = (k: string) => k in obj && !skipSet.has(k) && obj[k] != null;
  const keys = [...order.filter(has), ...Object.keys(obj).filter((k) => !order.includes(k) && has(k))];
  if (!keys.length) return null;
  return (
    <dl className={"rail-dl sd-kv" + (twoUp ? " two-up" : "")}>
      {keys.map((k) => (
        <Fragment key={k}>
          <dt>{keyLabel(k)}</dt>
          <dd>
            <Value k={k} v={obj[k]} />
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}

/** Triage confidence against the low-confidence threshold: a small meter with the tick at 0.55. */
export function ConfidenceMeter({ value }: { value: number }) {
  const v = Math.max(0, Math.min(1, Number(value) || 0));
  const low = v < LOW_CONFIDENCE;
  return (
    <span className={"sd-meter" + (low ? " low" : "")} title={`Below ${LOW_CONFIDENCE.toFixed(2)} a case goes to a person`}>
      <span className="sd-meter-track" aria-hidden="true">
        <span className="sd-meter-fill" style={{ width: `${v * 100}%` }} />
        <span className="sd-meter-tick" style={{ left: `${LOW_CONFIDENCE * 100}%` }} />
      </span>
      <span className="mono">{v.toFixed(2)}</span>
      {low && <span className="sd-meter-word">below the threshold</span>}
    </span>
  );
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** One tool call: the policy sentence, then arguments and a result that adds something. */
export function ToolCallView({ call, showName = true }: { call: { tool: string; status: ToolStatus; policy: string | null; args: Record<string, unknown>; result: Record<string, unknown> | null; decided_by?: string | null }; showName?: boolean }) {
  const args = isPlain(call.args) ? call.args : {};
  const result = isPlain(call.result) && !same(call.result, args) ? call.result : null;
  return (
    <div className="sd-tool">
      {showName && (
        <div className="sd-tool-head">
          <span className="sd-tool-name">
            {toolWord(call.tool)} <code>{call.tool}</code>
          </span>
          <ToolStatusWord status={call.status} />
          {call.decided_by && <span className="muted">decided by {call.decided_by}</span>}
        </div>
      )}
      {call.policy && <p className="sd-tool-policy">{call.policy}</p>}
      {Object.keys(args).length > 0 && (
        <div className="sd-tool-part">
          <span className="sd-tool-label">Arguments</span>
          <KvList obj={args} twoUp={false} />
        </div>
      )}
      {result && Object.keys(result).length > 0 && (
        <div className="sd-tool-part">
          <span className="sd-tool-label">Result</span>
          <KvList obj={result} order={["reversal_id", "amount_kes", "counterparty", "incident_number", "site_name", "status", "note"]} skip={["found"]} twoUp={false} />
        </div>
      )}
    </div>
  );
}

function CandidateList({ items, titles, top }: { items: any[]; titles: Record<string, string>; top: string | null }) {
  return (
    <ol className="sd-cites">
      {items.map((c, i) => {
        const id = String(c?.article_id ?? "");
        const matched = Array.isArray(c?.matched) ? c.matched.filter((m: unknown) => typeof m === "string") : [];
        return (
          <li key={id || i} className={id && id === top ? "top" : ""}>
            <span className="sd-cite-title">{titles[id] ?? id}</span>
            <span className="sd-cite-id mono">{id}</span>
            <span className="sd-cite-score mono">{typeof c?.score === "number" ? c.score.toFixed(1) : "—"}</span>
            {matched.length > 0 && <span className="sd-cite-matched">matched {matched.map((m: string) => softWords(m)).join(", ")}</span>}
          </li>
        );
      })}
    </ol>
  );
}

/** The detail under a step, trimmed to what the sentence, the header and the verdict do not say. */
function StepBody({ step, detail, titles }: { step: Step; detail: CaseDetail; titles: Record<string, string> }): ReactNode {
  const d = isPlain(step.detail) ? step.detail : {};
  if (!Object.keys(d).length) return null;
  const a = step.agent;

  if (a === "intake") return null; // channel, language, number and the account are in the sentence and the header

  if (a === "triage") {
    const reasons = Array.isArray(d.reasons) ? d.reasons.filter((r) => typeof r === "string") : [];
    const scores = isPlain(d.scores) ? d.scores : {};
    const places = Array.isArray(d.places) ? d.places : [];
    const flags = Array.isArray(d.risk_flags) ? d.risk_flags : [];
    const scoreText = Object.entries(scores)
      .map(([k, v]) => `${(CATEGORY_WORD as Record<string, string>)[k] ?? humanWords(k)} ${typeof v === "number" ? v.toFixed(1) : String(v)}`)
      .join(", ");
    const placeText = places
      .map((p: any) => (isPlain(p) ? `${String(p.name ?? "")}${Array.isArray(p.regions) && p.regions.length ? ` (${p.regions.join(", ")})` : ""}` : String(p)))
      .filter(Boolean)
      .join(", ");
    return (
      <>
        <dl className="rail-dl sd-kv two-up">
          <dt>Confidence</dt>
          <dd>
            <ConfidenceMeter value={Number(d.confidence)} />
          </dd>
          <dt>Route chosen</dt>
          <dd>{valueText("route", d.route)}</dd>
          {flags.length > 0 && (
            <>
              <dt>Risk flags</dt>
              <dd className="sd-flag">{flags.map((f) => humanWords(f)).join(", ")}</dd>
            </>
          )}
          {d.intent != null && (
            <>
              <dt>Intent</dt>
              <dd>
                {humanWords(d.intent)}
                {d.tool ? <span className="muted dim">, via {toolPhrase(d.tool)}</span> : null}
              </dd>
            </>
          )}
          {placeText && (
            <>
              <dt>Places named</dt>
              <dd>{placeText}</dd>
            </>
          )}
          {scoreText && (
            <>
              <dt>Category scores</dt>
              <dd className="mono wrap">{scoreText}</dd>
            </>
          )}
          <dt>Decided by</dt>
          <dd>{d.source === "rules" ? "the rules" : d.source === "llm" ? "the rules, with an LLM tie-break" : valueText("source", d.source)}</dd>
        </dl>
        {reasons.length > 0 && (
          <div className="sd-reasons">
            <span className="sd-tool-label">Reasons that fired</span>
            <ul>
              {reasons.map((r, i) => (
                <li key={i}>{softWords(r)}</li>
              ))}
            </ul>
          </div>
        )}
      </>
    );
  }

  if (a === "resolver") {
    const cands = Array.isArray(d.candidates) ? d.candidates : [];
    const grounded = d.grounded === true;
    const top = typeof d.article_id === "string" ? d.article_id : null;
    return (
      <>
        <dl className="rail-dl sd-kv two-up">
          <dt>Grounded</dt>
          <dd>
            <span className={"state" + (grounded ? " ok" : " warn")}>{grounded ? "Yes" : "No"}</span>
            {typeof d.score === "number" && (
              <span className="muted dim">
                {" "}
                (best score <span className="mono">{d.score.toFixed(1)}</span>)
              </span>
            )}
          </dd>
          {top && (
            <>
              <dt>Best article</dt>
              <dd>
                {titles[top] ? `${titles[top]} ` : ""}
                <span className="mono">{top}</span>
              </dd>
            </>
          )}
          {d.escalate != null && d.escalate !== false && (
            <>
              <dt>Escalate</dt>
              <dd className="sd-flag">{typeof d.escalate === "string" ? `article ${d.escalate} is marked escalate` : "the article is marked escalate"}</dd>
            </>
          )}
        </dl>
        {cands.length > 0 && (
          <div className="sd-reasons">
            <span className="sd-tool-label">Candidates, by score</span>
            <CandidateList items={cands} titles={titles} top={top} />
          </div>
        )}
      </>
    );
  }

  if (a === "action" && step.action === "called_tool" && typeof d.tool === "string") {
    const status = (typeof d.status === "string" ? d.status : "ok") as ToolStatus;
    return (
      <ToolCallView
        showName={false}
        call={{
          tool: d.tool,
          status,
          policy: typeof d.policy === "string" ? d.policy : null,
          args: isPlain(d.args) ? d.args : {},
          result: isPlain(d.result) ? d.result : null,
        }}
      />
    );
  }

  if (a === "action" && step.action === "planned") {
    const args = isPlain(d.args) ? d.args : {};
    if (!Object.keys(args).length) return null;
    return (
      <div className="sd-tool-part">
        <span className="sd-tool-label">Arguments</span>
        <KvList obj={args} twoUp={false} />
      </div>
    );
  }

  if (a === "escalation") {
    const waiting = detail.tool_calls.find((c) => c.status === "needs_approval");
    const also = Array.isArray(d.also_matched) ? d.also_matched.filter((x) => typeof x === "string") : [];
    return (
      <dl className="rail-dl sd-kv">
        <dt>Reason</dt>
        <dd>{reasonWord(d.reason_code)}</dd>
        {d.reason != null && (
          <>
            <dt>Told the customer</dt>
            <dd>{String(d.reason)}</dd>
          </>
        )}
        {d.evidence != null && (
          <>
            <dt>Evidence</dt>
            <dd>{softWords(d.evidence)}</dd>
          </>
        )}
        {also.length > 0 && (
          <>
            <dt>Also matched</dt>
            <dd>{also.map((x) => reasonWord(x)).join(", ")}</dd>
          </>
        )}
        {waiting ? (
          <>
            <dt>Waiting for approval</dt>
            <dd>{toolPhrase(waiting.tool)}</dd>
          </>
        ) : d.held_tool ? (
          <>
            <dt>Held, not run</dt>
            <dd>{toolPhrase(d.held_tool)}, for whoever takes the case</dd>
          </>
        ) : null}
      </dl>
    );
  }

  // Human steps: the sentence says who and what; a note is in it too.
  if (a === "human") return null;

  return <KvList obj={d} skip={["msisdn_masked", "channel", "language", "category", "urgency", "sentiment"]} />;
}

/**
 * An outage near the place the customer named that was not linked (a region-only match is too
 * weak to link): said on the step, outside its disclosure, so a person sees the candidate.
 */
function Nearby({ step }: { step: Step }) {
  const n = isPlain(step.detail) && isPlain(step.detail.nearby_incident) ? step.detail.nearby_incident : null;
  if (!n) return null;
  const id = typeof n.id === "string" ? n.id : "";
  const num = typeof n.incident_number === "string" ? n.incident_number : "an open ticket";
  const title = typeof n.title === "string" && n.title ? n.title : "";
  return (
    <p className="sd-step-nearby">
      An outage nearby (
      {id ? (
        <Link to={`/incidents/${encodeURIComponent(id)}`} className="mono link" title={title || undefined}>
          {num}
        </Link>
      ) : (
        <span className="mono">{num}</span>
      )}
      ) may be the cause; not linked.
    </p>
  );
}

/** The step the verdict turns on: the one that starts open. */
function pivotSeq(detail: CaseDetail): number | null {
  const c = detail.complaint;
  const steps = detail.steps;
  const find = (fn: (s: Step) => boolean) => steps.find(fn)?.seq ?? null;
  const last = (fn: (s: Step) => boolean) => [...steps].reverse().find(fn)?.seq ?? null;
  // The follow-up agent's step is what a reopened or restored case turns on.
  if (c.escalation?.reason_code === "still_down_after_restore" && (c.status === "escalated" || c.status === "in_progress"))
    return last((s) => s.agent === "followup" && s.action === "still_down_reported") ?? last((s) => s.agent === "followup");
  if (c.status === "closed") return last((s) => s.agent === "followup");
  if (c.status === "awaiting_approval") return last((s) => s.agent === "action" && s.action === "called_tool" && (s.detail as any)?.status === "needs_approval") ?? find((s) => s.agent === "escalation");
  if (c.status === "escalated" || c.status === "in_progress" || c.status === "resolved") return last((s) => s.agent === "escalation") ?? last((s) => s.agent === "human");
  if (c.status === "answered") return last((s) => s.agent === "resolver");
  if (c.status === "action_taken") {
    return (
      last((s) => s.agent === "action" && s.action === "called_tool" && !["lookup_account", "update_ticket"].includes(String((s.detail as any)?.tool))) ??
      last((s) => s.agent === "action")
    );
  }
  return null;
}

export default function AgentTrace({ detail, titles }: { detail: CaseDetail; titles: Record<string, string> }) {
  const steps = [...detail.steps].sort((x, y) => x.seq - y.seq);
  const total = steps.reduce((n, s) => n + (Number.isFinite(s.duration_ms) ? s.duration_ms : 0), 0);
  const pivot = pivotSeq(detail);
  return (
    <section className="sd-section" aria-labelledby="sd-trace-h">
      <div className="head-row sd-section-head">
        <h3 id="sd-trace-h">What the agents did</h3>
        <span>
          {steps.length} {steps.length === 1 ? "step" : "steps"} in <span className="mono">{fmtMs(total)}</span>
        </span>
      </div>
      <ol className="sd-trace">
        {steps.map((s, i) => {
          const person = s.agent === "human" || s.agent === "escalation";
          const body = <StepBody step={s} detail={detail} titles={titles} />;
          const sentence = stepSentence(s, titles);
          const hasBody = body != null;
          return (
            <li key={`${s.seq}-${i}`} className={"sd-step" + (person ? " hitl" : "")}>
              <AgentDot agent={s.agent} />
              {hasBody ? (
                <details className="sd-step-d" open={s.seq === pivot || undefined}>
                  <summary className="sd-step-line">
                    <span className="sd-step-agent">{agentWord(s.agent)}</span>
                    <span className="sd-step-sum">{sentence}</span>
                  </summary>
                  <div className="sd-step-body">{body}</div>
                </details>
              ) : (
                <div className="sd-step-line static">
                  <span className="sd-step-agent">{agentWord(s.agent)}</span>
                  <span className="sd-step-sum">{sentence}</span>
                </div>
              )}
              <Nearby step={s} />
            </li>
          );
        })}
      </ol>
      <details className="sd-raw">
        <summary>Raw trace, as recorded</summary>
        <pre className="pre sd-pre" tabIndex={0} data-keep-tab="">
          {JSON.stringify({ steps: detail.steps, tool_calls: detail.tool_calls }, null, 2)}
        </pre>
      </details>
    </section>
  );
}
