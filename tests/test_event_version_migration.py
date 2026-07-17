from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, call


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/013_bind_event_recommendation_versions.py"
    )
    spec = spec_from_file_location("event_recommendation_version_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_event_version_migration_adds_server_defaulted_bindings(monkeypatch):
    migration = _load_migration()
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
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_column.call_args_list == [
        call("event_recommendations", "event_version"),
        call("events", "version"),
    ]
