from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, call


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/017_add_primary_intake_judge_state.py"
    )
    spec = spec_from_file_location("primary_intake_judge_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_judge_migration_adds_only_cursor_and_enum_outcome_metadata(monkeypatch):
    migration = _load_migration()
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
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [call("primary_intake_judge_state")]
