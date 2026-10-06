import { Link } from "react-router-dom";

/**
 * The side note beside the two public forms (/complain, /track): what happens after you send, and
 * where a complaint can be. Plain words for a customer, no product terms. Beside the form on a wide
 * screen, under it on a phone.
 */

export function ComplainNext() {
  return (
    <aside className="cp-aside" aria-labelledby="cp-next-title">
      <h2 id="cp-next-title">What happens next</h2>
      <ol className="cp-next">
        <li>
          <span className="cp-next-n" aria-hidden="true">
            1
          </span>
          <div>
            <p className="cp-next-head">We read it straight away</p>
            <p>What it is about, how urgent it is, and whether a person should answer it.</p>
          </div>
        </li>
        <li>
          <span className="cp-next-n" aria-hidden="true">
            2
          </span>
          <div>
            <p className="cp-next-head">You get an answer, or a person</p>
            <p>
              Most complaints are answered on this page within seconds. Fraud, SIM swaps, large refunds and legal matters go to
              a person, and we tell you by when they will reply.
            </p>
          </div>
        </li>
        <li>
          <span className="cp-next-n" aria-hidden="true">
            3
          </span>
          <div>
            <p className="cp-next-head">Follow it with your reference</p>
            <p>
              Keep the reference you are given. <Link to="/track">Track a complaint</Link> shows where it is, any time.
            </p>
          </div>
        </li>
      </ol>
      <p className="cp-aside-note">Never share your M-PESA PIN, here or anywhere.</p>
    </aside>
  );
}

export function TrackNext() {
  return (
    <aside className="cp-aside" aria-labelledby="cp-where-title">
      <h2 id="cp-where-title">Where a complaint can be</h2>
      <ul className="cp-where">
        <li>
          <span className="cp-where-dot" aria-hidden="true" />
          <div>
            <p className="cp-next-head">Received</p>
            <p>We have it and are reading it.</p>
          </div>
        </li>
        <li>
          <span className="cp-where-dot hitl" aria-hidden="true" />
          <div>
            <p className="cp-next-head">With a person</p>
            <p>Someone from our team is answering it, by the time we gave you.</p>
          </div>
        </li>
        <li>
          <span className="cp-where-dot warn" aria-hidden="true" />
          <div>
            <p className="cp-next-head">Part of an outage we know about</p>
            <p>Our engineers are already fixing the network in your area. We tell you when it is back.</p>
          </div>
        </li>
        <li>
          <span className="cp-where-dot ok" aria-hidden="true" />
          <div>
            <p className="cp-next-head">Answered or fixed</p>
            <p>If it is still not working for you, say so from your complaint's page and we look again.</p>
          </div>
        </li>
      </ul>
    </aside>
  );
}
