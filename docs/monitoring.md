# Monitoring

The Compose stack derives Prometheus counters from the same redacted audit log
records that the OpenTelemetry Collector forwards to the configured OTLP
destination. The worker does not expose an inbound metrics port. Prometheus
scrapes the Collector over the internal Compose network and binds its UI only
to `127.0.0.1:9090`. Prometheus sends firing and resolved alerts to the pinned
Alertmanager service at `alertmanager:9093`; Alertmanager persists its state and
binds its operator UI only to `127.0.0.1:9093`.

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

The worker also sends four state gauges and three operational counters outbound
to the Collector over OTLP/HTTP. The worker still opens no listener:

| Prometheus gauge | Labels | Meaning |
| --- | --- | --- |
| `thenetwork_producer_last_success_timestamp_seconds` | None | Unix timestamp recorded only after a complete successful IMAP poll, including an empty poll. It remains zero after process start until the first success and does not advance after a failed or incomplete cycle. |
| `thenetwork_job_queue_depth` | None | Number of Procrastinate `todo` jobs that are immediately runnable or whose `scheduled_at` is due. Future-scheduled periodic or retry work and jobs already running are excluded. Due work waiting behind a queue or task lock remains backlog. |
| `thenetwork_oldest_pending_job_age_seconds` | None | Oldest runnable age in seconds. Age starts at `scheduled_at` for due scheduled work and at the initial `deferred` event for immediately runnable work. An empty queue reports zero. |
| `thenetwork_primary_intake_paused` | `reason` | `1` when the durable `PrimaryIntakeState` singleton is paused; `0` when active or absent. `reason` is one of `none`, `admin`, `new_sender_burst`, `coordinated_abuse`, or fail-closed `unknown`, allowing rules to distinguish automated stops from administrator-requested pauses. |

| Prometheus counter | Labels | Meaning |
| --- | --- | --- |
| `thenetwork_control_actions_total` | `action`, `actor`, `reason` | Committed state-changing pause, resume, ban, and unban operations. No-op requests do not increment it. `actor` is `admin` or `system`; reasons are closed server-owned categories. |
| `thenetwork_agent_usage_limit_exceeded_total` | None | Agent runs interrupted by the configured Pydantic AI usage limit. Each interrupted run increments once. |
| `thenetwork_jobs_exhausted_total` | None | `process_email` jobs that failed their final configured Procrastinate attempt. Intermediate retry failures do not increment it. |

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
ALERTMANAGER_SMTP_PASSWORD_FILE=./secrets/alertmanager-smtp-password
ALERTMANAGER_SMTP_REQUIRE_TLS=true
```

`ALERTMANAGER_OPERATOR_EMAIL` must be a dedicated, operator-controlled mailbox.
It must not equal `IMAP_ACCOUNT`, `EMAIL_FROM`, a relay address, or any other
application intake address. No production recipient is stored in this
repository. Create the password file outside version control with mode `0600`;
Compose mounts it read-only at `/run/secrets/alertmanager-smtp-password`, and
the rendered Alertmanager configuration contains only that path, never the
credential. Leave `ALERTMANAGER_SMTP_USERNAME` empty only for an SMTP relay that
does not authenticate. Keep TLS required in production.

Alertmanager groups by alert name, severity, and category. Event alerts have no
group wait and a one-minute group interval so a short burst is sent once rather
than once per Prometheus evaluation. Critical alerts repeat hourly, warnings
repeat every 12 hours, and one-shot event alerts repeat after 24 hours only if
they somehow remain firing. Every receiver sends a resolved notification.
`CollectorUnavailable` inhibits worker-scoped alerts because those alerts
cannot be trusted while their metric source is unavailable.

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
| `CollectorPipelineDegraded` | critical | A failed log export in 10 minutes or exporter queue use over 80 percent. | 5m |
| `SystemControlActionObserved` | critical | A new system pause or future automatic ban counter increment. Administrator controls are excluded. | immediate |
| `AgentUsageLimitExceeded` | warning | A new agent usage-limit interruption. | immediate |
| `ProcessEmailJobExhausted` | critical | A final `process_email` attempt exhausted retries. Intermediate failures never increment this counter. | immediate |

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

### CollectorPipelineDegraded

Check the configured OTLP destination, Collector exporter retries, and queue
capacity. Restore export before restarting the Collector so queued data is not
discarded unnecessarily.

### SystemControlActionObserved

Confirm the bounded `action` and `reason`, inspect the corresponding current
state, and follow the automated-intake runbook when intake was paused. Signed
administrator controls are deliberately excluded to avoid duplicate emails.

### AgentUsageLimitExceeded

Inspect the redacted run lifecycle and configured usage ceiling. The alert is
one notification for the new counter event and carries no run identifier.

### ProcessEmailJobExhausted

Inspect restricted worker logs for the terminal job failure and resolve the
underlying dependency. Only the final exhausted attempt alerts.

## Operator workflow

Open the local UIs directly on the host or through an authenticated SSH tunnel:

```bash
ssh -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 operator@server
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

After rollout, verify both Prometheus targets are up and the Alertmanager status
page reports the expected receiver. Verify the dedicated operator mailbox with
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

It validates the Compose, Collector, worker-metric fixture, Prometheus rules,
and Alertmanager configuration; starts only the Collector, Prometheus, and
Alertmanager; sends seven fixed worker metrics over OTLP/HTTP; and checks all
seven with one Prometheus query. Promtool covers pending, firing, and resolved
rule behavior without starting another service or sending email. The
fixed-metric container performs queries over the internal Compose network,
independent of host-port forwarding. The script does not inject Docker logs,
connect to port `24224`, send SMTP traffic, or contact the configured production
OTLP log backend. The worker remains stopped and exposes no inbound port.
Validation containers and their network are removed before the script returns;
named Prometheus and Alertmanager data volumes are preserved.
