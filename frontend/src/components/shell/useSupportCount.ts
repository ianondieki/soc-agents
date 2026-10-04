import { useEffect, useState } from "react";

/**
 * Support cases with a person on them (escalated, or awaiting an approval), for the badge on the
 * sidebar's Support group. `GET /api/v1/support/metrics` may not exist yet (404) or fail: the
 * badge then shows nothing, and the next look is a long way off. A plain fetch, outside api.ts,
 * so a missing route never counts towards "API unreachable".
 */
export function useSupportCount(): number | null {
  const [n, setN] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const again = (ms: number) => {
      if (!cancelled) timer = window.setTimeout(load, ms);
    };
    const load = async () => {
      try {
        const r = await fetch("/api/v1/support/metrics", { headers: { Accept: "application/json" } });
        if (!r.ok) {
          if (!cancelled) setN(null);
          again(r.status === 404 ? 300_000 : 60_000);
          return;
        }
        const m = await r.json();
        const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
        const by = m && typeof m === "object" ? (m.by_status ?? m) : {};
        const total = num(m?.escalated ?? by?.escalated) + num(m?.awaiting_approval ?? by?.awaiting_approval);
        if (!cancelled) setN(total);
        again(30_000);
      } catch {
        if (!cancelled) setN(null);
        again(60_000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);
  return n;
}
