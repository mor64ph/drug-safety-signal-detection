"""
Email one digest to each user with unseen changes in the drugs they track.

    python scripts/send_digests.py              # send
    python scripts/send_digests.py --dry-run    # report what would be sent
    python scripts/send_digests.py --limit 20   # cap the run

Built for a scheduler. Run it after `python -m src.snapshots` has compared the
newest scored table against the previous one, typically once a day; there is no
harm in running it more often, because a notification is only ever emailed once.

Who receives one:

  notify_opt_in    they asked for alerts
  email_verified   the address has been confirmed to exist
  is_active        the account has not been disabled
  and at least one notification that is unread and has never been emailed

read_at matters as well as emailed_at: somebody who has already seen the change
on their dashboard does not need it again in their inbox.

emailed_at is stamped only when delivery succeeded. A failed send leaves the row
untouched, so the next run picks it up rather than losing it silently.

No address is printed by this script, including in --dry-run. User ids only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from src import mailer  # noqa: E402
from src.models import (  # noqa: E402
    Notification,
    User,
    get_session,
    init_db,
    utcnow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reportscope.digests")


def pending() -> dict[int, list[Notification]]:
    """Unread, never-emailed notifications grouped by the user who should get them."""
    db = get_session()
    rows = db.execute(
        select(Notification, User)
        .join(User, User.id == Notification.user_id)
        .where(
            Notification.read_at.is_(None),
            Notification.emailed_at.is_(None),
            User.notify_opt_in.is_(True),
            User.email_verified.is_(True),
            User.is_active.is_(True),
        )
        .order_by(Notification.user_id, Notification.created_at)
    ).all()

    grouped: dict[int, list[Notification]] = {}
    for notification, _user in rows:
        grouped.setdefault(notification.user_id, []).append(notification)
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(description="Send notification digests.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be sent, send nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many users (0 = no limit)")
    args = parser.parse_args()

    init_db()
    db = get_session()
    grouped = pending()

    if not grouped:
        log.info("nothing to send")
        return 0

    log.info("%d user(s) with pending alerts", len(grouped))
    sent = failed = 0

    for n, (user_id, notifications) in enumerate(sorted(grouped.items()), 1):
        if args.limit and n > args.limit:
            log.info("limit reached, stopping")
            break

        user = db.get(User, user_id)
        if user is None:
            continue

        if args.dry_run:
            log.info("would send user_id=%s items=%d", user_id, len(notifications))
            continue

        if not mailer.send_notification_digest(user, notifications):
            failed += 1
            continue

        stamp = utcnow()
        for notification in notifications:
            notification.emailed_at = stamp
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            # The message is already gone. Losing the stamp means the next run
            # sends it again, which is the better of the two failures, but it
            # needs to be visible in the log when it happens.
            log.exception("digest sent but emailed_at not stamped user_id=%s", user_id)
            failed += 1
            continue
        sent += 1

    if not args.dry_run:
        from src import analytics

        if sent:
            analytics.bump("digests_sent", amount=sent)
        log.info("digests sent=%d failed=%d", sent, failed)
    return 1 if failed and not sent else 0


if __name__ == "__main__":
    raise SystemExit(main())
