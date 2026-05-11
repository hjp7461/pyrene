"""Schema-RAG pipeline (PRD-002).

Day 1 ships the indexer + embedding client + models. Day 2 adds the
`SchemaRetriever` Protocol + `PgvectorRetriever` and wires them into
`Deps`; this module re-exports both layers so callers do not need to know
the submodule layout.
"""

from pyrene_sql.schema.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingClient,
    OpenAIEmbedder,
)
from pyrene_sql.schema.indexer import SchemaIndexer, render_chunk_description
from pyrene_sql.schema.models import DEFAULT_CONNECTION_ID, ColumnSpec, SchemaChunk
from pyrene_sql.schema.retriever import PgvectorRetriever, SchemaRetriever

__all__ = [
    "DEFAULT_CONNECTION_ID",
    "EMBEDDING_DIMENSIONS",
    "ColumnSpec",
    "EmbeddingClient",
    "OpenAIEmbedder",
    "PgvectorRetriever",
    "SchemaChunk",
    "SchemaIndexer",
    "SchemaRetriever",
    "render_chunk_description",
]
