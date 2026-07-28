from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, call

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration(filename: str, module_name: str):
    path = Path(__file__).parent.parent / "alembic/versions" / filename
    spec = spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_judge_migration_adds_only_cursor_and_enum_outcome_metadata(monkeypatch):
    migration = _load_migration(
        "017_add_primary_intake_judge_state.py", "primary_intake_judge_migration"
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    args = operation.create_table.call_args.args
    assert args[0] == "primary_intake_judge_state"
    columns = {item.name for item in args[1:] if hasattr(item, "name")}
    assert columns == {
        "key",
        "last_observed_at",
        "last_mailbox_uid",
        "last_run_at",
        "last_verdict",
        "last_reason",
    }
    assert columns.isdisjoint({"sender", "email", "domain", "subject", "body"})
    assert operation.bulk_insert.call_args.args[1] == [{"key": "primary"}]


def test_judge_migration_downgrade_removes_state(monkeypatch):
    migration = _load_migration(
        "017_add_primary_intake_judge_state.py", "primary_intake_judge_migration"
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [call("primary_intake_judge_state")]


def test_event_migration_upgrade_defines_lifecycle_ledger_and_suppression(monkeypatch):
    migration = _load_migration(
        "012_add_event_persistence.py", "event_persistence_migration"
    )
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
    migration = _load_migration(
        "012_add_event_persistence.py", "event_persistence_migration"
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [
        call("event_suppressions"),
        call("event_recommendations"),
        call("events"),
    ]


def test_event_migration_upgrade_and_downgrade_execute(monkeypatch):
    migration = _load_migration(
        "012_add_event_persistence.py", "event_persistence_migration"
    )
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


def test_event_version_migration_adds_server_defaulted_bindings(monkeypatch):
    migration = _load_migration(
        "013_bind_event_recommendation_versions.py",
        "event_recommendation_version_migration",
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    assert [entry.args[0] for entry in operation.add_column.call_args_list] == [
        "events",
        "event_recommendations",
    ]
    event_version = operation.add_column.call_args_list[0].args[1]
    recommendation_version = operation.add_column.call_args_list[1].args[1]
    assert event_version.name == "version"
    assert recommendation_version.name == "event_version"
    assert event_version.nullable is False
    assert recommendation_version.nullable is False
    assert str(event_version.server_default.arg) == "1"
    assert str(recommendation_version.server_default.arg) == "1"


def test_event_version_migration_downgrade_removes_bindings(monkeypatch):
    migration = _load_migration(
        "013_bind_event_recommendation_versions.py",
        "event_recommendation_version_migration",
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_column.call_args_list == [
        call("event_recommendations", "event_version"),
        call("events", "version"),
    ]


def test_primary_intake_migration_adds_durable_singleton_state(monkeypatch):
    migration = _load_migration(
        "015_add_primary_intake_state.py", "primary_intake_state_migration"
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    args = operation.create_table.call_args.args
    assert args[0] == "primary_intake_state"
    columns = {column.name: column for column in args[1:]}
    assert set(columns) == {
        "key",
        "paused",
        "pause_reason",
        "paused_at",
        "updated_at",
    }
    assert columns["key"].primary_key is True
    assert columns["paused"].nullable is False
    assert str(columns["paused"].server_default.arg) == "false"
    inserted = operation.bulk_insert.call_args.args[1]
    assert len(inserted) == 1
    assert inserted[0]["key"] == "primary"
    assert inserted[0]["paused"] is False


def test_primary_intake_migration_downgrade_removes_state(monkeypatch):
    migration = _load_migration(
        "015_add_primary_intake_state.py", "primary_intake_state_migration"
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [call("primary_intake_state")]


def test_observation_migration_contains_only_sealed_metadata(monkeypatch):
    migration = _load_migration(
        "016_add_primary_intake_observations.py",
        "primary_intake_observation_migration",
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    args = operation.create_table.call_args.args
    assert args[0] == "primary_intake_observations"
    columns = {item.name for item in args[1:] if hasattr(item, "name")}
    assert columns == {
        "mailbox_uid",
        "trace_id",
        "observed_at",
        "sender_authenticated",
        "sender_known",
        "sender_fingerprint",
        "domain_fingerprint",
        "body_fingerprint",
        "uq_primary_intake_observation_trace",
    }
    assert columns.isdisjoint({"sender", "email", "domain", "subject", "body"})
    assert operation.create_index.call_count == 6


def test_observation_migration_downgrade_removes_table(monkeypatch):
    migration = _load_migration(
        "016_add_primary_intake_observations.py",
        "primary_intake_observation_migration",
    )
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [call("primary_intake_observations")]
