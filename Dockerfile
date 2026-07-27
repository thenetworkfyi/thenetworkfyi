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

# Bake the gist sanitizer's weights into the image. Sanitization is mandatory
# and has no fallback (docs/security.md THE SEAL layer 4), so a first start
# that has to reach the network before it can open the queue is a start that
# can fail for reasons unrelated to this deployment.
#
# These land in /opt, deliberately NOT in HF_HOME. The scanner's hf-cache is a
# named volume, and Docker seeds a named volume from the image only when the
# volume is empty - any deployment that already ran the content scanner has a
# populated hf-cache, which would shadow baked weights and fail at startup.
#
# Only these four files are ever loaded; the rest of the repo is not fetched.
#
# The read permissions are applied in this same layer: appuser must be able to
# read these, and a separate `chmod -R` would rewrite metadata on every file
# and so duplicate the whole 2.7 GB in a second layer.
ARG SANITIZE_MODEL_REPO=openai/privacy-filter
RUN python - "$SANITIZE_MODEL_REPO" <<'PY'
import os
import stat
import sys

from huggingface_hub import snapshot_download

target = "/opt/sanitizer-model"
snapshot_download(
    sys.argv[1],
    local_dir=target,
    allow_patterns=[
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
    ],
)

for root, dirs, files in os.walk(target):
    for name in dirs:
        path = os.path.join(root, name)
        os.chmod(path, os.stat(path).st_mode | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    for name in files:
        path = os.path.join(root, name)
        os.chmod(path, os.stat(path).st_mode | stat.S_IRGRP | stat.S_IROTH)
os.chmod(target, os.stat(target).st_mode | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
PY
# Point the worker at the baked copy. The settings default stays the hub id so
# local `uv run` uses the developer's own cache.
ENV SANITIZE_MODEL=/opt/sanitizer-model

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
