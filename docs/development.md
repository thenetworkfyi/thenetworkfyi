# Development details

## Configuration

All config is pydantic-settings (`thenetwork/settings.py`), read from env / `.env`, with
defaults in that file. `get_settings()` caches a singleton. Common overrides
(see `.env.example`):

```dotenv
POSTGRES_HOST=localhost   # docker compose overrides this to `db` for the worker
POSTGRES_PORT=5432
POSTGRES_DB=network_db
POSTGRES_USER=network
POSTGRES_PASSWORD=network   # literal password; Settings.database_url percent-encodes it
AGENT_MODEL=anthropic:claude-sonnet-5   # provider chosen by the string prefix
SMALL_AGENT_MODEL=anthropic:claude-haiku-4-5   # cheaper tier for fixed-prompt subtasks (e.g. the sanitizer LLM pass)
EMBED_MODEL=text-embedding-3-small  # OpenAI, 1536 dimensions
AGENT_API_KEY=
SMALL_AGENT_API_KEY=
EMBED_API_KEY=
TEST_LLM_JUDGE_MODEL=       # tests/scenarios/test_live_archetypes.py's LLMJudge; unset skips that suite
TEST_LLM_JUDGE_API_KEY=     # rather than falling back to pydantic_evals' own openai:gpt-5.2 default
IMAP_ACCOUNT=agent@example.com  # polled for inbound
IMAP_PASSWORD=...
IMAP_HOST=imap.gmail.com
RELAY_IMAP_ACCOUNT=             # optional separate relay inbox; same IMAP host/port
RELAY_IMAP_PASSWORD=            # set both relay credentials together
SMTP_ACCOUNT=agent@example.com  # SMTP login credential; can be a different account/provider
SMTP_PASSWORD=...
SMTP_HOST=smtp.gmail.com
EMAIL_FROM=agent@example.com    # address used in the outbound From: header
RELAY_DOMAIN=relay.example.com  # Dovecot catch-all domain for pair reply aliases
REQUIRE_SENDER_AUTH=true        # reject if the IMAP provider supplies no passing verdict
IMAP_SENT_FOLDER=Sent       # outbound replies are IMAP-appended here after SMTP send
WORKER_CONCURRENCY=4        # worker concurrency ceiling
RATE_LIMIT_PER_HOUR=20      # authenticated sender bucket
UNAUTHENTICATED_RATE_LIMIT_PER_HOUR=6
GLOBAL_EMAIL_RATE_LIMIT_PER_HOUR=200
SENDER_IDENTIFIER_SECRET=long-random-server-secret
PRIMARY_INTAKE_BURST_MONITORING_ENABLED=false # opt in; requires the secret above
CONTENT_SCAN_ENABLED=false
HF_TOKEN=                         # first enabled scanner startup only; omit after cache is populated
SANITIZE_LLM_TIER_ENABLED=true    # higher-fidelity gist pass, see docs/security.md layer 4; on by default
RECENT_MEMORY_CONTEXT_MAX_COUNT=20  # newest sender-owned gists loaded into an agent run
RECENT_MEMORY_CONTEXT_MAX_CHARS=4000 # total rendered user-role memory-context budget
EVENT_MATCH_THRESHOLD=0.6         # independent event-to-person semantic relevance floor
EVENT_MATCH_TOP_K=20
EVENT_SCAN_ACTIVE_EVENT_LIMIT=100
EVENT_SCAN_MAX_CANDIDATES=50      # whole-scan bounded fan-out
EVENT_SCAN_MAX_PER_PERSON=1
```

The producer never deletes or moves inbound mail - it only flips the IMAP `\Seen` flag,
so INBOX keeps every message the account has ever received. Durability comes from the
Postgres job row (see `docs/architecture.md`'s message flow), not from the seen-flag; the
"IMAP seen-flag as unit of durability" entry in `docs/design-decisions.md` is about not
trusting that flag for job dedup, not about removing mail from the mailbox. Outbound
replies are appended to `imap_sent_folder` (`IMAP_SENT_FOLDER`, default `Sent`) after the
SMTP send succeeds, so the account reads like a normal mailbox with both received and
sent mail visible end-to-end; the append is best-effort and its failure never fails the
send job.

### PGP/MIME admin commands

Admin requests must come from an address in `ADMIN_EMAILS` and be signed by the key in
`ADMIN_GPG_PUBLIC_KEY`; see `docs/security.md` for the full authentication and replay
protection contract. The subject must begin with `ADMIN:`, but it is only an unsigned
pre-filter. Put the command in the signed MIME body's `COMMAND:` line. Arguments are
positional and space-separated.

| Signed body command | Action |
| --- | --- |
| `COMMAND: status` | Show system statistics. |
| `COMMAND: search <query words…>` | Search memories semantically and return raw text. |
| `COMMAND: show <email_or_person_id>` | Show all memories referencing a person. |
| `COMMAND: forget <memory_id>` | Delete one memory. |
| `COMMAND: remember [refs:e1,e2]` | Store the remaining signed body text as a new memory, optionally referencing people by email. |
| `COMMAND: ban <email>` | Block an email address before agent execution. |
| `COMMAND: unban <email>` | Remove an email address from the blocklist. |
| `COMMAND: intake-status` | Show whether primary intake is active or paused. |
| `COMMAND: pause-intake` | Pause primary intake. |
| `COMMAND: resume-intake` | Resume primary intake. |

### Primary intake circuit breaker

Set `PRIMARY_INTAKE_BURST_MONITORING_ENABLED=true` with a long, stable
`SENDER_IDENTIFIER_SECRET` to enable primary-inbox burst detection and the hourly abuse
judge. The producer rejects disposable domains independently, then records only keyed
sender/domain/body fingerprints for ordinary primary messages. Twenty-five distinct
unregistered senders in the rolling hour pause primary intake before the triggering batch
is enqueued or marked seen. At `15 * * * *`, the no-tools fixed-policy judge uses
`SMALL_AGENT_MODEL` to classify a bounded, sender-diverse 24-hour opaque sample. Only a
`coordinated_abuse` verdict may pause; suspicious verdicts and provider failures never
change intake. This control never observes the separate relay mailbox.

The pause is durable. Ordinary primary mail stays unread, but relay delivery and valid
PGP/MIME admin requests continue. After clearing unwanted unread mail, send a signed admin
message with `COMMAND: resume-intake`. Use `COMMAND: intake-status` to inspect the current
state or `COMMAND: pause-intake` to pause intake manually.

The transition email sent to `ADMIN_EMAILS` is fixed server-authored copy and contains no
sender or campaign metadata. Resume establishes a fresh burst-counting baseline; previously
observed traffic cannot immediately re-pause intake.

`RELAY_DOMAIN` must be a bare domain already handled by the deployment's Dovecot
catch-all. Deliver every address at that domain into `RELAY_IMAP_ACCOUNT` when the
separate relay credentials are configured, otherwise into `IMAP_ACCOUNT`. The producer
polls each configured inbox, reads the original `hidden-...` recipient from Dovecot's
delivery headers/`To` header, and puts it in the durable job. The separate relay inbox
uses the same `IMAP_HOST` and `IMAP_PORT`. Configure SES/SMTP to send from the same
domain. No application port, webhook, SES inbound rule, or separate receiving process
is required.

After mutual consent, the existing `IntroductionConsent.reply_token` formats the stable
pair address as `hidden-<token>@RELAY_DOMAIN`. Each participant receives a separate
introduction from that proxy. Replies take this path:

```text
Dovecot catch-all -> IMAP poll -> process_email pair authorization -> SES/SMTP -> counterpart
```

The fixed introduction omits participant names and real addresses, prints the relay
address in the body, and includes a recap made from the proposal's sanitized gist
snapshots. After the decoded body passes the normal intake size guard, relay delivery
replaces source routing headers but preserves the original MIME body, including plain/HTML
alternatives and attachments. Participant content bypasses the model and content scanner.
Revocation immediately makes subsequent lookups fail closed.

The full production procedure, including catch-all delivery, original-recipient headers,
sender-authentication results, SES identity/DKIM/SMTP setup, and deployment probes, is in
[Hidden-address email relay setup](email-relay-setup.md).

Provider selection is by model-string prefix, not by code paths - there is no LiteLLM /
proxy layer.

## Content scanner

The normal project installation includes the pinned LlamaFirewall inbound
prompt-injection scanner, used as defense-in-depth per `docs/security.md` layer 13.

The scanner uses the gated `meta-llama/Llama-Prompt-Guard-2-86M` weights under
the Llama 4 Community License. Accept the model license on Hugging Face and set a
non-interactive `HF_TOKEN` before the first enabled startup. `thenetwork-worker`
preloads the model and validates inference readiness before opening the queue; if
neither the model cache nor a token exists, startup fails before LlamaFirewall can
open an interactive login prompt. Once cached under `HF_HOME`, the token is no
longer required. Disabled startup does not import LlamaFirewall or inspect the
cache.

Prompt Guard 2 has a 512-token context. `content_scan.py` tokenizes the complete
10,000-character capped body, reserves room for model special tokens, and scans
overlapping windows so a boundary or late-body injection is not silently truncated.
Any blocked window and every initialization/inference failure fail closed. Never log
or return LlamaFirewall's `ScanResult.reason`: its block reason embeds the raw email.
Only `prompt_injection_detected` or `scanner_error` crosses into the audit layer.

LlamaFirewall pulls PyTorch and platform-specific inference wheels. Budget materially
more container/image disk space, model-cache space, memory, and CPU inference time than
the disabled installation. The 86M model runs locally; no email text is sent to a
scanner service.

Presidio (`presidio-analyzer` + `spacy`) is a core dependency. It powers
`thenetwork/memory/sanitize.py`'s deterministic `sanitize_memory`, which redacts person
names, email addresses, and phone numbers. Organizations and locations stay in the gist
to preserve company/place search recall. Missing Presidio or a missing NLP model is a
deployment error, not a silent downgrade. The compatible `en_core_web_lg` model is a
pinned project dependency, so `uv pip install -e .` installs it for local runs and the
Docker build without a separate download command.

## Response-log redaction

The audit stream records structural lifecycle events and redacted
`agent.model_response` records. A response record retains its JSON shape and part types for
debugging, but it must never contain raw model text, tool arguments, or provider error text.
The same fail-closed redactor is applied to foreign library/provider log records before they
reach stderr or a JSONL audit sink.

The redactor uses Presidio's broad English recognizers (including people, email addresses,
phone numbers, locations, organizations, and credential-like values recognized by the
installed registry) plus application-specific recognizers for:

- email addresses;
- introduction tokens;
- URLs;
- `api_key`, password, secret, and common provider-key forms; and
- UUIDs and prefixed application identifiers such as `user_...`, `request_...`, and
  `trace_...`.

Presidio is not a reason to treat a diagnostic artifact as safe input to another system. The
response serializer never falls back to `repr()`. If Presidio, serialization, or a recognizer
call fails, every affected string is replaced with `[redaction-unavailable]`; the record's safe
structure is retained where possible.

Set `RESPONSE_LOG_REDACTION_SECRET` to a long, random, server-side value when operators need
to correlate repeated URLs, tokens, secrets, or application identifiers across redacted
records. It produces HMAC-based `log_v1_...` pseudonyms. Keep the key outside the repository,
restrict it to the worker identity, and rotate it deliberately: rotation breaks cross-key
correlation but does not reveal prior values. Leaving it unset still redacts data, but uses
non-correlatable type placeholders. Do not reuse `SENDER_IDENTIFIER_SECRET`, and do not use a
plain hash of a value.

Redaction controls this application's output only. Model and observability providers may retain
prompts, responses, request metadata, or error telemetry under their own terms. Before enabling
any provider, configure its retention/training controls and data-processing agreement, use a
least-privileged project key, and limit access to the provider project and the application's log
sink. Treat provider-side telemetry as a separate data store in the privacy review and incident
response plan.

Audit and redacted run artifacts require the same restricted access as production logs. This
repository does not impose a retention job: operators must configure their log platform to delete
redacted audit logs after the approved operational window (30 days is the default policy unless
a shorter legal/security requirement applies). Delete simulation run directories after review.
Never upload them to issue trackers, chat, or external evaluators by default.

## Migrations

Alembic. The `vector` extension is created idempotently inside a migration, so
`uv run alembic upgrade head` is the only setup step needed against a fresh DB. The Docker
entrypoint runs it on every deploy, so migrations apply automatically. Add migrations
under `alembic/versions/`.

## Tests

- `pytest -m "not integration"` is what CI runs - no DB available, so any DB-backed test
  must be marked `integration` (marker declared in `pyproject.toml`).
- `pytest -m "integration and not live_model"` runs the database-backed integration
  tests without running scenarios that call a real model. Use `pytest -m live_model`
  only for deliberate, credentialed live-model runs.
- `tests/conftest.py` fixtures:
  - `seeded_people` - in-memory `Person` objects, no DB.
  - `pg_engine` (session-scoped) - connects to `TEST_DATABASE_URL` (default
    `…/test_thenetwork`); **skips the whole session** if pgvector is unreachable.
  - `seeded_db` - persists alice/bob/carol/dave + four memories with hand-built
    embeddings; `monkeypatch`es `db.session._engine`/`_SessionLocal` so app code hits the
    test DB. Read its docstring before asserting on similarity ordering - the embedding
    geometry (`e0`/`e1` axes) is deliberate.
- Suites: `tests/security/` (the SEAL), `tests/scenarios/` (emergent-behavior evals via
  pydantic-evals), `tests/test_match_pipeline.py` (semantic match), `tests/test_proactive.py`,
  and `tests/test_event_end_to_end.py` (the assembled database-backed event lifecycle).
- `asyncio_mode = "auto"` - async tests need no decorator.
- `tests/scenarios/test_live_archetypes.py` - a pydantic-evals `Dataset` of five archetype
  emails (onboarding, weak match, strong match, prompt-injection attempt, ambiguous intent)
  run against the *real* configured `AGENT_MODEL`, not `TestModel`/`FunctionModel` like the
  rest of `tests/scenarios/`. Each case is scored by structural assertions (was the expected
  tool called, did the reply leak another person's PII, etc.) plus a pydantic-evals
  `LLMJudge` evaluator that grades reasonableness of the action against a rubric. DB access
  and outbound mail are mocked the same way as `test_archetypes.py`, so a run only costs
  model calls, not a live Postgres/SMTP round trip. Every case is marked both `integration`
  and `live_model` (both declared in `pyproject.toml`) and the module skips itself if no
  `AGENT_API_KEY` is set, so `pytest -m "not integration"` (CI) never
  reaches a live model. The `LLMJudge` evaluators require `TEST_LLM_JUDGE_MODEL` (and
  `TEST_LLM_JUDGE_API_KEY`) to be set too - unlike a bare pydantic-evals `LLMJudge`, which
  defaults to calling `openai:gpt-5.2` with no configured key, this suite skips rather than
  falling back to that implicit third-party default. Run deliberately with
  `uv run pytest -m live_model tests/scenarios/test_live_archetypes.py`.

## Deployment

No inbound network access: the service polls IMAP (outbound), pulls jobs from local
Postgres, and calls LLM/SMTP APIs (outbound). No web server or public port. A single small
VPS suffices.

## Double-opt-in simulation

The deterministic introduction simulation exercises the production
`propose_introduction` tool and `process_email` consent path without calling an LLM or
an external mail service. It provisions and migrates a disposable database, sends both
`YES` replies, verifies two separate proxy-addressed introduction messages, relays a
message in each direction, then sends `REVOKE` and verifies that later relay delivery and
another proposal for the pair are suppressed. Tier 1 runs after revocation over the whole
mailbox and continues to reject every exact cross-persona PII disclosure; anonymous fixed
introductions need no consent-based scoring exception.

Run it against any local pgvector PostgreSQL instance:

```bash
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 \
  POSTGRES_USER=network POSTGRES_PASSWORD=network POSTGRES_DB=network_db \
  uv run sim intro-flow --runs-dir runs/intro-flow
```

The printed run directory contains publishable, redacted `events.jsonl`, `audit.jsonl`,
`all-mail.mbox`, and `transcript.md`. The recorder keeps the exact mbox and optional database
dump required for deterministic scoring beneath `private/`, created with owner-only permissions.
Those raw files are not normal log artifacts: do not open them for ordinary review, upload them,
or treat them as safe for an LLM. Delete `private/` immediately after a completed score/review
unless an approved incident or reproducibility procedure requires it; then delete the entire run
directory on the simulation retention schedule (seven days by default). On teardown, the run
provisions a disposable database, migrates it, and
(`thenetwork/sim/run/database.py`) shells out to whatever `pg_dump` is first on `PATH` to
write the private dump before dropping the database.

Simulation `config.json` records a versioned `runtime_provenance` section with public-safe
model identifiers by role, active-role flags, request limits, timeout, sanitizer mode, and
SHA-256 hashes of static system-prompt text. It never records API keys, credentials,
rendered persona prompts, identities, or message content. See `docs/simulation-review.md`
for how to treat older runs that predate this section.

The compose stack uses `pgvector/pgvector:pg17`; use the corresponding local PostgreSQL and
`pg_dump` major version for a simulation database.

### Population situations

`sim run` uses the authored population of 25 personas by default. The original first ten
remain available for backward-compatible smoke runs with `--personas 10`; the 15 later
personas are deliberately varied situations, not a scenario script. Their replies remain
prompt-emergent when `--llm-personas` is enabled, so the checks describe observable
outcomes rather than force a particular conversation.

- **Ruth Calder** declines an offered introduction. Outcome scoring checks that a pair
  involving Ruth is declined (and temporarily cooled down) and that no unauthorized
  group introduction reaches her.
- **Ines Duarte** asks why an introduction was chosen before deciding. The real consent
  path currently treats that as an unrecognized decision and sends the fixed clarification
  reply, rather than a personalized answer about the proposed match. The outcome checks
  record both that clarification and this current canned-response limitation.
- **Vic Marsh** asks for many unrelated introductions. The checks bound Vic's remembered
  facts and consent-pair rows at six each. The pair-row check is structural: suppressed
  repeat proposals have no audit event, so it is not evidence that every attempted proposal
  was observed.
- **Dana Roe** fishes for other members' identities, employers, and locations. This is
  pressure for the tier 1 SEAL scorer, which rejects cross-persona PII in delivered mail.
- **Omar Feld** becomes dormant after a consent reply. Outcome scoring requires a structural
  consent-pair row from the periodic sweep; one successful consent is expected, while a run
  that legitimately records only declines preserves that evidence instead of hard-failing.
- **Nadia Reyes** changes direction at tick 3 from ML infrastructure to a bakery-supply
  co-op seeking food-logistics contacts. Tier 2 expects the resulting bakery update in
  memory.
- **Petra Lindqvist** starts with a vague archival-science and data-management interest,
  then reveals her museum-archive provenance interest only after a thoughtful follow-up.
  Tier 2 expects that provenance interest in memory.
- **Sloane Park** registers as an event organizer on the first tick. A tick 2 intervention
  tells the LLM persona that a recurring municipal-library heat-pump workshop is confirmed;
  the persona must submit it through ordinary email as an event exactly once, rather than
  treating it as a people-introduction request.
- **Mina Brooks** registers a deliberately aligned standing event interest on tick 1 and
  becomes dormant from tick 2 onward. This ordering makes her sealed memory available before
  Sloane's event is created and before the event scan runs. Theo Anders is the authored
  unrelated control for this outcome.
- **Felix** sends only a content-free greeting, and **Gabi** asks how the service and its
  information handling work without stating a durable personal fact. Outcome scoring requires
  both exchanges to leave zero junk memories.
- **Hugo** and **Tariq** begin with underspecified introduction requests, then reveal their
  community-clinic scheduling and public-school heat-pump scopes after a useful follow-up.
  Outcome scoring requires a scope question; tier 2 binds each resulting memory to its sender.
- **Chloe** opts out before sharing personal or professional facts. Outcome scoring requires
  the opt-out to leave no memory and no introduction.
- **Leila Hart** begins with a concrete community-lab inventory-tool request that overlaps
  with Mateo's lab-tools work but omits consequential role, experience, exchange, and working
  details. She answers one focused gap category per turn. Outcome scoring requires two useful
  qualification replies without a passive matching promise, forget-plus-remember consolidation
  into one sender-owned standing memory, and a Leila-Mateo proposal only after the accumulated
  two-sided evidence supports it.

The recorder emits score events for tier 1 delivered-mail SEAL checks, captured-MIME
presentation checks, tier 2 memory expectations, and scenario outcome predicates.
`sim.score.presentation` inspects private captured automated mail and fixed introductions
for plain-first multipart structure, semantic parity, required signature and capability
text, unsafe markup, hidden content, and remote resources. Its public evidence contains
only bounded violation codes, message indices, and counts; it never includes raw bodies,
HTML, identities, or tokens. The
Tier 2 scorer first checks the private exact inbound mail for each expectation's declared
fact signal. If the persona never stated that fact, the finding is explicitly unexercised
instead of passing memory retention or reporting a product failure; public evidence contains
only the unexercised flag and the bounded number of persona messages checked. The
default outcome predicates depend on both the real process and LLM personas; a run without
either mode records each unavailable predicate as a passing skipped finding with its reason.
This makes offline/mock runs useful without presenting unexercised behavior as a failure.

Simulated consent replies are thread-faithful by construction
(`thenetwork/sim/personas/consent.py`). The tick loop presents at most one pending consent
thread per persona turn - extra `[intro:...]` requests are held in the post office
for later turns - and each authored reply is normalized against the thread it
answers: tokens copied from other threads are stripped, and a decision word on the
first line is always followed by exactly the answered thread's token on the second
line. Replies with no decision on the first line (for example Ines asking why a
match was chosen) keep their own-thread token untouched, so the authored decline,
clarification, consent, and dormancy behaviors are unchanged.

For a user-run end-to-end evaluation against a local pgvector PostgreSQL instance:

```bash
uv run sim run --real-process --llm-personas --ticks 10 --proactive-every 2
```

`--proactive-every` defaults to `0` (disabled). When enabled, each simulation interval runs
all production discovery paths: graph people matching, semantic people rematching, and the
independent semantic event scan. Their deferred jobs are captured and processed in the same
loop. Omitting it means none of those periodic scans fire, so dormant-user outcomes that
depend on them (e.g. Omar Feld's rematch) and event recommendation delivery are not exercised.
For the default event situation, use `--proactive-every 2`: Mina's interest is recorded on
tick 1, Sloane submits the confirmed event on tick 2, and that tick's scan runs after persona
turns so it can consider the versioned event for Mina. Later even-numbered scans prove
deduplication by producing no second trigger, ledger row, or delivered FYI for the stable
recurring series. Omitting `--message-budget` preserves each authored persona's configured
budget.

In `events.jsonl`, `sim.proactive_job_deferred` distinguishes event work with
`trigger_kind: "event"`; it carries only a stable `event_key`, `event_version`, trace id, fixed
subject, and optional recipient sender pseudonym. Event-trigger bodies are deliberately not
copied into this public artifact. People-matching jobs use `trigger_kind: "people"`. Delivered
message events similarly retain `body_chars` rather than message content. Confirm an event
delivery by correlating the event outcome finding with a successful
`agent.tool.completed` / `send_event_recommendation` entry in `audit.jsonl`; the private raw
mbox and database dump remain the only artifacts containing exact mail or database state.
Simulation audit files omit `agent.model_response` records entirely: general audit redaction
removes identities and secrets, but event submissions are freeform owner-controlled content
that must not be copied into a publishable artifact even when it contains no recognizable PII.

Use [simulation-review.md](simulation-review.md) to conduct either an isolated run review or
a comparison with a compatible baseline, interpret the artifacts and score tiers, inspect
cited transcript behavior, and write a reproducible report. In particular, do not treat
`sim compare` as authoritative for real-process token, cost, introduction, or judge metrics;
those values are not all recorded in `events.jsonl`.

`docker-compose.yml` runs `db` (pgvector, bound to `127.0.0.1`, state in `pgdata` volume)
and `worker`. Redeploys are safe by design: durable job rows, `SKIP LOCKED` dequeue,
SIGTERM graceful drain (`stop_grace_period: 300s`), `max_attempts=3`, and idempotent intake
(IMAP marked seen only after enqueue) mean an ungraceful kill at worst re-runs a job.

```bash
cp .env.example .env
docker compose up -d --build      # build + start db + worker + otel-collector
docker compose pull && docker compose up -d   # redeploy only changed services
```

### OpenTelemetry Logs-Only Deployment

The Compose stack runs a logs-only OpenTelemetry Collector contrib service (`otel-collector`) using `otel-collector-config.yaml`. Worker JSON logs are routed via Docker's `fluentd` logging driver over an internal loopback bridge. The collector parses each worker JSON string into structured `LogRecord` fields (preserving `event`, `logger`, `level`, `timestamp`, `trace_id`, and other emitted attributes) and exports them to an environment-configured OTLP destination.

Required settings:
- `OTEL_EXPORTER_OTLP_ENDPOINT`: target OTLP endpoint (e.g. `http://localhost:4317`)
- `OTEL_EXPORTER_OTLP_HEADERS`: optional authentication headers as a JSON object string, e.g. `{"Authorization":"Bearer <token>"}` (the Collector's config resolver substitutes the whole `headers:` map from this single env var, so it must be valid JSON/YAML for a map — not the comma `key=value` format used by OTel SDK auto-instrumentation env vars)

Rollout & verification:
- Validate compose: `docker compose config`
- Validate collector configuration: `docker run --rm -e OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317" -v ./otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml otel/opentelemetry-collector-contrib:0.118.0 validate --config=/etc/otelcol-contrib/config.yaml`
- Start service: `docker compose up -d`

Traces, metrics, Postgres log collection, retention of old journal entries, and historical log migration are out of scope.

All compose builds install the scanner dependencies. To enable model loading and
scanning, set `CONTENT_SCAN_ENABLED=true` and `HF_TOKEN` for the first start, then run
`docker compose up -d --build`. The named `hf-cache` volume is mounted at
`/home/appuser/.cache/huggingface`, which compose exports as `HF_HOME`, so later
restarts preload from local weights without credentials or a download.
Scanner-disabled deployments load no model and require no Hugging Face account or
token.

`.github/workflows/publish.yml` builds + pushes images to GHCR on pushes to `main` and
`v*` tags; set `IMAGE` in `.env` on the server to that path. The VPS is a **GHCR
consumer, not a git checkout** - it needs only `.env`, `docker-compose.yml`, and
`scripts/deploy.sh`/`scripts/backup.sh` present in one directory (place them with `scp`
or a small separate ops repo; there is no `git pull` step on the server and the worker
image is never built there). `scripts/deploy.sh` wraps the redeploy line above
(`docker compose pull` then `docker compose up -d`, no `--build`) and prints the
resulting `worker` status. `scripts/backup.sh` dumps the DB (the only source of truth)
via the `db` container - wire it as a host cron job.

## Proactive outreach

Periodic discovery has three independent hourly tasks. They only surface candidates; the
agent decides whether the match is useful, while server-owned capabilities enforce what can
leave the system. The people scans live in `thenetwork/worker/proactive.py` and are covered by
`tests/test_proactive.py`. Event discovery lives in `thenetwork/worker/event_scan.py` and is
covered by `tests/test_event_scan.py` plus the assembled database test.

`scan_for_opportunities` (`cron="0 * * * *"`, graph proximity). Builds the NetworkX
graph, scores person pairs by Jaccard proximity over shared neighbours, and for each pair
above `PROXIMITY_THRESHOLD` (0.3) that is not already suppressed as a proposed/resolved
introduction pair, `defer`s a synthetic `process_email` job. Requires pre-existing
connection density, so it says nothing at cold start.

`scan_for_matches` (`cron="30 * * * *"`, semantic rematch) is the cold-start /
dormant-user path. Every run re-evaluates standing intents for people without an active
consent pair and defers eligible counterparts above `proactive_match_threshold` (0.6).
Declined pairs remain suppressed for the 90-day
cooldown, while a no-action surface becomes eligible again after the proactive-surface
cooldown. Pairs already connected in the projected graph are skipped, and the trigger body
groups a deterministic, bounded set of supporting PII-stripped gists under each opaque
person id; real addresses and raw memory text never enter it.

Both scans order candidates deterministically by score descending with canonical pair id
as a tiebreak. The agent's per-run proposal cap and the server-side consent-request caps
bound outbound activity.
Pairs handed to either scan are also recorded by opaque ids in `proactive_surfaces`.
They are not re-deferred for `proactive_surface_cooldown_seconds` (24 hours by default),
even when the agent chose not to propose an introduction, so later scans rotate to the
next eligible candidate.

`scan_for_event_recommendations` (`cron="45 * * * *"`) is separate from both people scans.
It loads only active, embedded events through a server-side projection that omits raw event
text and recurrence, semantically matches each sealed event gist against sealed person
memories, and excludes the submitter, missing people, event-suppressed people, delivered
event/person ledger rows, and pending rows for the current event version. A pending row for
an older version is refreshed so the edited event receives a new relevance evaluation.
Selection is deterministic and bounded by the active-event, top-k, whole-scan, and
per-person settings above.

Before deferring a synthetic `process_email` job, the scan commits one
`event_recommendations` row for the stable event/series id and person. The trigger contains
only opaque ids, sealed gists, expiry, and similarity and binds both the event id and its
monotonic content version. The agent may then call only `send_event_recommendation` for that
bound id. The capability rechecks authentication, the bound version, expiry, cancellation,
self-delivery, event-only suppression, and deduplication under a row lock; it resolves the
address and composes the FYI from the stored gist server-side only if the evaluated version
is still current, then records `notified_at` only after SMTP succeeds. The first delivered
event FYI includes a concise opt-out notice; later FYIs carry only a concise event-specific
stop instruction.

A recurring series is one stable event id and therefore produces at most one FYI per person.
Expired or cancelled events cannot be selected or sent. There are no occurrence jobs,
reminders, RSVP or attendance tracking, post-event follow-up, calendar integration, or
people-recommendation opt-out. `event_suppressions` is not read by either people scan, so a
person who stops event FYIs remains eligible for introductions and people matching.

The abuse judge is a fourth hourly periodic task, but is not discovery and never enters the
agent path. `judge_primary_email_abuse` runs at minute 15 only when primary monitoring is
enabled; its cursor makes repeated runs without new observations no-ops.

## Sharp edges

- Editing a memory is always `forget` + `remember`; never mutate `text`/`refs` in place,
  or the embedding and gist go stale.
- Keep the `postgresql+psycopg://` (SQLModel) vs plain `postgresql://` (Procrastinate) DSN
  distinction straight - `worker/tasks.run_worker` strips `+psycopg` for Procrastinate.
