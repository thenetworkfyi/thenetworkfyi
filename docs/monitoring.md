# Monitoring

The Compose stack derives Prometheus counters from the same redacted audit log
records that the OpenTelemetry Collector forwards to the configured OTLP
destination. The worker does not expose an inbound metrics port. Prometheus
scrapes the Collector over the internal Compose network and binds its UI only
to `127.0.0.1:9090`.

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

## Validation

Run the repository-root validation script on a host with Docker and `jq`:

```bash
./validate-monitoring.sh
```

It validates the Compose, Collector, metrics-fixture, and Prometheus
configuration; starts only the Collector and Prometheus; sends seven fixed
worker state and operational metrics to the Collector over OTLP/HTTP; and checks
all seven with one Prometheus query. The fixed-metric container also performs that query over
the internal Compose network, independent of host-port forwarding during
recreation. The script does not inject Docker logs, connect to port `24224`, or
contact the configured production OTLP log backend. The bounded audit counter
catalog remains covered by the Collector configuration tests. The worker
remains stopped and exposes no inbound port. Validation containers and their
network are removed before the script returns; the named Prometheus data volume
is preserved.
