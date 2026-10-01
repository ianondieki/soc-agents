/** Per-browser-session demo state keys. In a plain module, not in a component file, so App
 *  and the pages that read them never import each other. */

/** Mission Control auto-runs the storm once per browser session when the board is empty. */
export const AUTO_STORM_KEY = "noc_auto_storm_v1";

/** The guided demo's current step, so the panel follows the presenter across pages. */
export const GUIDE_STEP_KEY = "noc_guide_v1";
