"""
M11: usage counters.

Two questions this has to answer before an investor conversation: is anyone
using it, and are they looking for drugs the catalogue does not contain. The
second one decides what gets scored next, and it is only visible in the
unmatched queries.

Everything the admin view shows is an aggregate. No row in it identifies a
person, and no address appears anywhere in this module. Recording is
best-effort throughout: a counter that fails must never take the search with it.
"""

from __future__ import annotations

import hmac
import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request, session
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from src.models import (
    DailyStat,
    Notification,
    SearchHistory,
    User,
    UserMedication,
    _setting,
    get_session,
    utcnow,
)

log = logging.getLogger("rxsignal.analytics")

bp = Blueprint("analytics", __name__)


def bump(metric: str, day: date | None = None, amount: int = 1) -> None:
    """
    Add to one day's counter, creating the row if this is the first event.

    UPDATE-then-INSERT rather than INSERT-then-UPDATE: the row exists for all
    but the first event of the day, so the common path is a single statement.
    The unique constraint catches the race on that first event.
    """
    db = get_session()
    when = day or utcnow().date()
    try:
        result = db.execute(
            update(DailyStat)
            .where(DailyStat.day == when, DailyStat.metric == metric)
            .values(value=DailyStat.value + amount)
        )
        if not result.rowcount:
            db.add(DailyStat(day=when, metric=metric, value=amount))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        try:
            db.execute(
                update(DailyStat)
                .where(DailyStat.day == when, DailyStat.metric == metric)
                .values(value=DailyStat.value + amount)
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            log.warning("could not record metric %s", metric)


def record_search(drug_query: str, matched: str | None, user_id: int | None) -> None:
    """
    Log one search. Failures are swallowed: this is instrumentation, and the
    user asked for signal results, not for their search to be counted.
    """
    query = (drug_query or "").strip()[:120]
    if not query:
        return
    matched_name = (matched or "").strip()[:120] or None
    db = get_session()
    try:
        db.add(
            SearchHistory(
                user_id=user_id,
                drug_query=query,
                matched_drug=matched_name,
                created_at=utcnow(),
            )
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.warning("could not record search")
        return
    bump("searches")
    if not matched:
        bump("searches_unmatched")


def _count(db, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def summary() -> dict:
    """Aggregate figures for the admin page. No per-user rows leave this function."""
    db = get_session()
    now = utcnow()
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)

    top_rows = db.execute(
        select(
            func.lower(func.coalesce(SearchHistory.matched_drug, SearchHistory.drug_query)),
            func.count(SearchHistory.id),
        )
        .group_by(func.lower(func.coalesce(SearchHistory.matched_drug, SearchHistory.drug_query)))
        .order_by(func.count(SearchHistory.id).desc())
        .limit(20)
    ).all()

    # Queries that matched nothing are the coverage backlog: each one is a drug
    # somebody wanted and the catalogue does not have.
    unmatched_rows = db.execute(
        select(func.lower(SearchHistory.drug_query), func.count(SearchHistory.id))
        .where(SearchHistory.matched_drug.is_(None))
        .group_by(func.lower(SearchHistory.drug_query))
        .order_by(func.count(SearchHistory.id).desc())
        .limit(20)
    ).all()

    tracked_rows = db.execute(
        select(func.lower(UserMedication.drug), func.count(UserMedication.id))
        .group_by(func.lower(UserMedication.drug))
        .order_by(func.count(UserMedication.id).desc())
        .limit(20)
    ).all()

    daily_rows = db.execute(
        select(DailyStat.day, DailyStat.metric, DailyStat.value)
        .where(DailyStat.day >= (now - timedelta(days=14)).date())
        .order_by(DailyStat.day.desc())
    ).all()

    daily: dict[date, dict[str, int]] = {}
    for day, metric, value in daily_rows:
        daily.setdefault(day, {})[metric] = value

    return {
        "generated_at": now,
        "users_total": _count(db, select(func.count(User.id))),
        "users_verified": _count(
            db, select(func.count(User.id)).where(User.email_verified.is_(True))
        ),
        "users_newsletter": _count(
            db, select(func.count(User.id)).where(User.newsletter_opt_in.is_(True))
        ),
        "users_notify": _count(
            db, select(func.count(User.id)).where(User.notify_opt_in.is_(True))
        ),
        "users_7d": _count(
            db, select(func.count(User.id)).where(User.created_at >= week)
        ),
        "searches_total": _count(db, select(func.count(SearchHistory.id))),
        "searches_7d": _count(
            db, select(func.count(SearchHistory.id)).where(SearchHistory.created_at >= week)
        ),
        "searches_30d": _count(
            db, select(func.count(SearchHistory.id)).where(SearchHistory.created_at >= month)
        ),
        "searches_unmatched": _count(
            db,
            select(func.count(SearchHistory.id)).where(SearchHistory.matched_drug.is_(None)),
        ),
        "top_searches": [(term, n) for term, n in top_rows],
        "top_unmatched": [(term, n) for term, n in unmatched_rows],
        "top_tracked": [(term, n) for term, n in tracked_rows],
        "notifications_created": _count(db, select(func.count(Notification.id))),
        "notifications_emailed": _count(
            db, select(func.count(Notification.id)).where(Notification.emailed_at.isnot(None))
        ),
        "notifications_unread": _count(
            db, select(func.count(Notification.id)).where(Notification.read_at.is_(None))
        ),
        "medications_tracked": _count(db, select(func.count(UserMedication.id))),
        "medications_distinct": _count(
            db, select(func.count(func.distinct(func.lower(UserMedication.drug))))
        ),
        "daily": sorted(daily.items(), reverse=True),
    }


def _admin_ok() -> bool:
    """
    The admin code arrives as ?admin_code=, not ?code=, because the site-wide
    access gate already owns that parameter and would reject the request before
    this view ran. A match is remembered in the session so the code is not
    pasted into a URL -- and into a proxy log -- on every visit.
    """
    expected = _setting("RXSIGNAL_ADMIN_CODE")
    if not expected:
        return False
    if session.get("admin_ok") is True:
        return True
    supplied = (request.args.get("admin_code") or request.form.get("admin_code") or "").strip()
    if supplied and hmac.compare_digest(supplied, expected):
        session["admin_ok"] = True
        return True
    return False


@bp.route("/admin/analytics")
def admin_analytics():
    if not _admin_ok():
        # Unset code means closed, never open: a deployment that forgot to
        # configure one must not publish its usage figures to the internet.
        log.warning("rejected admin analytics request")
        return render_template(
            "error.html", code=404, message="That page does not exist."
        ), 404

    try:
        stats = summary()
    except SQLAlchemyError:
        log.exception("analytics summary failed")
        return render_template(
            "error.html", code=500,
            message="The analytics query failed. The application itself is fine.",
        ), 500

    return render_template("admin_analytics.html", s=stats, now=datetime.now())
