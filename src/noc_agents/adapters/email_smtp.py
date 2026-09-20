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
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Sequence


@dataclass
class EmailResult:
    ok: bool
    mode: str  # "smtp" | "mock" | "disabled" | "error"
    detail: str
    to: list[str]


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
    subject: str,
    body: str,
    to: Sequence[str] | None = None,
    html: bool = False,
) -> EmailResult:
    """Send email via Gmail SMTP when configured; otherwise mock success for tests."""
    recipients = list(to) if to else demo_recipients()
    if not recipients:
        return EmailResult(
            ok=True,
            mode="mock",
            detail="No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)",
            to=[],
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
    msg["To"] = ", ".join(recipients)
    if html:
        msg.set_content(body)
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    return _deliver(msg, recipients)


def send_message(msg: EmailMessage, *, to: Sequence[str] | None = None) -> EmailResult:
    """Send an ALREADY BUILT message (the iMIP calendar invite, §7.5.6 / RFC 6047).

    ``send_email`` builds its own single-part ``EmailMessage``, so there is nowhere to put a
    ``text/calendar; method=REQUEST`` part — an invite sent through it arrives as prose and
    no client draws an Accept button. This entry point takes the finished message from
    ``services/ics.build_imip_message`` and does only the transport.

    Two things it deliberately does NOT do, both of which ``send_email`` does:

    * it does not fall back to ``demo_recipients()``. The recipients are the attendees the
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
    ``send_email`` may fall back to the demo mailbox and ``send_message`` may not).
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
        )
    except Exception as exc:  # noqa: BLE001 — surface mail errors to ops notes
        return EmailResult(
            ok=False,
            mode="error",
            detail=f"SMTP send failed: {type(exc).__name__}: {exc}",
            to=recipients,
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
