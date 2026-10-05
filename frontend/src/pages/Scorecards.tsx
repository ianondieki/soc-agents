import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Calculator, ChevronDown, RefreshCw } from "lucide-react";
import LaneOff from "../components/LaneOff";
import VendorPeriodPicker, { type PickerValue } from "../components/VendorPeriodPicker";
import ScorecardDetail from "../components/ScorecardDetail";
import { scorecardApi } from "../components/scorecardApi";
import {
  failureView,
  gateView,
  hiddenStatusesNote,
  isPeriod,
  mayCompute,
  periodWords,
  statusView,
  termsView,
  type ComputeResult,
  type FailureView,
  type Scorecard,
  type Vendor,
} from "../components/scorecardModel";
import { humanEnum } from "../lib/agents";
import { detailOf, statusOf } from "../lib/apiError";
import { IconCheck } from "../lib/icons";
import { fmtDate, fmtHM } from "../lib/time";
import "./Scorecards.css";

/**
 * `ScorecardsPage` (spec §7.10, §7.6): vendor scorecards as evidence, not verdicts.
 *
 * A card is a commercial document. Supply Chain puts its numbers in front of a vendor, so this
 * page shows every number with its argument: the formula, the contract term it was measured
 * against, the incidents left out and why, and the stop-clock minutes taken off. Raw and
 * normalised values sit side by side and are never swapped.
 *
 * What the page is careful about:
 *
 * 1. **Off, forbidden, empty and not-found each look different.** The whole lane answers 404
 *    while `SCORECARDS_ENABLED` is off (the default). The page says the lane is off and how
 *    to turn it on, as `pages/Pirs.tsx` does. A 403 is "not available to your role". An empty
 *    list explains which statuses this role is shown. A 404 on one card is "not found, or not
 *    visible to your role": the server uses 404, never 403, for a card a role may not see.
 * 2. **Who sees what is the server's decision.** SHADOW, DRAFT and WITHHELD are visible to
 *    duty_manager, management and admin only (§7.6.2). The page does not filter by role
 *    itself; it explains what the server returned. `/api/v1/session` reports the demo role
 *    switcher, which is the principal only while `AUTH_DISABLED=true`.
 * 3. **Nothing here sends anything.** Compute makes DRAFT / SHADOW / WITHHELD cards. Publish
 *    releases a card and starts its dispute window. No route on this surface notifies anyone.
 * 4. **Disputes are not built** (CONFORMANCE C-02). The dispute control is shown disabled,
 *    with the reason, rather than wired to an endpoint that does not exist.
 *
 * No WS event exists for scorecards, so the list refetches on filter changes (debounced
 * ~500 ms, §7.10), after an action on this page, and on demand. The open card refetches with
 * it: `reloadTick` is passed to `ScorecardDetail` as `tick`, so Compute, Refresh, an action,
 * and a click on the row that is already open all refetch the card. A recompute keeps the
 * card's id, so without this the open card would keep showing its old status.
 */

const DEBOUNCE_MS = 500;

/** "duty_manager" → "duty manager": a role id as a person reads it. */
function roleWords(value: string): string {
  return humanEnum(String(value || "").toUpperCase());
}

type Session = { display_name?: string; role?: string } | null;

export default function Scorecards({ session }: { session: Session }) {
  const role = (session?.role || "").trim();
  const [filters, setFilters] = useState<PickerValue>({ vendor: "", period: "", status: "ALL" });
  const [rows, setRows] = useState<Scorecard[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [listFailure, setListFailure] = useState<{ view: FailureView; detail: string } | null>(null);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  const [computePeriod, setComputePeriod] = useState("");
  const [computeVendor, setComputeVendor] = useState("");
  const [computing, setComputing] = useState(false);
  const [computeResult, setComputeResult] = useState<ComputeResult | null>(null);
  const [computeFailure, setComputeFailure] = useState<{ view: FailureView; detail: string } | null>(null);

  const seq = useRef(0);
  /** True for the mount load and for an explicit reload; a filter change is debounced. */
  const immediate = useRef(true);

  const reload = useCallback(() => {
    immediate.current = true;
    setReloadTick((t) => t + 1);
  }, []);

  // The list: at once on mount and on reload, debounced on filter changes. `immediate` is
  // cleared only when the timer fires, so StrictMode's dev-time double effect does not turn
  // the mount load into a debounced one. A response that arrives after a newer request was
  // issued is dropped, so a slow answer cannot overwrite a fast one.
  useEffect(() => {
    const delay = immediate.current ? 0 : DEBOUNCE_MS;
    const mine = ++seq.current;
    const timer = window.setTimeout(() => {
      immediate.current = false;
      scorecardApi
        .list({ vendor: filters.vendor, period: filters.period, status: filters.status })
        .then((list) => {
          if (mine !== seq.current) return;
          setRows(Array.isArray(list) ? list : []);
          setListFailure(null);
          setLoaded(true);
        })
        .catch((e) => {
          if (mine !== seq.current) return;
          // Drop the previous rows: they answered other filters and must not pass for these.
          setRows([]);
          const detail = detailOf(e, "");
          setListFailure({ view: failureView(statusOf(e), "list", detail), detail });
          setLoaded(true);
        });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [filters, reloadTick]);

  // Vendor names for the picker. Best effort: when this fails (lane off, or a role outside
  // READERS once auth is on), the picker falls back to the vendor codes in the list.
  useEffect(() => {
    scorecardApi
      .vendors()
      .then((v) => setVendors(Array.isArray(v) ? v : []))
      .catch(() => setVendors([]));
  }, []);

  const vendorOptions = useMemo(() => {
    const byCode = new Map<string, string>();
    for (const v of vendors) if (v.code) byCode.set(v.code, v.display_name || v.code);
    for (const r of rows) if (r.vendor_code && !byCode.has(r.vendor_code)) byCode.set(r.vendor_code, r.vendor_name || r.vendor_code);
    return [...byCode.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([code, name]) => ({ code, name }));
  }, [vendors, rows]);

  const compute = async () => {
    setComputing(true);
    setComputeResult(null);
    setComputeFailure(null);
    try {
      const res = await scorecardApi.compute({ period: computePeriod, vendor: computeVendor });
      setComputeResult(res);
      reload();
    } catch (e) {
      const detail = detailOf(e, "");
      setComputeFailure({ view: failureView(statusOf(e), "compute", detail), detail });
    } finally {
      setComputing(false);
    }
  };

  /** Open a card; a click on the card that is already open refetches it (and the list). */
  const openCard = (id: string) => {
    if (id === selected) reload();
    else setSelected(id);
  };

  const heading = (
    <div className="page-head">
      <div>
        <h1>Vendor scorecards</h1>
        <p
          className="lead"
          title="Every line carries its formula and the contract term it was measured against, and raw and normalised values sit side by side. Nothing on this page sends anything to a vendor."
        >
          Each vendor's month against its terms. Evidence, not verdicts: every figure shows its formula. Times in EAT.
        </p>
      </div>
      <div className="page-actions">
        <span className="sc-role">Viewing as {role ? roleWords(role) : "unknown role"}</span>
      </div>
    </div>
  );

  // ---- whole-page states: off, sign in, not available to this role -------------------------
  if (listFailure && (listFailure.view.kind === "off" || listFailure.view.kind === "role" || listFailure.view.kind === "signin")) {
    const off = listFailure.view.kind === "off";
    if (off) {
      return (
        <div>
          {heading}
          <LaneOff title="Vendor scorecards are off in this demo" flag="SCORECARDS_ENABLED">
            score each vendor's month against its contract terms, line by line
          </LaneOff>
        </div>
      );
    }
    return (
      <div>
        {heading}
        <div className="panel">
          <h2 className="panel-title">{listFailure.view.title}</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            {listFailure.view.body} Your role here is <strong>{role ? roleWords(role) : "unknown"}</strong>.
          </p>
          {listFailure.detail && <div className="pre">{listFailure.detail}</div>}
        </div>
      </div>
    );
  }

  const hiddenNote = hiddenStatusesNote(role);
  const statusCount = (...wanted: string[]) => rows.filter((r) => wanted.includes(String(r.status || "").toUpperCase())).length;
  const released = statusCount("PUBLISHED", "FINAL");
  const toReview = statusCount("DRAFT", "SHADOW");
  const withheld = statusCount("WITHHELD");
  const latest = rows.reduce((max, r) => (isPeriod(r.period) && r.period > max ? r.period : max), "");
  const filtered = Boolean(filters.vendor || filters.period || filters.status !== "ALL");

  return (
    <div className="sc">
      {heading}

      {loaded && !listFailure && (
        <dl className="sc-figures">
          <div>
            <dt>{filtered ? "Cards shown" : "Cards"}</dt>
            <dd>{rows.length}</dd>
          </div>
          <div className={released ? "ok" : undefined}>
            <dt>Released to vendors</dt>
            <dd>{released}</dd>
          </div>
          <div className={toReview ? "hitl" : undefined}>
            <dt>Waiting for a person</dt>
            <dd>{toReview}</dd>
          </div>
          <div className={withheld ? "bad" : undefined}>
            <dt>Withheld by the gate</dt>
            <dd>{withheld}</dd>
          </div>
          <div>
            <dt>Latest month</dt>
            <dd className="sc-figure-word">{latest ? periodWords(latest) : "—"}</dd>
          </div>
        </dl>
      )}

      <div className="panel sc-toolbar">
        <div className="sc-toolbar-row">
          <VendorPeriodPicker vendors={vendorOptions} value={filters} onChange={setFilters} />
          <button className="btn sc-refresh" onClick={reload} title="Fetch the cards again">
            <RefreshCw size={15} strokeWidth={1.75} aria-hidden="true" />
            Refresh
          </button>
        </div>
        {mayCompute(role) && (
          <div className="sc-compute">
            <span className="sc-compute-title" id="sc-compute-title">
              Compute an ended month
            </span>
            <div className="sc-compute-row" role="group" aria-labelledby="sc-compute-title">
              <label>
                <span>Month</span>
                <input
                  type="month"
                  value={computePeriod}
                  disabled={computing}
                  onChange={(e) => setComputePeriod(e.target.value)}
                  title="Blank: the last month that has ended"
                />
              </label>
              <label>
                <span>Vendor</span>
                <select value={computeVendor} disabled={computing} onChange={(e) => setComputeVendor(e.target.value)}>
                  <option value="">Every vendor with tickets</option>
                  {vendorOptions.map((v) => (
                    <option key={v.code} value={v.code}>
                      {v.name && v.name !== v.code ? v.name : v.code}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="btn"
                disabled={computing || (computePeriod !== "" && !isPeriod(computePeriod))}
                onClick={compute}
                title="Computes draft, shadow or withheld cards for an ended month. It cannot publish, and it refuses (409) a card that is already published or final."
              >
                <Calculator size={15} strokeWidth={1.75} aria-hidden="true" />
                {computing ? "Computing…" : "Compute"}
              </button>
              <span className="sc-compute-note">Makes working papers only; it never publishes or sends anything.</span>
            </div>
          </div>
        )}
        {computeResult && (
          <div className="sc-result" role="status">
            <IconCheck /> {periodWords(computeResult.period)}: {computeResult.computed} computed, {computeResult.skipped} skipped.
            {computeResult.computed_detail && computeResult.computed_detail.length > 0 && (
              <div>Computed: {computeResult.computed_detail.map((x) => x.replace(":", " ")).join(", ")}.</div>
            )}
            {!computeResult.computed_detail && (
              <div>Your role is told the counts only. Which vendor came out shadow or withheld is visible to duty managers, management and admins.</div>
            )}
            <div className="sc-result-run">
              Run <span className="mono">{computeResult.run_id}</span>
            </div>
          </div>
        )}
        {computeFailure && (
          <div className="sc-alert" role="status">
            <span className="chip warn">{computeFailure.view.title}</span>
            <div>
              <div>{computeFailure.view.body}</div>
              {computeFailure.detail && <div className="pre">{computeFailure.detail}</div>}
            </div>
          </div>
        )}
      </div>

      {listFailure && (
        <div className="sc-alert" role="status">
          <span className="chip warn">{listFailure.view.title}</span>
          <div>
            <div>{listFailure.view.body}</div>
            {listFailure.detail && <div className="pre">{listFailure.detail}</div>}
          </div>
        </div>
      )}

      <section className="panel sc-panel" aria-labelledby="sc-cards">
        <div className="panel-head">
          <h2 id="sc-cards" className="panel-title">
            Cards
          </h2>
          <span className="muted">Open one for its lines, formulas and evidence</span>
        </div>

        {!loaded && (
          <div className="skeleton-rows" aria-hidden="true">
            <span className="skeleton" />
            <span className="skeleton" />
            <span className="skeleton" />
          </div>
        )}

        {rows.length > 0 && (
          <ul className="sc-list">
            {rows.map((r) => {
              const sv = statusView(r.status);
              const gate = gateView(r.data_quality);
              const terms = termsView(r);
              const open = selected === r.id;
              const st = String(r.status || "").toLowerCase();
              return (
                <li key={r.id} className={"sc-row" + (open ? " open" : "")}>
                  <button
                    type="button"
                    className="sc-row-btn"
                    aria-expanded={open}
                    aria-controls={open ? "sc-detail" : undefined}
                    onClick={() => openCard(r.id)}
                  >
                    <span className="sc-vendor">
                      <span className="sc-vendor-name">{r.vendor_name || r.vendor_code || r.vendor_id}</span>
                      <span className="sc-vendor-sub">
                        {r.vendor_name && r.vendor_code && r.vendor_name !== r.vendor_code && <span className="mono">{r.vendor_code}</span>}
                        <span>{periodWords(r.period)}</span>
                      </span>
                    </span>
                    <span className="sc-status">
                      <span className={`sc-pill ${st}`}>{sentence(humanEnum(sv.label))}</span>
                      <span className="sc-status-tag">{sentence(humanEnum(sv.tag))}</span>
                    </span>
                    <span className={"sc-gate" + (gate.passed === false ? " bad" : gate.passed == null ? " warn" : "")}>
                      <span className="sc-gate-word">
                        {gate.passed === true ? "Gate passed" : gate.passed === false ? "Gate failed" : "Gate not recorded"}
                      </span>
                      <span className="sc-gate-sub">
                        {gate.inferred} of {gate.restored} restores inferred, limit {gate.threshold}
                      </span>
                    </span>
                    <span className={"sc-terms" + (terms.kind === "CONTRACT" ? "" : " warn")}>
                      {terms.kind === "CONTRACT" ? "Contract terms" : terms.kind === "DEFAULTS" ? "Default terms, no contract" : "Terms not recorded"}
                    </span>
                    <span className="sc-when">
                      <span>{r.dispute_window_ends_at ? `Disputes close ${when(r.dispute_window_ends_at)}` : "No dispute window yet"}</span>
                      <span className="sc-when-sub">Computed {when(r.computed_at)}</span>
                    </span>
                    <ChevronDown className="sc-chev" size={18} strokeWidth={1.75} aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        {loaded && rows.length === 0 && !listFailure && (
          <div className="empty sc-empty">
            <p>{filtered ? "No scorecards match these filters." : "No scorecards yet."}</p>
            {filtered && (
              <button className="btn sm" onClick={() => setFilters({ vendor: "", period: "", status: "ALL" })}>
                Clear filters
              </button>
            )}
            {hiddenNote ? <p>{hiddenNote}</p> : null}
            <p>
              Cards are computed for each ended month (EAT). The hourly <code>scorecard_close</code> job does this when the
              scheduler is on, or a shift supervisor and above can compute a month above.
            </p>
          </div>
        )}
        {loaded && rows.length > 0 && hiddenNote && <p className="muted sc-hidden-note">{hiddenNote}</p>}
      </section>

      {selected && (
        <div id="sc-detail" className="sc-detail">
          <ScorecardDetail key={selected} cardId={selected} tick={reloadTick} session={session} onChanged={reload} />
        </div>
      )}
    </div>
  );
}

const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
/** "20 Oct 2026, 00:00" in EAT: the day and the minute, without seconds. */
const when = (v: unknown) => `${fmtDate(v)}, ${fmtHM(v)}`;
