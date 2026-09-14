"""
Alembic environment.

The URL is read through src.models.database_url(), which is the same path the
application takes: DATABASE_URL from the environment, falling back to .env,
falling back to the SQLite file under data/. One source of truth means a
migration cannot be applied to a database the app has never seen.

    alembic upgrade head        apply
    alembic downgrade -1        undo the last one
    alembic revision -m "..."   start a new one
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.models import Base, database_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL is NOT written into the Alembic config. set_main_option() stores it
# in a ConfigParser, which treats % as interpolation syntax, so a generated
# password containing one comes back mangled and fails to parse -- reported as
# "Could not parse SQLAlchemy URL", which points at the URL rather than at the
# round trip that corrupted it. Passing it straight to create_engine avoids
# the problem instead of escaping around it.

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, so the same migration script works on both backends.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
