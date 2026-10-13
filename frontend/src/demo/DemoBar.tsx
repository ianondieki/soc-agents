import { resetDemo } from "./install";
import "./DemoBar.css";

/**
 * The line above every page of the public demo: what this is, and a way back to the start. In
 * normal flow, so it never covers the page; it scrolls away with the top of the page. A phone gets
 * the short sentence (only one of the two is ever displayed, so a screen reader reads one).
 */
export default function DemoBar() {
  return (
    <div className="demo-bar" role="note" aria-label="About this demo">
      <p className="demo-bar-text demo-bar-long">
        <strong>Public demo.</strong> The console replays a recorded run of its real backend, so nothing you do here reaches a
        customer or an engineer.
      </p>
      <p className="demo-bar-text demo-bar-short">
        <strong>Public demo</strong> with recorded data. Nothing you do here reaches anyone.
      </p>
      <button type="button" className="btn sm demo-bar-reset" onClick={resetDemo}>
        Start over
      </button>
    </div>
  );
}
