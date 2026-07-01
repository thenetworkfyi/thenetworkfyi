#!/usr/bin/env bash
# Deploy the latest changes from main on the VPS.
#
# Usage (run from the repo root on the server):
#   ./scripts/deploy.sh
#
# What it does:
#   1. Pull the latest commits from origin.
#   2. Rebuild and restart only changed containers (worker image).
#      The DB container is untouched; Alembic migrations run inside the
#      worker entrypoint automatically on every start.
#
# For a zero-downtime swap the worker drains in-flight jobs before stopping
# (stop_grace_period: 300s in docker-compose.yml). Durable Procrastinate job
# rows mean an ungraceful kill at worst retries a job (max_attempts=3).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Pulling latest commits..."
git pull --ff-only

echo "==> Rebuilding and restarting changed services..."
docker compose pull --quiet
docker compose up -d --build

echo "==> Deploy complete. Worker status:"
docker compose ps worker
