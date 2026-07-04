#!/usr/bin/env bash
# Deploy the latest published image on the VPS.
#
# Usage (run from the directory holding .env and docker-compose.yml on the
# server - no git checkout required):
#   ./scripts/deploy.sh
#
# What it does:
#   1. Pull the latest image tag (IMAGE in .env, published by
#      .github/workflows/publish.yml on every push to main).
#   2. Restart only changed containers. The DB container is untouched;
#      Alembic migrations run inside the worker entrypoint automatically on
#      every start.
#
# For a zero-downtime swap the worker drains in-flight jobs before stopping
# (stop_grace_period: 300s in docker-compose.yml). Durable Procrastinate job
# rows mean an ungraceful kill at worst retries a job (max_attempts=3).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Pulling latest images..."
docker compose pull --quiet

echo "==> Restarting changed services..."
docker compose up -d

echo "==> Deploy complete. Worker status:"
docker compose ps worker
