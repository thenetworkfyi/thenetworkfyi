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
```

Low-volume deployments generally get more useful whole-number results from
`increase` over an hour or a day. `rate` is useful for alerting on sustained
activity or failures.

## Validation

Run the repository-root validation script on a host with Docker, `curl`, and
`jq`:

```bash
./validate-monitoring.sh
```

It validates the Compose, Collector, and Prometheus configuration; starts only
the Collector and Prometheus; injects representative redacted audit records;
and waits until all catalog counters are queryable. The script fixes the OTLP
destination to a local plaintext validation sink and explicitly enables the
exporter's insecure transport for that sink, so synthetic events cannot be
sent to a configured production backend. Production transport remains TLS by
default. The worker remains stopped and exposes no inbound port.
