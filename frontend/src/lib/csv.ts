import { parseInstant } from "./time";

/**
 * A spreadsheet file from rows, for the "Download CSV" buttons. RFC 4180 quoting (a cell with a
 * comma, a quote or a line break is quoted, quotes doubled), CRLF line ends, and a byte-order mark
 * so Excel reads the UTF-8 (site names, "M‑PESA") as written. A cell that a spreadsheet would run
 * as a formula (it starts with = + - @, a tab or a carriage return) gets a leading apostrophe:
 * a ticket title is free text from an alarm or a vendor, and must never execute on a desk.
 */
export function toCsv(header: string[], rows: unknown[][]): string {
  const lines = [header, ...rows].map((r) => r.map(cell).join(","));
  return "﻿" + lines.join("\r\n") + "\r\n";
}

function cell(value: unknown): string {
  let s = value == null ? "" : typeof value === "number" ? (Number.isFinite(value) ? String(value) : "") : String(value);
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** "2026-10-12 14:05" in EAT (UTC+3 all year), the form a spreadsheet sorts; "" when unknown. */
export function eatStamp(value: unknown): string {
  const d = parseInstant(value);
  if (!d) return "";
  const t = new Date(d.getTime() + 3 * 3_600_000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${t.getUTCFullYear()}-${p(t.getUTCMonth() + 1)}-${p(t.getUTCDate())} ${p(t.getUTCHours())}:${p(t.getUTCMinutes())}`;
}

/** Hand the browser a file to save. The object URL is released once the click has been taken. */
export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
