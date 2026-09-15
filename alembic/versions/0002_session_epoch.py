"""Add users.session_epoch, so a password reset can evict existing sessions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15

Defect D-05. Sessions are stateless signed cookies, so there is no server-side
record to delete and a cookie issued before a password reset stayed valid for
its full 30-day lifetime. The one action a person takes *because* they think
someone else is in their account did not remove that someone else.

The epoch is copied into the session at sign-in and compared on every request.
Bumping it invalidates every cookie issued before the bump, without storing
per-session state.

server_default is required, not cosmetic: the column is NOT NULL and existing
rows need a value at the moment the column is added. Without it the ALTER
fails on any table that already has users -- which is every deployment this
migration actually matters for.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_epoch", sa.Integer(), nullable=False,
                  server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "session_epoch")
