"""
Production entry point.

Flask's built-in server is single-threaded, has no request queueing and is not
written for hostile input; its own documentation says not to expose it. This
serves the same app through Waitress, which is a real WSGI server and works on
Windows (gunicorn does not).

    python serve.py                      # 0.0.0.0:8000
    PORT=8080 python serve.py            # different port

Environment:
    PORT                   listen port, default 8000
    RXSIGNAL_ACCESS_CODE   if set, the whole site requires this code
    RXSIGNAL_RATE_LIMIT    requests per minute per IP, default 60
    RXSIGNAL_SECRET_KEY    signs session cookies; required for a public instance
    DATABASE_URL           accounts store, default sqlite:///data/rxsignal.db
    SMTP_HOST              unset writes mail to data/outbox/ instead of sending
    APP_BASE_URL           absolute base for links in email

Put a TLS-terminating reverse proxy in front of this. The app sets HSTS only
when it sees a secure request, so the proxy must forward X-Forwarded-Proto.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from src.app import ACCESS_HASH, GATE_ENABLED, _load_results, _setting  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("rxsignal")


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))

    df = _load_results()
    if df.empty:
        log.error("no scored data found. Run `python run.py --score` first.")
        return 1

    undiagnosed = 0
    if "diagnosed" in df.columns:
        undiagnosed = int((~df["diagnosed"].fillna(False)).sum())

    log.info("serving %s pairs across %s drugs",
             f"{len(df):,}", df["drug"].nunique())
    if undiagnosed:
        log.warning(
            "%s of %s rows have no bias diagnostics. They are hidden from the "
            "default view, but finish `run.py --score` before public release.",
            f"{undiagnosed:,}", f"{len(df):,}")
    if GATE_ENABLED:
        log.info("access gate: ENABLED (%s)",
                 "hashed code" if ACCESS_HASH else "plaintext code")
    else:
        log.warning(
            "access gate OFF -- this instance is open to anyone who finds it. "
            "Run `python scripts/make_access_code.py` and set "
            "RXSIGNAL_ACCESS_CODE_HASH for a review deployment.")

    # No gate and no signing key is a public deployment whose sessions die on
    # every restart: users are signed out mid-task, half-finished verification
    # links stop working, and the cause is invisible from the outside. Refuse
    # rather than serve that. Behind the gate it is only an inconvenience to a
    # reviewer, so it stays a warning there.
    if not _setting("RXSIGNAL_SECRET_KEY") and not GATE_ENABLED:
        log.error(
            "refusing to start: RXSIGNAL_SECRET_KEY is unset and the access gate "
            "is off. Run `python scripts/make_secret_key.py` and add the line to "
            ".env, or set an access code for a closed review deployment.")
        return 1

    try:
        from waitress import serve
    except ImportError:
        log.error("waitress is not installed. Run: pip install waitress")
        return 1

    from src.app import app
    log.info("listening on 0.0.0.0:%d", port)
    serve(app, host="0.0.0.0", port=port, threads=8,
          ident="rxsignal", max_request_body_size=16 * 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
