"""
Send one test message and say precisely what went wrong if it fails.

    python scripts/test_email.py you@example.com

Worth having because the alternative is diagnosing mail through the
registration flow, where a failure looks like "no email arrived" and could be
credentials, TLS, the From address, a provider restriction or the spam folder.
This separates them.
"""

from __future__ import annotations

import smtplib
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models import _setting  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/test_email.py you@example.com")
        return 2
    recipient = sys.argv[1].strip()

    host = _setting("SMTP_HOST")
    port = int(_setting("SMTP_PORT", "587") or "587")
    user = _setting("SMTP_USER")
    password = _setting("SMTP_PASSWORD")
    sender = _setting("SMTP_FROM")

    print("configuration")
    print(f"  SMTP_HOST      {host or '(unset)'}")
    print(f"  SMTP_PORT      {port}")
    print(f"  SMTP_USER      {user or '(unset)'}")
    print(f"  SMTP_PASSWORD  {'set, ' + str(len(password)) + ' chars' if password else '(unset)'}")
    print(f"  SMTP_FROM      {sender or '(unset)'}")
    print(f"  recipient      {recipient}")
    print()

    if not host:
        print("SMTP_HOST is unset, so the app writes .eml files to data/outbox/")
        print("instead of sending. Add the SMTP settings to .env.")
        return 1
    if not sender:
        print("SMTP_FROM is unset. Resend rejects a message with no From address.")
        return 1

    from src.mailer import send

    ok = send(
        recipient,
        "reportscope: test message",
        "If you are reading this, SMTP is configured correctly.\n\n"
        "Nothing else is implied -- this message was sent by a setup script, "
        "not by the application.\n",
    )

    if ok:
        print("Handed off to the server with no error.")
        print("Check the inbox, and the spam folder. If neither has it, look at")
        print("the provider's own delivery log -- the message left here fine.")
        return 0

    print("Delivery failed. Re-running with the error exposed:\n")
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as s:
                s.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=context)
                s.ehlo()
                s.login(user, password)
        print("  Connection and login SUCCEEDED, so the send itself was refused.")
        print("  Usually the From address: on Resend's test domain you may only")
        print("  send to the address the account was registered with, and any")
        print("  other From requires a verified domain.")
    except smtplib.SMTPAuthenticationError as exc:
        print(f"  Authentication refused: {exc.smtp_code} {exc.smtp_error!r}")
        print("  For Resend the username is the literal word 'resend' and the")
        print("  password is the API key beginning re_.")
    except smtplib.SMTPException as exc:
        print(f"  SMTP error: {exc.__class__.__name__}: {exc}")
    except ssl.SSLError as exc:
        print(f"  TLS error: {exc}")
        print("  Port 465 is implicit TLS; 587 is STARTTLS. Mismatching them fails here.")
    except OSError as exc:
        print(f"  Could not reach {host}:{port} -- {exc}")
        print("  A corporate network may be blocking outbound SMTP.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
