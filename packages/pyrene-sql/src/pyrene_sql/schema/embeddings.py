"""Embedding clients for the schema-RAG indexer.

PRD-002 §2.2 F3 requires exponential backoff on transient embedding-API
failures. We deliberately wrap retry inside the embedder (not the caller's
indexer) so that tests can plug a deterministic mock without the indexer
caring whether the underlying transport has its own retries.

Provider note (PRD-002 L-01): we ship one concrete embedder for OpenAI's
`text-embedding-3-small` with `dimensions=1024`. voyage-3 also defaults to
1024, so a future `VoyageEmbedder` can be added without altering the column
type. ADR-013 (c) explicitly forbids changing `vector(N)` in-place.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

# Embedding dimensions chosen at the schema/initdb level. Defined here so
# embedders (and tests) can assert their model honors the project-wide pin.
EMBEDDING_DIMENSIONS: int = 1024


class EmbeddingClient(Protocol):
    """Async embedding interface. PRD-002 §4."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one 1024-dim embedding per input text, same order."""
        ...


class OpenAIEmbedder:
    """OpenAI-backed `EmbeddingClient` with manual exponential backoff.

    We use the official `openai` package (already a transitive dep via
    pydantic-ai) but invoke it inside `asyncio.to_thread` so we do not pull
    its async client just to call one method.

    Retry policy (PRD-002 §2.2 F3): up to 3 attempts with 0.5s, 1.5s, 4.5s
    delays. Anything beyond that surfaces to the caller — better to fail
    the `index-schema` command than silently retry forever.
    """

    _MAX_ATTEMPTS = 3
    _BASE_DELAY = 0.5
    _BACKOFF_FACTOR = 3.0

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        dimensions: int = EMBEDDING_DIMENSIONS,
    ) -> None:
        if dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"OpenAIEmbedder dimensions must be {EMBEDDING_DIMENSIONS}; "
                f"got {dimensions}. The DB column is vector({EMBEDDING_DIMENSIONS})."
            )
        # Imported lazily so `import pyrene_sql.schema` does not require the
        # `openai` package at module-load time (mypy-only / mock-only paths).
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        last_exc: Exception | None = None
        for attempt in range(self._MAX_ATTEMPTS):
            try:
                return await asyncio.to_thread(self._embed_sync, texts)
            except Exception as exc:  # pragma: no cover - exercised in live runs
                last_exc = exc
                if attempt == self._MAX_ATTEMPTS - 1:
                    break
                delay = self._BASE_DELAY * (self._BACKOFF_FACTOR**attempt)
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions,
        )
        return [item.embedding for item in response.data]


__all__ = ["EMBEDDING_DIMENSIONS", "EmbeddingClient", "OpenAIEmbedder"]
