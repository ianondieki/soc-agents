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
import { fmtDateTime, parseInstant } from "../lib/time";

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
        <h3>{failure.view.title}</h3>
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

  return (
    <div style={{ position: "relative" }}>
      {/* SHADOW watermark: tiled over the whole card, above the text, faint, never clickable. */}
      {sv.watermark && (
        <div
          aria-hidden="true"
          data-watermark={sv.watermark}
          style={{
            position: "absolute",
            inset: 0,
            pointerEvents: "none",
            backgroundImage: watermarkImage(sv.watermark),
            backgroundRepeat: "repeat",
            borderRadius: "var(--radius)",
            zIndex: 2,
          }}
        />
      )}
      <div className="panel">
        {/* ---- header ------------------------------------------------------------ */}
        <div className="panel-head" style={{ flexWrap: "wrap" }}>
          <div>
            <h3 className="head-row" style={{ margin: 0 }}>
              {card.vendor_code || "vendor " + card.vendor_id}
              {card.vendor_name ? <span className="muted">{card.vendor_name}</span> : null}
            </h3>
            <div className="facts">
              <span>
                Period {card.period} ({periodWords(card.period)}, EAT calendar month)
              </span>
              <span>SLA terms {card.sla_terms_version}</span>
            </div>
          </div>
          <div className="facts">
            <span className={sv.chip}>{humanEnum(sv.label)}</span>
            <span>{humanEnum(sv.tag)}</span>
            {terms.kind === "CONTRACT" ? (
              <span>{humanEnum(terms.label)}</span>
            ) : (
              <span className="attn warn">
                <IconDot /> {humanEnum(terms.label)}
              </span>
            )}
            {card.shadow_reviewed_by ? <span>Shadow-reviewed by {card.shadow_reviewed_by}</span> : null}
            {refreshing ? <span role="status">Refreshing…</span> : null}
          </div>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          {sv.summary}
        </p>

        {/* ---- "defaults, not contract": never dismissible (§7.6.6) --------------- */}
        {terms.kind !== "CONTRACT" ? (
          <div style={NOTICE}>
            <span className="chip warn">{humanEnum(terms.label)}</span>
            <div>
              <div>{terms.text}</div>
              <div className="muted" style={{ marginTop: "0.25rem" }}>
                Every band, target and credit figure on this card rests on these terms. Read them as placeholders, not as
                anything a vendor agreed to.
              </div>
            </div>
          </div>
        ) : (
          <div className="muted" style={{ marginBottom: "0.6rem" }}>
            Terms: {terms.text}
          </div>
        )}

        {/* ---- WITHHELD: the gate, with its numbers -------------------------------- */}
        {sv.label === "WITHHELD" && (
          <div style={WITHHELD_BOX}>
            <div style={{ display: "flex", gap: "0.55rem", alignItems: "center", flexWrap: "wrap" }}>
              <span className="chip danger">withheld</span>
              <strong style={{ fontSize: "var(--fs-lg)", color: "var(--text-bright)" }}>The data-quality gate failed</strong>
            </div>
            <div style={{ marginTop: "0.45rem", color: "var(--text-bright)" }}>{gate.sentence}</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: "0.5rem", marginTop: "0.6rem" }}>
              <Fact label="Inferred restores" value={`${gate.inferred} of ${gate.restored}`} />
              <Fact label="Inferred share" value={gate.pct} />
              <Fact label="Limit" value={gate.threshold} note={gate.yamlPath} />
              <Fact label="Eligible incidents" value={gate.incidents} />
            </div>
            {gate.bySource.length > 0 && (
              <div className="muted" style={{ marginTop: "0.5rem" }}>
                Inferred by source: {gate.bySource.map(([src, n]) => `${src} ${n}`).join(", ")}
              </div>
            )}
            {gate.inferredIncidents.length > 0 && (
              <div className="muted" style={{ marginTop: "0.25rem" }}>
                Incidents whose restore was inferred:{" "}
                <span style={{ fontFamily: "var(--mono)", color: "var(--text)" }}>{gate.inferredIncidents.join(", ")}</span>
              </div>
            )}
            {gate.reason && (
              <div className="pre" style={{ marginTop: "0.55rem" }}>
                {gate.reason}
              </div>
            )}
          </div>
        )}

        {/* ---- facts ------------------------------------------------------------- */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "0.55rem", margin: "0.4rem 0 0.9rem" }}>
          <Fact label="Computed (EAT)" value={fmtDateTime(card.computed_at)} small />
          <Fact
            label="Data-quality gate"
            value={gate.passed === true ? "Passed" : gate.passed === false ? "Failed" : "Not recorded"}
            note={sv.label === "WITHHELD" ? null : `${gate.inferred} of ${gate.restored} restores inferred (${gate.pct}); limit ${gate.threshold}`}
            small
          />
          <Fact
            label="Shadow review"
            value={card.shadow_reviewed_by ? card.shadow_reviewed_by : card.shadow_required ? "Required, not yet recorded" : "Not required"}
            note={card.shadow_reviewed_at ? fmtDateTime(card.shadow_reviewed_at) + " EAT" : null}
            small
          />
          {card.published_at && <Fact label="Published (EAT)" value={fmtDateTime(card.published_at)} small />}
          {card.dispute_window_ends_at && (
            <Fact
              label="Dispute window closes (EAT)"
              value={fmtDateTime(card.dispute_window_ends_at)}
              note={windowWords(now, windowEnds)}
              small
            />
          )}
          {card.finalised_at && <Fact label="Finalised (EAT)" value={fmtDateTime(card.finalised_at)} small />}
          <Fact label="Computed by run" value={card.computed_by_run_id} small mono />
        </div>

        {/* ---- human acts ---------------------------------------------------------- */}
        {(anyAction || actions.none) && (
          <div style={{ borderTop: "1px solid var(--border)", paddingTop: "0.7rem", marginTop: "0.2rem" }}>
            <h4 style={H4}>Actions</h4>
            {actions.none && <div className="muted">{actions.none}</div>}
            {anyAction && !actions.roleMayAct && (
              <div className="muted">
                Shadow review, publish and finalise are duty_manager / admin acts (§7.6.3). Your role ({role || "unknown"}) can read this card only.
              </div>
            )}
            {anyAction && actions.roleMayAct && (
              <>
                <div className="form-row">
                  <input
                    aria-label="Your name, recorded as the actor"
                    placeholder={"Your name (default: " + (session?.display_name || "the session name") + ")"}
                    value={actor}
                    disabled={busy}
                    onChange={(e) => setActor(e.target.value)}
                    style={{ minWidth: "18rem" }}
                  />
                  <span className="muted">
                    A person's name. The server requires letters, strips invisible characters and refuses automation names such as
                    "system" (400). Left empty, the session's display name is recorded. With sign-in on, the server records the
                    signed-in user and ignores this field.
                  </span>
                </div>

                {actions.review.show && (
                  <div className="form-row">
                    <input
                      aria-label="What did you check? (required)"
                      placeholder="What did you check? (required)"
                      value={rationale}
                      disabled={busy}
                      onChange={(e) => setRationale(e.target.value)}
                      style={{ minWidth: "24rem", flex: 1 }}
                    />
                    <button className="btn primary" disabled={busy || !actions.review.enabled || !rationale.trim()} onClick={() => act("review")}>
                      Record shadow review
                    </button>
                    <span className="muted">Records that you inspected this first card. It does not publish it.</span>
                  </div>
                )}

                {actions.publish.show && (
                  <div className="form-row">
                    <input
                      aria-label="Reason for releasing it (required)"
                      placeholder="Reason for releasing it (required)"
                      value={reason}
                      disabled={busy || !actions.publish.enabled}
                      onChange={(e) => setReason(e.target.value)}
                      style={{ minWidth: "24rem", flex: 1 }}
                    />
                    <button className="btn good" disabled={busy || !actions.publish.enabled || !reason.trim()} onClick={() => act("publish")}>
                      Publish to vendor
                    </button>
                    <span className="muted">
                      {actions.publish.why ||
                        "Releases the card: the vendor may see it and the dispute window starts. It cannot be recomputed afterwards. Nothing is sent to anyone."}
                    </span>
                  </div>
                )}

                {actions.finalise.show && (
                  <div className="form-row">
                    <input
                      aria-label="Reason for finalising (optional)"
                      placeholder="Reason (optional)"
                      value={finaliseReason}
                      disabled={busy || !actions.finalise.enabled}
                      onChange={(e) => setFinaliseReason(e.target.value)}
                      style={{ minWidth: "20rem", flex: 1 }}
                    />
                    <button className="btn" disabled={busy || !actions.finalise.enabled} onClick={() => act("finalise")}>
                      Finalise
                    </button>
                    <span className="muted">
                      {actions.finalise.why
                        ? actions.finalise.why +
                          (card.dispute_window_ends_at ? ` It closes ${fmtDateTime(card.dispute_window_ends_at)} EAT (${windowWords(now, windowEnds)}).` : "")
                        : "The window has closed. The server also refuses while any line has an open dispute."}
                    </span>
                  </div>
                )}
              </>
            )}
            {actionNote && (
              <span className="muted" role="status">
                <IconCheck /> {actionNote}
              </span>
            )}
            {actionFailure && (
              <div style={{ ...NOTICE, margin: "0.5rem 0" }} role="status">
                <span className="chip warn">{actionFailure.view.title}</span>
                <div>
                  <div>{actionFailure.view.body}</div>
                  {actionFailure.detail && <div className="pre" style={{ marginTop: "0.35rem" }}>{actionFailure.detail}</div>}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ---- lines ------------------------------------------------------------- */}
        <h4 style={H4} className="head-row">
          Lines
          <span className="muted">{lines.length} lines</span>
          <span className="muted">raw beside normalised, never instead of it</span>
          <span className="muted">select a line for its formula and evidence</span>
        </h4>
        <ScorecardLinesTable
          lines={lines}
          canDispute={dispute.canDispute}
          disputeReason={dispute.reason}
          selectedId={selected?.id ?? null}
          onSelect={setSelected}
        />

        {/* ---- operator discipline: OUR record-keeping, shown to the vendor -------- */}
        <h4 style={H4} className="head-row">
          Operator discipline
          <span className="muted">the operator's own record-keeping; it moves no vendor KPI</span>
        </h4>
        <div className="facts">
          <span>
            Stop clocks recorded: <strong style={{ color: "var(--text)" }}>{d.scc_events ?? "not recorded"}</strong>
          </span>
          <span>
            Recorded more than {d.late_scc_opening_threshold_min ?? "?"} min after they started:{" "}
            <strong style={{ color: "var(--text)" }}>{d.late_scc_openings ?? "not recorded"}</strong>
          </span>
        </div>
        {(d.late_scc_opening_events || []).length > 0 && (
          <div className="muted" style={{ marginTop: "0.25rem" }}>
            Late openings:{" "}
            {(d.late_scc_opening_events || [])
              .map(
                (ev) =>
                  `${ev.incident} ${humanEnum(ev.scc_code)} +${ev.opening_delay_min} min${ev.reversed ? " (reversed)" : ""}`
              )
              .join("; ")}
          </div>
        )}
        <div className="facts" style={{ marginTop: "0.25rem" }}>
          <span>
            Missing utility-power stop clock with confirmed planned power:{" "}
            <strong style={{ color: "var(--text)" }}>
              {d.missing_scc_with_confirmed_power == null ? "not computed" : String(d.missing_scc_with_confirmed_power)}
            </strong>
          </span>
          {d.missing_scc_with_confirmed_power == null && d.missing_scc_with_confirmed_power_note ? (
            <span>{d.missing_scc_with_confirmed_power_note}</span>
          ) : null}
        </div>

        {card.narrative && (
          <>
            <h4 style={H4} className="head-row">
              Narrative
              {card.narrative_ai_assisted ? <span className="muted">AI-assisted</span> : null}
            </h4>
            <div className="pre">{card.narrative}</div>
          </>
        )}
      </div>

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
        />
      )}
    </div>
  );
}

const H4 = { margin: "1.05rem 0 0.5rem", fontSize: "var(--fs-md)", color: "var(--text-bright)" } as const;

const NOTICE = {
  display: "flex",
  gap: "0.6rem",
  alignItems: "flex-start",
  margin: "0 0 0.75rem",
  padding: "0.6rem 0.75rem",
  border: "1px solid rgba(255, 193, 77, 0.45)",
  background: "rgba(255, 193, 77, 0.07)",
  borderRadius: 10,
} as const;

const WITHHELD_BOX = {
  border: "3px solid var(--p1)",
  background: "rgba(255, 59, 92, 0.12)",
  borderRadius: 12,
  padding: "0.8rem 1rem",
  margin: "0 0 0.9rem",
} as const;

function Fact({ label, value, note, small, mono }: { label: string; value: string; note?: string | null; small?: boolean; mono?: boolean }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "0.5rem 0.65rem", background: "rgba(8, 16, 30, 0.6)" }}>
      <div style={{ fontSize: "var(--fs-xs)", color: "var(--muted)" }}>{label}</div>
      <div
        style={{
          fontFamily: mono ? "var(--mono)" : undefined,
          fontSize: small ? (mono ? "var(--fs-xs)" : "var(--fs-md)") : "var(--fs-lg)",
          // Never bold and mono together: an identifier or a measurement is set at 500.
          fontWeight: mono ? 500 : 600,
          color: "var(--text-bright)",
          wordBreak: "break-word",
        }}
      >
        {value}
      </div>
      {note ? (
        <div className="muted" style={{ fontSize: "var(--fs-xs)", marginTop: "0.15rem", wordBreak: "break-word" }}>
          {note}
        </div>
      ) : null}
    </div>
  );
}
