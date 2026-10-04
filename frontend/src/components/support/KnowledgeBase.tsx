import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Search } from "lucide-react";
import { CATEGORY_WORD, errorDetail, supportApi, type KbArticle, type KbHit } from "../../lib/support";
import { fmtDate } from "../../lib/time";
import Markdownish from "./Markdownish";

/**
 * The knowledge base the resolver answers from: search on the left (BM25 on the server, the
 * same ranking the resolver uses, with each hit's score), the article on the right, read as
 * text. With nothing typed the whole base is listed by category.
 */
export default function KnowledgeBase({ stacked, articles, state, error, onRetry }: { stacked: boolean; articles: KbArticle[]; state: "loading" | "ok" | "error"; error: string | null; onRetry: () => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [open, setOpen] = useState(false); // phone: the article replaces the list
  const timer = useRef(0);
  const asked = useRef(0);

  useEffect(() => {
    window.clearTimeout(timer.current);
    const term = q.trim();
    if (!term) {
      setHits(null);
      setSearchError(null);
      return;
    }
    timer.current = window.setTimeout(() => {
      const mine = ++asked.current;
      supportApi
        .kbSearch(term)
        .then((r) => {
          if (mine !== asked.current) return;
          setHits(Array.isArray(r?.results) ? r.results : []);
          setSearchError(null);
        })
        .catch((e) => {
          if (mine !== asked.current) return;
          setSearchError(errorDetail(e, "The search failed."));
        });
    }, 220);
    return () => window.clearTimeout(timer.current);
  }, [q]);

  const byId = useMemo(() => Object.fromEntries(articles.map((a) => [a.id, a])), [articles]);
  const sorted = useMemo(
    () => [...articles].sort((a, b) => (a.category === b.category ? a.title.localeCompare(b.title) : a.category.localeCompare(b.category))),
    [articles]
  );
  const current = selected ? byId[selected] : !stacked && sorted.length ? sorted[0] : null;
  const pick = (id: string) => {
    setSelected(id);
    setOpen(true);
  };
  const showList = !stacked || !open || !current;
  const showArticle = !stacked || (open && current);

  return (
    <div className={"sd-split sd-kb" + (showArticle && stacked ? " has-case" : "")}>
      {showList && (
        <div className="panel sd-list-col">
          <div className="sd-filters">
            <label className="sd-search">
              <Search size={16} strokeWidth={1.75} aria-hidden="true" />
              <span className="sr-only">Search the knowledge base</span>
              <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search articles" autoComplete="off" />
            </label>
          </div>
          {state === "loading" && (
            <div className="skeleton-rows" aria-hidden="true">
              {["70%", "55%", "64%", "48%", "60%"].map((w, i) => (
                <span key={i} className="skeleton" style={{ width: w }} />
              ))}
            </div>
          )}
          {state === "error" && (
            <div className="empty" role="alert">
              Couldn't load the knowledge base. {error}
              <button type="button" className="btn sm" onClick={onRetry}>
                Retry
              </button>
            </div>
          )}
          {state === "ok" && hits && hits.length === 0 && !searchError && <div className="empty">No article matches “{q.trim()}”. Try the customer's own words, in English or Kiswahili.</div>}
          {searchError && (
            <div className="empty" role="alert">
              {searchError}
            </div>
          )}
          {state === "ok" && !searchError && (
            <ol className="sd-rows" aria-label={hits ? `Articles matching ${q.trim()}` : "All articles"}>
              {(hits ? hits.map((h) => ({ id: h.article_id, title: h.title, category: byId[h.article_id]?.category, snippet: h.snippet, score: h.score })) : sorted.map((a) => ({ id: a.id, title: a.title, category: a.category, snippet: a.summary, score: null as number | null }))).map((r) => (
                <li key={r.id}>
                  <button type="button" className="sd-row sd-kb-row" aria-current={current?.id === r.id ? "true" : undefined} onClick={() => pick(r.id)}>
                    <span className="sd-row-subject">{r.title}</span>
                    {r.score != null && (
                      <span className="sd-row-age mono" title="BM25 score">
                        {r.score.toFixed(1)}
                      </span>
                    )}
                    <span className="sd-row-facts">
                      <span>{r.category ? CATEGORY_WORD[r.category] ?? r.category : ""}</span>
                      <span className="mono">{r.id}</span>
                    </span>
                    {r.snippet && <span className="sd-row-why">{r.snippet}</span>}
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      {showArticle && current && (
        <article className="panel sd-article" aria-labelledby="sd-kb-title">
          {stacked && (
            <div className="sd-back">
              <button type="button" className="btn ghost sm" onClick={() => setOpen(false)}>
                <ArrowLeft size={16} strokeWidth={1.75} aria-hidden="true" />
                Back to the articles
              </button>
            </div>
          )}
          <h2 id="sd-kb-title" className="sd-article-title">
            {current.title}
          </h2>
          <div className="facts sd-article-facts">
            <span className="mono">{current.id}</span>
            <span>{CATEGORY_WORD[current.category] ?? current.category}</span>
            <span>updated {fmtDate(current.updated_at)}</span>
          </div>
          {current.summary && <p className="sd-article-summary">{current.summary}</p>}
          <Markdownish className="sd-md" text={current.body} />
        </article>
      )}
      {showArticle && !current && state === "ok" && !stacked && (
        <div className="panel sd-article">
          <div className="empty">Pick an article to read it here.</div>
        </div>
      )}
    </div>
  );
}
