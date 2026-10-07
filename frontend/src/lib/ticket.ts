/** Not open: the service is back (RESTORED, awaiting close) or the record is done. The one meaning
 *  of "open" on every page, matching the Incident board's Open tab and `/api/v1/metrics/summary`
 *  (`services.lifecycle.NOT_OPEN_STATUSES`), so a figure and the list beside it agree. */
const NOT_OPEN = new Set(["RESTORED", "CLOSED", "CANCELLED"]);

export function isOpenTicket(status: unknown): boolean {
  return !NOT_OPEN.has(String(status ?? "").toUpperCase());
}
