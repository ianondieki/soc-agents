/**
 * Moving the recording to now. Every instant in the recording is moved by one offset (the time
 * between when it was recorded and when the visitor started the demo, or the storm), so a ticket
 * the storm opened reads "opened a minute ago" whenever the link is opened. Planned work further
 * ahead than `ROUND_AFTER_MS` moves by whole hours instead, so a window planned for 01:00 stays at
 * 01:00. Times written inside text ("by 17:08 EAT on Tue 13 Oct") move the same way.
 */

const ISO = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/;
const EAT_MS = 3 * 3_600_000;
const HOUR = 3_600_000;
/** Instants this far past the recording are planned work: they move by whole hours. */
const ROUND_AFTER_MS = 6 * HOUR;

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");

export interface Shift {
  /** The offset, in ms. */
  by: number;
  /** When the recording was made (ms), to tell planned work from the rest. */
  recordedAt: number;
}

function moved(ms: number, s: Shift): number {
  if (ms > s.recordedAt + ROUND_AFTER_MS) return ms + Math.round(s.by / HOUR) * HOUR;
  return ms + s.by;
}

/** One ISO instant, moved, written back in the same style (Z or none, with or without fraction). */
export function shiftIso(value: string, s: Shift): string {
  const m = ISO.exec(value);
  if (!m) return value;
  const zone = m[8];
  const naive = !zone;
  const ms = Date.parse(naive ? `${value}Z` : value);
  if (!Number.isFinite(ms)) return value;
  const iso = new Date(moved(ms, s)).toISOString(); // 2026-10-13T10:02:01.247Z
  const body = m[7] ? iso.slice(0, 23) : m[6] !== undefined ? iso.slice(0, 19) : iso.slice(0, 16);
  if (naive) return body;
  return zone === "Z" ? `${body}Z` : `${body}Z`;
}

// "2026-10-13 13:07 EAT", "17:08 EAT on Tue 13 Oct", or a bare "04:00 EAT".
const TEXT_TIME =
  /(\b\d{4}-\d{2}-\d{2} )?\b([01]\d|2[0-3]):([0-5]\d) EAT( on (?:Sun|Mon|Tue|Wed|Thu|Fri|Sat) (\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))?/g;

// "2026-10-13 10:17:18.219426": a naive UTC instant written into a note ("SLA ack due …").
const TEXT_UTC = /\b(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(\.\d+)?\b(?! EAT)/g;

/** Times written in text, moved: a bare time keeps its own day's place (the offset's hours). */
export function shiftText(value: string, s: Shift): string {
  if (/\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(value)) {
    value = value.replace(TEXT_UTC, (all, date: string, time: string, frac?: string) => {
      const ms = Date.parse(`${date}T${time}Z`);
      if (!Number.isFinite(ms)) return all;
      const iso = new Date(moved(ms, s)).toISOString();
      return `${iso.slice(0, 10)} ${iso.slice(11, 19)}${frac ?? ""}`;
    });
  }
  if (!value.includes(" EAT")) return value;
  const year = new Date(s.recordedAt).getUTCFullYear();
  return value.replace(TEXT_TIME, (all, date: string | undefined, hh: string, mm: string, on: string | undefined, dd?: string, mon?: string) => {
    let base: number;
    if (date) base = Date.parse(`${date.trim()}T${hh}:${mm}:00Z`) - EAT_MS;
    else if (on && dd && mon) base = Date.UTC(year, MONTHS.indexOf(mon), Number(dd), Number(hh), Number(mm)) - EAT_MS;
    else {
      // No day: move the time of day by the offset, as the instants around it move.
      const day = new Date(s.recordedAt + EAT_MS);
      base = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), Number(hh), Number(mm)) - EAT_MS;
    }
    if (!Number.isFinite(base)) return all;
    const t = new Date(moved(base, s) + EAT_MS);
    const hm = `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())} EAT`;
    if (date) return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())} ${hm}`;
    if (on) return `${hm} on ${WEEKDAYS[t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
    return hm;
  });
}

export function shiftString(value: string, s: Shift): string {
  if (s.by === 0) return value;
  if (value.length >= 16 && value.length <= 35 && value[4] === "-" && value[10] === "T") return shiftIso(value, s);
  return shiftText(value, s);
}
