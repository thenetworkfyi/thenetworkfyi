"""OpenAI embedding helpers backed by LlamaIndex.

Memories are stored in pgvector ``Vector(1536)`` columns, so this module accepts
only the OpenAI models that can produce 1536-dimensional embeddings. All batching
and retry are delegated to LlamaIndex's built-in implementation.
"""

from __future__ import annotations

from llama_index.core.embeddings import BaseEmbedding
from llama_index.embeddings.openai import OpenAIEmbedding

from thenetwork.settings import get_settings

_client: BaseEmbedding | None = None

EMBEDDING_DIMENSIONS = 1536
_OPENAI_MODELS_WITH_CONFIGURABLE_DIMENSIONS = frozenset(
    {"text-embedding-3-small", "text-embedding-3-large"}
)
_OPENAI_MODELS_WITH_NATIVE_1536_DIMENSIONS = frozenset({"text-embedding-ada-002"})


def _unsupported_model_error(model: str) -> ValueError:
    return ValueError(
        f"Unsupported embed_model: {model!r}. The Network stores embeddings in "
        f"Vector({EMBEDDING_DIMENSIONS}) and supports only OpenAI "
        "text-embedding-3-small, text-embedding-3-large (configured to 1536 "
        "dimensions), or text-embedding-ada-002 (native 1536 dimensions). "
        "Using another provider or dimension requires a database migration."
    )


def validate_embedding_configuration(model: str | None = None) -> None:
    """Fail at startup when ``embed_model`` cannot match the pgvector schema."""
    configured_model = model if model is not None else get_settings().embed_model
    if configured_model not in (
        _OPENAI_MODELS_WITH_CONFIGURABLE_DIMENSIONS
        | _OPENAI_MODELS_WITH_NATIVE_1536_DIMENSIONS
    ):
        raise _unsupported_model_error(configured_model)


def _make_embed_client(model: str, api_key: str) -> BaseEmbedding:
    validate_embedding_configuration(model)
    if model in _OPENAI_MODELS_WITH_CONFIGURABLE_DIMENSIONS:
        return OpenAIEmbedding(
            model=model, api_key=api_key, dimensions=EMBEDDING_DIMENSIONS
        )
    return OpenAIEmbedding(model=model, api_key=api_key)


def _get_client() -> BaseEmbedding:
    global _client
    if _client is None:
        s = get_settings()
        _client = _make_embed_client(s.embed_model, s.embed_api_key)
    return _client


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Returns a 1536-dim vector."""
    client = _get_client()
    return await client.aget_text_embedding(text)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch using LlamaIndex's built-in batching + retry."""
    client = _get_client()
    return await client.aget_text_embedding_batch(texts)


embed = embed_text
