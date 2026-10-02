import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import ContractsDrawer from "../components/ContractsDrawer";
import LaneOff from "../components/LaneOff";
import { humanEnum } from "../lib/agents";

/** "duty_manager" → "duty manager": a role id as a person reads it. */
function roleWords(value: string): string {
  return humanEnum(String(value || "").toUpperCase());
}

/**
 * Contracts page (spec §7.8, Phase 5 Lane 5B). Three things, in the order a reader needs them:
 *
 * 1. **Status** — is the lane on, how big is the corpus (the §7.8 measurement, with the
 *    prompt-stuffing verdict in words), and will an ask produce a cited model answer or the
 *    deterministic clause list, and why.
 * 2. **Ask** — the same drawer the incident workspace mounts, open by default here. A
 *    `?incident=<id>` in the URL narrows the ask to that incident's vendor (server-side).
 * 3. **Clause search** and the **contract list** the current role may see. Both are scoped by
 *    the server; the page has no contract picker because the scope is not the asker's to choose.
 *
 * With `CONTRACTS_ENABLED=false` (the default) every route is 404 and the page says so in one
 * line — "off" must never look like "no contracts" (§7.10, the 3 a.m. rules).
 */

type Contract = {
  id: string;
  title: string;
  counterparty_vendor_id: string;
  effective_date: string | null;
  version: string;
  allowed_roles: string[];
  third_party_processing_permitted: boolean;
  token_count: number;
  clauses: number | null;
};

type Status = {
  enabled: boolean;
  role: string;
  allowed_contract_ids: string[];
  corpus: { contracts: number; est_tokens: number; ceiling_tokens: number; fits_in_prompt: boolean };
  fts5_available: boolean;
  llm: { cited_answers: boolean; provider: string; unavailable_reason: string | null; model: string };
  disclosure: string;
};

type Hit = {
  clause_id: string;
  contract_title: string;
  clause_number: string;
  heading: string | null;
  text: string;
  rank: number;
  score: number;
};

export default function Contracts() {
  const loc = useLocation();
  const incidentId = new URLSearchParams(loc.search).get("incident");
  const [status, setStatus] = useState<Status | null>(null);
  const [off, setOff] = useState(false);
  const [rows, setRows] = useState<Contract[]>([]);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [searchNote, setSearchNote] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .contractsStatus()
      .then((s: Status) => {
        if (!live) return;
        setStatus(s);
        setOff(false);
        api.contracts().then((r: Contract[]) => live && setRows(r)).catch(() => live && setRows([]));
      })
      .catch((e: any) => {
        if (!live) return;
        setStatus(null);
        setOff(String(e?.message || "").startsWith("404"));
      });
    return () => {
      live = false;
    };
  }, []);

  const search = async () => {
    const query = q.trim();
    if (!query) return;
    setSearchNote(null);
    try {
      const res = await api.contractsSearch(query, incidentId);
      setHits(res.hits || []);
      if (res.reason) setSearchNote(res.reason);
    } catch {
      setHits([]);
      setSearchNote("Search failed. The rest of this page is unaffected.");
    }
  };

  return (
    <div className="content-narrow">
      <div className="page-head">
        <div>
          <h1>Contracts</h1>
          <p
            className="lead"
            title="Advisory only: nothing here writes a credit, a penalty or a regulator submission."
          >
            Clause search and cited, advisory answers from the contracts you may see.
          </p>
        </div>
      </div>

      {off && (
        <LaneOff title="The contracts assistant is off in this demo" flag="CONTRACTS_ENABLED">
          search contract clauses and get cited, advisory answers
        </LaneOff>
      )}

      {status && (
        <div className="panel" style={{ marginBottom: "1rem" }}>
          <div className="panel-head">
            <h2 className="panel-title">Status</h2>
            <div className="facts">
              <span>Role: {roleWords(status.role)}</span>
              {status.llm.cited_answers ? (
                <span>Cited answers from {status.llm.model}</span>
              ) : (
                <span>Clause list only: {status.llm.unavailable_reason || "no citations provider"}</span>
              )}
              {status.fts5_available ? <span>FTS5 index</span> : <span className="chip danger">FTS5 unavailable</span>}
            </div>
          </div>
          <p className="muted">
            Corpus you may see: {status.corpus.contracts} contract{status.corpus.contracts === 1 ? "" : "s"}, about{" "}
            {status.corpus.est_tokens.toLocaleString()} tokens (chars/4 estimate) against a {status.corpus.ceiling_tokens.toLocaleString()}-token
            line.{" "}
            {status.corpus.fits_in_prompt
              ? "Below the line: the whole allowed corpus is placed in the prompt for a cited answer; BM25 ranks clauses for the list and the nearest-clause fallback."
              : "Above the line: only the BM25 top-20 clauses are sent to the model."}
          </p>
        </div>
      )}

      {!off && <ContractsDrawer incidentId={incidentId} defaultOpen />}

      {!off && (
        <div className="panel" style={{ marginTop: "1rem" }}>
          <div className="panel-head">
            <h2 className="panel-title">Clause search</h2>
            {incidentId && <span className="muted">Narrowed to incident {incidentId.slice(0, 8)}…</span>}
          </div>
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <input
              aria-label="Search clause text and headings"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && search()}
              placeholder="restoration time rural site…"
              style={{ flex: 1 }}
            />
            <button className="btn" onClick={search} disabled={!q.trim()}>
              Search
            </button>
          </div>
          <p className="muted" style={{ marginTop: "0.4rem" }}>
            Deterministic BM25 over clause text and headings. No model, no judgement — a ranked list, nothing more.
          </p>
          {searchNote && <p className="muted">{searchNote}</p>}
          {hits && hits.length === 0 && !searchNote && <div className="empty">No clause matched.</div>}
          {hits && hits.length > 0 && (
            <div className="list">
              {hits.map((h) => (
                <div key={h.clause_id} className="row" style={{ cursor: "default" }}>
                  <span className="mono">§{h.clause_number}</span>
                  <div>
                    <div className="head-row">
                      <strong>{h.contract_title}</strong>
                      {h.heading ? <span className="muted">{h.heading}</span> : null}
                    </div>
                    <div className="muted">{h.text.length > 300 ? h.text.slice(0, 299) + "…" : h.text}</div>
                  </div>
                  <span className="muted">#{h.rank}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {!off && (
        <div className="panel" style={{ marginTop: "1rem" }}>
          <h2 className="panel-title">Contracts you may see</h2>
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Vendor</th>
                <th>Effective</th>
                <th>Version</th>
                <th>Clauses</th>
                <th>Tokens (est.)</th>
                <th>Roles</th>
                <th>Hosted model</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id}>
                  <td>{c.title}</td>
                  <td className="muted">{c.counterparty_vendor_id}</td>
                  <td>{c.effective_date || "—"}</td>
                  <td>{c.version}</td>
                  <td>{c.clauses ?? "—"}</td>
                  <td>{c.token_count.toLocaleString()}</td>
                  <td className="muted">{c.allowed_roles.map(roleWords).join(", ")}</td>
                  <td>{c.third_party_processing_permitted ? "permitted" : "local only"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && (
            <div className="empty">
              No contract is visible to your role. Legal decides each contract's allowed roles at ingest.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
