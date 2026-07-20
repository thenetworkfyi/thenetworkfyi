from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, call


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/016_add_primary_intake_observations.py"
    )
    spec = spec_from_file_location("primary_intake_observation_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_observation_migration_contains_only_sealed_metadata(monkeypatch):
    migration = _load_migration()
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
    migration = _load_migration()
    operation = Mock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    assert operation.drop_table.call_args_list == [call("primary_intake_observations")]
