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

`grafana/dashboards/growth-kpi.json` is "The Network - Growth & Network Health":
the North Star dashboard. The core value-creation event for a network-effects
product like this one is not a processed email, it is a *completed
introduction* - two people who reached mutual consent and can now talk - so
this dashboard leads with introductions reaching the `introduced` consent
state (`thenetwork_introduction_transitions{action="consent",consent_state="introduced"}`)
as its hero panel, trended both as a rolling-7-day total and per day. Below
that, the full funnel (`proposed` -> `one_consented` -> `introduced`) and the
off-ramps (`declined`, `revoked`) chart the same counter by `action` and
`consent_state`, and a conversion-rate panel divides introduced by proposed
over a rolling 30 days to track match quality independent of proposal volume.
Every panel filters by `action` (`propose`/`consent`/`decline`/`revoke`), not
`consent_state` alone: a `clarify` reply (someone asking why a match was
proposed, rather than accepting/declining) also emits a
`introduction.consent_transition` audit event, carrying the proposal's
current, unchanged `consent_state` - filtering on `consent_state` alone would
double-count those clarifications against genuine `propose`/`consent`
transitions. This dashboard uses only the pre-existing `thenetwork_introduction_transitions`
counter and its `action`/`consent_state` labels; see the
[counter catalog](#counter-catalog) for the full label set.

Below the funnel, a network-health row adds four unlabeled gauge panels:
registered people (`thenetwork_people_total`, a live count, not the cumulative
`thenetwork_accounts_created_total` counter), activated people
(`thenetwork_activated_people_total`, people referenced by at least one
memory - the Cold Start Problem's "hard side" liquidity signal), a derived
activation-rate panel (activated / registered, no new metric), weekly active
senders (`thenetwork_active_senders_weekly`, a 7-day proxy for distinct active
senders), and network density (`thenetwork_network_density`, average graph
degree sampled from the existing hourly `scan_for_opportunities` graph
projection - the network-effects flywheel signal). See the gauge table above
for exact semantics and the proxy caveats on the two people-activity metrics.

`grafana/dashboards/system-resources.json` is "The Network - System Resources":
host, database, and worker-process infrastructure health, as distinct from the
product/reliability signals above. It has three row sections. **Host** reads
the `hostmetrics` receiver's `system_*` series: CPU busy percent and time by
state, load averages, memory usage by state, filesystem free/used bytes by
mount, network I/O/errors/drops, and disk I/O. **PostgreSQL** reads the
`postgresql` receiver's `postgresql_*` series: active backends, database size,
table count, configured connection limit, commit/rollback rate, row operation
rate, block cache hit ratio, and background writer activity. **Worker process
and cgroup** reads the worker's own OTLP-pushed `thenetwork_worker_*` gauges
and counters: process resident memory, open file descriptors, thread count,
and CPU time by state from `/proc/self`, plus cgroup v2 memory
current/max/peak and CPU scheduling-period/throttling counters from
`/sys/fs/cgroup`. See the [host, Postgres, and worker resource
metrics](#host-postgres-and-worker-resource-metrics) section below for the
full catalog, exact label sets, and the `docker_stats` rejection rationale.
Every panel here queries only those documented names and labels; none use
`trace_id`, `run_id`, a sender pseudonym, or any opaque person/event id.

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
workloads `email_agent`, `memory_sanitizer` (currently unemitted - gist
sanitization is a local classifier that bills no model endpoint; the label is
reserved for the planned periodic gist sweep), `abuse_judge`, or `embedding`, the
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

Every gauge below is registered in `thenetwork/worker/metrics.py` with a UCUM
curly-brace annotation unit (for example `"{jobs}"`, `"{people}"`,
`"{paused}"`), never the dimensionless unit `"1"`. The OTel Collector's
Prometheus exporter silently appends a `_ratio` suffix to any gauge
instrument registered with unit `"1"`, regardless of whether the value is
actually a fraction; the metric names below matched exactly what every
consuming dashboard and `prometheus-alert-rules.yml` expression already
expected, so an earlier `unit="1"` registration here would make the affected
panels and alerts silently see no data. Keep new dimensionless worker gauges
on an annotation unit rather than `"1"` to avoid reintroducing this.

`configure_worker_metrics` builds its `MeterProvider` with a fixed
`service.name`/`service.instance.id` of `thenetwork-worker` - the same value
the logs pipeline already uses - rather than the OTel SDK's default
per-process random `service.instance.id`. There is exactly one long-lived
worker process, so a stable identity is correct here. Without it, every
worker restart mints a new `service.instance.id`, which the Collector's
Prometheus exporter surfaces as a distinct `exported_instance` label; Prometheus
then treats the restart as a brand-new series rather than a continuation, so
live-state gauges like `thenetwork_people_total` fork on every restart with
the old series' last value frozen. A bare `metric_name` panel query with no
aggregation across `exported_instance` can then read that frozen (and
possibly stale/zero) old series instead of the current one, depending on
query resolution. Any such zombie series minted before this fix ages out on
its own after the 30-day Prometheus retention window.

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

## Host, Postgres, and worker resource metrics

Two additional Collector receivers export infrastructure-health metrics
alongside the redacted-audit counters above, and the worker pushes ten more
process/cgroup self-metrics over its existing OTLP path. All of these exit
through the separate `prometheus/host` exporter on `:8890` (host and Postgres)
or the existing `prometheus/audit` exporter on `:8889` (worker), scraped as
the `thenetwork-host-metrics` and `thenetwork-audit-activity` Prometheus jobs
respectively - never the redacted `thenetwork.audit` log pipeline, since none
of this is derived from application log records.

### Why not `docker_stats`

Docker's container-stats API (`docker stats` / the Engine API's
`/containers/{id}/stats`) was considered and rejected as the source for this
work in favor of the two socket-free receivers actually used:

- It requires bind-mounting the Docker socket into the Collector container,
  handing it a credential that can create, exec into, or delete *any*
  container on the host, including the database - a large privilege
  escalation surface for a metrics sidecar that has no other reason to reach
  the Engine API.
- It only reports cgroup-level counters for the containers it is told about,
  never true host-wide state - free disk space, host load average, and
  network-interface counters are not container-scoped and are absent from its
  output entirely.
- It reports nothing about Postgres internals (backends, commits, cache hit
  ratio, bloat) - that requires talking to Postgres itself, not the container
  runtime.

The `hostmetrics` receiver instead reads `/proc` and `/sys` directly through a
read-only bind mount (`- /:/hostfs:ro` in `docker-compose.yml`, `root_path:
/hostfs` in `otel-collector-config.yaml`) - filesystem visibility only, no
Docker socket, no container/Engine API access of any kind. The `postgresql`
receiver connects to Postgres directly over SQL using the dedicated
least-privilege `pg_monitor` role (see `docs/development.md`'s migration
notes), which exposes exactly the built-in statistics views Postgres itself
maintains. The worker's process and cgroup metrics read `/proc/self` and
`/sys/fs/cgroup` directly from inside its own container - no socket, no
sidecar, no separate collection path.

### Host metrics (`hostmetrics` receiver, Prometheus job `thenetwork-host-metrics`)

| Prometheus metric | Labels | Meaning |
| --- | --- | --- |
| `system_cpu_time_seconds_total` | `cpu`, `state` | Cumulative seconds each logical CPU spent in each scheduler state (`idle`, `user`, `system`, `nice`, `iowait`, `irq`, `softirq`, `interrupt`, `steal`). Use `rate()`; sum `state!="idle"` for busy time. |
| `system_cpu_load_average_1m` / `_5m` / `_15m` | None | Standard Unix load averages. |
| `system_memory_usage_bytes` | `state` | Bytes in each memory state (`used`, `free`, `cached`, `buffered`, `slab_reclaimable`, `slab_unreclaimable`). |
| `system_filesystem_usage_bytes` | `device`, `mountpoint`, `type`, `mode`, `state` | Bytes used/free/reserved per mounted filesystem. Virtual/pseudo filesystems (`proc`, `sysfs`, `overlay`, etc.) and `/dev/*` mounts are excluded by the receiver's `exclude_fs_types`/`exclude_mount_points` config. |
| `system_filesystem_inodes_usage` | `device`, `mountpoint`, `type`, `mode`, `state` | Inode counts per mounted filesystem, same exclusions. |
| `system_disk_io_bytes_total` | `device`, `direction` | Cumulative bytes read/written per block device. |
| `system_disk_operations_total` | `device`, `direction` | Cumulative disk operation counts. |
| `system_disk_io_time_seconds_total`, `system_disk_operation_time_seconds_total`, `system_disk_weighted_io_time_seconds_total` | `device` (+`direction` on operation time) | Time the disk spent active/servicing operations. |
| `system_disk_merged_total` | `device`, `direction` | Reads/writes merged into a single physical operation. |
| `system_disk_pending_operations` | `device` | Current queue depth of pending I/O. |
| `system_network_io_bytes_total`, `system_network_packets_total` | `device`, `direction` | Cumulative bytes/packets transmitted and received per interface. |
| `system_network_errors_total`, `system_network_dropped_total` | `device`, `direction` | Cumulative interface errors and drops. |
| `system_network_connections` | `protocol`, `state` | Current connection count by TCP state. |

### PostgreSQL metrics (`postgresql` receiver, same Prometheus job)

Collected with the dedicated `pg_monitor`-granted role over `databases:
[${env:POSTGRES_DB}]`; see `docs/development.md` for the role provisioning
migration.

| Prometheus metric | Labels | Meaning |
| --- | --- | --- |
| `postgresql_backends` | None | Active connection count, sampled live from `pg_stat_activity` at scrape time. Reports no series at all when zero connections are active at that instant - this is expected gauge-snapshot behavior, not a scrape failure; a running worker's connection pool normally keeps this nonzero. |
| `postgresql_connection_max` | None | Configured `max_connections`. |
| `postgresql_database_count` | None | Number of user databases visible to the role. |
| `postgresql_db_size_bytes` | None | On-disk size of the configured database. |
| `postgresql_table_count` | None | Number of user tables. |
| `postgresql_table_size_bytes`, `postgresql_index_size_bytes` | None | Disk space used by tables and indexes. |
| `postgresql_commits_total`, `postgresql_rollbacks_total` | None | Cumulative transaction commit/rollback counts. |
| `postgresql_operations_total` | `operation` (`ins`, `upd`, `hot_upd`, `del`) | Cumulative row-level DML operation counts. |
| `postgresql_rows` | `state` (`live`, `dead`) | Current estimated row counts. |
| `postgresql_blocks_read_total` | `source` (`heap_hit`, `heap_read`, `idx_hit`, `idx_read`, `tidx_hit`, `tidx_read`, `toast_hit`, `toast_read`) | Cumulative block reads; `*_hit` is served from Postgres's shared buffer cache, `*_read` came from disk/OS cache. `sum(rate({source=~".*_hit"})) / sum(rate(...))` is the cache hit ratio. |
| `postgresql_index_scans_total` | None | Cumulative index scan count. |
| `postgresql_bgwriter_buffers_allocated_total` | None | Cumulative buffers allocated. |
| `postgresql_bgwriter_buffers_writes_total` | `source` (`bgwriter`, `checkpoints`) | Cumulative buffers written by the background writer vs. checkpoints. |
| `postgresql_bgwriter_checkpoint_count_total` | `type` (`requested`, `scheduled`) | Cumulative checkpoint counts. |
| `postgresql_bgwriter_duration_milliseconds_total` | `type` (`sync`, `write`) | Cumulative checkpoint I/O time. |
| `postgresql_bgwriter_maxwritten_total` | None | Times the background writer stopped early because it had written too many buffers. |
| `postgresql_table_vacuum_count_total` | None | Cumulative manual vacuum count. |

### Worker process and cgroup metrics (existing worker OTLP path, Prometheus job `thenetwork-audit-activity`)

Read from `/proc/self` and `/sys/fs/cgroup` inside the worker container on
every export interval. Every reader is best effort: a missing file, an older
kernel lacking `memory.peak`, or malformed content yields no observation for
that instrument on that scrape rather than raising. `state="user"` and
`state="system"` are the only labels used; nothing here carries a `trace_id`,
sender, or job identifier.

The OTel Collector's Prometheus exporter appends a `_ratio` suffix to any
gauge instrument registered with the dimensionless unit `"1"`, regardless of
whether the value is actually a fraction. Every dimensionless gauge in this
codebase (worker process open FDs and thread count here; queue depth,
primary-intake-paused, and the growth gauges documented above) instead uses a
UCUM curly-brace annotation unit (for example `"{fds}"`, `"{threads}"`) so the
exporter emits the plain metric name below with no suffix - see
`thenetwork/worker/metrics.py` for the full instrument registrations.

| Prometheus metric | Labels | Meaning |
| --- | --- | --- |
| `thenetwork_worker_process_resident_memory_bytes` | None | Worker process RSS, from `/proc/self/status` `VmRSS`. |
| `thenetwork_worker_process_cpu_seconds` | `state` (`user`, `system`) | Cumulative process CPU time since start, from `/proc/self/stat` `utime`/`stime`. Resets only on process restart; use `rate()` for a utilization view. |
| `thenetwork_worker_process_open_fds` | None | Open file descriptor count, from the size of `/proc/self/fd`. |
| `thenetwork_worker_process_threads` | None | Thread count, from `/proc/self/status` `Threads`. |
| `thenetwork_worker_cgroup_memory_current_bytes` | None | Current cgroup v2 memory usage, from `/sys/fs/cgroup/memory.current`. |
| `thenetwork_worker_cgroup_memory_max_bytes` | None | Configured cgroup v2 memory limit, from `memory.max`. Absent when unlimited (the file literally reads `"max"`). |
| `thenetwork_worker_cgroup_memory_peak_bytes` | None | Peak cgroup v2 memory usage, from `memory.peak`. Absent on kernels that don't expose this file. |
| `thenetwork_worker_cgroup_cpu_periods_total` | None | Elapsed CPU scheduling periods, from `cpu.stat` `nr_periods`. |
| `thenetwork_worker_cgroup_cpu_throttled_periods_total` | None | Scheduling periods in which the container was throttled, from `cpu.stat` `nr_throttled`. |
| `thenetwork_worker_cgroup_cpu_throttled_seconds_total` | None | Cumulative throttled CPU time, from `cpu.stat` `throttled_usec`. |

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

After rollout, verify all four Prometheus targets (`otel-collector-internal`,
`thenetwork-audit-activity`, `thenetwork-host-metrics`, and `loki`) are up and
the Alertmanager status page
reports the expected receiver. Verify the dedicated operator mailbox with
a separately planned notification test during deployment. Silence only a
specific alert for a bounded maintenance window; do not silence
`CollectorUnavailable` globally.

This local stack cannot detect total VPS failure, loss of the host network, or
failure of Prometheus and Alertmanager together. Production availability still
requires an independent external probe or dead-man monitor.

## Validation

There is no end-to-end validation script. Validate each configuration file on its
own, then check the running stack through its own APIs:

```bash
docker compose config
docker run --rm -v ./otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml \
  otel/opentelemetry-collector-contrib:0.118.0 validate --config=/etc/otelcol-contrib/config.yaml
docker run --rm -v ./loki-config.yaml:/etc/loki/config.yaml \
  grafana/loki:3.6.11 -config.file=/etc/loki/config.yaml -verify-config=true
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$PWD/prometheus-alert-rules.yml:/etc/prometheus/rules/thenetwork.yml:ro" \
  prom/prometheus:v3.5.5 check config /etc/prometheus/prometheus.yml
curl http://127.0.0.1:9090/api/v1/targets
curl --get http://127.0.0.1:3100/loki/api/v1/query_range \
  --data-urlencode 'query={service_name="thenetwork-worker"}' --data-urlencode 'since=1h'
```

Every Prometheus target should report `up`, the Loki query should return the
worker's own JSON lines unchanged, and the Grafana datasource health pages at
`http://127.0.0.1:3000` should be green for both Prometheus and Loki with all four
provisioned dashboards loaded.

For rollout recovery, keep `loki-data` when recreating services. A normal
`docker compose up -d` reuses it. Do not use `docker compose down --volumes` in
production unless deleting retained logs is intentional. If Loki cannot start,
validate `loki-config.yaml`, inspect the volume mount and free space, then start
Loki before the Collector. Existing Prometheus metrics remain separate in
`prometheus-data`.
