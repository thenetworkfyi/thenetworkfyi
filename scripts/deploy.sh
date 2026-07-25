#!/usr/bin/env bash
# Deploy the latest commit on the VPS.
#
# Usage (run from the git checkout that also holds .env and docker-compose.yml
# on the server):
#   ./scripts/deploy.sh
#
# What it does:
#   1. Pull the latest commit on main.
#   2. Rebuild the worker image locally and force-recreate containers so the
#      new code and dependencies take effect. The DB container is untouched;
#      Alembic migrations run inside the worker entrypoint automatically on
#      every start.
#
# This is also what .github/workflows/ci.yml's deploy job runs over SSH after
# tests pass on a push to main.
#
# For a zero-downtime swap the worker drains in-flight jobs before stopping
# (stop_grace_period: 300s in docker-compose.yml). Durable Procrastinate job
# rows mean an ungraceful kill at worst retries a job (max_attempts=3).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Pulling latest code..."
git pull origin main

echo "==> Rebuilding and recreating containers..."
docker compose up -d --build --force-recreate

echo "==> Deploy complete. Worker status:"
docker compose ps worker
