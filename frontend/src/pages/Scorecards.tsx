import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { detailOf, statusOf } from "../lib/apiError";
import { fmtDateTime } from "../lib/time";

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
 * ~500 ms, §7.10), after an action on this page, and on demand.
 */

const DEBOUNCE_MS = 500;

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
          setListFailure({ view: failureView(statusOf(e), "list"), detail: detailOf(e, "") });
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
      setComputeFailure({ view: failureView(statusOf(e), "action"), detail: detailOf(e, "") });
    } finally {
      setComputing(false);
    }
  };

  const heading = (
    <div style={{ display: "flex", gap: "0.6rem", alignItems: "baseline", flexWrap: "wrap" }}>
      <h2 style={{ margin: 0 }}>Vendor scorecards</h2>
      <span className="chip">VIEWING AS · {role || "unknown role"}</span>
    </div>
  );

  // ---- whole-page states: off, sign in, not available to this role -------------------------
  if (listFailure && (listFailure.view.kind === "off" || listFailure.view.kind === "role" || listFailure.view.kind === "signin")) {
    const off = listFailure.view.kind === "off";
    return (
      <div>
        {heading}
        <div className="panel" style={{ marginTop: "0.9rem" }}>
          <div className="panel-head">
            <h3>{listFailure.view.title}</h3>
            <span className="chip">{off ? "LANE OFF" : listFailure.view.title.toUpperCase()}</span>
          </div>
          {off ? (
            <div className="empty">
              Vendor scorecards are not enabled on this deployment. Set <code>SCORECARDS_ENABLED=true</code> in{" "}
              <code>.env</code> and restart the API. With the flag off, the whole lane is invisible by design: every{" "}
              <code>/scorecards</code> route answers 404 and the <code>scorecard_close</code> job does nothing.
            </div>
          ) : (
            <>
              <p className="muted" style={{ marginTop: 0 }}>
                {listFailure.view.body} Your role here is <strong>{role || "unknown"}</strong>.
              </p>
              {listFailure.detail && <div className="pre">{listFailure.detail}</div>}
            </>
          )}
        </div>
      </div>
    );
  }

  const hiddenNote = hiddenStatusesNote(role);

  return (
    <div>
      {heading}
      <p className="muted">
        A scorecard is evidence, not a verdict. Every line carries its formula and the contract term it was measured
        against, and raw and normalised values sit side by side. Nothing on this page sends anything to a vendor. Times
        are EAT.
      </p>

      <div className="panel" style={{ marginBottom: "1rem" }}>
        <VendorPeriodPicker vendors={vendorOptions} value={filters} onChange={setFilters} />
        <div className="form-row" style={{ marginTop: "0.6rem", marginBottom: 0 }}>
          <button className="btn" onClick={reload}>
            Refresh
          </button>
          {mayCompute(role) && (
            <>
              <span style={{ flex: 1 }} />
              <label className="muted">
                Compute{" "}
                <input
                  type="month"
                  value={computePeriod}
                  disabled={computing}
                  onChange={(e) => setComputePeriod(e.target.value)}
                  title="Blank: the last month that has ended"
                />
              </label>
              <select value={computeVendor} disabled={computing} onChange={(e) => setComputeVendor(e.target.value)}>
                <option value="">every vendor with incidents</option>
                {vendorOptions.map((v) => (
                  <option key={v.code} value={v.code}>
                    {v.code}
                  </option>
                ))}
              </select>
              <button
                className="btn"
                disabled={computing || (computePeriod !== "" && !isPeriod(computePeriod))}
                onClick={compute}
                title="Computes DRAFT, SHADOW or WITHHELD cards for an ended month. It cannot publish, and it refuses (409) a card that is already PUBLISHED or FINAL."
              >
                {computing ? "Computing…" : "Compute period"}
              </button>
            </>
          )}
        </div>
        {computeResult && (
          <div className="muted" style={{ marginTop: "0.5rem" }}>
            <span className="chip ok">COMPUTED</span> Period {computeResult.period}: {computeResult.computed} computed,{" "}
            {computeResult.skipped} skipped. Run <span style={{ fontFamily: "var(--mono)" }}>{computeResult.run_id}</span>.
            {computeResult.computed_detail && computeResult.computed_detail.length > 0 && (
              <div>Computed: {computeResult.computed_detail.join(" · ")}</div>
            )}
            {computeResult.skipped_detail && computeResult.skipped_detail.length > 0 && (
              <div>Skipped: {computeResult.skipped_detail.join(" · ")}</div>
            )}
            {!computeResult.computed_detail && (
              <div>Your role is told the counts only. Which vendor came out SHADOW or WITHHELD is visible to duty_manager, management and admin.</div>
            )}
          </div>
        )}
        {computeFailure && (
          <div style={FAILURE_BOX} role="status">
            <span className="chip warn">{computeFailure.view.title.toUpperCase()}</span>
            <div>
              <div>{computeFailure.view.body}</div>
              {computeFailure.detail && <div className="pre" style={{ marginTop: "0.35rem" }}>{computeFailure.detail}</div>}
            </div>
          </div>
        )}
      </div>

      {listFailure && (
        <div style={FAILURE_BOX} role="status">
          <span className="chip warn">{listFailure.view.title.toUpperCase()}</span>
          <div>
            <div>{listFailure.view.body}</div>
            {listFailure.detail && <div className="pre" style={{ marginTop: "0.35rem" }}>{listFailure.detail}</div>}
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          <h3>Cards</h3>
          <span className="chip">{rows.length}</span>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Period</th>
                <th>Status</th>
                <th>Data-quality gate</th>
                <th>Terms</th>
                <th>Computed (EAT)</th>
                <th>Dispute window closes (EAT)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const sv = statusView(r.status);
                const gate = gateView(r.data_quality);
                const terms = termsView(r);
                return (
                  <tr
                    key={r.id}
                    tabIndex={0}
                    aria-selected={selected === r.id}
                    onClick={() => setSelected(r.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelected(r.id);
                      }
                    }}
                    style={{ cursor: "pointer", background: selected === r.id ? "rgba(62, 203, 255, 0.07)" : undefined }}
                  >
                    <td>
                      <strong>{r.vendor_code || r.vendor_id}</strong>
                      {r.vendor_name ? <div className="muted">{r.vendor_name}</div> : null}
                    </td>
                    <td>
                      {r.period}
                      <div className="muted">{periodWords(r.period)}</div>
                    </td>
                    <td>
                      <span className={sv.chip}>
                        {sv.label} · {sv.tag}
                      </span>
                    </td>
                    <td>
                      <span className={gate.passed === false ? "chip danger" : gate.passed === true ? "chip" : "chip warn"}>
                        {gate.passed === true ? "PASSED" : gate.passed === false ? "FAILED" : "NOT RECORDED"}
                      </span>
                      <div className="muted">
                        {gate.inferred} of {gate.restored} inferred ({gate.pct}) · limit {gate.threshold}
                      </div>
                    </td>
                    <td>
                      <span className={terms.chip}>{terms.label}</span>
                    </td>
                    <td className="muted">{fmtDateTime(r.computed_at)}</td>
                    <td className="muted">{r.dispute_window_ends_at ? fmtDateTime(r.dispute_window_ends_at) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {loaded && rows.length === 0 && !listFailure && (
          <div className="empty">
            No scorecards match these filters.
            {hiddenNote ? <div style={{ marginTop: "0.4rem" }}>{hiddenNote}</div> : null}
            <div style={{ marginTop: "0.4rem" }}>
              Cards are computed per ended EAT month. The hourly <code>scorecard_close</code> job does this when the
              scheduler is on, or shift_supervisor and above can use Compute above.
            </div>
          </div>
        )}
        {!loaded && <div className="empty">Loading scorecards…</div>}
        {loaded && rows.length > 0 && hiddenNote && <div className="muted" style={{ marginTop: "0.5rem" }}>{hiddenNote}</div>}
      </div>

      {selected && (
        <div style={{ marginTop: "1rem" }}>
          <ScorecardDetail key={selected} cardId={selected} session={session} onChanged={reload} />
        </div>
      )}
    </div>
  );
}

const FAILURE_BOX = {
  display: "flex",
  gap: "0.6rem",
  alignItems: "flex-start",
  margin: "0.5rem 0 0.75rem",
  padding: "0.6rem 0.75rem",
  border: "1px solid rgba(255, 193, 77, 0.45)",
  background: "rgba(255, 193, 77, 0.07)",
  borderRadius: 10,
} as const;
