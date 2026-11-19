-- Session 107: Habitica retirement. Drop the import provenance columns and the stale health rows.
-- Apply AFTER the code that no longer ADDs the columns is deployed (ensure_schema would re-add them).
DROP INDEX IF EXISTS habits_habitica_src_idx;
DROP INDEX IF EXISTS todos_habitica_src_idx;
ALTER TABLE habits DROP COLUMN IF EXISTS habitica_source_id;
ALTER TABLE todos  DROP COLUMN IF EXISTS habitica_source_id;
DELETE FROM health_checks WHERE service = 'habitica';
