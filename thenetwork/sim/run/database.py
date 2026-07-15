"""Per-run Postgres database lifecycle for real-process simulations."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import procrastinate
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import thenetwork.db.session as db_session
import thenetwork.security.rate_limit as rate_limit
import thenetwork.worker.tasks as worker_tasks
from thenetwork.settings import get_settings


_DATABASE_NAME_PATTERN = re.compile(r"sim_[a-z0-9]+")


def new_sim_database_name() -> str:
    """Return a Postgres-safe, collision-resistant simulation database name."""
    return f"sim_{uuid4().hex}"


@contextmanager
def provision_sim_database(
    database_name: str,
    *,
    keep: bool = False,
    dump_path: Callable[[], Path | None] | None = None,
):
    """Create, migrate, select, and eventually drop a simulation database."""
    if (
        _DATABASE_NAME_PATTERN.fullmatch(database_name) is None
        or len(database_name) > 63
    ):
        raise ValueError("simulation database names must match sim_<alphanumeric>")

    settings = get_settings()
    original_database = settings.postgres_db
    admin_url = make_url(settings.database_url).set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    quoted_name = admin_engine.dialect.identifier_preparer.quote(database_name)

    original_engine = db_session._engine
    original_session_factory = db_session._SessionLocal
    original_limiter = rate_limit._limiter
    original_storage = rate_limit._storage
    original_connector = worker_tasks.app.connector
    original_job_manager_connector = worker_tasks.app.job_manager.connector
    scratch_engine = None

    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {quoted_name}"))
            created = True

        try:
            settings.postgres_db = database_name
            db_session._engine = None
            db_session._SessionLocal = None
            rate_limit._limiter = None
            rate_limit._storage = None
            worker_connector = procrastinate.PsycopgConnector(
                conninfo=settings.database_url.replace(
                    "postgresql+psycopg://", "postgresql://"
                )
            )
            worker_tasks.app.connector = worker_connector
            worker_tasks.app.job_manager.connector = worker_connector
            _upgrade_database()
            _ensure_procrastinate_schema()
            yield database_name
        finally:
            scratch_engine = db_session._engine
            db_session._engine = original_engine
            db_session._SessionLocal = original_session_factory
            rate_limit._limiter = original_limiter
            rate_limit._storage = original_storage
            worker_tasks.app.connector = original_connector
            worker_tasks.app.job_manager.connector = original_job_manager_connector
            settings.postgres_db = original_database

            if scratch_engine is not None:
                scratch_engine.dispose()
    finally:
        try:
            if created and not keep:
                try:
                    if dump_path is not None:
                        destination = dump_path()
                        if destination is not None:
                            _dump_database(database_name, destination)
                finally:
                    with admin_engine.connect() as connection:
                        connection.execute(
                            text(f"DROP DATABASE {quoted_name} WITH (FORCE)")
                        )
        finally:
            admin_engine.dispose()


def _dump_database(database_name: str, destination: Path) -> None:
    """Write a portable custom-format dump of a disposable simulation database."""
    settings = get_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ | {"PGPASSWORD": settings.postgres_password}
    subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--file",
            str(destination),
            "--host",
            settings.postgres_host,
            "--port",
            str(settings.postgres_port),
            "--username",
            settings.postgres_user,
            database_name,
        ],
        check=True,
        env=environment,
    )


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _upgrade_database() -> None:
    project_root = _project_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(config, "head")


def _ensure_procrastinate_schema() -> None:
    """Create Procrastinate's queue tables in each disposable simulation DB."""
    with worker_tasks.app.open():
        worker_tasks.app.schema_manager.apply_schema()
