import { useCallback, useEffect, useRef, useState } from "react";
import { scorecardApi } from "./scorecardApi";
import ScorecardLinesTable from "./ScorecardLinesTable";
import LineDrawer from "./LineDrawer";
import {
  cardActions,
  disputeAffordance,
  excludedOf,
  failureView,
  gateView,
  periodWords,
  statusView,
  termsView,
  watermarkImage,
  windowWords,
  type FailureView,
  type Scorecard,
  type ScorecardLine,
} from "./scorecardModel";
import { humanEnum } from "../lib/agents";
import { detailOf, statusOf } from "../lib/apiError";
import { IconCheck, IconDot } from "../lib/icons";
import { fmtDate, fmtDateTime, fmtHM, parseInstant } from "../lib/time";

/**
 * One vendor scorecard (spec §7.6, §7.10): the card, its gates, its 22 lines and the three
 * human acts that move it on.
 *
 * What each status looks like, and why:
 *
 * - **SHADOW** has a diagonal "SHADOW" watermark tiled across the whole card and into the line
 *   drawer, plus "internal only" in words beside the status chip. The watermark is decorative (aria-hidden);
 *   the words carry the meaning (§7.10: never colour or decoration alone).
 * - **WITHHELD** leads with the data-quality gate: how many restore times were inferred, out
 *   of how many, the percentage, the limit and the YAML key it comes from, the incidents and
 *   the sources. The server's reason follows verbatim. It offers no publish button, because
 *   the service refuses a WITHHELD card (`ScorecardGateError`).
 * - **DRAFT** is marked as an internal working paper. Publish is offered to duty_manager/admin.
 * - **PUBLISHED** shows the dispute window as the absolute EAT close time plus the time
 *   left. Finalise is offered once the window has closed.
 * - **FINAL** is read-only.
 *
 * On every card, whatever the status, "defaults, not contract" (§7.6.6) is a banner that
 * cannot be dismissed, not a footnote.
 *
 * Failure: a 404 is "not found, or not visible to your role" (the server hides unreleased
 * cards and other operators' cards behind a 404 on purpose), and a 403 is "not available to
 * your role". Both are said in words beside the server's own sentence; neither blanks the page.
 *
 * Freshness: the card is refetched whenever `tick` changes. The page passes its list tick, so
 * Compute, Refresh, a click on the already-open row and every action refetch the open card as
 * well as the list. A recompute keeps the card's id (it is derived from operator, vendor and
 * period) and can turn SHADOW into WITHHELD and clear the shadow review, so a card fetched
 * once at open is not good enough. While a refetch runs, the old card stays on screen marked
 * "Refreshing…". If the refetch fails, the card is taken down and the failure shown, rather than
 * leaving figures on screen that could not be confirmed.
 */

const WINDOW_TICK_MS = 60_000;

export default function ScorecardDetail({
  cardId,
  tick = 0,
  session,
  onChanged,
}: {
  cardId: string;
  /** Bumped by the page whenever the data may have changed; each change refetches the card. */
  tick?: number;
  session: { display_name?: string; role?: string } | null;
  onChanged?: () => void;
}) {
  const role = (session?.role || "").trim();
  const [card, setCard] = useState<Scorecard | null>(null);
  const [failure, setFailure] = useState<{ view: FailureView; detail: string } | null>(null);
  const [selected, setSelected] = useState<ScorecardLine | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const [actor, setActor] = useState("");
  const [rationale, setRationale] = useState("");
  const [reason, setReason] = useState("");
  const [finaliseReason, setFinaliseReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionNote, setActionNote] = useState<string | null>(null);
  const [actionFailure, setActionFailure] = useState<{ view: FailureView; detail: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  /** The card id on screen, so a refetch of the SAME card keeps it visible while it loads. */
  const shownId = useRef<string | null>(null);
  /**
   * Set by a successful action just before `onChanged()`. The refetch the page's reload then
   * triggers is this component's own echo, so the action's success note stays. A refetch
   * caused by anything else (Compute, Refresh) clears the note, which may no longer be true.
   */
  const ownChange = useRef(false);

  useEffect(() => {
    let live = true;
    const sameCard = shownId.current === cardId;
    shownId.current = cardId;
    if (!sameCard) {
      setCard(null);
      setFailure(null);
      setSelected(null);
      setActionNote(null);
      setActionFailure(null);
    } else {
      setRefreshing(true);
      if (!ownChange.current) {
        setActionNote(null);
        setActionFailure(null);
      }
    }
    ownChange.current = false;
    scorecardApi
      .get(cardId)
      .then((c) => {
        if (!live) return;
        setCard(c);
        setFailure(null);
        // An open drawer stays on the same line, now showing the refetched figures.
        setSelected((prev) => (prev ? (c.lines ?? []).find((l) => l.id === prev.id) ?? null : null));
      })
      .catch((e) => {
        if (!live) return;
        setCard(null);
        setSelected(null);
        const detail = detailOf(e, "");
        setFailure({ view: failureView(statusOf(e), "detail", detail), detail });
      })
      .finally(() => {
        if (live) setRefreshing(false);
      });
    return () => {
      live = false;
    };
  }, [cardId, tick]);

  // The dispute-window words move with the clock; a minute is fine-grained enough for "h left".
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), WINDOW_TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  const closeDrawer = useCallback(() => setSelected(null), []);

  if (failure) {
    return (
      <div className="panel">
        <h2 className="panel-title">{failure.view.title}</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {failure.view.body}
        </p>
        {failure.detail && <div className="pre">{failure.detail}</div>}
      </div>
    );
  }
  if (!card) {
    return (
      <div className="panel">
        <div className="empty">Loading scorecard…</div>
      </div>
    );
  }

  const windowEnds = parseInstant(card.dispute_window_ends_at)?.getTime() ?? null;
  const sv = statusView(card.status, { nowMs: now, windowEndsMs: windowEnds });
  const terms = termsView(card);
  const gate = gateView(card.data_quality);
  const actions = cardActions(card, role, now, windowEnds);
  const dispute = disputeAffordance();
  const lines = card.lines ?? [];
  const titleParts = [card.vendor_code || card.vendor_id, card.period];
  const d = card.discipline || {};

  const act = async (what: "review" | "publish" | "finalise") => {
    setBusy(true);
    setActionNote(null);
    setActionFailure(null);
    const who = actor.trim() || undefined;
    try {
      const res =
        what === "review"
          ? await scorecardApi.shadowReview(card.id, { rationale: rationale.trim(), reviewed_by: who })
          : what === "publish"
            ? await scorecardApi.publish(card.id, { reason: reason.trim(), published_by: who })
            : await scorecardApi.finalise(card.id, { reason: finaliseReason.trim() || undefined, finalised_by: who });
      setCard(res.scorecard);
      setSelected(null);
      setRationale("");
      setReason("");
      setFinaliseReason("");
      setActionNote(
        what === "review"
          ? "Shadow review recorded. The card is still shadow; publishing is a separate act."
          : what === "publish"
            ? "Published. The dispute window is running. Nothing was sent to anyone."
            : "Finalised."
      );
      ownChange.current = true;
      onChanged?.();
    } catch (e) {
      const detail = detailOf(e, "");
      setActionFailure({ view: failureView(statusOf(e), "action", detail), detail });
    } finally {
      setBusy(false);
    }
  };

  const anyAction = actions.review.show || actions.publish.show || actions.finalise.show;
  const st = String(card.status || "").toLowerCase();
  const who = session?.display_name || "your session name";

  return (
    <div className="scd">
      {/* SHADOW watermark: tiled over the whole card, above the text, faint, never clickable. */}
      {sv.watermark && (
        <div
          aria-hidden="true"
          className="scd-watermark"
          data-watermark={sv.watermark}
          style={{ backgroundImage: watermarkImage(sv.watermark) }}
        />
      )}
      <section className="panel scd-panel" aria-labelledby="scd-title">
        {/* ---- header ------------------------------------------------------------ */}
        <header className="scd-head">
          <div className="scd-head-main">
            <h2 id="scd-title" className="scd-title">
              {card.vendor_name || card.vendor_code || "vendor " + card.vendor_id}
            </h2>
            <p className="scd-sub">
              {card.vendor_name && card.vendor_code && card.vendor_name !== card.vendor_code && (
                <span className="mono">{card.vendor_code}</span>
              )}
              <span>{periodWords(card.period)}, an EAT calendar month</span>
              <span>SLA terms {card.sla_terms_version}</span>
            </p>
          </div>
          <div className="scd-state">
            <span className={`sc-pill ${st}`}>{sentence(humanEnum(sv.label))}</span>
            <span className="scd-tag">{sentence(humanEnum(sv.tag))}</span>
            {refreshing ? <span role="status">Refreshing…</span> : null}
          </div>
        </header>
        <p className="scd-summary">{sv.summary}</p>

        {/* ---- "defaults, not contract": never dismissible (§7.6.6) --------------- */}
        {terms.kind !== "CONTRACT" ? (
          <div className="scd-notice">
            <IconDot />
            <div>
              <strong>{sentence(humanEnum(terms.label))}.</strong> {terms.text.replace(/^defaults, not contract:\s*/i, "").replace(/^./, (c) => c.toUpperCase())}
              <div className="scd-notice-sub">
                Every band, target and credit on this card rests on these terms. Read them as placeholders, not as anything a
                vendor agreed to.
              </div>
            </div>
          </div>
        ) : (
          <p className="scd-terms">Terms: {terms.text}</p>
        )}

        {/* ---- WITHHELD: the gate, with its numbers -------------------------------- */}
        {sv.label === "WITHHELD" && (
          <div className="scd-withheld">
            <h3>The data-quality gate failed</h3>
            <p>{gate.sentence}</p>
            <dl className="scd-facts scd-facts-gate">
              <div>
                <dt>Inferred restores</dt>
                <dd>
                  {gate.inferred} of {gate.restored}
                </dd>
              </div>
              <div>
                <dt>Inferred share</dt>
                <dd>{gate.pct}</dd>
              </div>
              <div>
                <dt>Limit</dt>
                <dd>{gate.threshold}</dd>
                <dd className="scd-fact-note mono">{gate.yamlPath}</dd>
              </div>
              <div>
                <dt>Eligible tickets</dt>
                <dd>{gate.incidents}</dd>
              </div>
            </dl>
            {gate.bySource.length > 0 && (
              <p className="scd-withheld-more">Inferred by source: {gate.bySource.map(([src, n]) => `${humanEnum(src)} ${n}`).join(", ")}</p>
            )}
            {gate.inferredIncidents.length > 0 && (
              <p className="scd-withheld-more">
                Tickets whose restore time was inferred: <span className="mono">{gate.inferredIncidents.join(", ")}</span>
              </p>
            )}
            {gate.reason && <div className="pre">{gate.reason}</div>}
          </div>
        )}

        {/* ---- facts ------------------------------------------------------------- */}
        <dl className="scd-facts">
          <div className={gate.passed === false ? "bad" : undefined}>
            <dt>Data-quality gate</dt>
            <dd>{gate.passed === true ? "Passed" : gate.passed === false ? "Failed" : "Not recorded"}</dd>
            {sv.label !== "WITHHELD" && (
              <dd className="scd-fact-note">
                {gate.inferred} of {gate.restored} restores inferred ({gate.pct}), limit {gate.threshold}
              </dd>
            )}
          </div>
          <div className={card.shadow_required && !card.shadow_reviewed_by ? "hitl" : undefined}>
            <dt>Shadow review</dt>
            <dd>{card.shadow_reviewed_by ? card.shadow_reviewed_by : card.shadow_required ? "Not yet recorded" : "Not required"}</dd>
            <dd className="scd-fact-note">
              {card.shadow_reviewed_at ? when(card.shadow_reviewed_at) : card.shadow_required ? "Needed before it can be published" : " "}
            </dd>
          </div>
          {card.dispute_window_ends_at ? (
            <div>
              <dt>Disputes close</dt>
              <dd>{when(card.dispute_window_ends_at)}</dd>
              <dd className="scd-fact-note">{sentence(windowWords(now, windowEnds))}</dd>
            </div>
          ) : (
            <div>
              <dt>Published</dt>
              <dd>{card.published_at ? when(card.published_at) : "Not yet"}</dd>
              <dd className="scd-fact-note">{card.finalised_at ? `Finalised ${when(card.finalised_at)}` : " "}</dd>
            </div>
          )}
          <div>
            <dt>Computed</dt>
            <dd>{when(card.computed_at)}</dd>
            <dd className="scd-fact-note mono" title="The run that computed this card">
              {card.computed_by_run_id}
            </dd>
          </div>
        </dl>

        {/* ---- human acts ---------------------------------------------------------- */}
        {(anyAction || actions.none) && (
          <section className="scd-actions" aria-labelledby="scd-actions-title">
            <h3 id="scd-actions-title" className="scd-h3">
              What a person does next
            </h3>
            {actions.none && <p className="scd-muted">{actions.none}</p>}
            {anyAction && !actions.roleMayAct && (
              <p className="scd-muted">
                Shadow review, publish and finalise are a duty manager's or an admin's acts (§7.6.3). Your role (
                {role ? role.replace(/_/g, " ") : "unknown"}) can read this card only.
              </p>
            )}
            {anyAction && actions.roleMayAct && (
              <div className="scd-forms">
                <label
                  className="scd-field"
                  title={`A person's name. The server requires letters, strips invisible characters and refuses automation names such as "system" (400). With sign-in on, the server records the signed-in user and ignores this field.`}
                >
                  <span>Recorded as</span>
                  <input
                    aria-label="Your name, recorded as the actor"
                    placeholder={who}
                    value={actor}
                    disabled={busy}
                    onChange={(e) => setActor(e.target.value)}
                  />
                  <small>Left empty, {who} is recorded.</small>
                </label>

                {actions.review.show && (
                  <div className="scd-act">
                    <label className="scd-field">
                      <span>Shadow review: what did you check?</span>
                      <input
                        aria-label="What did you check? (required)"
                        placeholder="Required"
                        value={rationale}
                        disabled={busy}
                        onChange={(e) => setRationale(e.target.value)}
                      />
                      <small>Records that you inspected this first card. It does not publish it.</small>
                    </label>
                    <button className="btn primary" disabled={busy || !actions.review.enabled || !rationale.trim()} onClick={() => act("review")}>
                      Record shadow review
                    </button>
                  </div>
                )}

                {actions.publish.show && (
                  <div className="scd-act">
                    <label className="scd-field">
                      <span>Publish: why release it?</span>
                      <input
                        aria-label="Reason for releasing it (required)"
                        placeholder="Required"
                        value={reason}
                        disabled={busy || !actions.publish.enabled}
                        onChange={(e) => setReason(e.target.value)}
                      />
                      <small>
                        {actions.publish.why ||
                          "The vendor may then see it and the dispute window starts. It cannot be recomputed afterwards. Nothing is sent to anyone."}
                      </small>
                    </label>
                    <button className="btn good" disabled={busy || !actions.publish.enabled || !reason.trim()} onClick={() => act("publish")}>
                      Publish to vendor
                    </button>
                  </div>
                )}

                {actions.finalise.show && (
                  <div className="scd-act">
                    <label className="scd-field">
                      <span>Finalise: reason (optional)</span>
                      <input
                        aria-label="Reason for finalising (optional)"
                        placeholder="Optional"
                        value={finaliseReason}
                        disabled={busy || !actions.finalise.enabled}
                        onChange={(e) => setFinaliseReason(e.target.value)}
                      />
                      <small>
                        {actions.finalise.why
                          ? actions.finalise.why +
                            (card.dispute_window_ends_at ? ` It closes ${fmtDateTime(card.dispute_window_ends_at)} EAT (${windowWords(now, windowEnds)}).` : "")
                          : "The window has closed. The server also refuses while any line has an open dispute."}
                      </small>
                    </label>
                    <button className="btn" disabled={busy || !actions.finalise.enabled} onClick={() => act("finalise")}>
                      Finalise
                    </button>
                  </div>
                )}
              </div>
            )}
            {actionNote && (
              <p className="scd-done" role="status">
                <IconCheck /> {actionNote}
              </p>
            )}
            {actionFailure && (
              <div className="sc-alert" role="status">
                <span className="chip warn">{actionFailure.view.title}</span>
                <div>
                  <div>{actionFailure.view.body}</div>
                  {actionFailure.detail && <div className="pre">{actionFailure.detail}</div>}
                </div>
              </div>
            )}
          </section>
        )}

        {/* ---- lines ------------------------------------------------------------- */}
        <div className="scd-lines-head">
          <h3 className="scd-h3">The {lines.length} lines</h3>
          <span className="scd-muted">Raw beside normalised, never instead of it. Open a line for its formula and evidence.</span>
        </div>
        <ScorecardLinesTable
          lines={lines}
          canDispute={dispute.canDispute}
          disputeReason={dispute.reason}
          selectedId={selected?.id ?? null}
          onSelect={setSelected}
        />

        {/* ---- operator discipline: OUR record-keeping, shown to the vendor -------- */}
        <div className="scd-discipline">
          <h3 className="scd-h3">Operator discipline</h3>
          <p className="scd-muted">The operator's own record-keeping. It moves no vendor figure.</p>
          <dl className="scd-facts scd-facts-small">
            <div>
              <dt>Stop clocks recorded</dt>
              <dd>{d.scc_events ?? "Not recorded"}</dd>
            </div>
            <div>
              <dt>Recorded over {d.late_scc_opening_threshold_min ?? "?"} min late</dt>
              <dd>{d.late_scc_openings ?? "Not recorded"}</dd>
            </div>
            <div>
              <dt>Planned power cut with no stop clock</dt>
              <dd>{d.missing_scc_with_confirmed_power == null ? "Not computed" : String(d.missing_scc_with_confirmed_power)}</dd>
            </div>
          </dl>
          {(d.late_scc_opening_events || []).length > 0 && (
            <p className="scd-muted">
              Late openings:{" "}
              {(d.late_scc_opening_events || [])
                .map((ev) => `${ev.incident} ${humanEnum(ev.scc_code)} +${ev.opening_delay_min} min${ev.reversed ? " (reversed)" : ""}`)
                .join("; ")}
            </p>
          )}
          {d.missing_scc_with_confirmed_power == null && d.missing_scc_with_confirmed_power_note ? (
            <p className="scd-muted">{sentence(d.missing_scc_with_confirmed_power_note)}</p>
          ) : null}
        </div>

        {card.narrative && (
          <>
            <h3 className="scd-h3">
              Narrative{card.narrative_ai_assisted ? <span className="scd-muted"> AI-assisted</span> : null}
            </h3>
            <div className="pre">{card.narrative}</div>
          </>
        )}
      </section>

      {selected && (
        <LineDrawer
          key={selected.id}
          formula={selected.formula}
          yaml_path={selected.yaml_path}
          excluded={excludedOf(selected)}
          sccMinutes={selected.scc_minutes_deducted}
          line={selected}
          cardTitle={[...titleParts, humanEnum(sv.label)]}
          watermark={sv.watermark}
          onClose={closeDrawer}
          canDispute={dispute.canDispute}
          disputeReason={dispute.reason}
        />
      )}
    </div>
  );
}

const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
/** "20 Oct 2026, 00:00" in EAT: the day and the minute, without seconds. */
const when = (v: unknown) => `${fmtDate(v)}, ${fmtHM(v)}`;
