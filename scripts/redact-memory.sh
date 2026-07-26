#!/usr/bin/env bash
# Redact PII from a Memory record in the database.
#
# Usage:
#   scripts/redact-memory.sh MEMORY_ID [--commit] [--string STRING] [--pattern PATTERN] [--replacement REPLACEMENT]
#   scripts/redact-memory.sh --help
#
# Without --commit, all changes are displayed as a dry run and rolled back.
# With --commit, changes to memory text, gist, and embedding are saved.
set -euo pipefail

cd "$(dirname "$0")/.."

exec uv run python -m thenetwork.scripts.redact_memory "$@"
