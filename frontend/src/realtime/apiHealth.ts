/**
 * Whether the API is answering, tracked in one place for every call (api.ts `req` and the storm's
 * own fetch report here), plus the server's clock as its answers state it.
 *
 * Honest state: one failed call is not an outage. A page shows "API unreachable" only after two
 * consecutive calls failed, any calls, with no answer in between; any answer resets the count. An
 * answer is any HTTP response below 500, a 404 from a lane that is off included: the API was
 * reached. A failure is a network error, a timeout or a 5xx (the dev proxy answers 500 when the
 * backend is down).
 *
 * The server clock: every answer carries a `Date` header in the server's time. The realtime layer
 * uses it to tell a frame the socket replays on connect (it happened before the connection opened,
 * so the lists the pages fetch already include it) from a live one, without trusting this
 * machine's clock. The header has one-second resolution and the answer's travel time is not
 * subtracted, so the estimate is a little early, never late: an old frame can at worst cost one
 * extra refetch, a new one is never mistaken for history.
 */

type Listener = () => void;

/** Consecutive failures at which the page says "API unreachable". */
export const UNREACHABLE_AFTER = 2;

let failures = 0;
/** Server time (ms) of the first answer this tab received, from its `Date` header (null before it). */
let firstAnswerMs: number | null = null;
/** Server clock minus this machine's clock, a lower bound (max over answers of Date − arrival). */
let offsetMs: number | null = null;
const listeners = new Set<Listener>();
const firstAnswerWaiters = new Set<() => void>();

function emit(): void {
  for (const fn of Array.from(listeners)) {
    try {
      fn();
    } catch {
      /* one listener must not starve the others */
    }
  }
}

/** A response came back: the API was reached. `date` is its `Date` header, when it had one. */
export function noteAnswer(date: string | null | undefined): void {
  const at = Date.now();
  const server = date ? Date.parse(date) : NaN;
  if (Number.isFinite(server)) {
    const off = server - at;
    offsetMs = offsetMs == null ? off : Math.max(offsetMs, off);
    if (firstAnswerMs == null) firstAnswerMs = server;
  } else if (firstAnswerMs == null) {
    // No header (an odd proxy): this machine's clock is the best there is.
    firstAnswerMs = at;
    offsetMs = offsetMs ?? 0;
  }
  if (firstAnswerWaiters.size > 0) {
    const waiting = Array.from(firstAnswerWaiters);
    firstAnswerWaiters.clear();
    for (const fn of waiting) {
      try {
        fn();
      } catch {
        /* ignore */
      }
    }
  }
  lastFailure = null;
  if (failures !== 0) {
    const was = failures;
    failures = 0;
    if (was >= UNREACHABLE_AFTER) emit();
  }
}

/** The last failure: the same call failing again within a second is one failure, not two (React's
 *  development double mount sends a page's mount request twice; a retry storm is one outage). */
let lastFailure: { path: string; at: number } | null = null;
const SAME_CALL_MS = 1000;

/** A call failed (network error, timeout, 5xx). `path` names the call (its URL without a query). */
export function noteFailure(path = ""): void {
  const at = Date.now();
  const key = path.split("?")[0];
  const repeat = !!key && !!lastFailure && lastFailure.path === key && at - lastFailure.at < SAME_CALL_MS;
  lastFailure = { path: key, at };
  if (repeat) return;
  failures += 1;
  if (failures === UNREACHABLE_AFTER) emit();
}

/** True while fewer than two consecutive calls have failed. */
export function apiHealthy(): boolean {
  return failures < UNREACHABLE_AFTER;
}

export function subscribeHealth(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The server's time now, estimated from the answers so far; null before the first answer. */
export function serverNow(): number | null {
  return offsetMs == null ? null : Date.now() + offsetMs;
}

/**
 * The server's time (ms) of the first successful REST answer this tab received, read from its
 * `Date` header (this machine's clock when it had none); null before it. The pages' mount loads
 * were asked around then, so a frame stamped after it may be news to them: the realtime layer
 * never counts such a frame as replayed history (realtime/useRealtime.ts).
 */
export function firstAnswerServerMs(): number | null {
  return firstAnswerMs;
}

/** Run `fn` once the first answer has arrived (at once if it already has). Returns a cancel. */
export function onFirstAnswer(fn: () => void): () => void {
  if (firstAnswerMs != null) {
    fn();
    return () => undefined;
  }
  firstAnswerWaiters.add(fn);
  return () => {
    firstAnswerWaiters.delete(fn);
  };
}

/** Report a finished fetch: a response below 500 is an answer, anything else a failure. */
export function noteResponse(r: Response, path = ""): void {
  if (r.status >= 500) noteFailure(path);
  else noteAnswer(r.headers.get("date"));
}
