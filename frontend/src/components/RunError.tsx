/**
 * The line under a run that says why it did not succeed — one component for both run
 * lists (the Observatory and Mission control): the two pages had already drifted apart once
 * on a FAILED run with no summary.
 *
 * Shown for any status that is not in flight and not a success. That includes CANCELLED,
 * because rejecting a HITL gate ends the run CANCELLED with the rejection reason in
 * error_summary (main.py _finish_waiting_run), and hiding it left the operator with a
 * bare word. A FAILED run says so even with no summary recorded — the absence is itself
 * the finding; any other status with nothing to say shows nothing.
 */
export function RunError({ status, summary }: { status?: string; summary?: string | null }) {
  if (!status || status === "RUNNING" || status === "WAITING_HITL" || status === "SUCCEEDED") return null;
  const text = typeof summary === "string" ? summary.trim() : "";
  if (!text && status !== "FAILED") return null;
  return (
    <div className="muted" style={{ color: "var(--danger-text)", overflowWrap: "anywhere" }}>
      {status === "FAILED" ? "Error" : "Reason"}: {text || "no error summary recorded"}
    </div>
  );
}
