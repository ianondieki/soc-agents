/** True in the public demo build (`vite build --mode demo`, src/demo). The demo's host page blocks
 *  file downloads, so a page hides its download buttons there rather than show one that does nothing. */
export const IS_DEMO = import.meta.env.VITE_DEMO === "1";
