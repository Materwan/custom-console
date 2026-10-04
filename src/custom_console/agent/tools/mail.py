"""Email tool, registered only when an SMTP server is configured."""

import smtplib
from email.message import EmailMessage
from typing import Callable, List

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded

PREVIEW_CHARS = 2_000  # of the body, shown in the permission question


def build_message(sender: str, recipient: str, subject: str, content: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(content)
    return message


def mail_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    settings = ctx.settings
    if not settings.smtp_host:
        return []  # no SMTP server: do not offer a tool that cannot work

    def describe(recipient: str, subject: str, content: str = "") -> str:
        body = content if len(content) <= PREVIEW_CHARS else content[:PREVIEW_CHARS] + " […]"
        return f"Agent wants to send an email to {recipient}\nSubject: {subject}\n\n{body}"

    @guarded(ctx, PermissionLevel.WRITE, describe=describe, rule=lambda recipient, **_: f"send_email:{recipient.strip().lower()}")
    def send_email(recipient: str, subject: str, content: str = "") -> ToolResult:
        """Send an email.

        Args:
            recipient: destination address.
            subject: subject line.
            content: plain text body.
        """
        sender = settings.smtp_from or settings.smtp_user
        if not sender:
            raise ValueError("Set SMTP_FROM (or SMTP_USER) to send emails.")
        message = build_message(sender, recipient, subject, content)

        smtp_class = smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if smtp_class is smtplib.SMTP:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password or "")
            smtp.send_message(message)
        return ToolResult.ok(f"Email sent to {recipient}.")

    return [send_email]
