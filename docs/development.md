# Development details

## Configuration

All config is pydantic-settings (`thenetwork/settings.py`), read from env / `.env`, with
defaults in that file. `get_settings()` caches a singleton. Common overrides
(see `.env.example`):

```dotenv
DATABASE_URL=postgresql+psycopg://network:network@localhost:5432/network_db
AGENT_MODEL=anthropic:claude-sonnet-4-6   # provider chosen by the string prefix
EMBED_MODEL=text-embedding-3-small
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
EMAIL_ACCOUNT=agent@example.com
EMAIL_PASSWORD=...
IMAP_HOST=imap.gmail.com
SMTP_HOST=smtp.gmail.com
WORKER_CONCURRENCY=4        # global LLM-spend ceiling
RATE_LIMIT_PER_HOUR=10      # per sender
CONTENT_SCAN_ENABLED=false
```

Provider selection is by model-string prefix, not by code paths — there is no LiteLLM /
proxy layer.

## Migrations

Alembic. The `vector` extension is created idempotently inside a migration, so
`alembic upgrade head` is the only setup step needed against a fresh DB. The Docker
entrypoint runs it on every deploy, so migrations apply automatically. Add migrations
under `alembic/versions/`.

## Tests

- `pytest -m "not integration"` is what CI runs — no DB available, so any DB-backed test
  must be marked `integration` (marker declared in `pyproject.toml`).
- `tests/conftest.py` fixtures:
  - `seeded_people` — in-memory `Person` objects, no DB.
  - `pg_engine` (session-scoped) — connects to `TEST_DATABASE_URL` (default
    `…/test_thenetwork`); **skips the whole session** if pgvector is unreachable.
  - `seeded_db` — persists alice/bob/carol/dave + four memories with hand-built
    embeddings; `monkeypatch`es `db.session._engine`/`_SessionLocal` so app code hits the
    test DB. Read its docstring before asserting on similarity ordering — the embedding
    geometry (`e0`/`e1` axes) is deliberate.
- Suites: `tests/security/` (the SEAL), `tests/scenarios/` (emergent-behavior evals via
  pydantic-evals), `tests/test_match_pipeline.py` (semantic match), `tests/test_proactive.py`.
- `asyncio_mode = "auto"` — async tests need no decorator.

## Deployment

No inbound network access: the service polls IMAP (outbound), pulls jobs from local
Postgres, and calls LLM/SMTP APIs (outbound). No web server or public port. A single small
VPS suffices.

`docker-compose.yml` runs `db` (pgvector, bound to `127.0.0.1`, state in `pgdata` volume)
and `worker`. Redeploys are safe by design: durable job rows, `SKIP LOCKED` dequeue,
SIGTERM graceful drain (`stop_grace_period: 300s`), `max_attempts=3`, and idempotent intake
(IMAP marked seen only after enqueue) mean an ungraceful kill at worst re-runs a job.

```bash
cp .env.example .env
docker compose up -d --build      # build + start db + worker
docker compose pull && docker compose up -d   # redeploy only changed services
```

`.github/workflows/publish.yml` builds + pushes images to GHCR on pushes to `main` and
`v*` tags; set `IMAGE` in `.env` on the server to that path. `scripts/backup.sh` dumps the
DB (the only source of truth) via the `db` container — wire it as a host cron job.

## Proactive outreach

`thenetwork/worker/proactive.py` holds two hourly periodic tasks (registered via
`import_paths` in `worker/tasks.py`). Both only surface candidates — the agent run
decides whether and how to introduce, so the SEAL still governs what leaves the system.
Unit-tested in `tests/test_proactive.py`.

`scan_for_opportunities` (`cron="0 * * * *"`, graph proximity). Builds the NetworkX
graph, scores person pairs by Jaccard proximity over shared neighbours, and for each pair
above `PROXIMITY_THRESHOLD` (0.3) `defer`s a synthetic `process_email` job. Requires
pre-existing connection density, so it says nothing at cold start.

`scan_for_matches` (`cron="30 * * * *"`, semantic rematch). This is the cold-start /
dormant-user path: it re-evaluates standing intents against *new* arrivals rather than
only at write time. Driven by memories created within
`proactive_rematch_lookback_minutes` (65) so a pair surfaces once, when the counterpart
shows up; for each such arrival it runs `match_memories` and, for any older
person-referencing memory about a *different* person scoring at least
`proactive_match_threshold` (0.5), `defer`s a job that re-engages the dormant owner of the
older note. Guards: pairs already connected in the projected graph are skipped (the
introduction memory is the durable dedup record); the similarity floor is conservative
*here specifically* because unsolicited outreach makes a false positive costly — the
interactive `search` tool deliberately takes no such floor. The trigger body carries only
opaque ids + PII-stripped gists; real addresses and raw memory text never enter it.

## Sharp edges

- Editing a memory is always `forget` + `remember`; never mutate `text`/`refs` in place,
  or the embedding and gist go stale.
- Keep the `postgresql+psycopg://` (SQLModel) vs plain `postgresql://` (Procrastinate) DSN
  distinction straight — `worker/tasks.run_worker` strips `+psycopg` for Procrastinate.
