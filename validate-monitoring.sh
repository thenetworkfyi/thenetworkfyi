#!/usr/bin/env bash

set -euo pipefail

readonly VALIDATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-agent-fixes-prometheus-validation}"
readonly TARGETS_URL="http://127.0.0.1:9090/api/v1/targets"
readonly QUERY_URL="http://127.0.0.1:9090/api/v1/query"
readonly AUDIT_COUNTER="thenetwork_worker_audit_events_total"
readonly OTLP_SINK_NAME="${COMPOSE_PROJECT_NAME}-otel-sink-$$"
readonly -a PRODUCT_COUNTERS=(
    thenetwork_accounts_created_total
    thenetwork_messages_processed_total
    thenetwork_messages_rejected_total
    thenetwork_agent_runs_total
    thenetwork_agent_tool_calls_total
    thenetwork_introduction_transitions_total
    thenetwork_outbound_emails_total
    thenetwork_relay_messages_forwarded_total
)

export COMPOSE_PROJECT_NAME
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-validation-only}"
export POSTGRES_USER="${POSTGRES_USER:-network}"
# Never send synthetic validation records to a caller's real OTLP backend.
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTLP_SINK_NAME}:4317"
export OTEL_EXPORTER_OTLP_HEADERS=""
export OTEL_EXPORTER_OTLP_INSECURE="true"

cd "$VALIDATION_ROOT"

for required_command in docker curl jq; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "missing required command: $required_command" >&2
        exit 1
    fi
done

show_diagnostics() {
    local exit_code=$?
    if ((exit_code > 0)); then
        docker compose ps otel-collector prometheus >&2 || true
        docker compose logs --no-color --tail=100 otel-collector prometheus >&2 || true
        docker logs --tail=100 "$OTLP_SINK_NAME" >&2 || true
    fi
    docker rm --force "$OTLP_SINK_NAME" >/dev/null 2>&1 || true
    exit "$exit_code"
}
trap show_diagnostics EXIT

docker compose config --quiet

docker run --rm \
    -e OTEL_EXPORTER_OTLP_ENDPOINT \
    -e OTEL_EXPORTER_OTLP_HEADERS \
    -e OTEL_EXPORTER_OTLP_INSECURE \
    -v "$VALIDATION_ROOT/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
    --entrypoint /bin/promtool \
    -v "$VALIDATION_ROOT/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    prom/prometheus:v3.5.5 \
    check config /etc/prometheus/prometheus.yml

docker compose up -d --force-recreate otel-collector prometheus

docker run --detach --rm \
    --name "$OTLP_SINK_NAME" \
    --network "${COMPOSE_PROJECT_NAME}_default" \
    -v "$VALIDATION_ROOT/tests/fixtures/otel-validation-sink.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.118.0 \
    --config=/etc/otelcol-contrib/config.yaml >/dev/null

sink_ready=0
for _attempt in $(seq 1 30); do
    if docker logs "$OTLP_SINK_NAME" 2>&1 \
        | grep "Everything is ready" >/dev/null; then
        sink_ready=1
        break
    fi
    sleep 1
done

if ((sink_ready == 0)); then
    echo "OTLP validation sink did not become ready" >&2
    exit 1
fi

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
    '{"event":"manual_validation","logger":"thenetwork.audit","level":"info"}' \
    '{"action":"insert","event":"database.action","logger":"thenetwork.audit","outcome":"success","record_type":"person"}' \
    '{"event":"worker.process_email.completed","logger":"thenetwork.audit","outcome":"success"}' \
    '{"event":"worker.message_rejected","logger":"thenetwork.audit","reason":"rate_limit"}' \
    '{"event":"agent.run.completed","logger":"thenetwork.audit","outcome":"success"}' \
    '{"event":"agent.tool.completed","logger":"thenetwork.audit","tool_name":"remember","tool_outcome":"created"}' \
    '{"action":"consent","consent_state":"introduced","event":"introduction.consent_transition","logger":"thenetwork.audit","outcome":"success"}' \
    '{"event":"email.smtp_send.completed","logger":"thenetwork.audit","outcome":"success","template_id":"conversational"}' \
    '{"event":"worker.relay_forwarded","logger":"thenetwork.audit","outcome":"success"}' \
    '{"event":"agent.tool.completed","logger":"thenetwork.audit","sender_id_hash":"unsafe-cardinality-value","tool_name":"attacker_selected_tool","tool_outcome":"sent"}' \
    '{"event":"email.smtp_send.completed","logger":"thenetwork.audit","outcome":"success","template_id":"attacker_selected_template"}' \
    '{"action":"insert","event":"database.action","logger":"thenetwork.audit","outcome":"exists","record_type":"person"}' \
    '{"action":"insert","event":"database.action","logger":"thenetwork.audit","outcome":"rate_limited","record_type":"person"}' \
    '{"action":"insert","event":"database.action","logger":"thenetwork.audit","outcome":"rejected_already_registered","record_type":"person"}' \
    '{"action":"insert","event":"database.action","logger":"thenetwork.audit","outcome":"rejected_unauthenticated","record_type":"person"}'

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

for product_counter in "${PRODUCT_COUNTERS[@]}"; do
    product_ready=0
    for _attempt in $(seq 1 30); do
        if product_json="$(
            curl --fail --silent --show-error --get \
                --data-urlencode "query=$product_counter" \
                "$QUERY_URL"
        )" && jq --exit-status '
            .status == "success"
            and (.data.result | length > 0)
            and ([.data.result[].value[1] | tonumber] | max == 1)
        ' <<<"$product_json" >/dev/null; then
            product_ready=1
            break
        fi
        sleep 2
    done
    if ((product_ready == 0)); then
        echo "product counter did not become queryable exactly once: $product_counter" >&2
        exit 1
    fi
    jq --compact-output \
        --arg counter "$product_counter" \
        '{counter: $counter, series: .data.result}' \
        <<<"$product_json"
done

for unsafe_query in \
    'thenetwork_agent_tool_calls_total{tool_name="attacker_selected_tool"}' \
    'thenetwork_outbound_emails_total{template_id="attacker_selected_template"}'; do
    unsafe_json="$(
        curl --fail --silent --show-error --get \
            --data-urlencode "query=$unsafe_query" \
            "$QUERY_URL"
    )"
    jq --exit-status \
        '.status == "success" and (.data.result | length == 0)' \
        <<<"$unsafe_json" >/dev/null
done

otlp_ready=0
for _attempt in $(seq 1 30); do
    if docker logs "$OTLP_SINK_NAME" 2>&1 \
        | grep "manual_validation" >/dev/null; then
        otlp_ready=1
        break
    fi
    sleep 2
done

if ((otlp_ready == 0)); then
    echo "redacted audit records did not reach the OTLP validation sink" >&2
    exit 1
fi

jq '[.data.activeTargets[] | {job: .labels.job, health}]' <<<"$targets_json"
jq . <<<"$counter_json"
docker compose ps otel-collector prometheus

trap - EXIT
docker rm --force "$OTLP_SINK_NAME" >/dev/null
echo "monitoring validation passed"
