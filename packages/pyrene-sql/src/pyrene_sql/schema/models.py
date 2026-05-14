"""Pydantic models for the schema-RAG pipeline.

PRD-002 §4 defines `ColumnSpec` and `SchemaChunk`. Both inherit
`StrictBaseModel` (BRIEF §6.1-1: `extra="forbid"`, frozen, whitespace strip).

`SchemaChunk` is the unit the indexer embeds — one row per (connection_id,
schema, table). The `description` field is the markdown blob the embedder
actually reads; `columns` is kept around so retrievers (Day 2) can render a
deterministic markdown table back into the system prompt without re-querying
information_schema.
"""

from __future__ import annotations

import warnings
from typing import Literal
from uuid import UUID

from pyrene_core import StrictBaseModel

ChunkType = Literal["table", "column"]
"""PRD-042 / ADR-020 — chunk_type closed enum mirroring the SQL CHECK."""

# Suppress the (harmless) Pydantic warning that `schema` shadows a deprecated
# BaseModel attribute. PRD-002 §4 specifies that field name explicitly so we
# accept the shadowing rather than renaming the field to `schema_name`.
_SCHEMA_SHADOW_MSG = (
    r'Field name "schema" in "SchemaChunk" shadows '
    r'an attribute in parent "StrictBaseModel"'
)
warnings.filterwarnings("ignore", message=_SCHEMA_SHADOW_MSG)

# Phase 1 single-connection default. PLAN-011 (Phase 2) replaces this with
# real per-tenant UUIDs and adds the row to a `connections` table; until then
# every chunk carries this fixed UUID so the UNIQUE(connection_id, schema,
# "table") constraint behaves like a 3-column primary key.
DEFAULT_CONNECTION_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")


class ColumnSpec(StrictBaseModel):
    """One column inside a table chunk. PRD-002 §4."""

    name: str
    data_type: str
    is_nullable: bool
    description: str | None = None


class SchemaChunk(StrictBaseModel):
    """One embedding-ready chunk. PRD-002 §4 + L-03 + PRD-042 (Hybrid).

    The `description` field is what gets embedded — the indexer renders the
    table name, table comment, and column list into markdown and stores it
    verbatim so retrievers (Day 2) can re-use it in the system prompt without
    going back to information_schema.

    PRD-042 / ADR-020 — Hybrid chunk strategy: a `SchemaChunk` is now
    *either* a table chunk (`chunk_type='table'`, `column_name=''`,
    `columns=` full column tuple) *or* a column chunk
    (`chunk_type='column'`, `column_name=col.name`, `columns=(col,)`
    single-element). The retriever uses `chunk_type` to drive
    `k_table=2 + k_column=5` two-stage retrieval with table-level dedup.

    Field naming note: `schema` matches the DB column `pyrene_schema_embeddings.schema`
    and the SQL identifier of the same name in `information_schema.tables`. Pydantic
    BaseModel exposes a deprecated `.schema()` method, so we override the attribute;
    mypy flags this as a redefinition and the assignment ignore below is the
    minimum-scope suppression. PRD-002 §4 specifies this exact field name.
    """

    connection_id: UUID
    schema: str  # type: ignore[assignment]
    table: str
    description: str
    columns: tuple[ColumnSpec, ...]
    chunk_type: ChunkType = "table"
    column_name: str = ""


__all__ = ["DEFAULT_CONNECTION_ID", "ChunkType", "ColumnSpec", "SchemaChunk"]
