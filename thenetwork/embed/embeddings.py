"""OpenAI embedding helpers backed by LlamaIndex.

Memories are stored in pgvector ``Vector(1536)`` columns, so this module accepts
only the OpenAI models that can produce 1536-dimensional embeddings. All batching
and retry are delegated to LlamaIndex's built-in implementation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.utils import get_tokenizer
from llama_index.embeddings.openai import OpenAIEmbedding
from pydantic_ai.messages import ModelResponse, RequestUsage

from thenetwork.llm_observability import LLMWorkload, record_llm_request
from thenetwork.settings import get_settings

_client: BaseEmbedding | None = None

EMBEDDING_DIMENSIONS = 1536
_OPENAI_MODELS_WITH_CONFIGURABLE_DIMENSIONS = frozenset(
    {"text-embedding-3-small", "text-embedding-3-large"}
)
_OPENAI_MODELS_WITH_NATIVE_1536_DIMENSIONS = frozenset({"text-embedding-ada-002"})


def _embedding_usage(texts: list[str]) -> RequestUsage:
    tokenizer = get_tokenizer()
    input_tokens = sum(len(tokenizer(text.replace("\n", " "))) for text in texts)
    return RequestUsage(input_tokens=input_tokens, output_tokens=0)


class _ObservedOpenAIEmbedding(OpenAIEmbedding):
    """Preserve LlamaIndex batching/retry while accounting per API batch."""

    async def _observe(
        self, texts: list[str], operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        started = monotonic()
        usage = _embedding_usage(texts)
        try:
            result = await operation()
        except BaseException as exc:
            record_llm_request(
                workload=LLMWorkload.EMBEDDING,
                configured_model_name=self.model_name,
                provider="openai",
                duration_ms=(monotonic() - started) * 1000,
                usage=usage,
                error_type=type(exc).__name__,
            )
            raise
        response = ModelResponse(
            parts=[],
            usage=usage,
            model_name=str(self.model_name),
            provider_name="openai",
        )
        record_llm_request(
            workload=LLMWorkload.EMBEDDING,
            configured_model_name=self.model_name,
            provider="openai",
            duration_ms=(monotonic() - started) * 1000,
            response=response,
        )
        return result

    async def _aget_text_embedding(self, text: str) -> list[float]:
        parent = super()
        return await self._observe([text], lambda: parent._aget_text_embedding(text))

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        parent = super()
        return await self._observe(texts, lambda: parent._aget_text_embeddings(texts))


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
        return _ObservedOpenAIEmbedding(
            model=model, api_key=api_key, dimensions=EMBEDDING_DIMENSIONS
        )
    return _ObservedOpenAIEmbedding(model=model, api_key=api_key)


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
