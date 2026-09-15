"""
M11: accounts.

Registration, sign-in, email verification, password reset, and deletion.

The design decisions worth knowing about:

  Consent is recorded at signup, with the version of the text that was on the
  screen. It is the only moment at which the record can honestly be made.

  /forgot answers identically whether or not the address is registered. A form
  that distinguishes the two cases is a membership oracle for a health tool,
  and "is this person on a pharmacovigilance site" is not a question a stranger
  should be able to ask.

  Account deletion is a hard delete. Rows disappear, along with everything the
  cascade reaches. A flag named deleted=True is not what the user asked for.

  Nothing in here logs an address. User ids only.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from functools import wraps

from flask import (
    Blueprint,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src import analytics, mailer
from src.security import constant_time_equal
from src.models import (
    Notification,
    User,
    UserMedication,
    get_session,
    hash_password,
    new_token,
    normalise_email,
    token_expiry,
    utcnow,
    verify_password,
    waste_time,
)

log = logging.getLogger("rxsignal.auth")

bp = Blueprint("auth", __name__)

# The wording shown beside the consent checkbox. Bump it when that wording
# changes; existing rows keep the version their user actually agreed to.
CONSENT_VERSION = "2026-09-14"

# D-04: a confirmation link is not a standing credential.
VERIFY_TTL_HOURS = 72

MIN_PASSWORD = 10
MAX_PASSWORD = 256
RESEND_INTERVAL_SECONDS = 300

# Deliberately permissive on the local part and strict on the shape. Anything
# tighter rejects real addresses; the verification email is what actually
# establishes that an address exists.
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9][A-Za-z0-9.\-]{0,252}\.[A-Za-z]{2,24}$")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def csrf_token() -> str:
    """Per-session token, minted on first use and reused until sign-in."""
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_protect():
    """
    Registered as an app-wide before_request rather than a decorator.

    Per-view protection is protection you can forget to apply, and the view you
    forget is the one that changes a password. Every POST in the application is
    state-changing, so there is no endpoint that would need an exemption.
    """
    if request.method != "POST":
        return None
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf", "")
    if not constant_time_equal(supplied, expected):
        log.warning("CSRF check failed for %s", request.path)
        return render_template(
            "error.html",
            code=400,
            message="That form expired or came from somewhere else. "
                    "Go back, reload the page, and try again.",
        ), 400
    return None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def current_user() -> User | None:
    """The signed-in user, or None. Cached per request."""
    if "user" in g:
        return g.user
    g.user = None
    uid = session.get("user_id")
    if uid:
        try:
            user = get_session().get(User, int(uid))
        except (SQLAlchemyError, ValueError, TypeError):
            log.exception("could not load session user")
            user = None
        if user is None or not user.is_active:
            session.pop("user_id", None)
        elif session.get("epoch") != (user.session_epoch or 0):
            # Issued before a password reset. Stateless cookies cannot be
            # revoked, so the epoch is what revokes them (D-05). Sessions
            # predating the column have no "epoch" key and are signed out
            # once, deliberately.
            session.clear()
        else:
            g.user = user
    return g.user


def _start_session(user: User) -> None:
    """
    Replace the session wholesale on sign-in.

    Keeping the pre-login session would let an attacker who fixed a session id
    in someone's browser ride it into the authenticated one; the CSRF token is
    reissued for the same reason.
    """
    session.clear()
    session["user_id"] = user.id
    session["epoch"] = user.session_epoch or 0
    session["_csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def verified_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("auth.login", next=request.path))
        if not user.email_verified:
            return render_template(
                "error.html",
                code=403,
                message="Confirm your email address first. We sent a link when "
                        "you registered; use the resend button at the top of the "
                        "page if it did not arrive.",
            ), 403
        return view(*args, **kwargs)

    return wrapped


def _safe_next(raw: str) -> str:
    """
    Only same-site paths survive.

    "//evil.example" is a protocol-relative URL that a browser follows off-site,
    so a leading double slash is rejected along with anything carrying a scheme.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return url_for("user_features.dashboard")
    return raw


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------

@bp.app_context_processor
def _inject_user():
    """
    Every page shows the nav, so every page needs the user.

    Wrapped because this also runs while rendering the 500 page, and a context
    processor that raises there replaces a handled error with an unhandled one.
    """
    try:
        user = current_user()
    except Exception:
        user = None
    return {
        "nav_user": user,
        "csrf_token": csrf_token,
        "consent_version": CONSENT_VERSION,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_email(raw: str) -> tuple[str, str | None]:
    email = normalise_email(raw)
    if not email:
        return "", "Enter your email address."
    if len(email) > 320 or not _EMAIL_RE.match(email):
        return email, "That does not look like an email address."
    return email, None


def _validate_password(pw: str, confirm: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"Passwords must be at least {MIN_PASSWORD} characters."
    if len(pw) > MAX_PASSWORD:
        return f"Passwords must be at most {MAX_PASSWORD} characters."
    if confirm is not None and pw != confirm:
        return "The two passwords do not match."
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", errors=[], email="")

    email, err = _validate_email(request.form.get("email", ""))
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    consent = request.form.get("consent") == "on"
    newsletter = request.form.get("newsletter") == "on"

    errors = [err] if err else []
    pw_err = _validate_password(password, confirm)
    if pw_err:
        errors.append(pw_err)
    if not consent:
        errors.append("You have to agree to the terms to create an account.")

    if errors:
        return render_template("register.html", errors=errors, email=email), 400

    db = get_session()

    # D-01. An existing address used to be told so, which let anyone with a
    # list of addresses test who has an account -- health-adjacent information
    # on a site like this. Both branches now end on the same page with the same
    # status, and the real owner is notified by email instead.
    #
    # Note what this forces: registration can no longer sign the new account
    # in. A session cookie on one branch and not the other is visible in the
    # response headers, so auto-login would leak exactly what the wording
    # stopped leaking. Confirming the address is now a step, not a formality.
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        if existing.is_active:
            mailer.send_async(mailer.send_registration_attempt, existing)
        log.info("registration attempted for an existing address")
        return render_template("register.html", check_email=True, errors=[],
                               email="")

    now = utcnow()
    user = User(
        email=email,
        email_verified=False,
        verify_token=new_token(),
        verify_sent_at=now,
        password_hash=hash_password(password),
        created_at=now,
        is_active=True,
        consent_at=now,
        consent_version=CONSENT_VERSION,
        newsletter_opt_in=newsletter,
        notify_opt_in=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two submissions of the same form, or two people racing the same
        # address. The unique index is the arbiter; the loser is told the truth.
        db.rollback()
        # Same response as the existing-address branch above, for the same
        # reason: this path is reachable by submitting the form twice.
        return render_template("register.html", check_email=True, errors=[],
                               email="")
    except SQLAlchemyError:
        db.rollback()
        log.exception("registration failed to commit")
        return render_template(
            "register.html",
            errors=["We could not create the account just now. Try again in a moment."],
            email=email,
        ), 500

    analytics.bump("registrations")
    mailer.send_async(mailer.send_verification, user, user.verify_token)
    log.info("registered user_id=%s", user.id)
    return render_template("register.html", check_email=True, errors=[], email="")


@bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")
    if request.method == "GET":
        return render_template("login.html", errors=[], email="", next_url=next_url)

    email = normalise_email(request.form.get("email", ""))
    password = request.form.get("password", "")

    db = get_session()
    user = db.scalar(select(User).where(User.email == email)) if email else None

    if user is None:
        waste_time()
        ok = False
    else:
        ok = user.is_active and verify_password(password, user.password_hash)

    if not ok:
        # One message for a wrong password, an unknown address and a disabled
        # account: distinguishing them would tell an attacker which addresses
        # are worth attacking.
        log.info("failed sign-in user_id=%s", user.id if user else "none")
        return render_template(
            "login.html",
            errors=["That email and password do not match an account."],
            email=email,
            next_url=next_url,
        ), 401

    user.last_login_at = utcnow()
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not stamp last_login_at")

    _start_session(user)
    analytics.bump("logins")
    log.info("sign-in user_id=%s", user.id)
    return redirect(_safe_next(next_url))


@bp.route("/logout", methods=["POST"])
def logout():
    uid = session.get("user_id")
    session.clear()
    if uid:
        log.info("sign-out user_id=%s", uid)
    return redirect(url_for("index"))


@bp.route("/verify/<token>")
def verify(token: str):
    db = get_session()
    user = db.scalar(select(User).where(User.verify_token == token)) if token else None
    if user is None:
        return render_template(
            "error.html",
            code=404,
            message="That confirmation link is not valid. It may already have "
                    "been used. Sign in and use the resend button if you still "
                    "need to confirm your address.",
        ), 404

    # D-04. The link used to work forever. A verification mail sits in an
    # inbox indefinitely -- an abandoned address, a shared family account, a
    # mail archive, a corporate link-scanner -- and following it also signed
    # the visitor in, so an old message was a standing credential. It now
    # expires, and it confirms the address without granting a session.
    age = None
    if user.verify_sent_at is not None:
        age = (utcnow() - user.verify_sent_at).total_seconds()
    if age is None or age > VERIFY_TTL_HOURS * 3600:
        return render_template(
            "error.html",
            code=410,
            message="That confirmation link has expired. Sign in and use the "
                    "resend button to get a fresh one.",
        ), 410

    user.email_verified = True
    user.verify_token = None
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("verification failed to commit")
        return render_template(
            "error.html", code=500,
            message="We could not confirm the address just now. Try the link again.",
        ), 500

    log.info("email verified user_id=%s", user.id)
    if session.get("user_id") == user.id:
        # Already signed in in this browser; there is nothing to grant.
        return redirect(url_for("user_features.dashboard"))
    # Deliberately does not start a session: opening a link out of an inbox is
    # not authentication (D-04).
    return render_template("login.html", errors=[], email=user.email,
                           next_url="", verified=True)


@bp.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    user = current_user()
    if user.email_verified:
        return redirect(url_for("auth.account"))

    now = utcnow()
    if user.verify_sent_at and (now - user.verify_sent_at).total_seconds() < RESEND_INTERVAL_SECONDS:
        wait = RESEND_INTERVAL_SECONDS - int((now - user.verify_sent_at).total_seconds())
        return render_template(
            "account.html",
            user=user,
            med_count=_med_count(user),
            errors=[f"A confirmation email went out recently. Try again in "
                    f"{max(wait, 1)} seconds, and check the spam folder."],
            saved=False,
        ), 429

    db = get_session()
    user.verify_token = new_token()
    user.verify_sent_at = now
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not reissue verification token")
        return redirect(url_for("auth.account"))

    mailer.send_verification(user, user.verify_token)
    return render_template(
        "account.html",
        user=user,
        med_count=_med_count(user),
        errors=[],
        saved=False,
        notice="Confirmation email sent. Check the spam folder if it does not arrive.",
    )


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "GET":
        return render_template("forgot.html", sent=False, errors=[], email="")

    email = normalise_email(request.form.get("email", ""))
    db = get_session()
    user = db.scalar(select(User).where(User.email == email)) if email else None

    if user is not None and user.is_active:
        # Do not mint a second token while the first is minutes old: a repeated
        # submission would otherwise mail a third party once per attempt.
        recent = (
            user.reset_token
            and user.reset_token_expires
            and (user.reset_token_expires - utcnow()).total_seconds() > 3480
        )
        if not recent:
            user.reset_token = new_token()
            user.reset_token_expires = token_expiry(hours=1)
            try:
                db.commit()
            except SQLAlchemyError:
                db.rollback()
                log.exception("could not issue reset token")
            else:
                mailer.send_async(mailer.send_password_reset, user,
                                  user.reset_token)

    # Same page, same status, and now genuinely the same timing: the send runs
    # off-thread, so the registered branch no longer pays for an SMTP round
    # trip that the unregistered branch skips (D-02).
    return render_template("forgot.html", sent=True, errors=[], email="")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token: str):
    db = get_session()
    user = db.scalar(select(User).where(User.reset_token == token)) if token else None
    expired = (
        user is None
        or user.reset_token_expires is None
        or user.reset_token_expires < utcnow()
    )

    if expired:
        return render_template(
            "reset.html",
            token=token,
            expired=True,
            errors=["That reset link has expired or has already been used. "
                    "Request a new one."],
        ), 400

    if request.method == "GET":
        return render_template("reset.html", token=token, expired=False, errors=[])

    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    err = _validate_password(password, confirm)
    if err:
        return render_template(
            "reset.html", token=token, expired=False, errors=[err]
        ), 400

    user.password_hash = hash_password(password)
    user.reset_token = None
    user.reset_token_expires = None
    # Evict every session issued before this moment. Someone resetting because
    # they suspect an intruder expects exactly that (D-05).
    user.session_epoch = (user.session_epoch or 0) + 1
    # Opening a link that only arrives by email proves the address works, which
    # is the same thing the verification email establishes.
    user.email_verified = True
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("password reset failed to commit")
        return render_template(
            "reset.html", token=token, expired=False,
            errors=["We could not save the new password. Try the link again."],
        ), 500

    log.info("password reset user_id=%s", user.id)
    _start_session(user)
    return redirect(url_for("user_features.dashboard"))


def _med_count(user: User) -> int:
    return int(
        get_session().scalar(
            select(func.count(UserMedication.id)).where(UserMedication.user_id == user.id)
        )
        or 0
    )


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = current_user()
    if request.method == "GET":
        return render_template(
            "account.html", user=user, med_count=_med_count(user), errors=[], saved=False
        )

    user.newsletter_opt_in = request.form.get("newsletter") == "on"
    user.notify_opt_in = request.form.get("notify") == "on"
    db = get_session()
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not save preferences")
        return render_template(
            "account.html", user=user, med_count=_med_count(user),
            errors=["We could not save that. Try again in a moment."], saved=False,
        ), 500

    log.info("preferences updated user_id=%s", user.id)
    return render_template(
        "account.html", user=user, med_count=_med_count(user), errors=[], saved=True
    )


@bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    user = current_user()
    if request.form.get("confirm", "").strip() != "DELETE":
        return render_template(
            "account.html",
            user=user,
            med_count=_med_count(user),
            errors=["Type DELETE in the box to confirm. Nothing has been deleted."],
            saved=False,
        ), 400

    uid = user.id
    db = get_session()
    try:
        # Relationship cascades remove medications, notifications and search
        # history; the FK is ON DELETE CASCADE so a direct SQL delete would do
        # the same. Nothing is retained, including the analytics rows.
        db.delete(user)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("account deletion failed user_id=%s", uid)
        return render_template(
            "account.html",
            user=user,
            med_count=_med_count(user),
            errors=["We could not delete the account just now. Nothing was "
                    "removed. Please try again."],
            saved=False,
        ), 500

    remaining = db.scalar(
        select(func.count(Notification.id)).where(Notification.user_id == uid)
    )
    if remaining:
        log.error("account deletion left %s notification rows user_id=%s", remaining, uid)

    session.clear()
    log.info("account deleted user_id=%s", uid)
    return render_template(
        "error.html",
        code="Deleted",
        message="Your account, your tracked drugs, your notifications and your "
                "search history have been removed. Nothing is kept.",
    )
