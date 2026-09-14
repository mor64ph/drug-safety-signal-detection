"""
M11: the signed-in half of the site.

A tracked drug list, the changes that have happened to those drugs, and the
searches that led there.

One rule governs what a tracked drug is allowed to show. Only strong-tier pairs
that have been through the bias diagnostics appear on the dashboard. An
undiagnosed row has not been checked for indication confounding, and
atorvastatin x TYPE 2 DIABETES MELLITUS at IC025 4.4 is what that looks like:
a top-ranked, entirely spurious result produced by the fact that diabetics are
prescribed statins. On the search page an unchecked row is at least surrounded
by its caveats and separated from the checked ones. On a page headed "your
medication" it would be read as a finding about the reader.
"""

from __future__ import annotations

import logging

from flask import Blueprint, redirect, render_template, request, url_for
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src import analytics
from src.auth import current_user, login_required, verified_required
from src.models import (
    Notification,
    SearchHistory,
    UserMedication,
    get_session,
    utcnow,
)

log = logging.getLogger("rxsignal.user")

bp = Blueprint("user_features", __name__)

# A tracked list is a medication list, not a watchlist. The cap is generous for
# a real polypharmacy patient and low enough that the dashboard stays readable
# and the fan-out stays bounded.
MAX_MEDICATIONS = 40

TOP_REACTIONS = 3


def _app():
    """
    src.app registers this blueprint, so its helpers are imported at call time
    rather than at module level.
    """
    from src import app as app_module

    return app_module


def resolve_drug(raw: str) -> tuple[str | None, str | None]:
    """
    Turn what the user typed into a drug name the results table contains.

    Brand names go through the same alias map the search box uses, so a user who
    types the name on the box gets the molecule. Anything the catalogue does not
    contain is refused rather than stored: a tracked drug that matches no row
    would sit on the dashboard for ever showing nothing, which reads as "no
    signals found for your medicine" -- a claim this tool has not earned for a
    drug it never scored.
    """
    app_module = _app()
    query = app_module._clean_query(raw)
    if not query:
        return None, ("Enter a drug name using letters, numbers and spaces only.")

    df = app_module._load_results()
    if df.empty or "drug" not in df.columns:
        return None, "The signal table is not loaded. Try again shortly."

    resolved = app_module._aliases().get(query.strip().lower(), query.strip()).lower()
    names = df["drug"].dropna().unique()

    for name in names:
        if str(name).lower() == resolved:
            return str(name), None

    partial = [str(n) for n in names if resolved in str(n).lower()]
    if len(partial) == 1:
        return partial[0], None

    return None, (
        f"'{query}' is not in the catalogue of {len(names)} scored drugs. "
        "Try the generic name. A drug that is missing has not been examined, "
        "which is not the same as having been examined and found clear."
    )


def _tracked_signals(drug: str) -> list[dict]:
    """Top strong-tier pairs for one drug, restricted to diagnosed rows."""
    app_module = _app()
    df = app_module._load_results()
    if df.empty or "drug" not in df.columns:
        return []

    sub = df[df["drug"] == drug]
    if sub.empty:
        return []
    if "diagnosed" in sub.columns:
        sub = sub[sub["diagnosed"].fillna(False).astype(bool)]
    if "tier" in sub.columns:
        sub = sub[sub["tier"] == "strong"]
    if sub.empty:
        return []
    if "IC025" in sub.columns:
        sub = sub.sort_values("IC025", ascending=False, na_position="last")
    return app_module._format_pairs(sub.head(TOP_REACTIONS))


def _medications(user_id: int) -> list[UserMedication]:
    return list(
        get_session().scalars(
            select(UserMedication)
            .where(UserMedication.user_id == user_id)
            .order_by(UserMedication.drug)
        )
    )


def _unread_count(user_id: int) -> int:
    return int(
        get_session().scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id, Notification.read_at.is_(None)
            )
        )
        or 0
    )


def _render_dashboard(errors: list[str] | None = None, status: int = 200):
    user = current_user()
    db = get_session()
    meds = _medications(user.id)
    cards = [
        {"row": m, "signals": _tracked_signals(m.drug)}
        for m in meds
    ]
    recent = list(
        db.scalars(
            select(SearchHistory)
            .where(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.created_at.desc())
            .limit(8)
        )
    )
    # The tracked list feeds /list directly. Truncated to what that page accepts
    # rather than handed over whole: a 40-drug list would silently lose the tail
    # there, and a link that quietly shows a subset of someone's medicines is
    # worse than a link that says how many it shows.
    list_cap = _app().MAX_LIST_DRUGS
    page = render_template(
        "my_dashboard.html",
        user=user,
        cards=cards,
        unread=_unread_count(user.id),
        recent=recent,
        errors=errors or [],
        max_medications=MAX_MEDICATIONS,
        list_drugs=",".join(m.drug for m in meds[:list_cap]),
        list_shown=min(len(meds), list_cap),
        list_total=len(meds),
        disclaimer=_app().DISCLAIMER,
    )
    return (page, status) if status != 200 else page


@bp.route("/my")
@login_required
def dashboard():
    return _render_dashboard()


@bp.route("/my/medications", methods=["POST"])
@verified_required
def add_medication():
    user = current_user()
    db = get_session()

    if len(_medications(user.id)) >= MAX_MEDICATIONS:
        return _render_dashboard(
            [f"You can track up to {MAX_MEDICATIONS} drugs. Remove one first."], 400
        )

    drug, err = resolve_drug(request.form.get("drug", ""))
    if err:
        return _render_dashboard([err], 400)

    existing = db.scalar(
        select(UserMedication).where(
            UserMedication.user_id == user.id, UserMedication.drug == drug
        )
    )
    if existing is not None:
        return _render_dashboard([f"{drug} is already on your list."], 400)

    db.add(UserMedication(user_id=user.id, drug=drug, added_at=utcnow()))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render_dashboard([f"{drug} is already on your list."], 400)
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not add medication user_id=%s", user.id)
        return _render_dashboard(
            ["We could not save that just now. Try again in a moment."], 500
        )

    analytics.bump("medications_added")
    log.info("medication added user_id=%s", user.id)
    return redirect(url_for("user_features.dashboard"))


@bp.route("/my/medications/<int:med_id>/delete", methods=["POST"])
@login_required
def delete_medication(med_id: int):
    user = current_user()
    db = get_session()
    try:
        # Scoped to the owner, so a guessed id deletes nothing.
        db.execute(
            delete(UserMedication).where(
                UserMedication.id == med_id, UserMedication.user_id == user.id
            )
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not remove medication user_id=%s", user.id)
        return _render_dashboard(["We could not remove that just now."], 500)
    return redirect(url_for("user_features.dashboard"))


@bp.route("/my/notifications")
@login_required
def notifications():
    user = current_user()
    db = get_session()
    rows = list(
        db.scalars(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .limit(200)
        )
    )
    # Which ones were new is recorded before they are marked read, so the page
    # the user is looking at still distinguishes them.
    new_ids = {n.id for n in rows if n.read_at is None}
    if new_ids:
        now = utcnow()
        try:
            for row in rows:
                if row.read_at is None:
                    row.read_at = now
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            log.exception("could not mark notifications read user_id=%s", user.id)

    return render_template(
        "notifications.html", user=user, rows=rows, new_ids=new_ids
    )


@bp.route("/my/history")
@login_required
def history():
    user = current_user()
    rows = list(
        get_session().scalars(
            select(SearchHistory)
            .where(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.created_at.desc())
            .limit(200)
        )
    )
    return render_template("history.html", user=user, rows=rows, cleared=False)


@bp.route("/my/history/clear", methods=["POST"])
@login_required
def clear_history():
    user = current_user()
    db = get_session()
    try:
        db.execute(delete(SearchHistory).where(SearchHistory.user_id == user.id))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not clear history user_id=%s", user.id)
        return render_template(
            "history.html", user=user, rows=[], cleared=False,
            errors=["We could not clear your history just now."],
        ), 500
    log.info("history cleared user_id=%s", user.id)
    return render_template("history.html", user=user, rows=[], cleared=True)
