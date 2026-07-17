#!/bin/sh
# ENTRYPOINT: run DB migrations ONCE, single-process, BEFORE the command (uvicorn) starts. With multiple
# workers the leader's `command.upgrade` races the others opening the SQLite DB and deadlocks forever
# (ADR-037) — busy_timeout/WAL doesn't reliably break it. Migrating here, with nothing else running,
# removes the race. `set -e` halts the container on a migration failure (ADR-035: fail at deploy, not a
# black screen). We `exec "$@"` so BOTH the dev CMD (uvicorn --workers 4) and the appliance compose's
# `command:` override (uvicorn --workers 2) get migrated-first. SD_MIGRATIONS_DONE tells the app's
# lifespan to skip its now-redundant run_migrations (which would re-introduce the multi-worker deadlock).
set -e
echo "[entrypoint] running DB migrations (single process, pre-workers)..."
python -c "import db_migrate; db_migrate.run_migrations()"
echo "[entrypoint] migrations complete; exec: $*"
export SD_MIGRATIONS_DONE=1
exec "$@"
