#!/usr/bin/env bash

set -euo pipefail

readonly VALIDATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_PROJECT_NAME="thenetwork-monitoring-validation-$$"
readonly QUERY_URL="http://prometheus:9090/api/v1/query"
readonly LOKI_QUERY_URL="http://loki:3100/loki/api/v1/query_range"
readonly METRICS_QUERY='{__name__=~"thenetwork_(producer_last_success_timestamp_seconds|job_queue_depth|oldest_pending_job_age_seconds|primary_intake_paused|control_actions_total|agent_usage_limit_exceeded_total|jobs_exhausted_total)"}'
readonly MESSAGE_COUNTER_QUERY='thenetwork_messages_processed_total{outcome="success"}'
readonly METRIC_FIXTURE_NAME="${COMPOSE_PROJECT_NAME}-metric-fixture-$$"
readonly METRIC_SOURCE_NAME="${COMPOSE_PROJECT_NAME}-metric-source-$$"
readonly LOG_FIXTURE_NAME="${COMPOSE_PROJECT_NAME}-log-fixture-$$"
readonly LOG_MARKER="loki-validation-$$"
readonly GRAFANA_AUTH_HEADER="Authorization: Basic $(printf 'admin:admin' | base64)"
readonly GRAFANA_DASHBOARD_UIDS=("thenetwork-worker-reliability" "thenetwork-llm-cost-usage" "thenetwork-growth-kpi" "thenetwork-system-resources")

export COMPOSE_PROJECT_NAME
export POSTGRES_DB="${POSTGRES_DB:-network_db}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-validation-only}"
export POSTGRES_USER="${POSTGRES_USER:-network}"
export ALERTMANAGER_OPERATOR_EMAIL="operator-validation-$$@example.invalid"
export ALERTMANAGER_SMTP_SMARTHOST="smtp.invalid:587"
export ALERTMANAGER_SMTP_FROM="monitoring-validation-$$@example.invalid"
export ALERTMANAGER_SMTP_USERNAME=""
export ALERTMANAGER_SMTP_PASSWORD="validation-only"
export ALERTMANAGER_SMTP_REQUIRE_TLS="false"
# Keep validation separate from a running deployment by asking Docker for
# ephemeral loopback ports. Production keeps the Compose defaults.
export OTEL_FLUENT_FORWARD_HOST_PORT=0
export LOKI_HOST_PORT=0
export PROMETHEUS_HOST_PORT=0
export ALERTMANAGER_HOST_PORT=0
export GRAFANA_HOST_PORT=0

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
        docker compose ps loki otel-collector prometheus alertmanager grafana >&2 || true
        docker compose logs --no-color --tail=100 \
            loki otel-collector prometheus alertmanager grafana >&2 || true
        docker logs --tail=100 "$METRIC_SOURCE_NAME" >&2 2>/dev/null || true
    fi
    docker rm --force \
        "$METRIC_SOURCE_NAME" \
        "$LOG_FIXTURE_NAME" \
        "$METRIC_FIXTURE_NAME" >/dev/null 2>&1 || true
    docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    exit "$exit_code"
}
trap cleanup EXIT

docker compose config --quiet

docker run --rm \
    -v "$VALIDATION_ROOT/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
    -v "$VALIDATION_ROOT/tests/fixtures/otel-worker-metrics-source.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
    -v "$VALIDATION_ROOT/loki-config.yaml:/etc/loki/config.yaml:ro" \
    grafana/loki:3.6.11 \
    -config.file=/etc/loki/config.yaml \
    -verify-config=true

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

docker compose up -d --force-recreate loki otel-collector alertmanager prometheus grafana

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

loki_ready=0
for _attempt in $(seq 1 30); do
    if docker exec "$METRIC_FIXTURE_NAME" \
        wget -qO- http://loki:3100/ready >/dev/null 2>&1; then
        loki_ready=1
        break
    fi
    sleep 1
done
if ((loki_ready == 0)); then
    echo "Loki did not become ready" >&2
    exit 1
fi

grafana_ready=0
for _attempt in $(seq 1 30); do
    if docker exec "$METRIC_FIXTURE_NAME" \
        wget -qO- http://grafana:3000/api/health >/dev/null 2>&1; then
        grafana_ready=1
        break
    fi
    sleep 2
done
if ((grafana_ready == 0)); then
    echo "Grafana did not become ready" >&2
    exit 1
fi

if ! grafana_datasources_json="$(docker exec "$METRIC_FIXTURE_NAME" \
    wget -qO- --header="$GRAFANA_AUTH_HEADER" \
    http://grafana:3000/api/datasources 2>/dev/null)"; then
    echo "could not list Grafana datasources" >&2
    exit 1
fi

for datasource_type in prometheus loki; do
    datasource_uid="$(jq -r --arg type "$datasource_type" \
        '[.[] | select(.type == $type)][0].uid // empty' \
        <<<"$grafana_datasources_json")"
    if [[ -z "$datasource_uid" ]]; then
        echo "Grafana has no provisioned $datasource_type datasource" >&2
        exit 1
    fi
    if ! datasource_health_json="$(docker exec "$METRIC_FIXTURE_NAME" \
        wget -qO- --header="$GRAFANA_AUTH_HEADER" \
        "http://grafana:3000/api/datasources/uid/${datasource_uid}/health" 2>/dev/null)" \
        || ! jq --exit-status '.status == "OK"' <<<"$datasource_health_json" >/dev/null; then
        echo "Grafana $datasource_type datasource is not healthy: ${datasource_health_json:-<no response>}" >&2
        exit 1
    fi
done

if ! grafana_dashboards_json="$(docker exec "$METRIC_FIXTURE_NAME" \
    wget -qO- --header="$GRAFANA_AUTH_HEADER" \
    'http://grafana:3000/api/search?type=dash-db' 2>/dev/null)"; then
    echo "could not list Grafana provisioned dashboards" >&2
    exit 1
fi

for expected_uid in "${GRAFANA_DASHBOARD_UIDS[@]}"; do
    if ! jq --exit-status --arg uid "$expected_uid" \
        'any(.[]; .uid == $uid)' <<<"$grafana_dashboards_json" >/dev/null; then
        echo "Grafana did not provision the expected dashboard: $expected_uid" >&2
        exit 1
    fi
done

if docker compose logs --no-color grafana 2>&1 \
    | grep -i 'provisioning' | grep -iq 'error'; then
    echo "Grafana logged a provisioning error" >&2
    exit 1
fi

jq '[.[] | {name, type, uid}]' <<<"$grafana_datasources_json"
jq '[.[] | {title, uid}]' <<<"$grafana_dashboards_json"

fluent_forward_address="$(docker compose port otel-collector 24224)"
if [[ -z "$fluent_forward_address" ]]; then
    echo "Collector Fluent Forward port was not published" >&2
    exit 1
fi

log_payload="$(jq --compact-output --null-input \
    --arg marker "$LOG_MARKER" \
    --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
        event: "worker.process_email.completed",
        logger: "thenetwork.audit",
        level: "info",
        timestamp: $timestamp,
        outcome: "success",
        validation_marker: $marker
    }')"

docker run --rm \
    --name "$LOG_FIXTURE_NAME" \
    --log-driver=fluentd \
    --log-opt "fluentd-address=$fluent_forward_address" \
    --log-opt fluentd-async=false \
    busybox:1.37.0 \
    printf '%s\n' "$log_payload"

encoded_loki_query="%7Bservice_name%3D%22thenetwork-worker%22%7D%20%7C%3D%20%22${LOG_MARKER}%22"
log_ready=0
counter_ready=0
for _attempt in $(seq 1 30); do
    if logs_json="$(docker exec "$METRIC_FIXTURE_NAME" \
        wget -qO- "${LOKI_QUERY_URL}?query=${encoded_loki_query}&limit=10" \
        2>/dev/null)" && jq --exit-status --arg marker "$LOG_MARKER" '
        (.status == "success")
        and ([.data.result[].values[] | .[1]] as $lines
        | ($lines | length) == 1
        and ($lines[0] | contains($marker)))
    ' <<<"$logs_json" >/dev/null; then
        log_ready=1
    fi

    if counter_json="$(docker exec "$METRIC_FIXTURE_NAME" \
        wget -qO- --post-data "query=$MESSAGE_COUNTER_QUERY" \
        "$QUERY_URL" 2>/dev/null)" && jq --exit-status '
        (.status == "success")
        and (.data.result | length == 1)
        and (.data.result[0].value[1] | tonumber == 1)
    ' <<<"$counter_json" >/dev/null; then
        counter_ready=1
    fi

    if ((log_ready == 1 && counter_ready == 1)); then
        break
    fi
    sleep 2
done

if ((log_ready == 0)); then
    echo "worker log did not reach Loki exactly once" >&2
    exit 1
fi
if ((counter_ready == 0)); then
    echo "worker log did not increment its Prometheus counter exactly once" >&2
    exit 1
fi

jq --arg marker "$LOG_MARKER" \
    '[.data.result[].values[] | select(.[1] | contains($marker)) | .[1]]' \
    <<<"$logs_json"
jq '[.data.result[] | {metric: .metric.__name__, labels: .metric, value: .value[1]}]' \
    <<<"$counter_json"

docker compose ps loki otel-collector prometheus alertmanager grafana

echo "worker logs, metrics, Grafana provisioning, and monitoring configuration validation passed"
