"""Gmail-compatible SMTP email for demo outage notifications.

Credentials MUST come from environment variables — never commit passwords.

Required for real send:
  GMAIL_ADDRESS   — full Gmail address (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD — 16-char App Password (Google Account → Security → App passwords)
Optional:
  DEMO_EMAIL_TO   — override recipient (defaults to GMAIL_ADDRESS)
  EMAIL_ENABLED   — "true"/"false" (default: FALSE; real sending is opt-in, even
                    when credentials are present — set EMAIL_ENABLED=true to send)
  SMTP_HOST       — default smtp.gmail.com
  SMTP_PORT       — default 587

Where a message goes (§7.9.1). ``send_email(*, to, subject, body, html, headers)`` sends to
exactly the ``to`` it is given. The demo mailbox (``DEMO_EMAIL_TO`` / ``GMAIL_ADDRESS``) is
reached ONLY through the named sentinel :data:`DEMO_MAILBOX` or its wrapper
:func:`send_demo_email` — never because a caller had no recipients. ``to=None`` is refused.

This module does not count or cap anything: ``EMAIL_DAILY_CAP`` is enforced by the outbox
dispatcher (``services/notify.email_cap_decision``), which is the one place that knows what
has already been sent today, and batching to :data:`RECIPIENTS_PER_MESSAGE` is done by the
dispatcher before it calls here (``services/notify.transmit_email``).
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Mapping, Sequence

#: Gmail SMTP accepts at most 100 recipients on one message, free and Workspace accounts alike
#: (§7.9.1; the Gmail API allows 500, SMTP does not). The dispatcher batches to this; the §6.2
#: renderer check ``services/validators.EMAIL_RECIPIENTS_PER_MESSAGE`` is the same number.
RECIPIENTS_PER_MESSAGE = 100

#: Headers the adapter owns: the envelope is built from ``to``/``subject`` and the relay
#: identity, so a caller-supplied ``headers`` entry may not rewrite who a message is from or to.
_ADAPTER_OWNED_HEADERS = frozenset({"subject", "from", "to", "cc", "bcc"})


class DemoMailbox:
    """Type of :data:`DEMO_MAILBOX`. A value a caller passes on purpose, not a missing argument."""

    __slots__ = ()

    def __repr__(self) -> str:  # shows up in signatures and tracebacks by name
        return "DEMO_MAILBOX"


#: ``send_email(to=DEMO_MAILBOX, …)`` means "whatever DEMO_EMAIL_TO / GMAIL_ADDRESS says".
#:
#: WHY A SENTINEL AND NOT ``to=None`` (Phase 4 blocker 1, docs/PHASE4.md). The pre-v2
#: signature was ``send_email(*, subject, body, to=None)`` and ``None`` fell back to the demo
#: mailbox. So "I did not resolve any recipients" and "send it to the demo inbox" were the SAME
#: call, which is how the Communications Authority notice was routed to ``DEMO_EMAIL_TO``: the
#: dispatcher never passed ``to`` and the adapter quietly filled one in. ``None`` now means
#: "recipients unknown" and is refused; reaching the demo inbox takes this named value.
DEMO_MAILBOX = DemoMailbox()


@dataclass
class EmailResult:
    ok: bool
    mode: str  # "smtp" | "mock" | "disabled" | "error"
    detail: str
    to: list[str]
    # How many messages the relay ACCEPTED in this call: 1 or 0 from one SMTP conversation, the
    # sum over batches from the dispatcher. ``None`` = not reported (a mock never reaches a
    # relay). EMAIL_DAILY_CAP counts this, not rows (services/notify.email_budget).
    accepted: int | None = None


def email_configured() -> bool:
    addr = (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    enabled = (os.getenv("EMAIL_ENABLED") or "false").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return False
    return bool(addr and pwd)


def demo_recipients() -> list[str]:
    raw = (os.getenv("DEMO_EMAIL_TO") or os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USER") or "").strip()
    if not raw:
        return []
    # comma-separated list supported
    return [p.strip() for p in raw.split(",") if p.strip()]


def email_status() -> dict:
    recipients = demo_recipients()
    return {
        "configured": email_configured(),
        "provider": "gmail_smtp" if email_configured() else "mock",
        "from": (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USER") or None),
        "recipients": recipients,
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "hint": (
            "Set GMAIL_ADDRESS + GMAIL_APP_PASSWORD (Google App Password) to enable real mail."
            if not email_configured()
            else "Real Gmail SMTP enabled for demo."
        ),
    }


def send_email(
    *,
    # COMPATIBILITY DEFAULT, deliberately a named sentinel and not ``None``. §7.9.1 makes ``to``
    # required; it keeps a default for one reason: ``POST /api/v1/email/test`` in ``main.py``
    # still calls ``send_email(subject=…, body=…)`` and that file is changed in the integration
    # step (to ``send_demo_email(...)``). Omitting ``to`` therefore still reaches the demo
    # mailbox, but only because the default IS ``DEMO_MAILBOX`` — read the signature and you
    # see where it goes. Once main.py moves, drop the default and ``to`` is required outright.
    to: Sequence[str] | DemoMailbox = DEMO_MAILBOX,
    subject: str,
    body: str,
    html: bool = False,
    headers: Mapping[str, str] | None = None,
) -> EmailResult:
    """Send one message to ``to`` via SMTP when configured; otherwise a mock result (tests, demo).

    * ``to=DEMO_MAILBOX`` → ``demo_recipients()``: the explicit demo path (see
      :func:`send_demo_email`);
    * ``to=[...]`` → exactly those addresses, at most :data:`RECIPIENTS_PER_MESSAGE` (the
      dispatcher batches; more than that here is refused, not truncated);
    * ``to=None`` → refused, ``mode="error"``: an unresolved recipient list is not a request
      for the demo inbox (the Phase 4 mis-delivery);
    * an empty list → the historical mock branch, ``detail`` verbatim (pinned by the golden
      test on the ``email.sent`` payload).

    ``headers`` are extra header fields (``List-Unsubscribe``, …). They cannot override
    ``Subject``/``From``/``To``/``Cc``/``Bcc``, which this function owns. Never raises.
    """
    if to is DEMO_MAILBOX:
        recipients = demo_recipients()
    elif to is None:
        return EmailResult(
            ok=False,
            mode="error",
            detail="refused: send_email(to=None) — recipients unresolved; pass DEMO_MAILBOX to mean the demo mailbox",
            to=[],
        )
    else:
        recipients = [str(a).strip() for a in to if str(a).strip()]
    if not recipients:
        return EmailResult(
            ok=True,
            mode="mock",
            detail="No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)",
            to=[],
        )
    if len(recipients) > RECIPIENTS_PER_MESSAGE:
        # A backstop, not the batching: the dispatcher splits audiences before calling here.
        # Refusing (rather than sending the first 100) means a batching bug shows up as an
        # error on the outbox row instead of as recipients who silently never got the notice.
        return EmailResult(
            ok=False,
            mode="error",
            detail=(
                f"refused: {len(recipients)} recipients on one message exceeds the SMTP limit of "
                f"{RECIPIENTS_PER_MESSAGE}; the dispatcher must batch"
            ),
            to=recipients,
        )

    if not email_configured():
        return EmailResult(
            ok=True,
            mode="mock",
            detail=f"SMTP not configured — would have sent to {recipients} (mock)",
            to=recipients,
        )

    user = (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USER") or "").strip()
    from_addr = os.getenv("SMTP_FROM") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    if len(recipients) == 1:
        msg["To"] = recipients[0]  # one recipient: addressed exactly as before
    else:
        # A batch goes Bcc (§7.9.1 "≤ 100-recipient Bcc messages"): an outage notice fanned out
        # to a distribution list must not hand every recipient the other recipients' mailboxes
        # (DPA 2019 minimisation). ``To:`` names the sender so the message is never header-less;
        # smtplib's ``send_message`` delivers to the Bcc list and strips the header in transit.
        msg["To"] = from_addr
        msg["Bcc"] = ", ".join(recipients)
    for name, value in (headers or {}).items():
        if name.strip().lower() in _ADAPTER_OWNED_HEADERS:
            continue
        del msg[name]  # no-op when absent; a repeat would otherwise become a second header
        msg[name] = value
    if html:
        msg.set_content(body)
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    return _deliver(msg, recipients)


def send_demo_email(
    *,
    subject: str,
    body: str,
    html: bool = False,
    headers: Mapping[str, str] | None = None,
) -> EmailResult:
    """Send to the demo mailbox (``DEMO_EMAIL_TO`` / ``GMAIL_ADDRESS``), and say so at the call site.

    The ONE sanctioned way to reach the demo inbox without naming ``DEMO_MAILBOX``: the
    ``/api/v1/email/test`` route and the dispatcher's ``recipients_ref="DEMO_EMAIL_TO"`` rows.
    Exactly ``send_email(to=DEMO_MAILBOX, …)``, including the empty-inbox mock result.
    """
    return send_email(to=DEMO_MAILBOX, subject=subject, body=body, html=html, headers=headers)


def send_message(msg: EmailMessage, *, to: Sequence[str] | None = None) -> EmailResult:
    """Send an ALREADY BUILT message (the iMIP calendar invite, §7.5.6 / RFC 6047).

    ``send_email`` builds its own single-part ``EmailMessage``, so there is nowhere to put a
    ``text/calendar; method=REQUEST`` part — an invite sent through it arrives as prose and
    no client draws an Accept button. This entry point takes the finished message from
    ``services/ics.build_imip_message`` and does only the transport.

    Two things it deliberately does NOT do, both of which ``send_email`` can:

    * it has no ``DEMO_MAILBOX`` path at all. The recipients are the attendees the
      calendar object names, and an invite quietly rerouted to the demo mailbox is an
      engineer who never learns about the window (and a mailbox that gets an event for a
      site it has nothing to do with). No recipients means no send;
    * it does not overwrite ``From:``. RFC 6047 §3 expects ``From:`` to equal the ORGANIZER,
      and several clients silently drop an invite where the two disagree — so the caller
      owns that header. NOTE for the operator: the relay must be allowed to send as that
      organiser address (Gmail refuses otherwise), which is an SMTP 5xx the outbox records,
      not something this adapter can paper over.

    Same ``EmailResult`` contract as ``send_email``: mock when unconfigured, never raises.
    """
    recipients = [str(a).strip() for a in (to if to is not None else _recipients_of(msg)) if str(a).strip()]
    if not recipients:
        return EmailResult(ok=True, mode="mock", detail="No recipients on the message — nothing sent (mock)", to=[])
    if not email_configured():
        return EmailResult(
            ok=True,
            mode="mock",
            detail=f"SMTP not configured — would have sent to {recipients} (mock)",
            to=recipients,
        )
    return _deliver(msg, recipients)


def _recipients_of(msg: EmailMessage) -> list[str]:
    """Addresses from To/Cc/Bcc of a built message, in header order."""
    from email.utils import getaddresses

    headers = [str(v) for field in ("To", "Cc", "Bcc") for v in msg.get_all(field, [])]
    return [addr for _name, addr in getaddresses(headers) if addr]


def _deliver(msg: EmailMessage, recipients: Sequence[str]) -> EmailResult:
    """The one SMTP conversation, shared by ``send_email`` and ``send_message``.

    Extracted unchanged from ``send_email``: same STARTTLS sequence, same 30 s timeout, same
    result strings (the mock/disabled decisions stay with the callers, because they differ —
    ``send_email`` may be sent to ``DEMO_MAILBOX`` on purpose and ``send_message`` may not).
    """
    user = (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    # Gmail app passwords are often shown with spaces
    password = password.replace(" ", "")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    recipients = list(recipients)
    if not msg["From"]:  # a caller-set From (the iMIP ORGANIZER) is never overwritten
        del msg["From"]  # no-op when absent; without it an empty header would be duplicated
        msg["From"] = os.getenv("SMTP_FROM") or user

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
        return EmailResult(
            ok=True,
            mode="smtp",
            detail=f"Sent via {host}:{port} to {recipients}",
            to=recipients,
            accepted=1,
        )
    except Exception as exc:  # noqa: BLE001 — surface mail errors to ops notes
        return EmailResult(
            ok=False,
            mode="error",
            detail=f"SMTP send failed: {type(exc).__name__}: {exc}",
            to=recipients,
            accepted=0,
        )


def parse_subject_body(composed: str) -> tuple[str, str]:
    """Split 'Subject: ...\\n\\nbody' format used by pipeline composer."""
    text = composed.strip()
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
        return subject, body
    return "NOC incident notification", text
