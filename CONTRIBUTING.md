# Contributing to The Network

Thank you for your interest in contributing to The Network.

## Design Decisions Gate

Before proposing architectural changes, schema modifications, or new abstractions, review [docs/design-decisions.md](docs/design-decisions.md).

Key guiding principles:
- Hand-write only genuine domain glue (memory + the privacy seal).
- Use established libraries for queues (Procrastinate), ORM (SQLModel/SQLAlchemy), rate limiting (limits), embeddings (llama-index), graph math (NetworkX), and email (imap-tools/aiosmtpd).
- Most "cleaner schema" or alternative architectural proposals have already been evaluated and documented in `docs/design-decisions.md`.

## The Invariant: THE SEAL Security Suite

The central design constraint of this project is **THE SEAL**: prompt injection must not be able to exfiltrate user identities or data, even under a compromised model. Leakage is made structurally impossible rather than prompt-dependent.

- Before touching code in `thenetwork/agent/`, `thenetwork/memory/`, `thenetwork/search/`, or `thenetwork/email/`, read [docs/security.md](docs/security.md).
- Any pull request must keep the security suite in `tests/security/` green. Red-team tests and contract tests in `tests/security/` are non-negotiable.

## Setup and Commands

```bash
# Install dependencies with dev extra
uv pip install -e ".[dev]"

# Start local Postgres with pgvector
docker compose up -d db

# Run database migrations
uv run alembic upgrade head

# Run non-integration test suite
uv run pytest -m "not integration"

# Run security red-team suite
uv run pytest tests/security/

# Lint and format
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
```

## Pull Request Guidelines

- Ensure `uv run pytest -m "not integration"` and `uv run pytest tests/security/` pass cleanly.
- Keep changes focused and aligned with existing architectural patterns.
- Follow tone conventions: avoid exclamation points and non-professional language in code, comments, and commit messages.
