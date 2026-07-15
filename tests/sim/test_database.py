from __future__ import annotations

from contextlib import contextmanager
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import thenetwork.db.session as db_session
import thenetwork.security.rate_limit as rate_limit
import thenetwork.sim.run.database as sim_database
import thenetwork.worker.tasks as worker_tasks
from thenetwork.settings import get_settings


class FakeAdminEngine:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.disposed = False
        self.dialect = SimpleNamespace(
            identifier_preparer=SimpleNamespace(quote=lambda name: f'"{name}"')
        )

    @contextmanager
    def connect(self):
        yield SimpleNamespace(
            execute=lambda statement: self.statements.append(str(statement))
        )

    def dispose(self) -> None:
        self.disposed = True


def test_provision_sim_database_switches_caches_and_cleans_up(monkeypatch):
    settings = get_settings()
    original_database = settings.postgres_db
    original_engine = db_session._engine
    original_factory = db_session._SessionLocal
    original_limiter = rate_limit._limiter
    original_storage = rate_limit._storage
    original_connector = worker_tasks.app.connector
    original_job_manager_connector = worker_tasks.app.job_manager.connector
    admin_engine = FakeAdminEngine()
    scratch_engine = Mock()
    scratch_connector = object()

    monkeypatch.setattr(
        sim_database, "create_engine", lambda *_args, **_kwargs: admin_engine
    )
    monkeypatch.setattr(
        sim_database.procrastinate,
        "PsycopgConnector",
        lambda **_kwargs: scratch_connector,
    )

    def assert_migration_context() -> None:
        assert settings.postgres_db == "sim_abc123"
        assert db_session._engine is None
        assert db_session._SessionLocal is None
        assert rate_limit._limiter is None
        assert rate_limit._storage is None
        assert worker_tasks.app.connector is scratch_connector
        assert worker_tasks.app.job_manager.connector is scratch_connector
        db_session._engine = scratch_engine

    monkeypatch.setattr(sim_database, "_upgrade_database", assert_migration_context)
    queue_schema = Mock()
    monkeypatch.setattr(sim_database, "_ensure_procrastinate_schema", queue_schema)

    with sim_database.provision_sim_database("sim_abc123") as database_name:
        assert database_name == "sim_abc123"

    assert admin_engine.statements == [
        'CREATE DATABASE "sim_abc123"',
        'DROP DATABASE "sim_abc123" WITH (FORCE)',
    ]
    assert admin_engine.disposed is True
    scratch_engine.dispose.assert_called_once_with()
    assert settings.postgres_db == original_database
    assert db_session._engine is original_engine
    assert db_session._SessionLocal is original_factory
    assert rate_limit._limiter is original_limiter
    assert rate_limit._storage is original_storage
    assert worker_tasks.app.connector is original_connector
    assert worker_tasks.app.job_manager.connector is original_job_manager_connector
    queue_schema.assert_called_once_with()


def test_project_root_locates_alembic_scripts():
    root = sim_database._project_root()
    assert (root / "alembic.ini").is_file()
    assert (root / "alembic").is_dir()


def test_provision_sim_database_rejects_unsafe_names():
    for database_name in ("network_db", "sim_bad-name", "sim_é", "sim_" + "a" * 60):
        with pytest.raises(ValueError):
            with sim_database.provision_sim_database(database_name):
                pass


def test_provision_sim_database_keep_retains_database(monkeypatch):
    admin_engine = FakeAdminEngine()
    monkeypatch.setattr(
        sim_database, "create_engine", lambda *_args, **_kwargs: admin_engine
    )
    monkeypatch.setattr(sim_database, "_upgrade_database", lambda: None)
    monkeypatch.setattr(sim_database, "_ensure_procrastinate_schema", lambda: None)

    with sim_database.provision_sim_database("sim_keep123", keep=True):
        pass

    assert admin_engine.statements == ['CREATE DATABASE "sim_keep123"']
    assert admin_engine.disposed is True


def test_provision_sim_database_dumps_before_cleanup(monkeypatch, tmp_path):
    admin_engine = FakeAdminEngine()
    dump = Mock()
    monkeypatch.setattr(
        sim_database, "create_engine", lambda *_args, **_kwargs: admin_engine
    )
    monkeypatch.setattr(sim_database, "_upgrade_database", lambda: None)
    monkeypatch.setattr(sim_database, "_ensure_procrastinate_schema", lambda: None)
    monkeypatch.setattr(sim_database, "_dump_database", dump)
    dump_path = tmp_path / "database.dump"

    def assert_dump_happens_before_drop(*_args) -> None:
        assert admin_engine.statements == ['CREATE DATABASE "sim_dump123"']

    dump.side_effect = assert_dump_happens_before_drop

    with sim_database.provision_sim_database(
        "sim_dump123", dump_path=lambda: dump_path
    ):
        pass

    dump.assert_called_once_with("sim_dump123", dump_path)
    assert admin_engine.statements == [
        'CREATE DATABASE "sim_dump123"',
        'DROP DATABASE "sim_dump123" WITH (FORCE)',
    ]


def test_dump_database_uses_custom_format_and_password_environment(
    monkeypatch, tmp_path
):
    run = Mock()
    monkeypatch.setattr(sim_database.subprocess, "run", run)
    destination = tmp_path / "run" / "database.dump"

    sim_database._dump_database("sim_dump123", destination)

    assert destination.parent.is_dir()
    command = run.call_args.args[0]
    assert command == [
        "pg_dump",
        "--format=custom",
        "--file",
        str(destination),
        "--host",
        get_settings().postgres_host,
        "--port",
        str(get_settings().postgres_port),
        "--username",
        get_settings().postgres_user,
        "sim_dump123",
    ]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["env"]["PGPASSWORD"] == get_settings().postgres_password
    assert run.call_args.kwargs["env"].get("PATH") == os.environ.get("PATH")
