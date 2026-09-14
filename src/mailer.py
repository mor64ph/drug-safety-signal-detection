"""
M11: outbound email.

Three messages leave this application: verify your address, reset your password,
and here is what changed for the drugs you track. All three are consequential --
the first two block access entirely -- so the failure mode matters more than the
feature set.

Two rules shape this module:

  Nothing here raises into a request. A mail server that is slow, unreachable
  or rejecting is a normal Tuesday; it must not turn a completed registration
  into a 500 with no account visible to the user.

  Nothing here logs an address, a subject line or a body. Application logs are
  copied, shipped and read by people who have no business reading a list of who
  signed up. The user id is enough to debug a delivery, and it is not personal
  data on its own.

With SMTP_HOST unset, messages are written to data/outbox/ as .eml files, which
open in any mail client. Development must not require a mail server, and a
verification email that vanishes silently is worse than one that was obviously
never sent.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from src.models import _setting

log = logging.getLogger("rxsignal.mailer")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OUTBOX = _PROJECT_ROOT / "data" / "outbox"

_TIMEOUT = 20


def base_url() -> str:
    """
    Absolute base for links in email. Relative links are meaningless in a mail
    client, so this has to be configured before anyone outside the machine can
    complete a signup.
    """
    return _setting("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def _from_address() -> str:
    return _setting("SMTP_FROM") or _setting("SMTP_USER") or "rxsignal@localhost"


def _build(to: str, subject: str, body_text: str, body_html: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _from_address()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="rxsignal")
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    return msg


def _write_outbox(msg: EmailMessage) -> bool:
    """
    Fallback delivery: drop the message on disk.

    The filename carries a timestamp and a counter rather than the recipient,
    because a directory listing is as public as a log line.
    """
    try:
        _OUTBOX.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = _OUTBOX / f"{stamp}.eml"
        path.write_bytes(bytes(msg))
        log.warning("SMTP_HOST unset: message written to %s instead of being sent",
                    path.relative_to(_PROJECT_ROOT))
        return True
    except OSError as exc:
        log.error("could not write outbox message: %s", exc.__class__.__name__)
        return False


def send(to: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """
    Deliver one message. Returns True when it was handed off or written to disk.

    Every SMTP failure mode is caught here. The caller decides what to tell the
    user; it never sees an exception from the mail path.
    """
    if not to or "@" not in to:
        log.error("refusing to send: recipient is not an address")
        return False

    msg = _build(to, subject, body_text, body_html)

    host = _setting("SMTP_HOST")
    if not host:
        return _write_outbox(msg)

    port = int(_setting("SMTP_PORT", "587") or "587")
    user = _setting("SMTP_USER")
    password = _setting("SMTP_PASSWORD")
    use_tls = _setting("SMTP_USE_TLS", "true").lower() not in ("0", "false", "no", "off")

    try:
        context = ssl.create_default_context()
        # 465 is implicit TLS: the handshake happens before any SMTP command,
        # so STARTTLS on that port fails rather than upgrading.
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT, context=context) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=_TIMEOUT) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        log.error("mail delivery failed: %s", exc.__class__.__name__)
        return False
    except Exception as exc:  # pragma: no cover - defensive
        log.error("mail delivery failed unexpectedly: %s", exc.__class__.__name__)
        return False
    return True


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

_FOOTER = (
    "\n\n--\nrxsignal analyses reporting patterns in the FDA Adverse Event "
    "Reporting System. It does not measure risk, incidence or causation, and it "
    "is not medical advice. Do not change how you take a medicine because of "
    "anything in this message; speak to your prescriber.\n"
)


def send_verification(user, token: str) -> bool:
    """Confirm the address is real and reachable before anything is sent to it."""
    link = f"{base_url()}/verify/{token}"
    text = (
        "Confirm your rxsignal address\n"
        "==============================\n\n"
        "Open this link to finish setting up your account:\n\n"
        f"  {link}\n\n"
        "If you did not create an rxsignal account, ignore this message. No "
        "account can be used until this link is opened."
        + _FOOTER
    )
    ok = send(user.email, "Confirm your rxsignal address", text)
    log.info("verification email user_id=%s sent=%s", user.id, ok)
    return ok


def send_password_reset(user, token: str) -> bool:
    link = f"{base_url()}/reset/{token}"
    text = (
        "Reset your rxsignal password\n"
        "=============================\n\n"
        "Open this link within one hour to choose a new password:\n\n"
        f"  {link}\n\n"
        "If you did not ask for this, nothing has changed and you can ignore "
        "this message. The link expires on its own."
        + _FOOTER
    )
    ok = send(user.email, "Reset your rxsignal password", text)
    log.info("password reset email user_id=%s sent=%s", user.id, ok)
    return ok


_KIND_WORDS = {
    "new_signal": "newly surfaced",
    "strengthened": "reported more strongly",
    "newly_labelled": "now described in the product label",
}


def send_notification_digest(user, notifications: list) -> bool:
    """
    One message covering everything unread for this user.

    Batched rather than per-change: a drug whose quarterly refresh moves twenty
    pairs would otherwise produce twenty emails, and the twentieth is deleted
    unread along with the first.
    """
    if not notifications:
        return False

    lines = [
        "Changes for the drugs you track",
        "================================",
        "",
        "These are changes in how often a reaction is reported alongside a drug.",
        "They are not new findings about the medicine, not a safety warning, and",
        "not a reason to stop taking anything.",
        "",
    ]
    for n in notifications:
        lines.append(f"* {n.drug} - {n.reaction_pt}")
        lines.append(f"    {_KIND_WORDS.get(n.kind, n.kind)}: {n.detail}")
        if n.ic025 is not None and n.prev_ic025 is not None:
            lines.append(f"    IC025 {n.prev_ic025:.2f} -> {n.ic025:.2f}")
        lines.append("")

    lines.append(f"See the detail: {base_url()}/my/notifications")
    lines.append("")
    lines.append(
        f"To stop these emails, switch off change alerts at {base_url()}/account"
    )
    text = "\n".join(lines) + _FOOTER

    count = len(notifications)
    subject = f"rxsignal: {count} change{'s' if count != 1 else ''} in drugs you track"
    ok = send(user.email, subject, text)
    log.info("digest user_id=%s items=%d sent=%s", user.id, count, ok)
    return ok
