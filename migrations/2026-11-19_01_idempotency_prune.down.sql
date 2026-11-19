-- Reverse of 2026-11-19_01_idempotency_prune.sql. Index only: response has been nullable since
-- S92 (never NOT NULL), and pruned rows hold NULL bodies, so SET NOT NULL would fail anyway.
-- Nulled/deleted rows only come back via pg_restore of the pre-idem-prune dump.
-- Revert the dashboard code first, or _ensure_app_schema recreates the index on next start.
DROP INDEX IF EXISTS app_idempotency_created_at_idx;
