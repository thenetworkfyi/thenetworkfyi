from unittest.mock import MagicMock, patch

import pytest

from thenetwork.embed.embeddings import (
    EMBEDDING_DIMENSIONS,
    _make_embed_client,
    validate_embedding_configuration,
)


@pytest.mark.parametrize(
    "model",
    ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
)
def test_validate_embedding_configuration_accepts_1536_dimension_models(model: str):
    validate_embedding_configuration(model)


@pytest.mark.parametrize("model", ["test:embed", "text-embedding-unknown"])
def test_validate_embedding_configuration_rejects_incompatible_models(model: str):
    with pytest.raises(ValueError, match=r"Vector\(1536\).*database migration"):
        validate_embedding_configuration(model)


@pytest.mark.parametrize("model", ["text-embedding-3-small", "text-embedding-3-large"])
def test_make_embed_client_requests_schema_dimensions_for_v3_models(model: str):
    with patch(
        "thenetwork.embed.embeddings._ObservedOpenAIEmbedding",
        return_value=MagicMock(),
    ) as client:
        _make_embed_client(model, "key")

    client.assert_called_once_with(
        model=model, api_key="key", dimensions=EMBEDDING_DIMENSIONS
    )


def test_make_embed_client_uses_native_dimensions_for_ada():
    with patch(
        "thenetwork.embed.embeddings._ObservedOpenAIEmbedding",
        return_value=MagicMock(),
    ) as client:
        _make_embed_client("text-embedding-ada-002", "key")

    client.assert_called_once_with(model="text-embedding-ada-002", api_key="key")


@pytest.mark.parametrize("entrypoint", ["main", "producer_main"])
def test_worker_entrypoints_stop_before_work_when_embedding_config_is_invalid(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
):
    from thenetwork.worker import tasks

    validate = MagicMock(side_effect=ValueError("invalid embedding configuration"))
    configure_logging = MagicMock()
    monkeypatch.setattr(
        "thenetwork.embed.embeddings.validate_embedding_configuration", validate
    )
    monkeypatch.setattr(tasks, "configure_audit_logging", configure_logging)

    with pytest.raises(ValueError, match="invalid embedding configuration"):
        getattr(tasks, entrypoint)()

    validate.assert_called_once_with()
    configure_logging.assert_not_called()
