#!/usr/bin/env bash

set -euo pipefail

readonly VALIDATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-agent-fixes-prometheus-validation}"
readonly TARGETS_URL="http://127.0.0.1:9090/api/v1/targets"
readonly QUERY_URL="http://127.0.0.1:9090/api/v1/query"
readonly AUDIT_COUNTER="thenetwork_worker_audit_events_total"

export COMPOSE_PROJECT_NAME
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-validation-only}"
export POSTGRES_USER="${POSTGRES_USER:-network}"
# Never send the synthetic validation record to a caller's real OTLP backend.
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4317"
export OTEL_EXPORTER_OTLP_HEADERS=""

cd "$VALIDATION_ROOT"

for required_command in docker curl jq; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "missing required command: $required_command" >&2
        exit 1
    fi
done

show_diagnostics() {
    local exit_code=$?
    if ((exit_code != 0)); then
        docker compose ps otel-collector prometheus >&2 || true
        docker compose logs --no-color --tail=100 otel-collector prometheus >&2 || true
    fi
    exit "$exit_code"
}
trap show_diagnostics EXIT

docker compose config --quiet

docker run --rm \
    -e OTEL_EXPORTER_OTLP_ENDPOINT \
    -e OTEL_EXPORTER_OTLP_HEADERS \
    -v "$VALIDATION_ROOT/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
    --entrypoint /bin/promtool \
    -v "$VALIDATION_ROOT/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    prom/prometheus:v3.5.5 \
    check config /etc/prometheus/prometheus.yml

docker compose up -d otel-collector prometheus

targets_ready=0
for _attempt in $(seq 1 30); do
    if targets_json="$(curl --fail --silent --show-error "$TARGETS_URL")" && \
        jq --exit-status '
            [.data.activeTargets[]
             | select(
                 .labels.job == "otel-collector-internal"
                 or .labels.job == "thenetwork-audit-activity"
               )
             | select(.health == "up")]
            | length == 2
        ' <<<"$targets_json" >/dev/null; then
        targets_ready=1
        break
    fi
    sleep 2
done

if ((targets_ready != 1)); then
    echo "Prometheus scrape targets did not become healthy" >&2
    exit 1
fi

docker run --rm \
    --log-driver=fluentd \
    --log-opt fluentd-address=127.0.0.1:24224 \
    --log-opt fluentd-async=false \
    busybox:1.37.0 \
    printf '%s\n' \
    '{"event":"manual_validation","logger":"thenetwork.audit","level":"info"}'

counter_ready=0
for _attempt in $(seq 1 30); do
    if counter_json="$(
        curl --fail --silent --show-error --get \
            --data-urlencode "query=$AUDIT_COUNTER" \
            "$QUERY_URL"
    )" && jq --exit-status '
        .status == "success"
        and (.data.result | length > 0)
        and ([.data.result[].value[1] | tonumber] | max > 0)
    ' <<<"$counter_json" >/dev/null; then
        counter_ready=1
        break
    fi
    sleep 2
done

if ((counter_ready != 1)); then
    echo "audit activity counter did not become queryable" >&2
    exit 1
fi

jq '[.data.activeTargets[] | {job: .labels.job, health}]' <<<"$targets_json"
jq . <<<"$counter_json"
docker compose ps otel-collector prometheus

trap - EXIT
echo "monitoring validation passed"
