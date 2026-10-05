import { useState } from "react";
import { errorDetail, isNetworkError, statusOfError, supportApi, type IncidentCustomers } from "../../lib/support";
import "./SendUpdateNow.css";

/**
 * "Send the update now" (docs/CLOSE_THE_LOOP.md §7.1): raises a held-back (or never-written)
 * customer update again. The server follows the same ladder, so it sends now or raises a fresh
 * approval card, and answers with the incident's customers payload; `onDone` hands that to the
 * parent, which draws the new state. Focus then moves to `focusId`, the parent's notice words,
 * so the keyboard lands on what happened.
 */
export default function SendUpdateNow({
  incidentId,
  onDone,
  focusId,
}: {
  incidentId: string;
  onDone: (d: IncidentCustomers) => void;
  focusId: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const send = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const d = await supportApi.sendCustomerUpdate(incidentId);
      onDone(d);
      window.requestAnimationFrame(() => document.getElementById(focusId)?.focus({ preventScroll: true }));
    } catch (e) {
      const s = statusOfError(e);
      setError(
        s === 409
          ? errorDetail(e, "The update cannot be sent right now.")
          : s === 403
            ? `Not permitted: ${errorDetail(e, "your role cannot send it")}.`
            : isNetworkError(e)
              ? "The API is unreachable; nothing was sent. Try again."
              : `Nothing was sent: ${errorDetail(e, "the request failed")}.`
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="send-now">
      <button type="button" className="btn sm" onClick={send} aria-disabled={busy || undefined} aria-busy={busy || undefined}>
        {busy ? "Sending…" : "Send the update now"}
      </button>
      {error && (
        <span className="send-now-error" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
