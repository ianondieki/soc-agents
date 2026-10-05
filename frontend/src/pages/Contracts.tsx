import { useEffect, useState } from "react";
import { FileText, Search } from "lucide-react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import ContractsDrawer from "../components/ContractsDrawer";
import LaneOff from "../components/LaneOff";
import { humanEnum } from "../lib/agents";
import { fmtDate } from "../lib/time";
import "./Contracts.css";

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

/** A seeded sample's title starts "SAMPLE (SYNTHETIC)": that becomes a tag, the rest the title. */
const SAMPLE_PREFIX = /^\s*SAMPLE\s*\(SYNTHETIC\)\s*/i;
function titleOf(title: string): { sample: boolean; text: string } {
  const t = String(title || "");
  return SAMPLE_PREFIX.test(t) ? { sample: true, text: t.replace(SAMPLE_PREFIX, "") } : { sample: false, text: t };
}

/** "vendor-sfc-egypro-fibre" -> "Egypro Fibre": the counterparty as a name; the id stays in the title. */
function vendorWords(id: string): string {
  const parts = String(id || "").split("-").filter(Boolean);
  if (parts[0] === "vendor") parts.shift();
  if (parts.length > 1 && parts[0].length <= 4) parts.shift();
  const out = parts.map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
  return out || id;
}

/** Roles as a person reads them, "MSP coordinator" included. */
function rolesWords(roles: string[]): string {
  return roles.map((r) => roleWords(r).replace(/^msp /, "MSP ")).join(", ");
}

/** Search results shown until asked for the rest. */
const HITS_SHOWN = 8;

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
  const [listed, setListed] = useState(false);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [allHits, setAllHits] = useState(false);
  const [searchNote, setSearchNote] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .contractsStatus()
      .then((s: Status) => {
        if (!live) return;
        setStatus(s);
        setOff(false);
        api
          .contracts()
          .then((r: Contract[]) => live && setRows(Array.isArray(r) ? r : []))
          .catch(() => live && setRows([]))
          .finally(() => live && setListed(true));
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
    setSearching(true);
    try {
      const res = await api.contractsSearch(query, incidentId);
      setHits(res.hits || []);
      setAllHits(false);
      if (res.reason) setSearchNote(res.reason);
    } catch {
      setHits([]);
      setSearchNote("Search failed. The rest of this page is unaffected.");
    } finally {
      setSearching(false);
    }
  };

  const clauses = rows.reduce((sum, c) => sum + (Number(c.clauses) || 0), 0);
  const corpus = status?.corpus;

  return (
    <div className="ct">
      <div className="page-head">
        <div>
          <h1>Contracts</h1>
          <p
            className="lead"
            title="Advisory only: nothing here writes a credit, a penalty or a regulator submission."
          >
            Search the clauses, or ask a question and get an advisory answer that cites them. Only the contracts your role
            may see.
          </p>
        </div>
        {status && (
          <div className="page-actions">
            <span className="ct-role">Viewing as {roleWords(status.role)}</span>
          </div>
        )}
      </div>

      {off && (
        <LaneOff title="The contracts assistant is off in this demo" flag="CONTRACTS_ENABLED">
          search contract clauses and get cited, advisory answers
        </LaneOff>
      )}

      {status && (
        <>
          <dl className="ct-figures">
            <div>
              <dt>Contracts you may see</dt>
              <dd>{corpus?.contracts ?? rows.length}</dd>
            </div>
            <div>
              <dt>Clauses indexed</dt>
              <dd>{listed ? clauses.toLocaleString() : "—"}</dd>
            </div>
            <div className={status.llm.cited_answers ? "ok" : undefined}>
              <dt>Answers</dt>
              <dd className="ct-figure-word">{status.llm.cited_answers ? "Cited by the model" : "Clause list only"}</dd>
            </div>
            <div className={status.fts5_available ? undefined : "bad"}>
              <dt>Clause search</dt>
              <dd className="ct-figure-word">{status.fts5_available ? "Ready" : "Unavailable"}</dd>
            </div>
          </dl>
          <p
            className="ct-how"
            title={`Corpus about ${corpus?.est_tokens.toLocaleString()} tokens (chars/4 estimate) against a ${corpus?.ceiling_tokens.toLocaleString()}-token line. ${
              corpus?.fits_in_prompt
                ? "Below the line: the whole allowed corpus is placed in the prompt for a cited answer; BM25 ranks clauses for the list and the nearest-clause fallback."
                : "Above the line: only the BM25 top-20 clauses are sent to the model."
            }`}
          >
            {status.llm.cited_answers
              ? corpus?.fits_in_prompt
                ? `Your contracts are small enough (about ${corpus.est_tokens.toLocaleString()} tokens) for the model to read them whole and quote the clauses it relies on.`
                : "Your contracts are too large to read whole, so the model reads the twenty clauses that match best and quotes the ones it relies on."
              : `The model is off on this deployment${
                  status.llm.unavailable_reason && status.llm.unavailable_reason !== "disabled"
                    ? ` (${humanEnum(status.llm.unavailable_reason)})`
                    : ""
                }, so a question returns the clauses that match it, ranked, with no judgement in them.`}
          </p>
        </>
      )}

      {!off && (
        <div className="ct-grid">
          <div className="ct-main">
            <ContractsDrawer incidentId={incidentId} defaultOpen flush />

            <section className="panel ct-search" aria-labelledby="ct-search-title">
              <div className="panel-head">
                <h2 id="ct-search-title" className="panel-title">
                  Search the clauses
                </h2>
                {incidentId && <span className="muted">Narrowed to ticket {incidentId.slice(0, 8)}…</span>}
              </div>
              <form
                className="ct-search-row"
                role="search"
                onSubmit={(e) => {
                  e.preventDefault();
                  search();
                }}
              >
                <span className="ct-search-field">
                  <Search size={16} strokeWidth={1.75} aria-hidden="true" />
                  <input
                    aria-label="Search clause text and headings"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Restoration time for a rural site"
                  />
                </span>
                <button className="btn" type="submit" disabled={!q.trim() || searching}>
                  {searching ? "Searching…" : "Search"}
                </button>
              </form>
              <p className="ct-hint" title="Deterministic BM25 over clause text and headings.">
                Words matched against clause text and headings, best first. No model and no judgement.
              </p>
              {searchNote && <p className="ct-hint">{searchNote}</p>}
              {hits && hits.length === 0 && !searchNote && <div className="empty">No clause matched.</div>}
              {hits && hits.length > 0 && (
                <ol className="ct-hits">
                  {(allHits ? hits : hits.slice(0, HITS_SHOWN)).map((h) => {
                    const t = titleOf(h.contract_title);
                    return (
                      <li key={h.clause_id} className="ct-hit">
                        <span className="ct-clause">§{h.clause_number}</span>
                        <div className="ct-hit-main">
                          <p className="ct-hit-head">
                            <strong>{h.heading ? h.heading.replace(/\s*\((SAMPLE|FICTIONAL)[^)]*\)\s*$/i, "") : `Clause ${h.clause_number}`}</strong>
                            <span>{t.text}</span>
                          </p>
                          <p className="ct-hit-text">{h.text.length > 300 ? h.text.slice(0, 299) + "…" : h.text}</p>
                        </div>
                        <span className="ct-rank" title="Rank in this search">
                          {h.rank}
                        </span>
                      </li>
                    );
                  })}
                </ol>
              )}
              {hits && hits.length > HITS_SHOWN && (
                <div className="ct-more">
                  <button className="btn sm" onClick={() => setAllHits((v) => !v)} aria-expanded={allHits}>
                    {allHits ? `Show the best ${HITS_SHOWN}` : `Show all ${hits.length} clauses`}
                  </button>
                </div>
              )}
            </section>
          </div>

          <section className="panel ct-list" aria-labelledby="ct-list-title">
            <div className="panel-head">
              <h2 id="ct-list-title" className="panel-title">
                Contracts you may see
              </h2>
              {listed && <span className="muted">{rows.length}</span>}
            </div>
            {!listed && status && (
              <div className="skeleton-rows" aria-hidden="true">
                <span className="skeleton" />
                <span className="skeleton" />
              </div>
            )}
            {listed && rows.length === 0 && (
              <div className="empty">
                No contract is visible to your role. Legal decides each contract's allowed roles when it is loaded; a duty
                manager, management, legal or an MSP coordinator sees the samples.
              </div>
            )}
            {rows.length > 0 && (
              <ul className="ct-contracts">
                {rows.map((c) => {
                  const t = titleOf(c.title);
                  return (
                    <li key={c.id} className="ct-contract">
                      <div className="ct-contract-head">
                        <span className="ct-doc" aria-hidden="true">
                          <FileText size={18} strokeWidth={1.75} />
                        </span>
                        <div className="ct-contract-title">
                          <h3>{t.text}</h3>
                          <p title={c.counterparty_vendor_id}>
                            {vendorWords(c.counterparty_vendor_id)}
                            {t.sample && <span className="ct-sample">Sample, made up for this demo</span>}
                          </p>
                        </div>
                      </div>
                      <dl className="ct-facts">
                        <div>
                          <dt>In force from</dt>
                          <dd>{c.effective_date ? fmtDate(c.effective_date) : "—"}</dd>
                        </div>
                        <div>
                          <dt>Version</dt>
                          <dd className="mono">{c.version}</dd>
                        </div>
                        <div>
                          <dt>Clauses</dt>
                          <dd>{c.clauses ?? "—"}</dd>
                        </div>
                        <div>
                          <dt>Size</dt>
                          <dd title="Tokens, estimated as characters divided by four">{c.token_count.toLocaleString()} tokens</dd>
                        </div>
                      </dl>
                      <p className="ct-who">
                        <span>Readable by {rolesWords(c.allowed_roles)}.</span>
                        <span className={c.third_party_processing_permitted ? undefined : "local"}>
                          {c.third_party_processing_permitted ? "The hosted model may read it." : "Never sent to a hosted model."}
                        </span>
                      </p>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
