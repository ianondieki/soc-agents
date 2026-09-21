import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import BlamelessHint from "./BlamelessHint";
import { detailOf, isStatus } from "../lib/apiError";
import { fmtDate, fmtDateTime } from "../lib/time";

/**
 * `PirEditor` (spec §7.10) — one post-incident review: the assembled timeline, the narrative
 * fields a human writes, the action items, and the publish gate.
 *
 * Two server behaviours are the reason this editor exists in this shape.
 *
 * 1. **The blameless validator.** `PATCH /pir/{id}` answers 422 with one exact sentence when
 *    `root_causes` or `contributing_factors` names a person on the incident (§7.7.3). The
 *    rule is therefore on screen *before* the save (`BlamelessHint`), and the 422 is rendered
 *    beside the two fields it is about — never as a toast, never as "Error: 422". The name
 *    that tripped it is not echoed by the server and is not reconstructed here.
 * 2. **Publish returns every blocker at once.** `POST /pir/{id}/publish` joins all unmet
 *    preconditions into one 422 because "a reviewer who has to discover three problems
 *    through three round-trips stops publishing reviews" (`services/pir.publish_blockers`).
 *    So the whole list is rendered as a checklist, each row with what to do about it, and the
 *    server's raw sentence is kept verbatim underneath so nothing is paraphrased away.
 *
 * A PUBLISHED review is immutable (the API answers 409). The editor goes read-only rather
 * than offering fields whose save cannot succeed.
 *
 * 3 a.m. rules (§7.10): statuses are chips with words in them, never colour alone; due dates
 * show the absolute EAT date beside "in 3 days" / "2 days overdue"; nothing animates.
 */

export type Pir = {
  id: string;
  incident_id: string;
  status: string;
  opened_reason: string;
  summary: string | null;
  impact: {
    users_affected?: number;
    duration_minutes?: number | null;
    adjusted_duration_minutes?: number | null;
    services?: string[];
    revenue_note?: string | null;
  };
  detection_method: string | null;
  detected_at: string | null;
  trigger: string | null;
  root_causes: string | null;
  contributing_factors: string | null;
  mtta_minutes: number | null;
  mttr_minutes: number | null;
  adjusted_mttr_minutes: number | null;
  timeline?: { ts: string; kind: string; title: string; detail: string; actor_role: string }[];
  went_well: string | null;
  went_poorly: string | null;
  got_lucky: string | null;
  ai_assisted: number;
  reviewer: string | null;
  reviewed_at: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
  actions?: PirAction[];
};

export type PirAction = {
  id: string;
  pir_id: string;
  type: string;
  priority: string;
  description: string;
  owner_token: string;
  due_date: string;
  status: string;
  problem_id: string | null;
  tracking_ref: string | null;
  created_at: string;
  closed_at: string | null;
};

/** `services/pir.py`: ACTION_TYPES, ACTION_PRIORITIES, ACTION_STATUSES, PIR_STATUSES. */
const ACTION_TYPES = ["prevent", "mitigate", "detect", "repair", "investigate"];
const ACTION_PRIORITIES = ["P0", "P1", "P2", "P3"];
const ACTION_STATUSES = ["OPEN", "IN_PROGRESS", "DONE", "WONT_DO"];
/** PUBLISHED is absent on purpose: it is reached through the publish route, which records the reviewer. */
const EDITABLE_STATUSES = ["DRAFT", "IN_REVIEW", "NOT_REQUIRED"];

/** The sentence `services/pir.BLAMELESS_MESSAGE` returns, matched on its stable opening clause. */
const BLAMELESS_RE = /describe what the system allowed/i;

/**
 * The publish gates, each with the action that clears it.
 *
 * Matching on a fragment rather than splitting the joined 422 on "; " is deliberate: one of
 * the blockers *contains* a semicolon ("a NOT_REQUIRED review cannot be published; move it to
 * DRAFT first"), so a naive split reports four problems where the server reported three.
 * Anything unmatched still reaches the reader — the raw sentence is rendered verbatim below.
 */
const PUBLISH_GATES: { match: RegExp; title: string; fix: string }[] = [
  {
    match: /NOT_REQUIRED review cannot be published/i,
    title: "This review is marked NOT_REQUIRED",
    fix: "Set the status to DRAFT and write it up before publishing.",
  },
  {
    match: /named reviewer is required/i,
    title: "No named reviewer",
    fix: "A postmortem nobody signed is a document nobody owns. Type the reviewer's name — a role label such as 'NOC Analyst' is not a name.",
  },
  {
    match: /P0 or P1 action item/i,
    title: "No P0/P1 action item, and users were affected",
    fix: "A user-affecting outage that produced no urgent action produced no learning. Add at least one P0 or P1 action item below.",
  },
  {
    match: BLAMELESS_RE,
    title: "A validated field still names a person",
    fix: "Rewrite root causes / contributing factors with role tokens (RNIO / FE / MSP_POWER) and save before publishing.",
  },
];

function statusChip(status: string): string {
  if (status === "PUBLISHED") return "chip ok";
  if (status === "IN_REVIEW") return "chip accent";
  return "chip";
}

function actionChip(status: string): string {
  if (status === "DONE") return "chip ok";
  if (status === "WONT_DO") return "chip";
  if (status === "IN_PROGRESS") return "chip accent";
  return "chip warn";
}

/** Whole days between today (EAT) and a `YYYY-MM-DD` due date, or `null` if unparseable. */
function daysUntil(due: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(due || "");
  if (!m) return null;
  // Both sides are taken at midnight UTC of the EAT calendar day, so the difference is a
  // whole number of days and no timezone arithmetic is invented here (defect #41: Kenya is
  // UTC+3 all year, which is the one shift applied before reading the calendar date).
  const dueMs = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const todayEat = new Date(Date.now() + 180 * 60000);
  const todayMs = Date.UTC(todayEat.getUTCFullYear(), todayEat.getUTCMonth(), todayEat.getUTCDate());
  return Math.round((dueMs - todayMs) / 86400000);
}

/** "in 3 days" / "due today" / "2 days OVERDUE" — the relative half of the §7.10 pair. */
function dueWords(due: string, closed: boolean): string {
  const d = daysUntil(due);
  if (d == null) return "";
  if (closed) return "closed";
  if (d === 0) return "due today";
  if (d > 0) return "in " + d + " day" + (d === 1 ? "" : "s");
  const late = Math.abs(d);
  return late + " day" + (late === 1 ? "" : "s") + " OVERDUE";
}

function minutes(value: number | null | undefined): string {
  if (value == null) return "—";
  return Math.round(value) + " min";
}

const TEXT_FIELDS: { key: keyof Pir; label: string; rows: number; hint?: string }[] = [
  { key: "summary", label: "Summary", rows: 3, hint: "what happened, in the words you would use to a duty manager" },
  { key: "trigger", label: "Trigger", rows: 2, hint: "the change or condition that started it" },
  { key: "root_causes", label: "Root causes", rows: 3, hint: "blameless-validated" },
  { key: "contributing_factors", label: "Contributing factors", rows: 3, hint: "blameless-validated" },
  { key: "went_well", label: "What went well", rows: 2 },
  { key: "went_poorly", label: "What went poorly", rows: 2 },
  { key: "got_lucky", label: "Where we got lucky", rows: 2 },
];

export default function PirEditor({
  pirId,
  incidentLabel,
  onChanged,
}: {
  pirId: string;
  /** `INC-…` joined from the incident list; the PIR routes carry `incident_id` only. */
  incidentLabel?: string | null;
  onChanged?: () => void;
}) {
  const [pir, setPir] = useState<Pir | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [saveNote, setSaveNote] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [blameless, setBlameless] = useState(false);

  const [reviewer, setReviewer] = useState("");
  const [rationale, setRationale] = useState("");
  const [publishError, setPublishError] = useState<string | null>(null);

  const [newAction, setNewAction] = useState({
    type: "prevent",
    priority: "P1",
    description: "",
    owner_token: "",
    due_date: "",
  });
  const [actionError, setActionError] = useState<string | null>(null);

  const adopt = useCallback((row: Pir) => {
    setPir(row);
    setDraft({
      summary: row.summary ?? "",
      trigger: row.trigger ?? "",
      root_causes: row.root_causes ?? "",
      contributing_factors: row.contributing_factors ?? "",
      went_well: row.went_well ?? "",
      went_poorly: row.went_poorly ?? "",
      got_lucky: row.got_lucky ?? "",
      detection_method: row.detection_method ?? "",
      revenue_note: row.impact?.revenue_note ?? "",
      status: row.status,
    });
  }, []);

  useEffect(() => {
    let live = true;
    setPir(null);
    setLoadError(null);
    setSaveError(null);
    setSaveNote(null);
    setPublishError(null);
    setBlameless(false);
    api
      .pir(pirId)
      .then((row: Pir) => {
        if (live) adopt(row);
      })
      .catch((e) => {
        if (live) setLoadError(detailOf(e, "This review could not be loaded."));
      });
    return () => {
      live = false;
    };
  }, [pirId, adopt]);

  const published = pir?.status === "PUBLISHED";
  const actions = pir?.actions ?? [];

  const changed = useMemo(() => {
    if (!pir) return {} as Record<string, string>;
    const out: Record<string, string> = {};
    for (const f of TEXT_FIELDS) {
      const key = f.key as string;
      if ((draft[key] ?? "") !== ((pir[f.key] as string | null) ?? "")) out[key] = draft[key] ?? "";
    }
    if ((draft.detection_method ?? "") !== (pir.detection_method ?? "")) {
      out.detection_method = draft.detection_method ?? "";
    }
    if ((draft.revenue_note ?? "") !== (pir.impact?.revenue_note ?? "")) {
      out.revenue_note = draft.revenue_note ?? "";
    }
    if ((draft.status ?? pir.status) !== pir.status) out.status = draft.status;
    return out;
  }, [draft, pir]);

  const dirty = Object.keys(changed).length > 0;

  const save = async () => {
    if (!pir || !dirty) return;
    setBusy(true);
    setSaveError(null);
    setSaveNote(null);
    setBlameless(false);
    try {
      const row: Pir = await api.pirPatch(pir.id, changed);
      adopt(row);
      setSaveNote("Saved");
      onChanged?.();
    } catch (e) {
      const detail = detailOf(e, "The review could not be saved.");
      setSaveError(detail);
      if (isStatus(e, 422) && BLAMELESS_RE.test(detail)) setBlameless(true);
    } finally {
      setBusy(false);
    }
  };

  const publish = async () => {
    if (!pir) return;
    setBusy(true);
    setPublishError(null);
    try {
      const row: Pir = await api.pirPublish(pir.id, {
        reviewer: reviewer.trim() || undefined,
        rationale: rationale.trim() || undefined,
      });
      adopt(row);
      setSaveNote("Published");
      onChanged?.();
    } catch (e) {
      const detail = detailOf(e, "Publishing failed.");
      setPublishError(detail);
      // Text can reach the validated fields from the model-draft path too, so the publish gate
      // re-applies the blameless rule; light the hint up when that is one of the blockers.
      if (BLAMELESS_RE.test(detail)) setBlameless(true);
    } finally {
      setBusy(false);
    }
  };

  const addAction = async () => {
    if (!pir) return;
    setActionError(null);
    if (!newAction.description.trim() || !newAction.owner_token.trim() || !newAction.due_date) {
      setActionError("An action item needs a description, a role-token owner and a due date.");
      return;
    }
    setBusy(true);
    try {
      await api.pirAddAction(pir.id, {
        ...newAction,
        description: newAction.description.trim(),
        owner_token: newAction.owner_token.trim(),
      });
      adopt(await api.pir(pir.id));
      setNewAction({ type: "prevent", priority: "P1", description: "", owner_token: "", due_date: "" });
      onChanged?.();
    } catch (e) {
      setActionError(detailOf(e, "The action item was not added."));
    } finally {
      setBusy(false);
    }
  };

  const moveAction = async (action: PirAction, status: string) => {
    if (!pir || status === action.status) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.pirPatchAction(pir.id, action.id, { status });
      adopt(await api.pir(pir.id));
      onChanged?.();
    } catch (e) {
      setActionError(detailOf(e, "The action item was not updated."));
    } finally {
      setBusy(false);
    }
  };

  const draftWithModel = async () => {
    if (!pir) return;
    setBusy(true);
    setSaveError(null);
    try {
      const res = await api.pirDraftLlm(pir.id);
      setSaveNote(
        res?.already_queued
          ? "A model draft is already queued"
          : "Model draft queued on the outbox"
      );
      adopt(await api.pir(pir.id));
    } catch (e) {
      setSaveError(detailOf(e, "The draft could not be queued."));
    } finally {
      setBusy(false);
    }
  };

  if (loadError) {
    return (
      <div className="panel">
        <div className="empty">{loadError}</div>
      </div>
    );
  }
  if (!pir) {
    return (
      <div className="panel">
        <div className="empty">Loading review…</div>
      </div>
    );
  }

  const gates = publishError ? PUBLISH_GATES.filter((g) => g.match.test(publishError)) : [];

  return (
    <div className="panel pir-editor">
      <div className="panel-head">
        <h3>{incidentLabel || pir.incident_id}</h3>
        <span className={statusChip(pir.status)}>{pir.status}</span>
        <span className="chip">OPENED · {pir.opened_reason}</span>
        {pir.ai_assisted ? <span className="chip accent">AI-ASSISTED DRAFT</span> : null}
        {published && pir.reviewer ? <span className="chip ok">SIGNED · {pir.reviewer}</span> : null}
      </div>

      <div className="pir-metrics">
        <div className="pir-metric">
          <div className="pir-metric-label">Users affected</div>
          <div className="pir-metric-value">{(pir.impact?.users_affected ?? 0).toLocaleString()}</div>
        </div>
        <div className="pir-metric">
          <div className="pir-metric-label">MTTA</div>
          <div className="pir-metric-value">{minutes(pir.mtta_minutes)}</div>
        </div>
        <div className="pir-metric">
          <div className="pir-metric-label">MTTR</div>
          <div className="pir-metric-value">{minutes(pir.mttr_minutes)}</div>
        </div>
        <div className="pir-metric">
          <div className="pir-metric-label">Adjusted MTTR (SCC deducted)</div>
          <div className="pir-metric-value">{minutes(pir.adjusted_mttr_minutes)}</div>
        </div>
        <div className="pir-metric">
          <div className="pir-metric-label">Detected (EAT)</div>
          <div className="pir-metric-value small">{fmtDateTime(pir.detected_at)}</div>
        </div>
      </div>

      {published && (
        <p className="muted">
          Published {fmtDateTime(pir.published_at)} EAT by <strong>{pir.reviewer || "—"}</strong>. A
          published review is the record of what a named human signed, so it is immutable — a
          correction belongs in a new action item, not in a rewrite of this text.
        </p>
      )}

      <BlamelessHint tripped={blameless} />

      <div className="pir-fields">
        {TEXT_FIELDS.map((f) => (
          <label key={f.key as string} className="pir-field">
            <span className="pir-field-label">
              {f.label}
              {f.hint ? <span className="muted"> · {f.hint}</span> : null}
            </span>
            <textarea
              rows={f.rows}
              disabled={published || busy}
              value={draft[f.key as string] ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, [f.key as string]: e.target.value }))}
            />
          </label>
        ))}
        <label className="pir-field">
          <span className="pir-field-label">Detection method</span>
          <input
            disabled={published || busy}
            value={draft.detection_method ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, detection_method: e.target.value }))}
          />
        </label>
        <label className="pir-field">
          <span className="pir-field-label">
            Revenue note
            <span className="muted">
              {" "}
              · the one impact field a human fills in; the rest are computed from the ticket
            </span>
          </span>
          <input
            disabled={published || busy}
            value={draft.revenue_note ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, revenue_note: e.target.value }))}
          />
        </label>
      </div>

      <div className="form-row">
        <label className="muted">
          Status{" "}
          <select
            disabled={published || busy}
            value={draft.status ?? pir.status}
            onChange={(e) => setDraft((d) => ({ ...d, status: e.target.value }))}
          >
            {(EDITABLE_STATUSES.includes(pir.status) ? EDITABLE_STATUSES : [pir.status, ...EDITABLE_STATUSES]).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button className="btn primary" disabled={published || busy || !dirty} onClick={save}>
          {dirty ? "Save changes" : "Saved"}
        </button>
        <button
          className="btn"
          disabled={published || busy}
          onClick={draftWithModel}
          title="Queues a redacted LLM_CALL on the outbox. DRAFT text only — it changes no status and publishes nothing."
        >
          Draft with model (assist)
        </button>
        {saveNote && <span className="chip ok">{saveNote}</span>}
      </div>

      {saveError && (
        <div className="pir-error">
          <span className="chip warn">REJECTED</span>
          <div>
            <div>{saveError}</div>
            {blameless && (
              <div className="muted" style={{ marginTop: "0.3rem" }}>
                Nothing was saved. Edit <strong>root causes</strong> or{" "}
                <strong>contributing factors</strong> above and save again.
              </div>
            )}
          </div>
        </div>
      )}

      {/* ---- action items ------------------------------------------------- */}
      <h4 className="pir-h4">
        Action items <span className="muted">· the owner is a role token, never a person (§7.7.6)</span>
      </h4>
      <table>
        <thead>
          <tr>
            <th>Priority</th>
            <th>Type</th>
            <th>Action</th>
            <th>Owner token</th>
            <th>Due (EAT)</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {actions.map((a) => {
            const closed = a.status === "DONE" || a.status === "WONT_DO";
            const words = dueWords(a.due_date, closed);
            return (
              <tr key={a.id}>
                <td>
                  <span className={"pill " + (a.priority === "P0" ? "P1" : a.priority)}>{a.priority}</span>
                </td>
                <td className="muted">{a.type}</td>
                <td>
                  {a.description}
                  {a.tracking_ref ? <div className="muted">ref {a.tracking_ref}</div> : null}
                </td>
                <td className="pir-mono">{a.owner_token}</td>
                {/* §7.10: the absolute date and the relative reading, always together. */}
                <td>
                  {fmtDate(a.due_date)}
                  <div className={words.indexOf("OVERDUE") >= 0 ? "pir-overdue" : "muted"}>{words}</div>
                </td>
                <td>
                  <span className={actionChip(a.status)}>{a.status}</span>
                  {!published && (
                    <select
                      disabled={busy}
                      value={a.status}
                      onChange={(e) => moveAction(a, e.target.value)}
                      style={{ marginLeft: "0.35rem" }}
                    >
                      {ACTION_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  )}
                  {a.closed_at ? <div className="muted">closed {fmtDateTime(a.closed_at)}</div> : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {actions.length === 0 && (
        <div className="empty">
          No action items yet. A user-affecting outage needs at least one P0 or P1 before this review
          can be published.
        </div>
      )}

      {!published && (
        <div className="pir-action-form">
          <select
            value={newAction.type}
            disabled={busy}
            onChange={(e) => setNewAction((a) => ({ ...a, type: e.target.value }))}
          >
            {ACTION_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <select
            value={newAction.priority}
            disabled={busy}
            onChange={(e) => setNewAction((a) => ({ ...a, priority: e.target.value }))}
          >
            {ACTION_PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <input
            placeholder="What will be done"
            value={newAction.description}
            disabled={busy}
            onChange={(e) => setNewAction((a) => ({ ...a, description: e.target.value }))}
          />
          <input
            placeholder="Owner token (RNIO / FE / MSP_POWER)"
            value={newAction.owner_token}
            disabled={busy}
            onChange={(e) => setNewAction((a) => ({ ...a, owner_token: e.target.value }))}
          />
          <input
            type="date"
            value={newAction.due_date}
            disabled={busy}
            onChange={(e) => setNewAction((a) => ({ ...a, due_date: e.target.value }))}
          />
          <button className="btn" disabled={busy} onClick={addAction}>
            Add action
          </button>
        </div>
      )}
      {actionError && (
        <div className="pir-error">
          <span className="chip warn">REJECTED</span>
          <div>{actionError}</div>
        </div>
      )}

      {/* ---- timeline ----------------------------------------------------- */}
      <h4 className="pir-h4">
        Assembled timeline{" "}
        <span className="muted">· work notes, agent steps, stop clocks, broadcasts, approvals</span>
      </h4>
      <div className="list">
        {(pir.timeline ?? []).map((t, i) => (
          <div key={t.ts + "-" + i} className="row" style={{ cursor: "default" }}>
            <span className="chip">{t.kind}</span>
            <div>
              <div>
                <strong>{t.title}</strong> · {t.actor_role}
              </div>
              <div className="muted">{t.detail}</div>
            </div>
            <span className="muted">{fmtDateTime(t.ts)}</span>
          </div>
        ))}
      </div>
      {(pir.timeline ?? []).length === 0 && (
        <div className="empty">No timeline entries were assembled for this incident.</div>
      )}

      {/* ---- publish ------------------------------------------------------ */}
      {!published && (
        <>
          <h4 className="pir-h4">Publish</h4>
          <p className="muted">
            Publishing is an A2 act: a <strong>named human</strong> signs it (§5.3.18), and the
            server checks every precondition in one pass so you see them all at once rather than
            one per attempt. With sign-in enabled the signed-in user is recorded as the reviewer and
            the name typed here is ignored.
          </p>
          <div className="form-row">
            <input
              placeholder="Reviewer (a person's name, not a role label)"
              value={reviewer}
              disabled={busy}
              onChange={(e) => setReviewer(e.target.value)}
              style={{ minWidth: "18rem" }}
            />
            <input
              placeholder="Rationale for the audit row (optional)"
              value={rationale}
              disabled={busy}
              onChange={(e) => setRationale(e.target.value)}
              style={{ minWidth: "18rem" }}
            />
            <button className="btn good" disabled={busy || dirty} onClick={publish}>
              Publish review
            </button>
            {dirty && <span className="chip warn">UNSAVED EDITS · save first</span>}
          </div>
          {publishError && (
            <div className="pir-blockers">
              <div className="pir-blockers-head">
                <span className="chip warn">NOT PUBLISHED</span>
                <strong>
                  {gates.length > 1
                    ? gates.length + " things still block this review"
                    : "This review cannot be published yet"}
                </strong>
              </div>
              <ul>
                {gates.map((g) => (
                  <li key={g.title}>
                    <strong>{g.title}</strong>
                    <div className="muted">{g.fix}</div>
                  </li>
                ))}
              </ul>
              {/* The server's exact words, never paraphrased away — and the only thing shown at
                  all when the message is one this build does not recognise. */}
              <div className="pre">{publishError}</div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
