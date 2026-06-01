"""
clip_ops/send_email.py — deterministic email delivery.

Delivery order:
  1. SendGrid (only if SENDGRID_API_KEY is set), via HTTPS POST (stdlib urllib).
  2. SMTP/STARTTLS (Gmail-compatible) if EMAIL_USER/EMAIL_PASS/EMAIL_TO are set.
  3. Otherwise: skip and log — never an error.

Every failure is appended to clip_ops/email.log. This module never raises to
its caller and never crashes the daily pipeline.

CLI:
  python3 -m clip_ops.send_email      # send the already-rendered daily_digest.md
"""
from __future__ import annotations

import datetime
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Dict

from clip_ops import config


def _log(msg: str) -> None:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    line = "[%s] %s\n" % (stamp, msg)
    try:
        with open(config.EMAIL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _send_via_sendgrid(subject: str, body: str, to_addr: str) -> bool:
    from_addr = config.EMAIL_FROM or config.EMAIL_USER or to_addr
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        config.SENDGRID_ENDPOINT,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer %s" % config.SENDGRID_API_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.getcode()
        if 200 <= code < 300:
            _log("SendGrid accepted message (HTTP %d) to %s" % (code, to_addr))
            return True
        _log("SendGrid returned HTTP %d to %s" % (code, to_addr))
        return False
    except urllib.error.HTTPError as exc:
        _log("SendGrid HTTPError %s: %s" % (exc.code, exc.reason))
        return False
    except Exception as exc:  # network down, DNS, timeout, etc.
        _log("SendGrid request failed: %r" % exc)
        return False


def _send_via_smtp(subject: str, body: str, to_addr: str) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM or config.EMAIL_USER
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config.EMAIL_USER, config.EMAIL_PASS)
            server.send_message(msg)
        _log("SMTP sent message to %s via %s:%d"
             % (to_addr, config.SMTP_HOST, config.SMTP_PORT))
        return True
    except Exception as exc:
        _log("SMTP send failed: %r" % exc)
        return False


def send_email(subject: str, body: str, to_addr: str = "") -> Dict[str, object]:
    """
    Attempt delivery. Returns a result dict; never raises.
      {"sent": bool, "method": "sendgrid"|"smtp"|"none", "reason": str}
    """
    to_addr = to_addr or config.EMAIL_TO
    if not to_addr:
        reason = "no recipient (EMAIL_TO unset) — email skipped"
        _log(reason)
        return {"sent": False, "method": "none", "reason": reason}

    if config.SENDGRID_API_KEY:
        if _send_via_sendgrid(subject, body, to_addr):
            return {"sent": True, "method": "sendgrid", "reason": "ok"}
        _log("falling back from SendGrid to SMTP")

    if config.EMAIL_USER and config.EMAIL_PASS:
        if _send_via_smtp(subject, body, to_addr):
            return {"sent": True, "method": "smtp", "reason": "ok"}
        return {"sent": False, "method": "smtp", "reason": "smtp delivery failed (see email.log)"}

    reason = ("no usable email transport configured "
              "(set SENDGRID_API_KEY, or EMAIL_USER+EMAIL_PASS) — email skipped")
    _log(reason)
    return {"sent": False, "method": "none", "reason": reason}


def main() -> int:
    """Send the already-rendered daily_digest.md. Always exits 0."""
    from clip_ops import digest as digest_mod
    try:
        with open(config.DIGEST_FILE, "r", encoding="utf-8") as f:
            body = f.read()
    except Exception as exc:
        _log("cannot read digest %s: %r" % (config.DIGEST_FILE, exc))
        print("send_email: no digest to send (%r)" % exc)
        return 0
    subject = digest_mod.extract_subject(body)
    result = send_email(subject, body)
    print("send_email: %s" % json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
