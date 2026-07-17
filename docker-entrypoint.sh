#!/bin/sh
# Run DB migrations ONCE, in a single process, BEFORE starting the uvicorn workers. With --workers 4,
# the workers race the leader's `command.upgrade` on the SQLite DB and deadlock it forever (ADR-037) —
# busy_timeout/WAL doesn't reliably break it. Migrating here, with no workers yet running, removes the
# race. `set -e` means a migration failure halts the container (ADR-035: fail at deploy, not a black
# screen). The app's lifespan still calls run_migrations, but it's a fast no-op once schema is at head.
set -e
echo "[entrypoint] running DB migrations (single process, pre-workers)..."
python -c "import db_migrate; db_migrate.run_migrations()"
echo "[entrypoint] migrations complete; starting uvicorn (4 workers)."
# Tell the app's lifespan the schema is already at head, so the leader worker skips a redundant
# run_migrations() that would deadlock against the other 3 workers (ADR-037).
export SD_MIGRATIONS_DONE=1
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
