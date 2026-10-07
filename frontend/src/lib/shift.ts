/**
 * The shift clock, shared by the Shift desk and the front page's handover countdown. The shift's
 * name comes from the profile (the backend's clock), its hours from the operator config.
 */

/** EAT is UTC+3 all year: no daylight saving to allow for. */
export const EAT_MS = 3 * 3600_000;

/** The operator config's shifts when the profile does not say (services/shifts.py reads the same). */
export const DEFAULT_HOURS: Record<string, { start: string; end: string }> = {
  day: { start: "08:00", end: "20:00" },
  night: { start: "20:00", end: "08:00" },
};

export const minutesOf = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
};

/** The current shift's window as instants: a night shift runs across midnight. */
export function shiftWindow(hours: { start: string; end: string }, now: Date): { start: Date; end: Date } {
  const eat = new Date(now.getTime() + EAT_MS); // read its UTC fields as the time in Nairobi
  const midnight = Date.UTC(eat.getUTCFullYear(), eat.getUTCMonth(), eat.getUTCDate()) - EAT_MS;
  const s = minutesOf(hours.start);
  const e = minutesOf(hours.end);
  const nowMin = eat.getUTCHours() * 60 + eat.getUTCMinutes();
  let startMs = midnight + s * 60_000;
  let endMs = midnight + e * 60_000;
  if (e <= s) {
    // Across midnight: before the end, it began yesterday; after the start, it ends tomorrow.
    if (nowMin < e) startMs -= 86_400_000;
    else endMs += 86_400_000;
  }
  return { start: new Date(startMs), end: new Date(endMs) };
}

/** The shift on now, the one that takes over, and when the handover is. */
export function currentShift(profile: any, now: Date): { shift: "day" | "night"; next: "day" | "night"; start: Date; end: Date } {
  const shift = String(profile?.shift || "day").toLowerCase() === "night" ? "night" : "day";
  const hours = profile?.shift_hours?.[shift] || DEFAULT_HOURS[shift];
  const win = shiftWindow(hours, now);
  return { shift, next: shift === "day" ? "night" : "day", ...win };
}
