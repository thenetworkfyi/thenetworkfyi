# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps for psycopg/llama-index wheels, plus gnupg for admin channel
# PGP/MIME verification (python-gnupg shells out to the gpg binary).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq5 gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies before copying app code, so changes under thenetwork/
# do not invalidate the model-install layer.
COPY pyproject.toml README.md ./
RUN python - <<'PY' > /tmp/project-requirements.txt
import tomllib

with open("pyproject.toml", "rb") as config_file:
    pyproject = tomllib.load(config_file)

for dependency in pyproject["build-system"]["requires"]:
    print(dependency)
for dependency in pyproject["project"]["dependencies"]:
    print(dependency)
PY
RUN pip install -r /tmp/project-requirements.txt
COPY thenetwork ./thenetwork
RUN pip install --no-deps --no-build-isolation .

# Alembic config + migrations needed at runtime for `alembic upgrade head`.
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Drop privileges.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser /home/appuser/.cache
USER appuser

ENTRYPOINT ["docker-entrypoint.sh"]
# Single long-running process: intake (IMAP poll), processing, proactive scans.
CMD ["thenetwork-worker"]
