/**
 * Reading an API failure without losing what the server actually said.
 *
 * `api.ts::req` rejects with `new Error(`${status}: ${bodyText}`)`, and the body is
 * FastAPI's `{"detail": "…"}`. Two lanes depend on getting that detail back out intact:
 *
 *  - the PIR blameless validator answers **422** with one exact sentence a reviewer is
 *    meant to act on (§7.7.3), and
 *  - `POST /pir/{id}/publish` answers **422** with *every* unmet precondition joined by
 *    `"; "` — deliberately, because "a reviewer who has to discover three problems through
 *    three round-trips stops publishing reviews" (`services/pir.publish_blockers`).
 *
 * A `friendlyError`-style clamp would truncate exactly the part that matters, so these
 * helpers do the opposite: they unwrap the envelope and hand back the server's words whole.
 * Nothing here ever throws — a malformed body degrades to the raw text.
 */

/** The HTTP status `req` prefixed onto the message, or `null` if there is none. */
export function statusOf(err: unknown): number | null {
  const raw = err instanceof Error ? err.message : typeof err === "string" ? err : "";
  const m = /^(\d{3}):/.exec(raw.trim());
  return m ? Number(m[1]) : null;
}

/** True when the failure carries exactly this status. */
export function isStatus(err: unknown, code: number): boolean {
  return statusOf(err) === code;
}

/**
 * The server's sentence, unwrapped from `{"detail": …}` and un-prefixed.
 *
 * FastAPI also answers 422 with a *list* of pydantic errors on a body it cannot parse;
 * those are joined into one readable line rather than rendered as JSON at 03:00.
 */
export function detailOf(err: unknown, fallback = "The request failed."): string {
  const raw = (err instanceof Error ? err.message : typeof err === "string" ? err : "").trim();
  if (!raw) return fallback;
  const body = raw.replace(/^\d{3}:\s*/, "");
  if (!body) return fallback;
  try {
    const parsed = JSON.parse(body);
    const detail = parsed?.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (Array.isArray(detail)) {
      const lines = detail
        .map((d: any) => {
          const where = Array.isArray(d?.loc) ? d.loc.filter((p: any) => p !== "body").join(".") : "";
          const msg = typeof d?.msg === "string" ? d.msg : "";
          return [where, msg].filter(Boolean).join(": ");
        })
        .filter(Boolean);
      if (lines.length) return lines.join("; ");
    }
  } catch {
    /* not JSON — the raw body is the best answer we have */
  }
  return body;
}

/** `"404: …"` — the shape every flag-gated lane uses to mean "not enabled here". */
export function isLaneOff(err: unknown): boolean {
  return statusOf(err) === 404;
}
