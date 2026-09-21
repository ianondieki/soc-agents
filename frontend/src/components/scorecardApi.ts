import type { ComputeResult, Scorecard, TransitionResult, Vendor } from "./scorecardModel";
import { computeQuery, listQuery } from "./scorecardModel";

/**
 * The scorecard routes (`api/routers/scorecards.py`, spec §7.6.3) and `GET /vendors` for the
 * picker.
 *
 * This file sits beside the page and not in `src/api.ts` only because this lane was scoped to
 * new files. `req` behaves exactly like `api.ts::req`. It rejects with
 * `new Error("<status>: <body>")`, so `lib/apiError` (`statusOf`, `detailOf`) reads these
 * failures the same way it reads every other route's. To fold it into `api.ts` later, move the
 * methods; nothing else changes.
 *
 * Every route answers 404 while `SCORECARDS_ENABLED` is off (the shipped default).
 */
async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}

export const scorecardApi = {
  /** Cards this caller may see, newest period first, WITHOUT lines. */
  list: (f: { vendor?: string; period?: string; status?: string }) => req<Scorecard[]>(`/api/v1/scorecards${listQuery(f)}`),
  /** One card WITH its 22 lines. 404 for another operator's card and for an unreleased card this role may not see. */
  get: (id: string) => req<Scorecard>(`/api/v1/scorecards/${encodeURIComponent(id)}`),
  /** shift_supervisor+. Computes DRAFT / SHADOW / WITHHELD only; this route cannot publish. */
  compute: (f: { period?: string; vendor?: string }) =>
    req<ComputeResult>(`/api/v1/scorecards/compute${computeQuery(f)}`, { method: "POST", body: "{}" }),
  /** duty_manager / admin. `reviewed_by` is honoured only while auth is off (`api.deps._actor`). */
  shadowReview: (id: string, body: { rationale: string; reviewed_by?: string }) =>
    req<TransitionResult>(`/api/v1/scorecards/${encodeURIComponent(id)}/shadow-review`, { method: "POST", body: JSON.stringify(body) }),
  /** duty_manager / admin. Starts the dispute window. Sends nothing to anyone. */
  publish: (id: string, body: { reason: string; published_by?: string }) =>
    req<TransitionResult>(`/api/v1/scorecards/${encodeURIComponent(id)}/publish`, { method: "POST", body: JSON.stringify(body) }),
  /** duty_manager / admin. Only after the window closes, with no OPEN dispute. */
  finalise: (id: string, body: { reason?: string; finalised_by?: string }) =>
    req<TransitionResult>(`/api/v1/scorecards/${encodeURIComponent(id)}/finalise`, { method: "POST", body: JSON.stringify(body) }),
  /** READERS. Used for the vendor picker only; on failure the picker falls back to the codes in the list. */
  vendors: () => req<Vendor[]>("/api/v1/vendors"),
};
