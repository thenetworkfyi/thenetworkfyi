#!/usr/bin/env bash
# Pull-based deploy. Checks the git remote for configuration changes and GHCR
# for a new worker image, and restarts the stack only when something actually
# changed.
#
# This replaces the earlier push-based SSH deploy. CI now publishes an image
# and stops there, so GitHub holds no credential for this host at all: a
# malicious commit has no deployment secret to steal. It also matches the rest
# of the service, which takes no inbound network access.
#
# Runs as the `deploy` user from thenetwork-deploy.timer, every minute. The
# steady-state cost is one registry manifest check; layers move only on a real
# release.
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/thenetwork}
BRANCH=${BRANCH:-main}
LOCK_FILE=${LOCK_FILE:-/tmp/thenetwork-deploy.lock}

# A slow pull must not overlap the next tick.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another deploy poll is still running; skipping this tick"
  exit 0
fi

cd "$REPO_DIR"

changed=""

git fetch --quiet origin "$BRANCH"
local_sha=$(git rev-parse HEAD)
remote_sha=$(git rev-parse "origin/$BRANCH")
if [ "$local_sha" != "$remote_sha" ]; then
  # Fast-forward only: a diverged checkout is an operator problem, not
  # something to resolve automatically at 3am.
  git merge --ff-only --quiet "origin/$BRANCH"
  echo "config: ${local_sha:0:12} -> ${remote_sha:0:12}"
  changed="config"
fi

# Resolved after the merge above, so a compose change to the image reference
# takes effect on the same tick that introduces it.
image_ref=$(docker compose config --images worker | head -n 1)

before=$(docker image inspect --format '{{.Id}}' "$image_ref" 2>/dev/null || true)
docker compose pull --quiet worker
after=$(docker image inspect --format '{{.Id}}' "$image_ref" 2>/dev/null || true)
if [ "$before" != "$after" ]; then
  echo "image: ${before:7:12} -> ${after:7:12} ($image_ref)"
  changed="${changed:+$changed, }image"
fi

if [ -z "$changed" ]; then
  exit 0
fi

echo "applying ($changed)"
docker compose up -d
docker compose ps worker
