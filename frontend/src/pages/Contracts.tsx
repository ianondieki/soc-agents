import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import ContractsDrawer from "../components/ContractsDrawer";

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
    <div>
      <h2 style={{ marginTop: 0 }}>Contracts</h2>
      <p className="muted">
        Clause search and cited advisory answers over the contracts your role may see. Advisory only: nothing here writes a
        credit, a penalty or a regulator submission.
      </p>

      {off && (
        <div className="panel" style={{ marginBottom: "1rem" }}>
          <div className="panel-head">
            <h3>Contracts assistant is off</h3>
            <span className="chip">CONTRACTS_ENABLED=false</span>
          </div>
          <p className="muted">
            This deployment has not enabled the contracts lane, so no contract is indexed and nothing can be asked. This is
            the shipped default, not an empty corpus.
          </p>
        </div>
      )}

      {status && (
        <div className="panel" style={{ marginBottom: "1rem" }}>
          <div className="panel-head">
            <h3>Status</h3>
            <span className="chip ok">ENABLED</span>
            <span className="chip">{status.role}</span>
            <span className={"chip " + (status.llm.cited_answers ? "ok" : "")}>
              {status.llm.cited_answers ? `cited answers · ${status.llm.model}` : `clause list only · ${status.llm.unavailable_reason || "no citations provider"}`}
            </span>
            <span className={"chip " + (status.fts5_available ? "" : "bad")}>{status.fts5_available ? "FTS5 index" : "FTS5 unavailable"}</span>
          </div>
          <p className="muted">
            Corpus you may see: {status.corpus.contracts} contract{status.corpus.contracts === 1 ? "" : "s"}, ≈
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
            <h3>Clause search</h3>
            {incidentId && <span className="chip">narrowed to incident {incidentId.slice(0, 8)}…</span>}
          </div>
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <input
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
                  <span className="chip">§{h.clause_number}</span>
                  <div>
                    <div>
                      <strong>{h.contract_title}</strong>
                      {h.heading ? ` · ${h.heading}` : ""}
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
          <h3>Contracts you may see</h3>
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Vendor</th>
                <th>Effective</th>
                <th>Version</th>
                <th>Clauses</th>
                <th>≈ tokens</th>
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
                  <td className="muted">{c.allowed_roles.join(", ")}</td>
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
