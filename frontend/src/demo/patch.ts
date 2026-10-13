/**
 * The reader of the recorder's patches (scripts/demo/record.py `diff` and `apply`; this file is
 * the same algorithm, line for line). A patch turns one recorded answer into the next:
 *
 *  - `{$v: x}`: the value is now x;
 *  - `{$n: d}`: a number moved by d (so two presses that each lower a count lower it twice);
 *  - `{$o: {k: patch}, $d: [k]}`: an object, key by key;
 *  - `{$l: {rm, ch, add, ord?}}`: a list whose items are named by `keyOf`: these left, these
 *    changed, these arrived at these places, and (when kept items moved) the new order.
 *
 * `leaf` sees every value a patch writes, so the caller can move a timestamp or swap a
 * placeholder for what the visitor typed.
 */

const KEY_FIELDS = ["id", "task_id", "uid", "key", "code", "region_code", "incident_id", "name", "seq"];

/** Sorted keys, no spaces: the same text Python's `json.dumps(sort_keys=True, separators=(",", ":"))` writes. */
export function canon(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (Array.isArray(v)) return `[${v.map(canon).join(",")}]`;
  const o = v as Record<string, unknown>;
  return `{${Object.keys(o)
    .sort()
    .map((k) => `${JSON.stringify(k)}:${canon(o[k])}`)
    .join(",")}}`;
}

export function keyOf(item: unknown): string {
  if (item && typeof item === "object" && !Array.isArray(item)) {
    const o = item as Record<string, unknown>;
    for (const k of KEY_FIELDS) {
      const v = o[k];
      if (typeof v === "string" || (typeof v === "number" && Number.isInteger(v))) return `${k}:${v}`;
    }
  }
  return `j:${canon(item)}`;
}

export type Leaf = (value: unknown) => unknown;
const same: Leaf = (v) => v;

/** Deep copy of a value with `leaf` applied to every string in it. */
export function mapStrings(value: unknown, leaf: Leaf): unknown {
  if (typeof value === "string") return leaf(value);
  if (Array.isArray(value)) return value.map((x) => mapStrings(x, leaf));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = mapStrings(v, leaf);
    return out;
  }
  return value;
}

export function applyPatch(value: unknown, patch: any, leaf: Leaf = same): unknown {
  if (patch == null) return value;
  if ("$v" in patch) return mapStrings(patch.$v, leaf);
  if ("$n" in patch) return (typeof value === "number" ? value : 0) + patch.$n;
  if ("$o" in patch) {
    const out: Record<string, unknown> = value && typeof value === "object" && !Array.isArray(value) ? { ...(value as object) } : {};
    for (const [k, p] of Object.entries(patch.$o as Record<string, unknown>)) out[k] = applyPatch(out[k], p, leaf);
    for (const k of (patch.$d as string[] | undefined) ?? []) delete out[k];
    return out;
  }
  const lp = patch.$l as { rm: string[]; ch: Record<string, unknown>; add: [number, unknown][]; ord?: string[] };
  const gone = new Set(lp.rm);
  let items = (Array.isArray(value) ? value : []).filter((x) => !gone.has(keyOf(x)));
  items = items.map((x) => {
    const k = keyOf(x);
    return k in lp.ch ? applyPatch(x, lp.ch[k], leaf) : x;
  });
  for (const [i, x] of lp.add) items.splice(Math.min(i, items.length), 0, mapStrings(x, leaf));
  if (lp.ord) {
    const rank = new Map(lp.ord.map((k, n) => [k, n] as const));
    const last = rank.size;
    // A stable sort: items the order does not name keep their places relative to each other.
    items = items
      .map((x, n) => ({ x, n, r: rank.get(keyOf(x)) ?? last }))
      .sort((a, b) => a.r - b.r || a.n - b.n)
      .map((e) => e.x);
  }
  return items;
}
