from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, call

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/012_add_event_persistence.py"
    spec = spec_from_file_location("event_persistence_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_event_migration_upgrade_defines_lifecycle_ledger_and_suppression(monkeypatch):
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    tables = {
        args[0]: {item.name: item for item in args[1:] if hasattr(item, "name")}
        for args, _kwargs in (entry for entry in operation.create_table.call_args_list)
    }
    assert set(tables) == {
        "events",
        "event_recommendations",
        "event_suppressions",
    }
    assert set(tables["events"]) >= {
        "id",
        "submitter_id",
        "text",
        "gist",
        "embedding",
        "recurrence",
        "expires_at",
        "cancelled_at",
    }
    assert tables["events"]["gist"].nullable is False
    assert tables["events"]["expires_at"].nullable is False
    assert set(tables["event_recommendations"]) >= {
        "event_id",
        "person_id",
        "considered_at",
        "notified_at",
        "uq_event_recommendation_event_person",
    }
    assert set(tables["event_suppressions"]) >= {"person_id", "suppressed_at"}


def test_event_migration_downgrade_removes_tables_in_dependency_order(monkeypatch):
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [
        call("event_suppressions"),
        call("event_recommendations"),
        call("events"),
    ]


def test_event_migration_upgrade_and_downgrade_execute(monkeypatch):
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    people = sa.Table(
        "people",
        sa.MetaData(),
        sa.Column("id", sa.String(), primary_key=True),
    )
    people.create(engine)

    with engine.begin() as connection:
        operation = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operation)

        migration.upgrade()
        assert set(sa.inspect(connection).get_table_names()) >= {
            "events",
            "event_recommendations",
            "event_suppressions",
        }

        migration.downgrade()
        assert set(sa.inspect(connection).get_table_names()) == {"people"}
