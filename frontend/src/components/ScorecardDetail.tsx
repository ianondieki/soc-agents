import { useCallback, useEffect, useState } from "react";
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
import { detailOf, statusOf } from "../lib/apiError";
import { fmtDateTime, parseInstant } from "../lib/time";

/**
 * One vendor scorecard (spec §7.6, §7.10): the card, its gates, its 22 lines and the three
 * human acts that move it on.
 *
 * What each status looks like, and why:
 *
 * - **SHADOW** has a diagonal "SHADOW" watermark tiled across the whole card and into the line
 *   drawer, plus an "INTERNAL ONLY" chip in words. The watermark is decorative (aria-hidden);
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
 */

const WINDOW_TICK_MS = 60_000;

export default function ScorecardDetail({
  cardId,
  session,
  onChanged,
}: {
  cardId: string;
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

  useEffect(() => {
    let live = true;
    setCard(null);
    setFailure(null);
    setSelected(null);
    setActionNote(null);
    setActionFailure(null);
    scorecardApi
      .get(cardId)
      .then((c) => {
        if (live) setCard(c);
      })
      .catch((e) => {
        if (live) setFailure({ view: failureView(statusOf(e), "detail"), detail: detailOf(e, "") });
      });
    return () => {
      live = false;
    };
  }, [cardId]);

  // The dispute-window words move with the clock; a minute is fine-grained enough for "h left".
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), WINDOW_TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  const closeDrawer = useCallback(() => setSelected(null), []);

  if (failure) {
    return (
      <div className="panel">
        <div className="panel-head">
          <h3>{failure.view.title}</h3>
          <span className="chip">{failure.view.kind === "notfound" ? "NOT FOUND" : failure.view.title.toUpperCase()}</span>
        </div>
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

  const sv = statusView(card.status);
  const terms = termsView(card);
  const gate = gateView(card.data_quality);
  const windowEnds = parseInstant(card.dispute_window_ends_at)?.getTime() ?? null;
  const actions = cardActions(card, role, now, windowEnds);
  const dispute = disputeAffordance();
  const lines = card.lines ?? [];
  const title = `${card.vendor_code || card.vendor_id} · ${card.period}`;
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
          ? "Shadow review recorded. The card is still SHADOW; publishing is a separate act."
          : what === "publish"
            ? "Published. The dispute window is running. Nothing was sent to anyone."
            : "Finalised."
      );
      onChanged?.();
    } catch (e) {
      setActionFailure({ view: failureView(statusOf(e), "action"), detail: detailOf(e, "") });
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
            <h3 style={{ margin: 0 }}>
              {card.vendor_code || "vendor " + card.vendor_id}
              {card.vendor_name ? <span className="muted"> · {card.vendor_name}</span> : null}
            </h3>
            <div className="muted">
              Period {card.period} ({periodWords(card.period)}, EAT calendar month) · sla_terms {card.sla_terms_version}
            </div>
          </div>
          <div className="chips">
            <span className={sv.chip}>
              {sv.label} · {sv.tag}
            </span>
            <span className={terms.chip}>{terms.label}</span>
            {card.shadow_reviewed_by ? <span className="chip ok">SHADOW-REVIEWED · {card.shadow_reviewed_by}</span> : null}
          </div>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          {sv.summary}
        </p>

        {/* ---- "defaults, not contract": never dismissible (§7.6.6) --------------- */}
        {terms.kind !== "CONTRACT" ? (
          <div style={NOTICE}>
            <span className="chip warn">{terms.label}</span>
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
              <span className="chip danger">WITHHELD</span>
              <strong style={{ fontSize: "1.1rem", color: "var(--text-bright)" }}>The data-quality gate failed</strong>
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
                Inferred by source: {gate.bySource.map(([src, n]) => `${src} ${n}`).join(" · ")}
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
            value={gate.passed === true ? "PASSED" : gate.passed === false ? "FAILED" : "NOT RECORDED"}
            note={sv.label === "WITHHELD" ? null : `${gate.inferred} of ${gate.restored} restores inferred (${gate.pct}); limit ${gate.threshold}`}
            small
          />
          <Fact
            label="Shadow review"
            value={card.shadow_reviewed_by ? card.shadow_reviewed_by : card.shadow_required ? "required · not yet recorded" : "not required"}
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
                    placeholder={"Your name (default: " + (session?.display_name || "the session name") + ")"}
                    value={actor}
                    disabled={busy}
                    onChange={(e) => setActor(e.target.value)}
                    style={{ minWidth: "18rem" }}
                  />
                  <span className="muted">
                    A named human, not a role label or "system". With sign-in on, the server records the signed-in user and ignores this field.
                  </span>
                </div>

                {actions.review.show && (
                  <div className="form-row">
                    <input
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
                        : "The window has closed. The server also refuses while any line has an OPEN dispute."}
                    </span>
                  </div>
                )}
              </>
            )}
            {actionNote && <span className="chip ok">{actionNote}</span>}
            {actionFailure && (
              <div style={{ ...NOTICE, margin: "0.5rem 0" }} role="status">
                <span className="chip warn">{actionFailure.view.title.toUpperCase()}</span>
                <div>
                  <div>{actionFailure.view.body}</div>
                  {actionFailure.detail && <div className="pre" style={{ marginTop: "0.35rem" }}>{actionFailure.detail}</div>}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ---- lines ------------------------------------------------------------- */}
        <h4 style={H4}>
          Lines <span className="muted">· {lines.length} · raw beside normalised, never instead of it · select a line for its formula and evidence</span>
        </h4>
        <ScorecardLinesTable
          lines={lines}
          canDispute={dispute.canDispute}
          disputeReason={dispute.reason}
          selectedId={selected?.id ?? null}
          onSelect={setSelected}
        />

        {/* ---- operator discipline: OUR record-keeping, shown to the vendor -------- */}
        <h4 style={H4}>
          Operator discipline <span className="muted">· the operator's own record-keeping; it moves no vendor KPI</span>
        </h4>
        <div className="muted">
          Stop clocks recorded: <strong style={{ color: "var(--text)" }}>{d.scc_events ?? "not recorded"}</strong> · recorded more than{" "}
          {d.late_scc_opening_threshold_min ?? "?"} min after they started:{" "}
          <strong style={{ color: "var(--text)" }}>{d.late_scc_openings ?? "not recorded"}</strong>
        </div>
        {(d.late_scc_opening_events || []).length > 0 && (
          <div className="muted" style={{ marginTop: "0.25rem" }}>
            Late openings:{" "}
            {(d.late_scc_opening_events || [])
              .map((ev) => `${ev.incident} ${ev.scc_code} +${ev.opening_delay_min} min${ev.reversed ? " (reversed)" : ""}`)
              .join(" · ")}
          </div>
        )}
        <div className="muted" style={{ marginTop: "0.25rem" }}>
          Missing UTILITY_POWER stop clock with confirmed planned power:{" "}
          <strong style={{ color: "var(--text)" }}>
            {d.missing_scc_with_confirmed_power == null ? "NOT COMPUTED" : String(d.missing_scc_with_confirmed_power)}
          </strong>
          {d.missing_scc_with_confirmed_power == null && d.missing_scc_with_confirmed_power_note
            ? " · " + d.missing_scc_with_confirmed_power_note
            : ""}
        </div>

        {card.narrative && (
          <>
            <h4 style={H4}>
              Narrative {card.narrative_ai_assisted ? <span className="chip accent">AI-ASSISTED</span> : null}
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
          cardTitle={`${title} · ${sv.label}`}
          watermark={sv.watermark}
          onClose={closeDrawer}
        />
      )}
    </div>
  );
}

const H4 = { margin: "1.05rem 0 0.5rem", fontSize: "0.95rem", color: "var(--text-bright)" } as const;

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
      <div style={{ fontSize: "0.7rem", letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--muted)" }}>{label}</div>
      <div
        style={{
          fontFamily: mono ? "var(--mono)" : undefined,
          fontSize: small ? (mono ? "0.78rem" : "0.95rem") : "1.2rem",
          fontWeight: 700,
          color: "var(--text-bright)",
          wordBreak: "break-word",
        }}
      >
        {value}
      </div>
      {note ? (
        <div className="muted" style={{ fontSize: "0.76rem", marginTop: "0.15rem", wordBreak: "break-word" }}>
          {note}
        </div>
      ) : null}
    </div>
  );
}
