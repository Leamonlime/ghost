-- Restores the columns and indexes (empty). The provenance ids and the health rows live in the
-- pre-retirement dump taken 15/11/2026 (~/backups/ghost-postgres/ghost_db-pre-habitica-retire-*.dump).
ALTER TABLE habits ADD COLUMN IF NOT EXISTS habitica_source_id TEXT;
ALTER TABLE todos  ADD COLUMN IF NOT EXISTS habitica_source_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS habits_habitica_src_idx ON habits(habitica_source_id) WHERE habitica_source_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS todos_habitica_src_idx  ON todos(habitica_source_id)  WHERE habitica_source_id IS NOT NULL;
