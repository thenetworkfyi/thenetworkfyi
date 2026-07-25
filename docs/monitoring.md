# Monitoring

The Compose stack retains worker logs in Loki and derives Prometheus counters
from the same records received by the OpenTelemetry Collector. Loki,
Prometheus, and Alertmanager persist their data in separate named volumes and
bind their operator endpoints only to loopback. The worker does not expose an
inbound metrics port.

## Log storage and querying

The Collector sends every worker log to `loki:3100` over Loki's native OTLP
endpoint while also sending the same record to the existing `count/audit`
connector. The transform extracts JSON fields as structured metadata without
clearing the original body, so the complete emitted JSON line remains readable
in query results. The static resource value `service.name=thenetwork-worker`
becomes the indexed Loki label `service_name`; per-event values remain
structured metadata rather than indexed labels.

Loki stores its TSDB index and chunks in the `loki-data` volume. The Compactor
enforces a 30-day (`720h`) retention period. Loki does not enforce a disk-size
limit, so host free space still needs normal operational monitoring. No Loki
credentials or other new environment variables are required for this internal,
single-tenant deployment. The HTTP API binds to `127.0.0.1:3100`; it is not
publicly exposed. Grafana runs bound to `127.0.0.1:3000` with automated Prometheus and Loki data source provisioning via `grafana/provisioning/datasources/datasources.yaml`.
Dashboards are provisioned the same way: `grafana/provisioning/dashboards/dashboards.yaml`
registers a file-based provider that loads every `*.json` dashboard dropped into
`grafana/dashboards/` into "The Network" folder, with a 10-second
`updateIntervalSeconds` so a new or edited dashboard file is picked up without
restarting the `grafana` container.

`grafana/dashboards/worker-reliability.json` is the Worker & Reliability overview:
producer poll staleness, Procrastinate job queue depth and oldest pending job
age, messages processed/rejected, agent run and tool-call outcomes, control
actions (pause/resume/ban/unban), primary-intake pause state, outbound email
outcomes, and relay-forwarded message volume. Every panel queries only the
Prometheus counter/gauge names and label dimensions documented in the
[counter catalog](#counter-catalog) below; none use `trace_id`, `run_id`, a
sender pseudonym, or any other opaque entity id as a label.

`grafana/dashboards/llm-cost-usage.json` is the LLM Cost & Token Usage
dashboard: logical request volume (`thenetwork_llm_requests_total` by
workload/provider/model/outcome/cost_status), token counts
(`thenetwork_llm_tokens_total` by workload/provider/model/token_type), rolling
24h estimated cost (`thenetwork_llm_estimated_cost_usd_total`, both broken down
and summed across all series), and p95 request latency
(`thenetwork_llm_request_duration_seconds`). The estimated-cost panels draw a
fixed $50/day reference line matching the `DailyLlmCostDrift` alert condition
in `prometheus-alert-rules.yml` (`sum(increase(thenetwork_llm_estimated_cost_usd_total[24h])) > 50`) -
update both the dashboard threshold and the alert expression together if that
budget changes. Every panel here also stays within the documented label set
(`workload`, `provider`, `model`, `outcome`, `cost_status`, `token_type`); no
`trace_id` or other unbounded/identifying label is used.

Query the last hour directly through the Loki API:

```bash
curl --get 'http://127.0.0.1:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="thenetwork-worker"}' \
  --data-urlencode 'since=1h' \
  --data-urlencode 'limit=100' | jq
```

Or use a locally installed `logcli` binary:

```bash
logcli --addr=http://127.0.0.1:3100 \
  query '{service_name="thenetwork-worker"}' --since=1h --limit=100
```

Filter the retained JSON lines with LogQL, for example:

```logql
{service_name="thenetwork-worker"} | json | level="error"
```

## LLM request and email lifecycle accounting

Every logical Pydantic AI request emits a content-free
`llm.request.completed` record. This is above the provider SDK's transport
retry layer, so `model.http_attempt.completed` remains the place to diagnose
individual HTTP attempts while `llm.request.completed` counts a successfully
returned or terminally failed logical request once. LlamaIndex embedding
batches emit the same accounting record without changing its batching or retry
behavior.

The request record includes the existing opaque `trace_id`, one of the bounded
workloads `email_agent`, `memory_sanitizer`, `abuse_judge`, or `embedding`, the
provider and model, request outcome and duration, input/output/cache token
counts when available, and `estimated_cost_usd`. `cost_status="unavailable"`
and a null cost distinguish missing price metadata or a failed request from a
genuine zero-cost request. Prompts, completions, embedding text, tool arguments,
and exception messages are never included in this record.

Find a recent intake record, copy its `trace_id`, and then inspect the complete
application timeline for that email:

```bash
logcli --addr=http://127.0.0.1:3100 --quiet --output=raw \
  query --since=24h --limit=100 \
  '{service_name="thenetwork-worker"} | json | event="intake.message_received"'

TRACE_ID='copy-the-opaque-trace-id'
logcli --addr=http://127.0.0.1:3100 --quiet --output=raw --forward \
  query --since=24h --limit=500 \
  "{service_name=\"thenetwork-worker\"} | json | trace_id=\"$TRACE_ID\""
```

Each `process_email` attempt finishes with one `email.lifecycle.completed`
record. It rolls up request counts, tokens, estimated cost, model time, and
agent time for that attempt. For ordinary IMAP intake it also reports queue
time and total observed time from `intake.message_received` to task completion.
That clock starts when the one-minute poll first sees the message, not at the
mail provider's original delivery timestamp. Retries reuse the same `trace_id`
and produce another attempt rollup; sum the trace's `llm.request.completed`
records when investigating total cost across all attempts.

Recorded dollar values are point-in-time estimates from the model, provider,
request timestamp, and bundled pricing metadata. They are useful for
attribution and trends but do not replace the provider's invoice, which remains
authoritative.

## Counter catalog

All names below are the Prometheus names returned by the query API. The
Collector configuration uses dotted OpenTelemetry names; the Prometheus
exporter normalizes dots to underscores and adds the `_total` suffix.

| Prometheus counter | Source audit record | Labels | Meaning |
| --- | --- | --- | --- |
| `thenetwork_worker_audit_events_total` | Any record with `logger=thenetwork.audit` | None from logs | Redacted audit records observed by the Collector. |
| `thenetwork_accounts_created_total` | `database.action` with `action=insert`, `record_type=person`, and `outcome=success` | None from logs | Successfully committed person accounts. Existing, unauthenticated, rejected, and rate-limited registration attempts are excluded. |
| `thenetwork_messages_processed_total` | `worker.process_email.completed` | `outcome` | Completed processing attempts. `success` includes early, server-owned handling such as rejection or relay forwarding; `error` is a failed attempt that may be retried. |
| `thenetwork_messages_rejected_total` | `worker.message_rejected` | `reason` | Messages stopped before agent processing, grouped by the closed rejection-reason category. |
| `thenetwork_agent_runs_total` | `agent.run.completed` | `outcome` | Completed agent attempts, split into `success` and `error`. |
| `thenetwork_agent_tool_calls_total` | `agent.tool.completed` | `tool_name`, `tool_outcome` | Completed tool calls. Exact server-side retry replays use `tool_outcome="replayed"`, distinct from the original result. |
| `thenetwork_introduction_transitions_total` | Successful `introduction.consent_transition` | `action`, `consent_state` | Successful consent workflow events and their resulting bounded state. Rejected attempts are excluded. |
| `thenetwork_outbound_emails_total` | `email.smtp_send.completed` | `outcome`, `template_id` | Completed SMTP attempts. Each recipient delivery has its own span, including the two deliveries for a completed introduction. |
| `thenetwork_relay_messages_forwarded_total` | Successful `worker.relay_forwarded` | None from logs | Participant relay messages handed to the outbound path successfully. |

The worker also sends state, operational, and model-accounting metrics outbound
to the Collector over OTLP/HTTP. The worker still opens no listener:

| Prometheus gauge | Labels | Meaning |
| --- | --- | --- |
| `thenetwork_producer_last_success_timestamp_seconds` | None | Unix timestamp recorded only after a complete successful IMAP poll, including an empty poll. It remains zero after process start until the first success and does not advance after a failed or incomplete cycle. |
| `thenetwork_job_queue_depth` | None | Number of Procrastinate `todo` jobs that are immediately runnable or whose `scheduled_at` is due. Future-scheduled periodic or retry work and jobs already running are excluded. Due work waiting behind a queue or task lock remains backlog. |
| `thenetwork_oldest_pending_job_age_seconds` | None | Oldest runnable age in seconds. Age starts at `scheduled_at` for due scheduled work and at the initial `deferred` event for immediately runnable work. An empty queue reports zero. |
| `thenetwork_primary_intake_paused` | `reason` | `1` when the durable `PrimaryIntakeState` singleton is paused; `0` when active or absent. `reason` is one of `none`, `admin`, `new_sender_burst`, `coordinated_abuse`, or fail-closed `unknown`, allowing rules to distinguish automated stops from administrator-requested pauses. |
| `thenetwork_people_total` | None | Live count of registered `people` rows, sampled fresh each collection interval - not a cumulative "accounts created" counter. |
| `thenetwork_activated_people_total` | None | Count of distinct people referenced by at least one memory (`unnest(memories.refs)`), a network-effects "hard side liquidity" signal. |
| `thenetwork_active_senders_weekly` | None | Distinct people referenced by a memory created in the trailing 7-day window. This is a proxy for active senders, not a literal authenticated-sender count: no table tracks per-person send timestamps, and this observability addition does not add one. |
| `thenetwork_network_density` | None | Average graph degree (`2 * edges / nodes`) sampled from the existing hourly `scan_for_opportunities` graph projection - it does not trigger a second graph build. Zero when the graph has no nodes. |

| Prometheus counter | Labels | Meaning |
| --- | --- | --- |
| `thenetwork_control_actions_total` | `action`, `actor`, `reason` | Committed state-changing pause, resume, ban, and unban operations. No-op requests do not increment it. `actor` is `admin` or `system`; reasons are closed server-owned categories. |
| `thenetwork_agent_usage_limit_exceeded_total` | None | Agent runs interrupted by the configured Pydantic AI usage limit. Each interrupted run increments once. |
| `thenetwork_jobs_exhausted_total` | None | `process_email` jobs that failed their final configured Procrastinate attempt. Intermediate retry failures do not increment it. |
| `thenetwork_llm_requests_total` | `workload`, `provider`, `model`, `outcome`, `cost_status` | Logical model and embedding requests. `cost_status` distinguishes priced estimates from unavailable pricing. |
| `thenetwork_llm_tokens_total` | `workload`, `provider`, `model`, `outcome`, `token_type` | Input, output, cache-read, and cache-write tokens reported or locally counted for embedding batches. |
| `thenetwork_llm_estimated_cost_usd_total` | `workload`, `provider`, `model` | Point-in-time estimated USD cost. Requests without price metadata do not add a synthetic zero. |

| Prometheus histogram | Labels | Meaning |
| --- | --- | --- |
| `thenetwork_llm_request_duration_seconds` | `workload`, `provider`, `model`, `outcome` | Logical request latency, including the provider SDK's internal retries. |
| `thenetwork_email_lifecycle_duration_seconds` | `outcome` | Poll-observed intake to task completion for jobs carrying an intake timestamp. |
| `thenetwork_email_queue_duration_seconds` | `outcome` | Poll-observed intake to `process_email` start. |
| `thenetwork_agent_run_duration_seconds` | `outcome` | Time spent in genuine agent runs. Attempts handled or rejected before the agent do not add zero-duration samples. |

Database sampling and OTLP export are best effort. A failed state read emits no
state-gauge observations for that collection interval, while producer polling,
queue processing, and intake behavior continue unchanged. Export happens on a
background SDK thread with a short timeout. The gauges have no application
labels and the database query selects only timestamps and aggregate state,
never job arguments or entity identifiers. Direct operational counters are
in-memory cumulative instruments and reset when the worker process restarts;
use `increase` or `rate` across restarts.

These are event counters, not current database state. For example,
`thenetwork_accounts_created_total` says how many account-creation events the
current Prometheus history has observed; it does not say how many person rows
currently exist. Similarly, the introduction counter is not the number of
currently active proposals.

### `model` label discontinuity for OpenRouter ids

Before this fix, the `model` label's charset rejected the `/` in OpenRouter
`vendor/model` ids (e.g. `google/gemma-4-31b-it`), so every OpenRouter request
was recorded under `model="unknown"` in both `thenetwork_llm_requests_total`
et al. and the audit `model_name` field. After the fix, new OpenRouter series
appear under their real `vendor/model` label instead. Any dashboard panel or
alert that filters or breaks down by `model="unknown"` for an OpenRouter
deployment will see that series stop accumulating and a new one appear under
the actual id - this is expected, not a data loss, but a saved dashboard query
pinned to `model="unknown"` will silently go quiet rather than error.

## Label policy

The Collector projects only the closed categories listed in the table. Its
conditions independently allow-list every projected value before creating a
series, even though the application audit layer already redacts and validates
those fields. Unknown, missing, non-string, or attacker-selected values do not
create product metric series.

Never project `trace_id`, `run_id`, `sender_id_hash`, an address, a name,
content, exception text, or any person, event, or other opaque entity ID into a
metric label. These values are identifying and/or unbounded. Prometheus also
adds its bounded scrape-target `job` and `instance` labels.

When adding a new server-owned email template or audit category, update both
the audit allow-list and the Collector condition before expecting a labelled
counter to accept it. This fail-closed behavior prevents a new free-form value
from silently increasing metric cardinality.

## Querying counters

Prometheus counters restart when the Collector restarts and may also reset when
the Compose stack or its persistent volume is replaced. Use `rate` or
`increase`, which account for counter resets, instead of subtracting raw
samples.

Examples:

```promql
sum(rate(thenetwork_messages_processed_total[5m])) by (outcome)

sum(increase(thenetwork_messages_rejected_total[1h])) by (reason)

sum(increase(thenetwork_agent_tool_calls_total{tool_outcome="replayed"}[24h])) by (tool_name)

sum(rate(thenetwork_outbound_emails_total{outcome="error"}[15m])) by (template_id)

sum(increase(thenetwork_control_actions_total{actor="system"}[1h])) by (action, reason)

increase(thenetwork_agent_usage_limit_exceeded_total[1h])

increase(thenetwork_jobs_exhausted_total[1h])

sum(increase(thenetwork_llm_estimated_cost_usd_total[24h])) by (workload, provider, model)

sum(increase(thenetwork_llm_tokens_total[24h])) by (workload, token_type)

sum(increase(thenetwork_llm_requests_total{outcome="error"}[1h])) by (workload, provider, model)

histogram_quantile(
  0.95,
  sum(rate(thenetwork_llm_request_duration_seconds_bucket[15m])) by (le, workload, provider, model)
)

histogram_quantile(
  0.95,
  sum(rate(thenetwork_email_lifecycle_duration_seconds_bucket[1h])) by (le)
)
```

Low-volume deployments generally get more useful whole-number results from
`increase` over an hour or a day. `rate` is useful for alerting on sustained
activity or failures.

## Alertmanager configuration

Set the following deployment values before starting Alertmanager:

```dotenv
ALERTMANAGER_OPERATOR_EMAIL=
ALERTMANAGER_SMTP_SMARTHOST=smtp.example.net:587
ALERTMANAGER_SMTP_FROM=
ALERTMANAGER_SMTP_USERNAME=
ALERTMANAGER_SMTP_PASSWORD=
ALERTMANAGER_SMTP_REQUIRE_TLS=true
```

`ALERTMANAGER_OPERATOR_EMAIL` must be a dedicated, operator-controlled mailbox.
It must not equal `IMAP_ACCOUNT`, `EMAIL_FROM`, a relay address, or any other
application intake address. No production recipient is stored in this
repository. Store `ALERTMANAGER_SMTP_PASSWORD` in the same gitignored `.env`
used for the application's other deployment credentials. Leave
`ALERTMANAGER_SMTP_USERNAME` empty only for an SMTP relay that does not
authenticate. Keep TLS required in production.

Alertmanager groups by alert name, severity, and category. Event alerts have no
group wait and a one-minute group interval so a short burst is sent once rather
than once per Prometheus evaluation. Critical alerts repeat hourly, warnings
repeat every 12 hours, and one-shot event alerts repeat after 24 hours only if
they somehow remain firing. Every receiver sends a resolved notification.
`CollectorUnavailable` inhibits worker-scoped alerts because those alerts
cannot be trusted while their metric source is unavailable. `LokiUnavailable`
inhibits `CollectorPipelineDegraded` so a single Loki outage produces one
notification rather than an availability alert plus its downstream symptom.

Event rules detect both an increase in an existing counter series and a new
nonzero series with no sample two minutes earlier. The latter case is required
for the first event after process start, when Prometheus may first observe the
counter at one rather than observing an initial zero.

Notification subjects and bodies render only alert status, the static alert
name, warning/critical severity, bounded control action/reason labels, static
summary text, and a static runbook reference. Never add metric values, raw error
text, trace or run IDs, sender pseudonyms, addresses, names, message content,
opaque entity IDs, or job arguments to rules or notification templates.

## Alert rules and runbooks

| Alert | Severity | Condition | `for` |
| --- | --- | --- | --- |
| `ProducerPollingStale` | critical | Last successful complete IMAP poll is over 15 minutes old; zero startup state is ignored. | 10m |
| `WorkerBacklogSustained` | warning | More than 25 runnable jobs and oldest runnable age over five minutes. | 10m |
| `AutomatedPrimaryIntakePaused` | critical | Current intake state is paused for `new_sender_burst` or `coordinated_abuse`; administrator pauses are excluded. | 1m |
| `OutboundEmailFailuresRepeated` | critical | At least three failed SMTP attempts in 15 minutes. | 5m |
| `AgentFailureRateElevated` | warning | More than 25 percent failures and at least five failures in 15 minutes. | 10m |
| `MessageRejectionsSpiking` | warning | At least 20 rejected messages in 10 minutes. | 5m |
| `CollectorUnavailable` | critical | Either Collector scrape target is down. | 2m |
| `LokiUnavailable` | critical | The Loki scrape target is down. | 2m |
| `CollectorPipelineDegraded` | critical | The Collector failed to send logs to Loki in 10 minutes or the Loki exporter queue exceeded 80 percent. | 5m |
| `SystemControlActionObserved` | critical | A new system pause or future automatic ban counter increment. Administrator controls are excluded. | immediate |
| `AgentUsageLimitExceeded` | warning | A new agent usage-limit interruption. | immediate |
| `ProcessEmailJobExhausted` | critical | A final `process_email` attempt exhausted retries. Intermediate failures never increment this counter. | immediate |
| `DailyLlmCostDrift` | warning | Rolling 24h estimated cost exceeds a fixed $50 default. | 15m |

### ProducerPollingStale

Check the producer container logs and IMAP connectivity, then run one manual
poll cycle. Confirm the success timestamp advances before silencing or closing
the alert.

### WorkerBacklogSustained

Inspect worker health and queue locks before increasing concurrency. Confirm
both queue depth and oldest age fall; a depth drop alone can hide one stuck job.

### AutomatedPrimaryIntakePaused

Inspect the fixed abuse-control reason and unread primary inbox traffic. Clear
unwanted mail, then use the signed `resume-intake` administrator command. A
signed administrator-requested pause is intentional and does not fire this
alert.

### OutboundEmailFailuresRepeated

Check SMTP reachability, provider status, authentication, and sending limits.
Do not copy provider error bodies into alert annotations or operator email.

### AgentFailureRateElevated

Check provider status and the redacted worker logs. The traffic floor prevents
one or two failures on a quiet system from paging.

### MessageRejectionsSpiking

Compare the bounded rejection-reason counters to expected abuse or deployment
changes. Investigate using restricted logs; never add sender-level labels.

### CollectorUnavailable

Check the Collector container and its ports `8888` and `8889` on the internal
Compose network. Worker alerts are inhibited until this source recovers.

### LokiUnavailable

Check `docker compose ps loki` and `docker compose logs loki`, then request
`http://127.0.0.1:3100/ready`. Confirm the `loki-data` volume is mounted and the
host has free disk space before restarting. `CollectorPipelineDegraded` is
inhibited while this root-cause alert is firing.

### CollectorPipelineDegraded

Check the Collector's `otlphttp/loki` exporter failures and queue metrics, then
check Loki readiness and disk space. Restore delivery before restarting the
Collector so queued records are not discarded unnecessarily.

### SystemControlActionObserved

Confirm the bounded `action` and `reason`, inspect the corresponding current
state, and follow the automated-intake runbook when intake was paused. Signed
administrator controls are deliberately excluded to avoid duplicate emails.

### AgentUsageLimitExceeded

Inspect the redacted run lifecycle and configured usage ceiling. The alert is
the sole operational notification for the new counter event and carries no run
identifier. The application does not send a second admin email.

### ProcessEmailJobExhausted

Inspect restricted worker logs for the terminal job failure and resolve the
underlying dependency. Only the final exhausted attempt alerts, and the
application does not send a second admin email.

### DailyLlmCostDrift

`DAILY_AGENT_TOKEN_CAP` (see @docs/development.md) bounds spend in tokens, not
dollars, and per-million-token pricing varies by 25-40x across models - the
default 15M-token cap costs roughly $5.40/day worst case on a cheap model such
as `openrouter:google/gemma-4-31b-it`, but 25-40x that (~$135-216/day) on
`anthropic:claude-sonnet-5`, `.env.example`'s documented `AGENT_MODEL`. This
alert is a dollar-denominated safety net independent of the token cap: it
fires on `thenetwork_llm_estimated_cost_usd_total` (see the counter catalog
above) directly, so a model swap or provider price change that keeps the same
token cap but sharply raises the dollar burn is still caught. The $50/day
default sits comfortably above ordinary cheap-model spend but below the
expensive-model worst case, so tune it in `prometheus-alert-rules.yml` to a
level appropriate for the currently configured `AGENT_MODEL` and expected
traffic. On firing, check the per-`workload`/`provider`/`model` cost
breakdown, confirm `AGENT_MODEL`/`SMALL_AGENT_MODEL` and `DAILY_AGENT_TOKEN_CAP`
are still consistent with the assumptions in `.env.example`, and re-derive the
cap if the model changed.

## Operator workflow

Open the local UIs directly on the host or through an authenticated SSH tunnel:

```bash
ssh -L 3100:127.0.0.1:3100 -L 9090:127.0.0.1:9090 \
  -L 9093:127.0.0.1:9093 operator@server
```

Use `http://127.0.0.1:9090/alerts` for rule state and
`http://127.0.0.1:9093` for groups, silences, and notification status. Create a
time-bounded silence from the UI or from inside the container, for example:

```bash
docker compose exec alertmanager amtool silence add \
  'alertname="WorkerBacklogSustained"' --duration=30m \
  --comment='planned queue maintenance'
```

Verify the complete configuration before deployment:

```bash
docker compose config --quiet
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$PWD/prometheus-alert-rules.yml:/etc/prometheus/rules/thenetwork.yml:ro" \
  prom/prometheus:v3.5.5 check config /etc/prometheus/prometheus.yml
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD:/workspace:ro" -w /workspace prom/prometheus:v3.5.5 \
  test rules tests/fixtures/prometheus-alert-rules.test.yml
docker compose run --rm --no-deps --entrypoint amtool alertmanager \
  check-config /etc/alertmanager/alertmanager.yml
```

After rollout, verify all three Prometheus targets (`otel-collector-internal`,
`thenetwork-audit-activity`, and `loki`) are up and the Alertmanager status page
reports the expected receiver. Verify the dedicated operator mailbox with
a separately planned notification test during deployment. Silence only a
specific alert for a bounded maintenance window; do not silence
`CollectorUnavailable` globally.

This local stack cannot detect total VPS failure, loss of the host network, or
failure of Prometheus and Alertmanager together. Production availability still
requires an independent external probe or dead-man monitor.

## Validation

Run the repository-root validation script on a host with Docker and `jq`:

```bash
./validate-monitoring.sh
```

It validates the Compose, Loki, Collector, worker-metric fixture, Prometheus
rules, and Alertmanager configuration. It starts an isolated validation project
on ephemeral loopback ports, sends seven fixed worker metrics over OTLP/HTTP,
and checks them with Prometheus. It also injects one fixed worker JSON record
through Docker's Fluent Forward logging driver, checks that Loki returns the
original line exactly once, and checks that the derived Prometheus counter is
exactly one. Promtool covers pending, firing, and resolved alert behavior. The
script also starts Grafana in the same isolated project and, over its HTTP API,
confirms the provisioned Prometheus and Loki datasources both report a healthy
`/api/datasources/uid/<uid>/health` status and that both
`grafana/dashboards/worker-reliability.json` and `grafana/dashboards/llm-cost-usage.json`
were loaded by the file-based dashboard provider, with no provisioning error in
the Grafana container logs. It still does not send email or start the worker.
Its containers, network, and validation-only named volumes are removed before
it returns.

For rollout recovery, keep `loki-data` when recreating services. A normal
`docker compose up -d` reuses it. Do not use `docker compose down --volumes` in
production unless deleting retained logs is intentional. If Loki cannot start,
validate `loki-config.yaml`, inspect the volume mount and free space, then start
Loki before the Collector. Existing Prometheus metrics remain separate in
`prometheus-data`.
