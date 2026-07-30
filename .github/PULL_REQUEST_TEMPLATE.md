## Description

Summary of proposed changes.

## Architectural Gate Check

- [ ] Reviewed [docs/design-decisions.md](../docs/design-decisions.md) if this PR modifies schemas, abstractions, or queue/mail architecture.
- [ ] Preserved THE SEAL security invariant ([docs/security.md](../docs/security.md)).

## Verification

- [ ] `uv run pytest -m "not integration"` passes locally.
- [ ] `uv run pytest tests/security/` passes locally.
- [ ] `uv run --extra dev ruff check .` and `uv run --extra dev ruff format --check .` pass clean.
