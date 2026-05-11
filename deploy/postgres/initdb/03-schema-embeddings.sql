-- PLAN-002 Day 1: pgvector schema embeddings table.
--
-- Why this lives in initdb:
--   PRD-002 §3.1 ships a single connection in Phase 1. The table must exist
--   before `pyrene-sql index-schema` runs, so we create it on first DB boot.
--   PLAN-011 (Phase 2) will migrate this table via alembic to add per-tenant
--   connection scoping; today the `connection_id` column already exists with
--   a fixed Phase 1 default so the eventual ALTER follows ADR-013 (c)'s
--   ADD COLUMN NULL → backfill → SET NOT NULL pattern *without* a type swap.
--
-- Why HNSW and not IVFFlat:
--   ≤100k rows expected (16 today, ~thousands at Phase 2 scale). HNSW gives
--   better recall at small row counts and avoids the IVFFlat REINDEX dance
--   when rows are inserted incrementally. ADR-013 (c) explicitly cites this
--   table as the canonical instance where HNSW is preferred.
--
-- Why 1024 dimensions:
--   text-embedding-3-small with `dimensions=1024` and voyage-3 (default 1024)
--   are both supported. Switching providers does not require a type change.
--   ADR-013 (c) notes `vector(N)` cannot be ALTERed — locking the dim in
--   initdb means the only escape hatch is DROP+RECREATE, which we accept as
--   a deliberate guard against silent provider drift.

CREATE EXTENSION IF NOT EXISTS vector;

-- gen_random_uuid() lives in pgcrypto; HNSW comes from pgvector (above).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS pyrene_schema_embeddings (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID         NOT NULL    DEFAULT '00000000-0000-0000-0000-000000000001',
    schema        TEXT         NOT NULL,
    "table"       TEXT         NOT NULL,
    description   TEXT         NOT NULL,
    embedding     vector(1024) NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL    DEFAULT NOW(),
    CONSTRAINT pyrene_schema_embeddings_unique_target
      UNIQUE (connection_id, schema, "table")
);

-- HNSW + cosine ops. m=16, ef_construction=64 are the pgvector defaults
-- recommended for the ≤100k row regime. Revisit if PRD-011 grows the table.
CREATE INDEX IF NOT EXISTS pyrene_schema_embeddings_embedding_hnsw
  ON pyrene_schema_embeddings
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Note: ANALYZE is intentionally NOT here. The indexer runs ANALYZE at the
-- end of each `index_all()` call so planner stats reflect the freshly-loaded
-- vectors instead of an empty table.
