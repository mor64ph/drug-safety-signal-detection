#!/bin/sh
# Apply migrations, then serve.
#
# Running `alembic upgrade head` on boot means a deploy that adds a column
# cannot start against a database that lacks it. Alembic is idempotent: on an
# already-current database this is a no-op costing about a second.
#
# If the migration fails the container must NOT start. A web process serving
# traffic against a half-migrated schema produces errors that look like
# application bugs and sends you looking in the wrong place.

set -e

echo "[entrypoint] applying database migrations"
alembic upgrade head

echo "[entrypoint] starting server"
exec python serve.py
