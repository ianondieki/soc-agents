import { Fragment, type ReactNode } from "react";
import {
  CATEGORY_WORD,
  MONO_KEYS,
  fmtMs,
  humanWords,
  keyLabel,
  toolWord,
  valueText,
  type CaseDetail,
  type Step,
  type ToolCall,
  type ToolStatus,
} from "../../lib/support";
import { fmtTime } from "../../lib/time";
import { AgentDot, ToolStatusWord, agentWord } from "./marks";

/**
 * The agent trace: every step the desk recorded, as a vertical timeline. Intake, Triage, then
 * the Resolver or the Action agent, then Escalation and a person. Each step shows its plain
 * sentence, then the structured detail as label and value pairs, drawn for what it is: the triage
 * confidence as a meter against the 0.55 threshold, the reasons that fired as a list, the
 * candidates with their scores, a tool call with its arguments, result, policy sentence and
 * status. The raw JSON sits behind one disclosure at the foot, never in the way.
 */

/** Triage sends a case to a person below this confidence (config/support/policy.yaml). */
const LOW_CONFIDENCE = 0.55;

const isPlain = (v: unknown): v is Record<string, unknown> => v != null && typeof v === "object" && !Array.isArray(v);
const isScalar = (v: unknown): boolean => v == null || ["string", "number", "boolean"].includes(typeof v);

/** A value cell: scalars as words, lists of scalars joined, anything deeper behind a disclosure. */
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
  return MONO_KEYS.has(k) || /^[A-Z0-9-]{6,}$/.test(text) ? <span className="mono wrap">{text}</span> : <>{text}</>;
}

function Nested({ label, value }: { label: string; value: unknown }) {
  return (
    <details className="sd-nested">
      <summary>{label}</summary>
      <pre className="pre sd-pre">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

/** Label and value pairs, the shared `.rail-dl` primitive. `order` puts named keys first. */
export function KvList({ obj, order = [], skip = [], twoUp = true }: { obj: Record<string, unknown>; order?: string[]; skip?: string[]; twoUp?: boolean }) {
  const skipSet = new Set(skip);
  // A null field says nothing ("tier: null" on a line with no account): it is left out.
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

/** One tool call: what was called, its status, the policy sentence, then arguments and result. */
export function ToolCallView({ call, titleKb, compact = false }: { call: { tool: string; status: ToolStatus; policy: string | null; args: Record<string, unknown>; result: Record<string, unknown> | null; decided_by?: string | null }; titleKb?: Record<string, string>; compact?: boolean }) {
  void titleKb;
  const args = isPlain(call.args) ? call.args : {};
  const result = isPlain(call.result) ? call.result : null;
  return (
    <div className="sd-tool">
      <div className="sd-tool-head">
        <span className="sd-tool-name">
          {toolWord(call.tool)} <code>{call.tool}</code>
        </span>
        <ToolStatusWord status={call.status} />
        {call.decided_by && <span className="muted">decided by {call.decided_by}</span>}
      </div>
      {call.policy && <p className="sd-tool-policy">{call.policy}</p>}
      {!compact && Object.keys(args).length > 0 && (
        <div className="sd-tool-part">
          <span className="sd-tool-label">Arguments</span>
          <KvList obj={args} twoUp={false} />
        </div>
      )}
      {!compact && result && Object.keys(result).length > 0 && (
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
            {matched.length > 0 && <span className="sd-cite-matched">matched {matched.join(", ")}</span>}
          </li>
        );
      })}
    </ol>
  );
}

function StepBody({ step, titles }: { step: Step; titles: Record<string, string> }) {
  const d = isPlain(step.detail) ? step.detail : {};
  if (!Object.keys(d).length) return null;
  const a = step.agent;

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
          <dt>Category</dt>
          <dd>{valueText("category", d.category)}</dd>
          <dt>Urgency</dt>
          <dd>{valueText("urgency", d.urgency)}</dd>
          <dt>Sentiment</dt>
          <dd>{valueText("sentiment", d.sentiment)}</dd>
          {scoreText && (
            <>
              <dt>Category scores</dt>
              <dd>{scoreText}</dd>
            </>
          )}
          <dt>Risk flags</dt>
          <dd className={flags.length ? "sd-flag" : undefined}>{flags.length ? flags.map((f) => humanWords(f)).join(", ") : "none"}</dd>
          {d.intent != null && (
            <>
              <dt>Intent</dt>
              <dd>
                <span className="mono">{String(d.intent)}</span>
                {d.tool ? <span className="muted dim"> via {toolWord(d.tool)}</span> : null}
              </dd>
            </>
          )}
          {placeText && (
            <>
              <dt>Places named</dt>
              <dd>{placeText}</dd>
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
                <li key={i}>{String(r)}</li>
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
            {typeof d.score === "number" && <span className="muted dim"> (best score {d.score.toFixed(1)})</span>}
          </dd>
          {top && (
            <>
              <dt>Answered from</dt>
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
    return (
      <dl className="rail-dl sd-kv two-up">
        <dt>Tool</dt>
        <dd>
          {toolWord(d.tool)} <code>{String(d.tool ?? "")}</code>
        </dd>
        {isPlain(d.args) && Object.keys(d.args).length > 0 && (
          <>
            <dt>Arguments</dt>
            <dd>
              <Value k="args" v={d.args} />
            </dd>
          </>
        )}
        {d.why != null && (
          <>
            <dt>Why this target</dt>
            <dd>{String(d.why)}</dd>
          </>
        )}
      </dl>
    );
  }

  if (a === "escalation") {
    return (
      <KvList
        obj={{
          reason_code: d.reason_code,
          reason: d.reason,
          evidence: d.evidence,
          also_matched: d.also_matched,
          held_tool: d.held_tool == null ? "none" : `${toolWord(d.held_tool)} (${String(d.held_tool)}), held for the person who takes the case`,
        }}
        order={["reason_code", "reason", "evidence", "also_matched", "held_tool"]}
      />
    );
  }

  // Intake, human steps and anything new: the detail as it is, with the number already in the hero.
  return <KvList obj={d} skip={["msisdn_masked"]} order={["channel", "language", "chars", "account_found", "tier", "claimed_by", "resolved_by", "note", "reason"]} />;
}

const ACTION_WORD: Record<string, string> = {
  received: "received the complaint",
  classified: "classified it",
  retrieved: "searched the knowledge base",
  answered: "answered",
  planned: "planned a tool call",
  called_tool: "called a tool",
  escalated: "sent it to a person",
  claimed: "claimed it",
  resolved: "resolved it",
  approved: "approved the held call",
  rejected: "rejected the held call",
};

export default function AgentTrace({ detail, titles }: { detail: CaseDetail; titles: Record<string, string> }) {
  const steps = [...detail.steps].sort((x, y) => x.seq - y.seq);
  const total = steps.reduce((n, s) => n + (Number.isFinite(s.duration_ms) ? s.duration_ms : 0), 0);
  return (
    <section className="sd-section" aria-labelledby="sd-trace-h">
      <div className="head-row sd-section-head">
        <h3 id="sd-trace-h">What the agents did</h3>
        <span>
          {steps.length} {steps.length === 1 ? "step" : "steps"}, <span className="mono">{fmtMs(total)}</span>
        </span>
      </div>
      <ol className="sd-trace">
        {steps.map((s, i) => {
          const human = s.agent === "human";
          const right: ReactNode = human ? <span className="mono">{fmtTime(s.at)}</span> : <span className="mono">{s.duration_ms > 0 ? fmtMs(s.duration_ms) : "<1 ms"}</span>;
          return (
            <li key={`${s.seq}-${i}`} className={"sd-step" + (human || s.agent === "escalation" ? " hitl" : "")}>
              <AgentDot agent={s.agent} />
              <div className="sd-step-main">
                <div className="sd-step-head">
                  <span className="sd-step-agent">{agentWord(s.agent)}</span>
                  <span className="sd-step-action">{ACTION_WORD[s.action] ?? humanWords(s.action)}</span>
                </div>
                <p className="sd-step-sum">{s.summary}</p>
                <div className="sd-step-body">
                  <StepBody step={s} titles={titles} />
                </div>
              </div>
              <span className="sd-step-ms">{right}</span>
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
