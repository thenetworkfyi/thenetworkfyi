from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, call


def _load_migration():
    path = (
        Path(__file__).parents[1] / "alembic/versions/015_add_primary_intake_state.py"
    )
    spec = spec_from_file_location("primary_intake_state_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_primary_intake_migration_adds_durable_singleton_state(monkeypatch):
    migration = _load_migration()
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
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [call("primary_intake_state")]
