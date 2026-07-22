#!/usr/bin/env bash

set -euo pipefail

readonly VALIDATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-agent-fixes-prometheus-validation}"
readonly QUERY_URL="http://prometheus:9090/api/v1/query"
readonly METRICS_QUERY='{__name__=~"thenetwork_(producer_last_success_timestamp_seconds|job_queue_depth|oldest_pending_job_age_seconds|primary_intake_paused|control_actions_total|agent_usage_limit_exceeded_total|jobs_exhausted_total)"}'
readonly METRIC_FIXTURE_NAME="${COMPOSE_PROJECT_NAME}-metric-fixture-$$"
readonly METRIC_SOURCE_NAME="${COMPOSE_PROJECT_NAME}-metric-source-$$"

export COMPOSE_PROJECT_NAME
export POSTGRES_DB="${POSTGRES_DB:-network_db}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-validation-only}"
export POSTGRES_USER="${POSTGRES_USER:-network}"
# Keep validation traffic away from a caller's configured log backend. This
# script does not inject logs; the endpoint only satisfies Collector startup.
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4317"
export OTEL_EXPORTER_OTLP_HEADERS=""
export OTEL_EXPORTER_OTLP_INSECURE="true"
export ALERTMANAGER_OPERATOR_EMAIL="operator-validation-$$@example.invalid"
export ALERTMANAGER_SMTP_SMARTHOST="smtp.invalid:587"
export ALERTMANAGER_SMTP_FROM="monitoring-validation-$$@example.invalid"
export ALERTMANAGER_SMTP_USERNAME=""
export ALERTMANAGER_SMTP_PASSWORD="validation-only"
export ALERTMANAGER_SMTP_REQUIRE_TLS="false"

cd "$VALIDATION_ROOT"

for required_command in docker jq; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "missing required command: $required_command" >&2
        exit 1
    fi
done

cleanup() {
    local exit_code=$?
    if ((exit_code > 0)); then
        docker compose ps otel-collector prometheus alertmanager >&2 || true
        docker compose logs --no-color --tail=100 \
            otel-collector prometheus alertmanager >&2 || true
        docker logs --tail=100 "$METRIC_SOURCE_NAME" >&2 2>/dev/null || true
    fi
    docker rm --force \
        "$METRIC_SOURCE_NAME" \
        "$METRIC_FIXTURE_NAME" >/dev/null 2>&1 || true
    docker compose down --remove-orphans >/dev/null 2>&1 || true
    exit "$exit_code"
}
trap cleanup EXIT

docker compose config --quiet

docker run --rm \
    -e OTEL_EXPORTER_OTLP_ENDPOINT \
    -e OTEL_EXPORTER_OTLP_HEADERS \
    -e OTEL_EXPORTER_OTLP_INSECURE \
    -v "$VALIDATION_ROOT/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
    -v "$VALIDATION_ROOT/tests/fixtures/otel-worker-metrics-source.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
    --entrypoint /bin/promtool \
    -v "$VALIDATION_ROOT/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    -v "$VALIDATION_ROOT/prometheus-alert-rules.yml:/etc/prometheus/rules/thenetwork.yml:ro" \
    prom/prometheus:v3.5.5 \
    check config /etc/prometheus/prometheus.yml

docker run --rm \
    --entrypoint /bin/promtool \
    -v "$VALIDATION_ROOT:/workspace:ro" \
    -w /workspace \
    prom/prometheus:v3.5.5 \
    test rules tests/fixtures/prometheus-alert-rules.test.yml

docker compose run --rm --no-deps --entrypoint amtool alertmanager \
    check-config /etc/alertmanager/alertmanager.yml

docker compose up -d --force-recreate otel-collector alertmanager prometheus

docker run --detach --rm \
    --name "$METRIC_FIXTURE_NAME" \
    --network "${COMPOSE_PROJECT_NAME}_default" \
    --network-alias worker-metrics-fixture \
    -v "$VALIDATION_ROOT/tests/fixtures/worker-metrics:/www:ro" \
    busybox:1.37.0 \
    httpd -f -p 8080 -h /www >/dev/null

docker run --detach --rm \
    --name "$METRIC_SOURCE_NAME" \
    --network "${COMPOSE_PROJECT_NAME}_default" \
    -v "$VALIDATION_ROOT/tests/fixtures/otel-worker-metrics-source.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    --config=/etc/otelcol-contrib/config.yaml >/dev/null

metrics_ready=0
for _attempt in $(seq 1 30); do
    if metrics_json="$(
        docker exec "$METRIC_FIXTURE_NAME" \
            wget -qO- --post-data "query=$METRICS_QUERY" \
            "$QUERY_URL" 2>/dev/null
    )" && jq --exit-status '
        (.status == "success")
        and ((
            [.data.result[] | {
                key: .metric.__name__,
                value: (.value[1] | tonumber)
            }]
            | from_entries
        ) as $values
        | ($values | length == 7)
          and $values.thenetwork_producer_last_success_timestamp_seconds == 1784732400
          and $values.thenetwork_job_queue_depth == 3
          and $values.thenetwork_oldest_pending_job_age_seconds == 120
          and $values.thenetwork_primary_intake_paused == 1
          and $values.thenetwork_control_actions_total == 1
          and $values.thenetwork_agent_usage_limit_exceeded_total == 1
          and $values.thenetwork_jobs_exhausted_total == 1)
    ' <<<"$metrics_json" >/dev/null; then
        metrics_ready=1
        break
    fi
    sleep 2
done

if ((metrics_ready == 0)); then
    echo "worker operational metrics did not reach Prometheus" >&2
    exit 1
fi

jq '[.data.result[] | {metric: .metric.__name__, value: .value[1]}]' \
    <<<"$metrics_json"

alertmanager_ready=0
for _attempt in $(seq 1 30); do
    if docker exec "$METRIC_FIXTURE_NAME" \
        wget -qO- http://alertmanager:9093/-/ready >/dev/null 2>&1; then
        alertmanager_ready=1
        break
    fi
    sleep 1
done
if ((alertmanager_ready == 0)); then
    echo "Alertmanager did not become ready" >&2
    exit 1
fi

docker compose ps otel-collector prometheus alertmanager

echo "worker metrics and monitoring configuration validation passed"
