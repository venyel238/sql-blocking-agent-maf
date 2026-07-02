"""
tools/notifications.py -- DBA approval email
---------------------------------------------
Builds and sends the DBA approval email when a hard gate (e.g. R9 large
transaction log) blocked an auto-kill. The SMTP send is injected as a
callable so this can be tested without a real mail server.
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel

from tools.detection import HeadBlocker

log = logging.getLogger("tools.notifications")


class DbaEmailInput(BaseModel):
    server_name: str
    head_blocker: HeadBlocker
    rca_report: str = ""
    log_used_mb: float = 0.0
    log_size_kill_threshold_gb: float = 20.0
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    dba_email: str = "evhdba@evolent.com"


class DbaEmailOutput(BaseModel):
    sent: bool = False
    skip_reason: Optional[str] = None


def send_dba_approval_email(
    input: DbaEmailInput,
    send_email: Callable[[str, str, str, str], None],
) -> DbaEmailOutput:
    """send_email(smtp_host, smtp_port_etc) is injected; see default_send_email for the real SMTP impl."""
    if not input.smtp_host or not input.smtp_from:
        log.warning("[%s] DBA approval email NOT sent — SMTP_HOST/SMTP_FROM not configured.", input.server_name)
        return DbaEmailOutput(sent=False, skip_reason="smtp_not_configured")

    head = input.head_blocker
    log_gb = input.log_used_mb / 1024
    subject = f"[SQL Agent] DBA Approval Required — Large Log on {input.server_name}"
    body = (
        f"SQL Blocking Agent — DBA Approval Required\n{'='*60}\n"
        f"Server:    {input.server_name}\n"
        f"Decision:  ALERT_ONLY (auto-kill blocked — log {log_gb:.1f} GB > {input.log_size_kill_threshold_gb:.0f} GB)\n"
        f"SPID:      {head.session_id}  login={head.login_name}\n"
        f"Wait:      {head.wait_duration_ms} ms  victims={head.victim_count}\n\n"
        f"To manually kill after review:\n  KILL {head.session_id}\n\n"
        f"{'='*60}\n{input.rca_report}\n"
    )

    try:
        send_email(input.dba_email, subject, body, input.smtp_from)
        log.info("[%s] DBA approval email sent to %s", input.server_name, input.dba_email)
        return DbaEmailOutput(sent=True)
    except Exception as e:
        log.error("[%s] Failed to send DBA approval email: %s", input.server_name, e)
        return DbaEmailOutput(sent=False, skip_reason=str(e))


def default_send_email(input: DbaEmailInput):
    """Returns a `send_email(to, subject, body, from_addr)` callable bound to input's SMTP config."""
    import smtplib
    from email.mime.text import MIMEText

    def _send(to_addr: str, subject: str, body: str, from_addr: str) -> None:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        with smtplib.SMTP(input.smtp_host, input.smtp_port, timeout=10) as s:
            s.starttls()
            if input.smtp_user:
                s.login(input.smtp_user, input.smtp_password or "")
            s.send_message(msg)

    return _send
