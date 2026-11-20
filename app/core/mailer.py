"""Outgoing email over SMTP. Without SMTP settings the message is printed to the console."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.settings import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_SECURITY, SMTP_USER

log = logging.getLogger("spacematch.mail")


def email_configured() -> bool:
    return bool(SMTP_HOST)


def send_email(to: str, subject: str, text: str) -> bool:
    """Returns True when handed to the SMTP server, False when only printed (no SMTP configured)."""
    if not email_configured():
        print(f"\n[email not configured - would send]\nTo: {to}\nSubject: {subject}\n\n{text}\n", flush=True)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject
    msg.set_content(text)
    context = ssl.create_default_context()
    if SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=20)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
    with server:
        if SMTP_SECURITY == "starttls":
            server.starttls(context=context)
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    log.info("Sent '%s' to %s", subject, to)
    return True
