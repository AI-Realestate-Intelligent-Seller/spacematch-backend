"""Outgoing email over SMTP. Without SMTP settings the message is printed to the console.

send_email never raises: a mail failure is logged and reported as False, so the request that triggered it
(password reset, invite, recovery code) still answers normally.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.settings import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_SECURITY, SMTP_TIMEOUT_S, SMTP_USER

log = logging.getLogger("spacematch.mail")


def email_configured() -> bool:
    return bool(SMTP_HOST)


def _send_smtp(to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject
    msg.set_content(text)
    context = ssl.create_default_context()
    if SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=SMTP_TIMEOUT_S)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S)
    with server:
        if SMTP_SECURITY == "starttls":
            server.starttls(context=context)
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def send_email(to: str, subject: str, text: str) -> bool:
    """True when handed to the SMTP server; False when only printed (no SMTP configured) or when sending failed."""
    if not email_configured():
        print(f"\n[email not configured - would send]\nTo: {to}\nSubject: {subject}\n\n{text}\n", flush=True)
        return False
    try:
        _send_smtp(to, subject, text)
    except Exception as exc:
        log.error("Email '%s' to %s via SMTP %s:%s failed: %s: %s",
                  subject, to, SMTP_HOST, SMTP_PORT, type(exc).__name__, exc)
        return False
    log.info("Sent '%s' to %s", subject, to)
    return True
