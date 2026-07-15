from unittest.mock import patch

from pydantic_ai.models.test import TestModel

from thenetwork.model_config import model_with_api_key


def test_model_with_api_key_supplies_key_to_selected_provider():
    provider = object()
    provider_class = patch(
        "thenetwork.model_config.infer_provider_class",
        return_value=lambda *, api_key, http_client: (
            provider if api_key == "role-key" else None
        ),
    )
    infer_model = patch(
        "thenetwork.model_config.infer_model",
        return_value=object(),
    )

    with provider_class as mock_provider_class, infer_model as mock_infer_model:
        resolved = model_with_api_key("anthropic:claude-test", "role-key", 90.0)
        factory = mock_infer_model.call_args.kwargs["provider_factory"]
        assert factory("anthropic") is provider

    mock_provider_class.assert_called_once_with("anthropic")
    assert resolved is mock_infer_model.return_value


def test_model_with_api_key_preserves_concrete_test_model():
    model = TestModel()

    assert model_with_api_key(model, "unused", 90.0) is model
