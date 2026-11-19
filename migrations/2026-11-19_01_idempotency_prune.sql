-- Ghost Home P3 (19/11/2026): app_idempotency retention support. Applied automatically by
-- dashboard _ensure_app_schema() on the first app request after deploy; kept here for the record.
-- Backup before enabling the live prune (GHOST_IDEM_PRUNE=1):
--   ~/backups/ghost-postgres/ghost_db-pre-idem-prune-<timestamp>.dump
CREATE INDEX IF NOT EXISTS app_idempotency_created_at_idx ON app_idempotency (created_at);
-- response was created nullable (S92); this is a no-op unless someone tightened it since.
ALTER TABLE app_idempotency ALTER COLUMN response DROP NOT NULL;
