"""Embedding helpers backed by LlamaIndex.

Provider is swappable via settings.embed_model — provider resolved from model name prefix.
All batching and retry are delegated to LlamaIndex's built-in implementation.
"""
from __future__ import annotations

from llama_index.core.embeddings import BaseEmbedding
from llama_index.embeddings.openai import OpenAIEmbedding

from thenetwork.settings import get_settings

_client: BaseEmbedding | None = None


def _make_embed_client(model: str, api_key: str) -> BaseEmbedding:
    """Resolve embedding provider from model name prefix."""
    if model.startswith("text-embedding"):
        return OpenAIEmbedding(model=model, api_key=api_key)
    raise ValueError(
        f"Unsupported embed_model: {model!r}. Add provider support in _make_embed_client."
    )


def _get_client() -> BaseEmbedding:
    global _client
    if _client is None:
        s = get_settings()
        _client = _make_embed_client(s.embed_model, s.openai_api_key)
    return _client


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Returns a 1536-dim vector."""
    client = _get_client()
    return await client.aget_text_embedding(text)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch using LlamaIndex's built-in batching + retry."""
    client = _get_client()
    return await client.aget_text_embedding_batch(texts)


async def embed_profile(bio: str, intent_description: str) -> tuple[list[float], list[float]]:
    """Return (identity_vector, intent_vector) for a profile in one batched call."""
    vectors = await embed_batch([bio, intent_description])
    return vectors[0], vectors[1]
