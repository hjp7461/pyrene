-- Read-only role for the run_select tool (F-03 dual defense, ADR-013 (d)).
-- Runs after 01-dvdrental.sh so the DVD Rental tables exist when GRANTs run.
-- The Postgres entrypoint executes us as POSTGRES_USER inside POSTGRES_DB.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pyrene_readonly') THEN
    CREATE ROLE pyrene_readonly LOGIN PASSWORD 'readonly';
  ELSE
    RAISE NOTICE 'role pyrene_readonly already exists; skipping CREATE ROLE';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE dvdrental TO pyrene_readonly;
GRANT USAGE ON SCHEMA public TO pyrene_readonly;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO pyrene_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO pyrene_readonly;

-- Future tables (e.g. created by future migrations under the owner role) inherit SELECT.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO pyrene_readonly;

-- Explicit revoke of write & DDL privileges.
-- Defense in depth: even if a future migration accidentally GRANTed extra rights,
-- these REVOKEs make the intent unambiguous.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON ALL TABLES IN SCHEMA public FROM pyrene_readonly;
REVOKE CREATE ON SCHEMA public FROM pyrene_readonly;
REVOKE CREATE ON DATABASE dvdrental FROM pyrene_readonly;

-- Block server-side filesystem & large-object functions that could exfiltrate.
-- These functions are restricted to superuser by default in PG16, but we
-- additionally REVOKE from PUBLIC to make the intent explicit. We loop over
-- all matching signatures because their argument lists vary by version.
DO $$
DECLARE
  fn record;
BEGIN
  FOR fn IN
    SELECT n.nspname AS schema_name,
           p.proname AS function_name,
           pg_get_function_identity_arguments(p.oid) AS args
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'pg_catalog'
       AND p.proname IN (
         'pg_read_server_files',
         'pg_read_binary_file',
         'pg_ls_dir',
         'lo_import',
         'lo_export'
       )
  LOOP
    EXECUTE format(
      'REVOKE EXECUTE ON FUNCTION %I.%I(%s) FROM PUBLIC',
      fn.schema_name, fn.function_name, fn.args
    );
  END LOOP;
END
$$;
