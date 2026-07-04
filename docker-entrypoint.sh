#!/usr/bin/env bash
# Apply pending DB migrations, then exec the requested process.
#
# Running migrations here is safe because there is exactly one worker. `exec`
# replaces the shell with the worker so it receives SIGTERM directly and can
# shut down gracefully (drain in-flight jobs) on `docker compose up`/redeploy.
set -euo pipefail

echo "Applying database migrations (alembic upgrade head)..."
alembic upgrade head

echo "Applying Procrastinate schema (if not already present)..."
if ! procrastinate --app=thenetwork.worker.tasks.app healthchecks >/dev/null 2>&1; then
    procrastinate --app=thenetwork.worker.tasks.app schema --apply
else
    echo "Procrastinate schema already present, skipping."
fi

echo "Starting: $*"
exec "$@"
