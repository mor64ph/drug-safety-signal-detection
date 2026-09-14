"""Initial schema: accounts, tracked drugs, notifications, snapshots, counters.

Revision ID: 0001
Revises:
Create Date: 2026-09-14

Mirrors src/models.py. Types are the common subset of SQLite and PostgreSQL:
no JSON column, no array, no dialect-specific default, so one script applies to
both.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("verify_token", sa.String(length=64), nullable=True),
        sa.Column("verify_sent_at", sa.DateTime(), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("reset_token", sa.String(length=64), nullable=True),
        sa.Column("reset_token_expires", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("consent_at", sa.DateTime(), nullable=False),
        sa.Column("consent_version", sa.String(length=32), nullable=False),
        sa.Column("newsletter_opt_in", sa.Boolean(), nullable=False),
        sa.Column("notify_opt_in", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_verify_token", "users", ["verify_token"], unique=False)
    op.create_index("ix_users_reset_token", "users", ["reset_token"], unique=False)

    op.create_table(
        "user_medications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("drug", sa.String(length=120), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "drug", name="uq_user_drug"),
    )
    op.create_index("ix_user_medications_user_id", "user_medications", ["user_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("drug", sa.String(length=120), nullable=False),
        sa.Column("reaction_pt", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=False),
        sa.Column("ic025", sa.Float(), nullable=True),
        sa.Column("prev_ic025", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("emailed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
    )

    op.create_table(
        "search_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("drug_query", sa.String(length=120), nullable=False),
        sa.Column("matched_drug", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_search_history_user_id", "search_history", ["user_id"])
    op.create_index("ix_search_created", "search_history", ["created_at"])

    op.create_table(
        "signal_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("drug", sa.String(length=120), nullable=False),
        sa.Column("reaction_pt", sa.String(length=200), nullable=False),
        sa.Column("ic025", sa.Float(), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("a", sa.Integer(), nullable=False),
        sa.Column("labelled", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date", "drug", "reaction_pt", name="uq_snapshot_pair"
        ),
    )
    op.create_index("ix_signal_snapshots_snapshot_date", "signal_snapshots", ["snapshot_date"])
    op.create_index("ix_signal_snapshots_drug", "signal_snapshots", ["drug"])
    op.create_index("ix_snapshot_date_drug", "signal_snapshots", ["snapshot_date", "drug"])

    op.create_table(
        "daily_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", "metric", name="uq_day_metric"),
    )
    op.create_index("ix_daily_stats_day", "daily_stats", ["day"])


def downgrade() -> None:
    op.drop_index("ix_daily_stats_day", table_name="daily_stats")
    op.drop_table("daily_stats")

    op.drop_index("ix_snapshot_date_drug", table_name="signal_snapshots")
    op.drop_index("ix_signal_snapshots_drug", table_name="signal_snapshots")
    op.drop_index("ix_signal_snapshots_snapshot_date", table_name="signal_snapshots")
    op.drop_table("signal_snapshots")

    op.drop_index("ix_search_created", table_name="search_history")
    op.drop_index("ix_search_history_user_id", table_name="search_history")
    op.drop_table("search_history")

    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_user_medications_user_id", table_name="user_medications")
    op.drop_table("user_medications")

    op.drop_index("ix_users_reset_token", table_name="users")
    op.drop_index("ix_users_verify_token", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
