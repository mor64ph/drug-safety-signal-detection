"""
M11: persistence.

Everything the scoring pipeline produces is derived and can be rebuilt from the
API. Everything in this module belongs to a person -- their account, the drugs
they track, what they were told and when -- and cannot be regenerated if it is
lost. That asymmetry is why it lives in a database with constraints rather than
in another file beside the parquet.

The schema stays inside the common subset of SQLite and PostgreSQL. SQLite is
enough for a single-process deployment and for local work; Postgres is what a
second process requires. No dialect-specific type appears here, so DATABASE_URL
is the only thing that differs between them.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    scoped_session,
    sessionmaker,
)

log = logging.getLogger("rxsignal.models")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_URL = "sqlite:///data/rxsignal.db"


def utcnow() -> datetime:
    """
    Naive UTC, deliberately.

    SQLite hands datetimes back without a timezone whatever was written, so a
    column populated with aware values returns naive ones and the first
    `expires < now` comparison raises TypeError -- inside a password reset, the
    one path a user cannot work around. Storing naive UTC everywhere keeps both
    sides of every comparison the same kind on both backends.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_token() -> str:
    """URL-safe token for email verification and password reset."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

try:
    from argon2 import PasswordHasher

    _hasher = PasswordHasher()
    HASH_BACKEND = "argon2id"
except ImportError:  # pragma: no cover - depends on the install
    _hasher = None
    HASH_BACKEND = "scrypt"


def hash_password(password: str) -> str:
    """Hash a password with whichever backend is installed."""
    if _hasher is not None:
        return _hasher.hash(password)
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password, method="scrypt")


def verify_password(password: str, stored: str) -> bool:
    """
    Check a password against a stored hash.

    Dispatch is on the stored hash, not on what is installed now. A database
    created before argon2-cffi was available holds scrypt hashes, and those
    accounts have to keep working after the dependency lands -- otherwise
    installing a library silently locks every existing user out.
    """
    if not stored or not password:
        return False
    if stored.startswith("$argon2"):
        if _hasher is None:
            log.error("argon2 hash found but argon2-cffi is not installed")
            return False
        try:
            return _hasher.verify(stored, password)
        except Exception:
            # A mismatch, a truncated hash and a hash written by a newer argon2
            # are all "this password does not open this account".
            return False
    from werkzeug.security import check_password_hash

    try:
        return check_password_hash(stored, password)
    except Exception:
        return False


_dummy_hash: str | None = None


def waste_time() -> None:
    """
    Run one hash for a login attempt on an address with no account.

    Without it, "no such user" returns in microseconds and a real account
    returns in the tens of milliseconds an argon2 verification costs, which
    turns the login form into an account-existence oracle that the deliberately
    uninformative error message was meant to close.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password("x" * 12, _dummy_hash)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verify_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    verify_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    reset_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reset_token_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Recorded at signup because it cannot be reconstructed later. A consent
    # flag added by a migration says only that somebody ran a migration.
    consent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)

    newsletter_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    medications: Mapped[list[UserMedication]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    searches: Mapped[list[SearchHistory]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        # No address: repr reaches logs and tracebacks, which are the two places
        # a user's email is least in their control.
        return f"<User id={self.id} verified={self.email_verified}>"


class UserMedication(Base):
    __tablename__ = "user_medications"
    __table_args__ = (UniqueConstraint("user_id", "drug", name="uq_user_drug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    drug: Mapped[str] = mapped_column(String(120), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="medications")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    drug: Mapped[str] = mapped_column(String(120), nullable=False)
    reaction_pt: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # Both bounds are kept so the message can say how far it moved. "IC025 rose"
    # is a claim the reader cannot check; "1.8 to 2.4" is one they can.
    ic025: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_ic025: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="notifications")


KINDS = ("new_signal", "strengthened", "newly_labelled")


class SearchHistory(Base):
    __tablename__ = "search_history"
    __table_args__ = (Index("ix_search_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable: most searches are made by people who never sign in, and those
    # still have to be counted for coverage decisions.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    drug_query: Mapped[str] = mapped_column(String(120), nullable=False)
    matched_drug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User | None] = relationship(back_populates="searches")


class SignalSnapshot(Base):
    """
    One scored pair as it stood on one day.

    Without a history there is no way to tell an emerging signal from a merely
    current one: the parquet always describes now, and "new" is a statement
    about two points in time.
    """

    __tablename__ = "signal_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "drug", "reaction_pt", name="uq_snapshot_pair"),
        Index("ix_snapshot_date_drug", "snapshot_date", "drug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    drug: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reaction_pt: Mapped[str] = mapped_column(String(200), nullable=False)
    ic025: Mapped[float] = mapped_column(Float, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    a: Mapped[int] = mapped_column(Integer, nullable=False)
    labelled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("day", "metric", name="uq_day_metric"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ---------------------------------------------------------------------------
# Engine and sessions
# ---------------------------------------------------------------------------

def _setting(name: str, default: str = "") -> str:
    """
    Read configuration from the environment, falling back to .env.

    The implementation lives here rather than in src.app, and src.app delegates
    to it, because Alembic has to resolve DATABASE_URL without constructing the
    application: importing src.app creates the tables on the way past, and a
    migration that runs after create_all finds every table already there and
    fails. One reader, and the database layer does not depend on the web layer.
    """
    if os.environ.get(name):
        return os.environ[name].strip()
    env_file = _PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return default


def database_url() -> str:
    """
    Resolve DATABASE_URL, anchoring a relative SQLite path to the project root.

    `sqlite:///data/rxsignal.db` is relative to the working directory, so the
    scheduler running scripts/send_digests.py from somewhere else would quietly
    create a second, empty database and report that nobody has any
    notifications. Absolute paths and in-memory URLs pass through untouched.

    Postgres URLs are rewritten onto the psycopg 3 dialect. Hosts hand out
    `postgres://` or `postgresql://`; SQLAlchemy 2 maps both to psycopg2, which
    is not installed, so the app would start and then fail on first connection
    with a bare ModuleNotFoundError that says nothing about the cause.
    """
    url = _setting("DATABASE_URL", _DEFAULT_URL).strip() or _DEFAULT_URL

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith(prefix + "/"):
        rel = url[len(prefix):]
        if rel and rel != ":memory:" and not os.path.isabs(rel):
            resolved = (_PROJECT_ROOT / rel).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            return prefix + resolved.as_posix()
    return url


_engine: Engine | None = None
_Session: scoped_session | None = None


def _sqlite_pragmas(dbapi_connection, _record):
    """
    SQLite ignores ON DELETE CASCADE unless foreign keys are enabled on every
    connection, and account deletion has to actually delete. WAL keeps a reader
    from blocking the writer across Waitress's threads.
    """
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = database_url()
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # Waitress serves on eight threads and pools connections across them.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        engine = create_engine(url, **kwargs)
        if engine.dialect.name == "sqlite":
            event.listen(engine, "connect", _sqlite_pragmas)
        _engine = engine
    return _engine


def get_session() -> Session:
    """Thread-local session. One per request; removed by the teardown below."""
    global _Session
    if _Session is None:
        _Session = scoped_session(
            sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
        )
    return _Session()


def remove_session(_exc: BaseException | None = None) -> None:
    """Flask teardown: return the connection instead of leaking it per request."""
    if _Session is not None:
        _Session.remove()


def init_db() -> None:
    """
    Create any table that does not exist yet.

    Alembic owns schema *changes*; this only bootstraps an empty database so a
    fresh checkout runs without a migration step. create_all never alters an
    existing table, so the two cannot fight.
    """
    Base.metadata.create_all(get_engine())


def init_app(app) -> None:
    """Attach the session teardown to a Flask app."""
    app.teardown_appcontext(remove_session)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def normalise_email(raw: str) -> str:
    """
    Strip and lowercase before every comparison and every write.

    The unique index is over the stored string, so "A@x.com" and "a@x.com" are
    two accounts unless the value is folded on the way in.
    """
    return (raw or "").strip().lower()


def token_expiry(hours: int = 1) -> datetime:
    return utcnow() + timedelta(hours=hours)
