/**
 * Which feature lanes this deployment runs, from `GET /api/v1/profile` (`lanes`, on/off only).
 *
 * A lane that is off answers 404 on every route, which every page already reads as "this lane is
 * off" (components/LaneOff). Asking anyway cost a red "Failed to load resource" in the browser
 * console on every visit to the front page, a ticket and each lane's page. So a request into a
 * lane known to be off is answered here with the same 404, without going out. Until the profile
 * has answered, a lane request waits for it (briefly: an older API without `lanes`, or a failed
 * profile call, lets every request through as before).
 */

type Lanes = Record<string, boolean>;

let lanes: Lanes | null = null;
let settle: (() => void) | null = null;
const ready = new Promise<void>((resolve) => {
  settle = resolve;
});
// Never hold a request for long: an unanswered profile means "unknown", and unknown asks.
const LANE_WAIT_MS = 2500;
window.setTimeout(() => settle?.(), LANE_WAIT_MS);

/** Called once the profile answers (or fails: `null`). */
export function setLanes(value: unknown): void {
  lanes = value && typeof value === "object" ? (value as Lanes) : null;
  settle?.();
}

/** Each lane's routes. The stop clocks and vendor rows ride on the scorecards lane (§7.6). */
const ROUTES: ReadonlyArray<[RegExp, string]> = [
  [/^\/api\/v1\/(scorecards|vendors)(\/|\?|$)/, "scorecards"],
  [/^\/api\/v1\/incidents\/[^/]+\/clock(\/|\?|$)/, "scorecards"],
  [/^\/api\/v1\/pir(\/|\?|$)/, "pir"],
  [/^\/api\/v1\/contracts(\/|\?|$)/, "contracts"],
  [/^\/api\/v1\/maintenance(\/|\?|$)/, "maintenance"],
];

function laneOf(path: string): string | null {
  for (const [re, lane] of ROUTES) if (re.test(path)) return lane;
  return null;
}

/** True only when the profile said this lane is off; unknown is not off. */
export function laneIsOff(lane: string): boolean {
  return lanes !== null && lanes[lane] === false;
}

/** For the request helper: wait for the lanes if `path` is in one, then say whether it is off. */
export async function laneOffFor(path: string): Promise<string | null> {
  const lane = laneOf(path);
  if (!lane) return null;
  await ready;
  return laneIsOff(lane) ? lane : null;
}
