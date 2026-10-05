import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { BrandMark } from "../shell/BrandMark";
import ThemeToggle from "./ThemeToggle";
import "../../pages/Complain.css";

/**
 * The frame of the public support pages (/complain, /track), outside the console shell: a skip
 * link, the slim header (brand mark, "Kenya NOC Support", "Front page", the theme toggle), the
 * page on the canvas, and the footer that says this is a demo. One component so the two
 * siblings cannot drift apart. Styles: pages/Complain.css (`.cp-*`).
 */
export default function PublicFrame({ children }: { children: ReactNode }) {
  return (
    <div className="cp">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="cp-top">
        <div className="cp-wrap">
          <Link className="cp-brand" to="/complain" aria-label="Kenya NOC Support, the complaint form">
            <BrandMark />
            <span>Kenya NOC Support</span>
          </Link>
          <div className="cp-top-actions">
            <Link className="cp-quiet-link" to="/">
              Front page
            </Link>
            <ThemeToggle className="cp-icon-btn" />
          </div>
        </div>
      </header>

      <main id="main" className="cp-main" tabIndex={-1}>
        <div className="cp-wrap">{children}</div>
      </main>

      <footer className="cp-foot">
        <div className="cp-wrap">
          <span>Demo and training product. Not an official Safaricom or Airtel system.</span>
        </div>
      </footer>
    </div>
  );
}
