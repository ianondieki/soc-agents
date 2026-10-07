import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";

/** An address that matches no page: say so, name the address, and give two ways back. */
export default function NotFound() {
  const { pathname } = useLocation();
  useEffect(() => {
    document.title = "No page here, Kenya NOC Mission Control";
    return () => {
      document.title = "Kenya NOC Mission Control";
    };
  }, []);
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>No page at this address</h1>
          <p className="lead">
            <span className="mono">{pathname}</span> is not a page in the console. It may be an old link or a typing slip.
          </p>
        </div>
      </div>
      <div className="nf-actions">
        <Link className="btn primary" to="/mission">
          Go to Mission control
        </Link>
        <Link className="btn" to="/">
          Open the front page
        </Link>
      </div>
    </div>
  );
}
