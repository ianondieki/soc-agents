import { useState } from "react";
import { api } from "../api";

/**
 * "Ask the contracts" — the workspace drawer for the contract assistant (spec §7.8).
 *
 * Self-contained on purpose, like `EarlierAtThisSite`: it owns its fetch and its state, so
 * wiring it into the incident workspace is one import and one element
 * (`<ContractsDrawer incidentId={inc.id} />`) and removing it is deleting the same two lines.
 * The Contracts page mounts it too, open by default.
 *
 * What it is careful about:
 *
 * 1. **It never takes the page down.** Every failure lands in a muted line. A 404 is the
 *    flag (`CONTRACTS_ENABLED=false`, the default) and is said in words, not shown as an error.
 * 2. **The label says where the answer came from.** Only `source=faq` is OFFICIAL — a
 *    Legal-approved answer. `llm` is AI-ASSISTED and rendered by the server from verbatim
 *    clause quotes; `deterministic` is a clause list with no judgement in it; `refused` is
 *    "no governing clause found" and is a real outcome, not a failure of the drawer.
 * 3. **The disclosure line is never trimmed.** It comes from the server on every non-FAQ
 *    answer and is shown as-is (§7.8.7).
 * 4. **The scope is the server's.** The drawer sends `question` and `incident_id` only; which
 *    contracts the answer may draw on is decided by the asker's role and the incident's vendor
 *    on the server (`allowed_contracts_for`). There is no contract picker here on purpose.
 *
 * 3 a.m. rules (§7.10): no status carried by colour alone — every chip has words in it.
 */

type Citation = {
  contract_id: string | null;
  contract_title?: string;
  contract_ref?: string | null;
  effective_date?: string | null;
  clause_number: string;
  cited_text: string | null;
};

type NearestClause = {
  clause_id: string;
  contract_id: string;
  contract_title: string;
  clause_number: string;
  heading: string | null;
  text: string;
  rank: number;
};

export type ContractAnswer = {
  query_id: string | null;
  source: "faq" | "llm" | "deterministic" | "refused";
  answer: string;
  citations: Citation[];
  validated: boolean;
  official: boolean;
  escalated_to_legal: boolean;
  nearest_clauses: NearestClause[];
  disclosure: string;
  allowed_contract_ids: string[];
  model: string | null;
  fallback_reason: string | null;
};

const SOURCE_LABEL: Record<ContractAnswer["source"], string> = {
  faq: "OFFICIAL · Legal-approved FAQ",
  llm: "AI-ASSISTED · verbatim quotes, validated",
  deterministic: "CLAUSE LIST · no model consulted",
  refused: "NO GOVERNING CLAUSE · escalate to Legal",
};

function sourceChipClass(source: ContractAnswer["source"]): string {
  if (source === "faq") return "chip ok";
  if (source === "refused") return "chip warn";
  return "chip";
}

export default function ContractsDrawer({
  incidentId,
  defaultOpen = false,
}: {
  incidentId?: string | null;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<ContractAnswer | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const ask = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      const body: Record<string, unknown> = { question: q };
      if (incidentId) body.incident_id = incidentId;
      const res = (await api.contractsAsk(body)) as ContractAnswer;
      setAnswer(res);
    } catch (e: any) {
      const msg = String(e?.message || e);
      // Console only; a failed advisory read never raises a toast or touches other state.
      setAnswer(null);
      setProblem(
        msg.startsWith("404")
          ? "Contracts assistant is off on this deployment (CONTRACTS_ENABLED=false). Nothing was asked."
          : msg.startsWith("403")
            ? "Your role may not ask the contracts assistant."
            : "The contracts assistant could not be reached. The rest of this page is unaffected."
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel" style={{ marginTop: "1rem" }}>
      <div className="panel-head">
        <h3>Ask the contracts</h3>
        {incidentId && <span className="chip" title="Answers are narrowed to this incident's vendor">incident scope</span>}
        <button className="btn" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide" : "Open"}
        </button>
      </div>

      {open && (
        <>
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. What is the response time for a Priority 1 fault?"
            maxLength={2000}
          />
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginTop: "0.5rem" }}>
            <button className="btn primary" onClick={ask} disabled={busy || !question.trim()}>
              {busy ? "Asking…" : "Ask"}
            </button>
            <span className="muted">
              Advisory. Which contracts may answer is decided by your role{incidentId ? " and this incident's vendor" : ""} — never by this form.
            </span>
          </div>

          {problem && (
            <p className="muted" style={{ marginTop: "0.75rem" }}>
              {problem}
            </p>
          )}

          {answer && (
            <div style={{ marginTop: "0.9rem" }}>
              <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
                <span className={sourceChipClass(answer.source)}>{SOURCE_LABEL[answer.source]}</span>
                {answer.escalated_to_legal && <span className="chip warn">ESCALATED TO LEGAL</span>}
                {answer.model && <span className="chip">{answer.model}</span>}
                {answer.fallback_reason && answer.source !== "llm" && (
                  <span className="chip" title="Why no cited model answer was produced">
                    reason: {answer.fallback_reason}
                  </span>
                )}
              </div>

              <div className="pre">{answer.answer}</div>

              {answer.citations.length > 0 && (
                <div style={{ marginTop: "0.6rem" }}>
                  <div className="muted" style={{ marginBottom: "0.3rem" }}>
                    Citations ({answer.citations.length})
                  </div>
                  <div className="list" style={{ maxHeight: 260 }}>
                    {answer.citations.map((c, i) => (
                      <div key={`${c.contract_id}-${c.clause_number}-${i}`} className="row" style={{ cursor: "default" }}>
                        <span className="chip">§{c.clause_number}</span>
                        <div>
                          <div>{c.contract_title || c.contract_ref || c.contract_id}</div>
                          {c.cited_text && <div className="muted">“{c.cited_text}”</div>}
                        </div>
                        <span className="muted">{c.effective_date || ""}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {answer.source === "refused" && answer.nearest_clauses.length > 0 && (
                <div style={{ marginTop: "0.6rem" }}>
                  <div className="muted" style={{ marginBottom: "0.3rem" }}>
                    Nearest clauses — not an answer, a place to start reading
                  </div>
                  <div className="list" style={{ maxHeight: 260 }}>
                    {answer.nearest_clauses.map((n) => (
                      <div key={n.clause_id} className="row" style={{ cursor: "default" }}>
                        <span className="chip">§{n.clause_number}</span>
                        <div>
                          <div>{n.contract_title}</div>
                          <div className="muted">{n.text.length > 220 ? n.text.slice(0, 219) + "…" : n.text}</div>
                        </div>
                        <span className="muted">#{n.rank}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* The server puts the disclosure on every non-FAQ answer; the FAQ carries its approver instead. */}
              <p className="muted" style={{ marginTop: "0.6rem" }}>
                {answer.disclosure}
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
