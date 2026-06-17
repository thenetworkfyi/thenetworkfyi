"""Embedding helpers backed by LlamaIndex's OpenAIEmbedding.

Provider is swappable via settings.embed_model — just change the model name.
All batching and retry are delegated to LlamaIndex's built-in implementation.
"""
from __future__ import annotations

from llama_index.embeddings.openai import OpenAIEmbedding

from thenetwork.settings import get_settings

_client: OpenAIEmbedding | None = None


def _get_client() -> OpenAIEmbedding:
    global _client
    if _client is None:
        s = get_settings()
        _client = OpenAIEmbedding(
            model=s.embed_model,
            api_key=s.openai_api_key,
            # 1536-dim output is fixed for text-embedding-3-small
        )
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
