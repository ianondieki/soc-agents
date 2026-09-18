/**
 * EAT (Africa/Nairobi) time formatting — verified defect #41.
 *
 * Kenya is UTC+3 all year (no DST). The backend stores **naive UTC** and, since
 * Wave 1 (`services/clock.py` + `api/serializers.py::_z`), stamps an explicit `Z`
 * on every timestamp that leaves a serializer. A few hand-built endpoints still
 * return a bare `"2026-09-17T09:00:00"` (see `/api/v1/audit`,
 * `/api/v1/incidents/{id}/timeline`, `/api/v1/shifts/ledger`, which return raw
 * `datetime` objects). A designator-less ISO string is read by the browser as
 * *local* time, so in Nairobi it silently backdates every row by three hours —
 * which is the defect. `parseInstant` therefore treats a bare timestamp as UTC,
 * and every formatter renders in `Africa/Nairobi`.
 *
 * Nothing here ever throws: an unparseable / missing value returns the caller's
 * fallback (default "—"), and if the browser's ICU has no tzdata for Nairobi we
 * fall back to fixed +03:00 arithmetic, which is exact for Kenya.
 */

export const EAT_TZ = "Africa/Nairobi";
export const EAT_LOCALE = "en-KE";
export const EAT_LABEL = "EAT";

/** Kenya is UTC+3 year-round, no DST — safe as a fixed fallback offset. */
const EAT_OFFSET_MINUTES = 180;

/** Trailing ISO-8601 timezone designator: `Z`, `+03`, `+0300`, `-03:00`. */
const HAS_DESIGNATOR = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/i;
/** A time component at all — date-only strings are already UTC per the JS spec. */
const HAS_TIME = /\d{2}:\d{2}/;

/**
 * Parse an API timestamp into a Date, reading designator-less strings as UTC.
 * Returns `null` for anything unusable rather than an Invalid Date.
 */
export function parseInstant(value: unknown): Date | null {
  if (value == null) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  if (typeof value !== "string") return null;

  let s = value.trim();
  if (!s) return null;
  s = s.replace(" ", "T"); // tolerate "YYYY-MM-DD HH:MM:SS"
  s = s.replace(/(\.\d{3})\d+/, "$1"); // Python emits 6 fractional digits; JS specifies 3
  if (HAS_TIME.test(s) && !HAS_DESIGNATOR.test(s)) s += "Z";

  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

type FormatterKey = string;
const formatterCache = new Map<FormatterKey, Intl.DateTimeFormat | null>();

function formatter(opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat | null {
  const key = JSON.stringify(opts);
  const cached = formatterCache.get(key);
  if (cached !== undefined) return cached;
  let made: Intl.DateTimeFormat | null = null;
  try {
    made = new Intl.DateTimeFormat(EAT_LOCALE, { timeZone: EAT_TZ, ...opts });
  } catch {
    made = null; // browser ICU without Africa/Nairobi — fall back to fixed +03:00
  }
  formatterCache.set(key, made);
  return made;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Fixed +03:00 rendering, used only when Intl cannot resolve the zone. */
function manual(d: Date, withDate: boolean, withSeconds: boolean): string {
  const t = new Date(d.getTime() + EAT_OFFSET_MINUTES * 60_000);
  const time =
    `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}` +
    (withSeconds ? `:${pad(t.getUTCSeconds())}` : "");
  if (!withDate) return time;
  return `${pad(t.getUTCDate())} ${MONTHS[t.getUTCMonth()]}, ${time}`;
}

function render(
  value: unknown,
  opts: Intl.DateTimeFormatOptions,
  withDate: boolean,
  withSeconds: boolean,
  fallback: string
): string {
  const d = parseInstant(value);
  if (!d) return fallback;
  const fmt = formatter(opts);
  if (!fmt) return manual(d, withDate, withSeconds);
  try {
    return fmt.format(d);
  } catch {
    return manual(d, withDate, withSeconds);
  }
}

const TIME_OPTS: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
};
const HM_OPTS: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", hour12: false };
const DATETIME_OPTS: Intl.DateTimeFormatOptions = {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
};
const DATE_OPTS: Intl.DateTimeFormatOptions = { day: "2-digit", month: "short", year: "numeric" };

/** `14:05:33` in EAT. Replaces the old `String(ts).slice(11, 19)` (which showed UTC). */
export function fmtTime(value: unknown, fallback = "—"): string {
  return render(value, TIME_OPTS, false, true, fallback);
}

/** `14:05` in EAT. */
export function fmtHM(value: unknown, fallback = "—"): string {
  return render(value, HM_OPTS, false, false, fallback);
}

/** `17 Sep, 14:05:33` in EAT. Replaces the old `String(ts).slice(0, 19)`. */
export function fmtDateTime(value: unknown, fallback = "—"): string {
  return render(value, DATETIME_OPTS, true, true, fallback);
}

/** `17 Sep 2026` in EAT. */
export function fmtDate(value: unknown, fallback = "—"): string {
  return render(value, DATE_OPTS, true, false, fallback);
}

/** `14:05 EAT` — for the few places where the zone must be spelled out. */
export function fmtEAT(value: unknown, fallback = "—"): string {
  const out = fmtHM(value, "");
  return out ? `${out} ${EAT_LABEL}` : fallback;
}
