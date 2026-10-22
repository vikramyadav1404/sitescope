"""Outgoing email over SMTP (STARTTLS). Does nothing when SMTP_HOST is not set."""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger("sitescope")


def enabled() -> bool:
    return bool(config.SMTP_HOST and config.MAIL_FROM)


def _send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


async def send(to: str, subject: str, body: str) -> bool:
    """True if the message was handed to the SMTP server."""
    if not enabled():
        log.info("email disabled; not sending %r to %s", subject, to)
        return False
    try:
        await asyncio.to_thread(_send, to, subject, body)
        return True
    except Exception:
        log.exception("sending %r to %s failed", subject, to)
        return False
