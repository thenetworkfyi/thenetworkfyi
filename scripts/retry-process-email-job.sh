#!/usr/bin/env bash

set -euo pipefail

if (( $# != 1 )); then
    echo "Usage: $0 JOB_ID" >&2
    exit 64
fi

job_id=$1
if [[ ! $job_id =~ ^[1-9][0-9]*$ ]]; then
    echo "JOB_ID must be a positive integer" >&2
    exit 64
fi

docker compose exec -T worker \
    procrastinate -a thenetwork.worker.tasks.app shell \
    retry "$job_id"
