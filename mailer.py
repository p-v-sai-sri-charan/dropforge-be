"""Transactional email (verification codes, order updates). Uses Resend (RESEND_API_KEY) or any SMTP server
(SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD); off when neither is configured. Never raises: an email that
can't be sent must not break the flow that triggered it."""
import html
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional

import httpx
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

SITE = os.environ.get("SITE_URL", "https://dropforge.in").rstrip("/")


def email_enabled() -> bool:
    return bool(os.environ.get("EMAIL_FROM") and (os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_HOST")))


def _smtp_send(msg: EmailMessage):
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    with (smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)) as s:
        if port != 465:
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


async def send_email(to: Optional[str], subject: str, html_body: str, text_body: str) -> bool:
    if not (to and email_enabled()):
        return False
    sender = os.environ["EMAIL_FROM"]  # e.g. "DropForge <orders@dropforge.in>"
    try:
        if os.environ.get("RESEND_API_KEY"):
            async with httpx.AsyncClient(timeout=20) as http:
                r = await http.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
                    json={"from": sender, "to": [to], "subject": subject, "html": html_body, "text": text_body},
                )
                r.raise_for_status()
        else:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, to, subject
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
            await run_in_threadpool(_smtp_send, msg)
        return True
    except Exception as e:
        logger.warning("email to %s failed: %s", to.split("@")[-1], e)  # log the domain only
        return False


def layout(title: str, paragraphs: list, button: Optional[tuple] = None, rows: Optional[list] = None) -> str:
    """Simple branded HTML email (dark header, readable body). All values are escaped."""
    e = html.escape
    body = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:22px;color:#1d2230">{e(p)}</p>' for p in paragraphs)
    if rows:
        body += '<table style="width:100%;border-collapse:collapse;margin:6px 0 18px">' + "".join(
            f'<tr><td style="padding:8px 0;border-bottom:1px solid #e8eaf0;color:#6b7285;font-size:13px">{e(k)}</td>'
            f'<td style="padding:8px 0;border-bottom:1px solid #e8eaf0;text-align:right;font-size:14px;color:#1d2230"><b>{e(v)}</b></td></tr>'
            for k, v in rows) + "</table>"
    if button:
        body += (f'<p style="margin:22px 0"><a href="{e(button[1])}" style="background:#173FAD;color:#fff;text-decoration:none;'
                 f'padding:12px 22px;border-radius:999px;font-weight:bold;font-size:14px;display:inline-block">{e(button[0])}</a></p>')
    return f"""<!doctype html><html><body style="margin:0;background:#f3f4f8;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:16px;overflow:hidden">
<tr><td style="background:#050505;padding:20px 28px;font-size:22px;font-weight:900;color:#F2F4F8;letter-spacing:-0.5px">DROP<span style="color:#6F93FF">FORGE</span></td></tr>
<tr><td style="padding:28px"><h1 style="margin:0 0 16px;font-size:22px;color:#050505">{e(title)}</h1>{body}
<p style="margin:24px 0 0;font-size:12px;color:#8b93a7">DropForge · <a href="{SITE}" style="color:#8b93a7">{SITE.replace("https://", "")}</a> ·
<a href="{SITE}/legal/contact" style="color:#8b93a7">Contact</a></p></td></tr></table></td></tr></table></body></html>"""
