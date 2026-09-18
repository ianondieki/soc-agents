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
    password = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    # Gmail app passwords are often shown with spaces
    password = password.replace(" ", "")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
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
